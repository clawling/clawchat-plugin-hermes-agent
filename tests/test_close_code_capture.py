"""The takeover backoff is driven by ws.close_code, so pin that it is real.

The supervisor tests stub _run_one_connection, which means they would still
pass if the close code were never captured from the socket at all. This test
runs a real websockets server that closes with 4001 and asserts the exact
expression the production teardown uses.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets
from websockets.asyncio.server import serve

from clawchat_gateway.connection import CLOSE_CODE_REPLACED, _ws_connect


@pytest.mark.asyncio
async def test_server_takeover_close_code_is_readable_after_close():
    async def handler(ws):
        # How the server closes the displaced connection.
        await ws.close(code=CLOSE_CODE_REPLACED, reason="connection replaced by a newer session for this device")

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await _ws_connect(f"ws://127.0.0.1:{port}")
        try:
            async for _ in ws:  # drains until the server closes
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        await ws.close()

        # The exact expression clawchat_gateway.connection uses in its teardown.
        assert getattr(ws, "close_code", None) == CLOSE_CODE_REPLACED, (
            "the takeover backoff reads ws.close_code; if this attribute is wrong "
            "the whole floor silently never engages"
        )


@pytest.mark.asyncio
async def test_ordinary_server_close_is_not_mistaken_for_a_takeover():
    async def handler(ws):
        await ws.close()  # normal 1000

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        ws = await _ws_connect(f"ws://127.0.0.1:{port}")
        try:
            async for _ in ws:
                pass
        except websockets.exceptions.ConnectionClosed:
            pass
        await ws.close()

        assert getattr(ws, "close_code", None) != CLOSE_CODE_REPLACED


class _FakeWS:
    """Minimal stand-in: a socket that is already gone, carrying a close code."""

    def __init__(self, close_code: int | None) -> None:
        self.close_code = close_code
        self.closed = False

    async def send(self, *_a, **_k) -> None:
        return None

    async def close(self, *_a, **_k) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


async def _run_one_connection_against(monkeypatch, tmp_path, close_code: int | None):
    from clawchat_gateway import connection as conn_mod
    from clawchat_gateway.config import ClawChatConfig

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(conn_mod, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(conn_mod, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(conn_mod, "get_clawchat_store", lambda: None)
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="t",
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    conn = conn_mod.ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)

    fake = _FakeWS(close_code)

    async def fake_connect(*_a, **_k):
        return fake

    monkeypatch.setattr(conn_mod, "_ws_connect", fake_connect)
    monkeypatch.setattr(conn_mod, "HANDSHAKE_TIMEOUT_SECONDS", 0.05)

    try:
        await conn._run_one_connection()
    except Exception:  # the handshake never completes; teardown is what matters
        pass
    return conn


@pytest.mark.asyncio
async def test_run_one_connection_records_the_takeover_close_code(monkeypatch, tmp_path):
    """Production teardown must actually store the code the backoff reads."""
    conn = await _run_one_connection_against(monkeypatch, tmp_path, CLOSE_CODE_REPLACED)
    assert conn._last_close_code == CLOSE_CODE_REPLACED, (
        "_run_one_connection must record the socket's close code, otherwise the "
        "takeover streak never increments and the floor never engages"
    )


@pytest.mark.asyncio
async def test_run_one_connection_records_an_ordinary_close_code(monkeypatch, tmp_path):
    conn = await _run_one_connection_against(monkeypatch, tmp_path, 1000)
    assert conn._last_close_code == 1000
