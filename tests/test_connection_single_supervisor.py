"""Single-live-supervisor invariant (duplicate-connection / mutual-kick root fix).

The Hermes reconnect watcher builds a FRESH adapter (=> new ``ClawChatConnection``)
on every retry and only best-effort disconnects the old one — a 5s budget it
abandons on timeout, and a ``wait_for``-cancelled ``connect()`` never disconnects
the prior connection at all. A leaked supervisor keeps opening WS sockets with the
SAME ``device_id``; msghub then mutually kicks the live connection in an endless
reconnect storm (observed in prod: ~116 takeovers/hour for one agent).

The connection enforces "at most one live supervisor per ``account_id`` in this
process" by superseding any orphaned prior supervisor when a fresh one starts.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from clawchat_gateway import connection as conn_mod
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection


def _jwt(payload: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256'})}.{seg(payload)}.sig"


def _fresh_token() -> str:
    now = int(time.time())
    return _jwt({"exp": now + 24 * 3600, "iat": now, "aid": "agt_x"})


async def _noop(_frame):
    return None


def _make_connection(account_id: str = "default") -> ClawChatConnection:
    cfg = ClawChatConfig(
        websocket_url="wss://example.test/ws",
        base_url="https://example.test",
        token=_fresh_token(),
        refresh_token="r0",
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    return ClawChatConnection(cfg, on_message=_noop, account_id=account_id)


@pytest.fixture(autouse=True)
def _clear_registry():
    # The registry is process-wide; isolate each test.
    conn_mod.ClawChatConnection._live_supervisors.clear()
    yield
    conn_mod.ClawChatConnection._live_supervisors.clear()


@pytest.fixture(autouse=True)
def _park_supervisor(monkeypatch):
    # Keep the supervisor "alive" without real WS I/O so the test controls its
    # lifetime: park inside the one-connection coroutine.
    async def _park(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ClawChatConnection, "_run_one_connection", _park, raising=True)


async def test_start_supersedes_orphaned_supervisor_for_same_account():
    a = _make_connection()
    await a.start()
    assert a._supervisor_task is not None
    assert ClawChatConnection._live_supervisors.get("default") is a

    # Host abandoned `a` (its connect() was wait_for-cancelled, no disconnect),
    # then built a fresh connection `b` for the same account.
    b = _make_connection()
    await b.start()

    # The orphaned supervisor must be torn down; only `b` stays live.
    assert a._supervisor_task is None
    assert a._stopping is True
    assert ClawChatConnection._live_supervisors.get("default") is b
    assert b._supervisor_task is not None and not b._supervisor_task.done()

    await b.stop()
    assert ClawChatConnection._live_supervisors.get("default") is None


async def test_distinct_accounts_do_not_supersede_each_other():
    a = _make_connection(account_id="acc_a")
    b = _make_connection(account_id="acc_b")
    await a.start()
    await b.start()
    try:
        # Different accounts are independent — both stay live.
        assert a._supervisor_task is not None and not a._supervisor_task.done()
        assert b._supervisor_task is not None and not b._supervisor_task.done()
        assert ClawChatConnection._live_supervisors.get("acc_a") is a
        assert ClawChatConnection._live_supervisors.get("acc_b") is b
    finally:
        await a.stop()
        await b.stop()


async def test_stop_bounds_slow_ws_close(monkeypatch):
    # A half-dead socket whose close() never returns must not make stop() hang —
    # otherwise supersession (start -> prior.stop) and the host's 5s disconnect
    # budget both stall, re-introducing the orphan.
    monkeypatch.setattr(conn_mod, "_WS_CLOSE_TIMEOUT_SECONDS", 0.05, raising=True)

    class _HangWS:
        def __init__(self):
            self.close_started = False

        async def close(self):
            self.close_started = True
            await asyncio.sleep(3600)

    a = _make_connection()
    a._ws = _HangWS()
    await asyncio.wait_for(a.stop(), timeout=2.0)
    assert a._ws.close_started is True
