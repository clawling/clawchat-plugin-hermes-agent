"""Inbound messages must not be handled inside the WebSocket read loop.

The read loop is the only thing that reads ``message.ack``. Several paths
reachable from the adapter's ``on_message`` callback send a reply and wait
for its ack before returning: the plugin's own replies to a non-owner's
``/new`` confirm, and every command Hermes dispatches inline (an owner's
plain ``yes`` to a dangerous-command approval, a group ``/new``). When the
read loop awaited ``on_message`` directly, that ack could never be read: the
send timed out after ``ack_timeout_ms``, the exception tore the read loop
down, the connection was rebuilt, and frames that arrived meanwhile — for
any chat — were lost.

These tests drive the real ``_read_loop`` against a fake socket whose acks
arrive through the same inbound stream, so the only way to read an ack is the
read loop itself. They pin:

* a handler awaiting an ack does not stall the read loop (no deadlock);
* other chats keep flowing while one chat's handler is blocked;
* frames of ONE chat are still handled strictly in arrival order, one at a time;
* a handler that raises costs that frame only, not the connection;
* ``stop()`` cancels in-flight handlers cleanly, also when called from one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState

CHAT_A = "cnv_" + "A" * 26
CHAT_B = "cnv_" + "B" * 26


class _FakeWs:
    """Inbound frames come from a queue; every ack-able send is acked through it."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []

    def push(self, frame: dict[str, Any]) -> None:
        self.inbox.put_nowait(json.dumps(frame))

    async def send(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        if frame.get("event") in {"message.send", "message.reply"}:
            self.push(
                {
                    "version": "2",
                    "event": "message.ack",
                    "trace_id": frame["trace_id"],
                    "chat_id": frame.get("chat_id"),
                    "payload": {"message_id": frame["payload"]["message_id"]},
                }
            )

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def __aiter__(self) -> "_FakeWs":
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


def _inbound(chat_id: str, n: int) -> dict[str, Any]:
    return {
        "version": "2",
        "event": "message.send",
        "trace_id": f"in-{chat_id[-1]}-{n}",
        "chat_id": chat_id,
        "sender": {"id": "usr_friend"},
        "payload": {"message_id": f"msg-{chat_id[-1]}-{n}", "message": {"body": str(n)}},
    }


def _reply(chat_id: str, n: int) -> dict[str, Any]:
    return {
        "version": "2",
        "event": "message.reply",
        "trace_id": f"out-{chat_id[-1]}-{n}",
        "chat_id": chat_id,
        "payload": {"message_id": f"reply-{chat_id[-1]}-{n}"},
    }


def _make_conn(on_message) -> tuple[ClawChatConnection, _FakeWs]:
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="tok_realistic_value",
        user_id="usr_agent",
        owner_user_id="usr_owner",
        ack_timeout_ms=5000,
    )
    conn = ClawChatConnection(cfg, on_message=on_message)
    ws = _FakeWs()
    conn._ws = ws
    conn._state = ConnectionState.READY
    return conn, ws


async def _settle(predicate, timeout: float = 2.0) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=timeout)


@pytest.mark.asyncio
async def test_handler_awaiting_an_ack_does_not_stall_the_read_loop():
    handled: list[str] = []
    acked: list[bool] = []
    conn: ClawChatConnection

    async def on_message(frame: dict[str, Any]) -> None:
        if frame["chat_id"] == CHAT_A:
            # e.g. the reply to a non-owner's /new confirm: send and wait for the ack.
            acked.append(await conn.send_frame(_reply(CHAT_A, 1), wait_for_ack=True))
        handled.append(frame["trace_id"])

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    try:
        ws.push(_inbound(CHAT_A, 1))
        ws.push(_inbound(CHAT_B, 1))
        await _settle(lambda: len(handled) == 2)
        assert acked == [True]
        assert set(handled) == {"in-A-1", "in-B-1"}
        assert not reader.done(), "the read loop must survive"
    finally:
        await conn.stop()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_one_chat_is_handled_in_order_while_other_chats_keep_flowing():
    release_a1 = asyncio.Event()
    started: list[str] = []
    finished: list[str] = []

    async def on_message(frame: dict[str, Any]) -> None:
        started.append(frame["trace_id"])
        if frame["trace_id"] == "in-A-1":
            await release_a1.wait()
        else:
            await asyncio.sleep(0)
        finished.append(frame["trace_id"])

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    try:
        for n in (1, 2, 3):
            ws.push(_inbound(CHAT_A, n))
        ws.push(_inbound(CHAT_B, 1))
        await _settle(lambda: "in-B-1" in finished)
        assert started[0] == "in-A-1"
        assert "in-A-2" not in started, "a chat's next frame waits for the previous one"

        release_a1.set()
        await _settle(lambda: len(finished) == 4)
        assert [t for t in finished if t.startswith("in-A")] == ["in-A-1", "in-A-2", "in-A-3"]
        assert [t for t in started if t.startswith("in-A")] == ["in-A-1", "in-A-2", "in-A-3"]
    finally:
        await conn.stop()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_recall_stays_ordered_behind_the_message_it_recalls():
    release = asyncio.Event()
    seen: list[str] = []

    async def on_message(frame: dict[str, Any]) -> None:
        if frame["event"] == "message.send":
            await release.wait()
        seen.append(frame["event"])

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    try:
        ws.push(_inbound(CHAT_A, 1))
        ws.push(
            {
                "version": "2",
                "event": "message.recall",
                "trace_id": "rcl-1",
                "chat_id": CHAT_A,
                "payload": {"message_id": "rcl:msg-A-1", "target_message_id": "msg-A-1"},
            }
        )
        await asyncio.sleep(0.05)
        assert seen == []
        release.set()
        await _settle(lambda: len(seen) == 2)
        assert seen == ["message.send", "message.recall"]
    finally:
        await conn.stop()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_raising_handler_costs_one_frame_not_the_connection():
    handled: list[str] = []

    async def on_message(frame: dict[str, Any]) -> None:
        if frame["trace_id"] == "in-A-1":
            raise RuntimeError("boom")
        handled.append(frame["trace_id"])

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    try:
        ws.push(_inbound(CHAT_A, 1))
        ws.push(_inbound(CHAT_A, 2))
        ws.push(_inbound(CHAT_B, 1))
        await _settle(lambda: len(handled) == 2)
        assert sorted(handled) == ["in-A-2", "in-B-1"]
        assert not reader.done()
    finally:
        await conn.stop()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_cancels_in_flight_handlers():
    entered = asyncio.Event()
    cancelled: list[str] = []

    async def on_message(frame: dict[str, Any]) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(frame["trace_id"])
            raise

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    ws.push(_inbound(CHAT_A, 1))
    ws.push(_inbound(CHAT_A, 2))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    await asyncio.wait_for(conn.stop(), timeout=2.0)
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)

    assert cancelled == ["in-A-1"]
    leftovers = [
        t for t in asyncio.all_tasks()
        if t is not asyncio.current_task() and (t.get_name() or "").startswith("clawchat-inbound")
    ]
    assert leftovers == []


@pytest.mark.asyncio
async def test_stop_called_from_a_handler_does_not_cancel_that_handler():
    """A handler may tear the gateway down itself (e.g. an inline /restart)."""
    done: list[str] = []
    conn: ClawChatConnection

    async def on_message(frame: dict[str, Any]) -> None:
        await conn.stop()
        done.append(frame["trace_id"])

    conn, ws = _make_conn(on_message)
    reader = asyncio.create_task(conn._read_loop(ws))
    ws.push(_inbound(CHAT_A, 1))
    await _settle(lambda: done == ["in-A-1"])
    reader.cancel()
    await asyncio.gather(reader, return_exceptions=True)
