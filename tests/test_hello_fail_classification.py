"""`hello-fail` reason handling must follow the §3.5 matching contract.

docs/client-integration.md §3.5 pins the contract in both directions:

  - `"authentication failed"` is matched by **exact equality** -> terminal for
    this token.
  - Any OTHER reason, including unknown future strings, is **transient ->
    backoff-reconnect with the same token**, never a terminal auth failure.
    "never widen your terminal branch to a substring/prefix of this string."

The bug these tests pin: a generic reason such as `"nonce mismatch"` (which the
server really does emit, and which §3.5 lists as "New connection") fell through
to AUTH_FAILED, setting `_stopping` or wiping the stored credentials -- forcing
the owner to re-pair after an ordinary handshake hiccup.
"""

from __future__ import annotations

import asyncio

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState


def _make_conn() -> ClawChatConnection:
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="t",
        refresh_token="",          # no refresh path: isolates the fall-through
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    return ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)


async def _feed_hello_fail(reason: str) -> ClawChatConnection:
    conn = _make_conn()
    conn._state = ConnectionState.HANDSHAKING
    # _dispatch_inbound only routes a hello-fail while the handshake is still
    # awaiting its result, so arm the same future the real connect path arms.
    conn._hello_wait = asyncio.get_running_loop().create_future()
    conn._pending_connect_id = "t1"
    await conn._dispatch_inbound(
        {"version": "2", "event": "hello-fail", "trace_id": "t1", "payload": {"reason": reason}}
    )
    return conn


# Reasons §3.5 lists that are NOT the terminal string. None may brick the client.
@pytest.mark.parametrize(
    "reason",
    ["nonce mismatch", "invalid connect event", "invalid connect payload",
     "some future reason the server has not invented yet"],
)
@pytest.mark.asyncio
async def test_non_terminal_reason_keeps_credentials_and_reconnects(reason: str) -> None:
    conn = await _feed_hello_fail(reason)
    assert conn._state != ConnectionState.AUTH_FAILED, f"{reason!r} must not be terminal"
    assert conn._stopping is False, f"{reason!r} must not stop the supervisor"
    assert conn._cfg.token == "t", f"{reason!r} must keep the token"


@pytest.mark.asyncio
async def test_exact_authentication_failed_is_terminal() -> None:
    conn = await _feed_hello_fail("authentication failed")
    assert conn._state == ConnectionState.AUTH_FAILED
    assert conn._stopping is True


@pytest.mark.parametrize("reason", ["invalid token", "token expired", "unauthorized"])
@pytest.mark.asyncio
async def test_speculative_terminal_matchers_are_not_terminal(reason: str) -> None:
    """§3.5: 'do not add speculative terminal matchers ... the server does not emit them'."""
    conn = await _feed_hello_fail(reason)
    assert conn._state != ConnectionState.AUTH_FAILED
    assert conn._cfg.token == "t"


@pytest.mark.parametrize("reason", ["remote auth service unavailable", "auth service unavailable"])
@pytest.mark.asyncio
async def test_backend_outage_stays_transient(reason: str) -> None:
    conn = await _feed_hello_fail(reason)
    assert conn._state != ConnectionState.AUTH_FAILED
    assert conn._cfg.token == "t"
