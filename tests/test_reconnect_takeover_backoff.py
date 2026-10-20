"""Reconnect backoff after a duplicate-session takeover (close code 4001).

The server enforces one session per (user, device) by takeover: the newer socket
wins and the older gets a 4001 close. When two supervisors for the same device
are alive, each eviction makes the loser reconnect and evict the winner — a
mutual-eviction loop. A connection that lived 5s
(BACKOFF_RESET_AFTER_SECONDS) resets the ordinary backoff to 500ms.

docs/client-integration.md already specifies the cure ("reconnect on a RAISED
backoff floor; an instant reconnect causes a mutual-eviction storm"); these
tests pin it.
"""

from __future__ import annotations

import asyncio

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway import connection
from clawchat_gateway.connection import ClawChatConnection


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)


def _make_conn() -> ClawChatConnection:
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="t",
        user_id="usr_1",
        owner_user_id="usr_owner",
        reconnect_jitter_ratio=0.0,  # deterministic delays
    )
    return ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)


async def _drive(conn: ClawChatConnection, *, close_codes: list[int | None], monkeypatch) -> list[float]:
    """Run the supervisor for len(close_codes) connections, returning the sleeps.

    Each stubbed connection reaches READY long enough to arm the stable-ready
    backoff reset (the condition that keeps the real loop alive), then ends with
    the given close code.
    """
    sleeps: list[float] = []
    remaining = list(close_codes)

    monkeypatch.setattr(conn, "_load_startup_activation_credentials", lambda: False)

    async def fake_connection() -> None:
        conn._stable_ready_reset_done = True
        conn._last_close_code = remaining.pop(0)

    monkeypatch.setattr(conn, "_run_one_connection", fake_connection)

    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a, **k):
        sleeps.append(delay)
        if not remaining:
            conn._stopping = True
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        await conn._supervisor()
    finally:
        await conn.stop()
    return sleeps


@pytest.mark.asyncio
async def test_takeover_close_raises_the_reconnect_floor(monkeypatch):
    """A 4001 eviction must not reconnect at the 500ms initial delay."""
    conn = _make_conn()
    sleeps = await _drive(conn, close_codes=[4001] * 5, monkeypatch=monkeypatch)

    assert sleeps, "the supervisor must have scheduled reconnects"
    assert min(sleeps) >= 60.0, (
        "every reconnect after a takeover must wait at least the floor; "
        f"got {sleeps} — an instant reconnect is what sustains the eviction loop"
    )


@pytest.mark.asyncio
async def test_repeated_takeovers_escalate_the_floor(monkeypatch):
    """Losing repeatedly must back off further, so one side eventually yields."""
    conn = _make_conn()
    sleeps = await _drive(conn, close_codes=[4001] * 5, monkeypatch=monkeypatch)

    assert sleeps == sorted(sleeps), f"the floor must not decrease while losing: {sleeps}"
    assert sleeps[-1] > sleeps[0], (
        f"a persistent loser must escalate rather than settle at a fixed rate: {sleeps}"
    )


@pytest.mark.asyncio
async def test_ordinary_disconnect_still_reconnects_fast(monkeypatch):
    """The floor is takeover-specific — a normal drop must stay responsive."""
    conn = _make_conn()
    sleeps = await _drive(conn, close_codes=[None] * 3, monkeypatch=monkeypatch)

    assert sleeps == pytest.approx([0.5, 0.5, 0.5]), (
        f"a stable connection that drops normally must resume at the initial delay: {sleeps}"
    )


@pytest.mark.asyncio
async def test_recovered_session_drops_back_to_the_fast_path(monkeypatch):
    """Once takeovers stop, the next ordinary drop is not punished."""
    conn = _make_conn()
    sleeps = await _drive(conn, close_codes=[4001, 4001, None, None], monkeypatch=monkeypatch)

    assert sleeps[0] >= 60.0 and sleeps[1] >= 60.0, f"takeover rounds must be floored: {sleeps}"
    assert sleeps[-1] == pytest.approx(0.5), (
        f"after the loop clears, the backoff must return to the initial delay: {sleeps}"
    )


@pytest.mark.parametrize(
    "streak,expected",
    [(0, 0.0), (1, 60.0), (2, 120.0), (3, 240.0), (4, 480.0), (5, 600.0), (50, 600.0)],
)
def test_takeover_backoff_floor_schedule(streak, expected):
    """The escalation schedule the docs promise, including its ceiling."""
    from clawchat_gateway.connection import takeover_backoff_floor

    assert takeover_backoff_floor(streak) == expected


def test_takeover_backoff_floor_is_zero_without_a_takeover():
    """No floor at all on the ordinary path — this must never slow a healthy reconnect."""
    from clawchat_gateway.connection import takeover_backoff_floor

    assert takeover_backoff_floor(0) == 0.0
    assert takeover_backoff_floor(-1) == 0.0


# ── 4002 duplicate-session refusal (opt-in) ──────────────────────────────────
# When the operator enables the duplicate-session refusal guard, the NEWER
# connection is turned away before hello-ok with close code 4002 and a JSON
# reason carrying retry_after_ms. The server already escalates it
# 60s -> 300s per consecutive refusal; a cooperative client waits that long
# instead of hammering the socket every reconnect_initial_delay_ms.

import json

CLOSE_CODE_DUPLICATE_THROTTLED = 4002


async def _drive_with_reasons(conn, *, closes, monkeypatch):
    """closes: list of (code, reason_str|None); returns the sleeps."""
    sleeps: list[float] = []
    remaining = list(closes)
    monkeypatch.setattr(conn, "_load_startup_activation_credentials", lambda: False)

    async def fake_connection() -> None:
        conn._stable_ready_reset_done = True
        code, reason = remaining.pop(0)
        conn._last_close_code = code
        conn._last_close_reason = reason

    monkeypatch.setattr(conn, "_run_one_connection", fake_connection)
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float, *a, **k):
        sleeps.append(delay)
        if not remaining:
            conn._stopping = True
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    try:
        await conn._supervisor()
    finally:
        await conn.stop()
    return sleeps


@pytest.mark.asyncio
async def test_refusal_close_waits_the_server_retry_after(monkeypatch):
    """A 4002 refusal must wait the server-provided retry_after_ms, not 500ms."""
    conn = _make_conn()
    reason = json.dumps({"reason": "duplicate_session_throttled", "retry_after_ms": 60000})
    sleeps = await _drive_with_reasons(conn, closes=[(CLOSE_CODE_DUPLICATE_THROTTLED, reason)], monkeypatch=monkeypatch)
    assert sleeps and sleeps[0] >= 60.0, (
        f"a 4002 refusal must honour retry_after_ms (60000ms); got {sleeps}"
    )


@pytest.mark.asyncio
async def test_refusal_honours_the_escalated_retry_after(monkeypatch):
    """The server doubles retry_after per refusal; the client tracks the value it is given."""
    conn = _make_conn()
    closes = [
        (CLOSE_CODE_DUPLICATE_THROTTLED, json.dumps({"reason": "duplicate_session_throttled", "retry_after_ms": 60000})),
        (CLOSE_CODE_DUPLICATE_THROTTLED, json.dumps({"reason": "duplicate_session_throttled", "retry_after_ms": 120000})),
    ]
    sleeps = await _drive_with_reasons(conn, closes=closes, monkeypatch=monkeypatch)
    assert sleeps[0] >= 60.0 and sleeps[1] >= 120.0, f"must follow the server's escalation; got {sleeps}"


@pytest.mark.asyncio
async def test_refusal_with_unparseable_reason_falls_back_to_floor(monkeypatch):
    """A malformed / missing reason still must not reconnect at the initial delay."""
    conn = _make_conn()
    sleeps = await _drive_with_reasons(conn, closes=[(CLOSE_CODE_DUPLICATE_THROTTLED, "not json")], monkeypatch=monkeypatch)
    assert sleeps and sleeps[0] >= 60.0, f"fallback floor must apply; got {sleeps}"
