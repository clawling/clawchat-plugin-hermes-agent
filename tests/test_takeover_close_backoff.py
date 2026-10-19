"""Peer 4001/4002 closes must reach the supervisor's reconnect floor branches.

websockets raises ConnectionClosedError after READY for application close codes.
Before hello-ok, a 4002 kills the reader and leaves the handshake to time out.
Previously both exceptions bypassed the floor and cleared the takeover streak.
These tests drive the real connection against a local WebSocket server.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

from clawchat_gateway import connection
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState


class _ReconnectClock:
    """Fake only the supervisor's sleeps; leave socket and watcher tasks alone."""

    def __init__(self, conn: ClawChatConnection, rounds: int) -> None:
        self.conn = conn
        self.rounds = rounds
        self.supervisor_task = asyncio.current_task()
        self.sleeps: list[float] = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, delay, *args, **kwargs):
        if asyncio.current_task() is not self.supervisor_task:
            return await asyncio.sleep(delay, *args, **kwargs)
        assert self.conn._state == ConnectionState.RECONNECTING
        self.sleeps.append(delay)
        if len(self.sleeps) == self.rounds:
            self.conn._stopping = True
        return await asyncio.sleep(0)


@asynccontextmanager
async def _peer(monkeypatch, close_codes):
    remaining = iter(close_codes)

    async def handler(ws):
        code = next(remaining)
        await ws.send(json.dumps({
            "type": "event", "event": "connect.challenge",
            "payload": {"nonce": "test-nonce"},
        }))
        request = json.loads(await ws.recv())
        assert request["event"] == "connect"
        if code == 4002:
            await ws.close(code=4002, reason=json.dumps({
                "reason": "duplicate_session_throttled", "retry_after_ms": 90000,
            }))
            return
        await ws.send(json.dumps({
            "type": "res", "requestId": request["trace_id"],
            "payload": {"type": "hello-ok"},
        }))
        await asyncio.wait_for(conn._ready_event.wait(), timeout=5)
        if code is None:
            ws.transport.abort()  # no close frame: the client records 1006
        else:
            await ws.close(code=code, reason="test close")

    # Avoid opening sqlite even during construction, and keep device resolution
    # independent of the machine running the tests.
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    if 4002 in close_codes:
        # Only the refusal case is meant to time out; keep the real deadline
        # for handshakes that must succeed.
        monkeypatch.setattr(connection, "HANDSHAKE_TIMEOUT_SECONDS", 0.2)
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = ClawChatConfig(
            websocket_url=f"ws://127.0.0.1:{port}",
            token="test-token",
            user_id="usr_test",
            owner_user_id="usr_owner",
            reconnect_jitter_ratio=0.0,
        )
        conn = ClawChatConnection(
            cfg, on_message=lambda *_a, **_k: None,
            connect_device_id="test-device",
        )
        assert conn._store is None
        yield conn


async def _drive(monkeypatch, close_codes):
    async with _peer(monkeypatch, close_codes) as conn:
        clock = _ReconnectClock(conn, len(close_codes))
        monkeypatch.setattr(connection, "asyncio", clock)
        await conn._supervisor()
        assert conn._attempt == len(close_codes)
        return conn, clock.sleeps


@pytest.mark.asyncio
async def test_takeover_after_ready_raises_connection_closed_error(monkeypatch):
    async def check():
        async with _peer(monkeypatch, [4001]) as conn:
            with pytest.raises(ConnectionClosedError):
                await conn._run_one_connection()
            assert conn._hello_wait.result() is True
            assert conn._last_close_code == 4001

    await asyncio.wait_for(check(), timeout=20)


@pytest.mark.asyncio
async def test_takeovers_after_ready_escalate_despite_read_errors(monkeypatch):
    conn, sleeps = await asyncio.wait_for(
        _drive(monkeypatch, [4001, 4001]), timeout=20,
    )
    assert sleeps[0] >= 60
    assert sleeps[1] >= 120
    assert conn._takeover_streak == 2
    assert conn._last_close_code == 4001


@pytest.mark.asyncio
async def test_refusal_before_hello_honours_retry_after_despite_timeout(monkeypatch):
    conn, sleeps = await asyncio.wait_for(
        _drive(monkeypatch, [4002]), timeout=20,
    )
    assert sleeps[0] >= 90
    assert conn._takeover_streak == 0
    assert conn._last_close_code == 4002
    assert conn._hello_wait.cancelled()  # wait_for timed out before hello-ok


@pytest.mark.asyncio
@pytest.mark.parametrize("abnormal_code", [None, 1011], ids=["tcp-abort", "1011"])
@pytest.mark.parametrize("after_takeover", [False, True], ids=["alone", "after-takeover"])
async def test_abnormal_drop_keeps_ordinary_delay(monkeypatch, abnormal_code, after_takeover):
    closes = [4001, abnormal_code] if after_takeover else [abnormal_code]
    conn, sleeps = await asyncio.wait_for(_drive(monkeypatch, closes), timeout=20)
    cfg = conn.config
    expected = min(
        cfg.reconnect_initial_delay_ms / 1000.0 * (2 if after_takeover else 1),
        cfg.reconnect_max_delay_ms / 1000.0,
    )
    if after_takeover:
        assert sleeps[0] >= 60
    assert sleeps[-1] == pytest.approx(expected)
    assert sleeps[-1] < 60
    assert conn._takeover_streak == 0
    assert conn._last_close_code == (1006 if abnormal_code is None else abnormal_code)
