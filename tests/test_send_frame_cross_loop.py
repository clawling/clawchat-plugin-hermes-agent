"""Tool-loop sends must keep socket, queue, ACK futures and timers on the gateway loop."""

from __future__ import annotations

import asyncio
import contextvars
import json
import threading
import time
from typing import Any

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState


_CALLER_CONTEXT = contextvars.ContextVar("send_frame_test_context", default="gateway")


def _frame() -> dict[str, Any]:
    return {
        "version": "2",
        "event": "message.send",
        "trace_id": "cross-loop-send",
        "chat_id": "cnv_" + "A" * 26,
        "payload": {"message_id": "cross-loop-message"},
    }


class _FakeWs:
    def __init__(self, loop, response):
        self.loop = loop
        self.response = response
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()
        self.sent_loops: list[asyncio.AbstractEventLoop] = []
        self.sent_contexts: list[str] = []

    async def send(self, text: str) -> None:
        self.sent_loops.append(asyncio.get_running_loop())
        self.sent_contexts.append(_CALLER_CONTEXT.get())
        frame = json.loads(text)
        if self.response is not None:
            reply = {
                **frame,
                "event": self.response,
                "payload": {**frame["payload"], "reason": "test rejection"},
            }
            # A gateway read must happen after send_frame registers its ACK.
            # Even before the fix, ONLY the owner loop delivers the response.
            self.loop.call_soon_threadsafe(
                self.loop.call_later, 0.02, self.inbox.put_nowait, json.dumps(reply)
            )

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


class _Gateway:
    def __init__(self, *, ack_timeout_ms=3000, response="message.ack"):
        self.loop = asyncio.new_event_loop()
        self.errors: list[dict[str, Any]] = []
        self.loop.set_exception_handler(lambda _loop, context: self.errors.append(context))
        self.thread = threading.Thread(target=self._run, name="test-gateway-loop")
        self.thread.start()
        try:
            self.call(self._start(ack_timeout_ms, response))
        except BaseException:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)
            raise

    def _run(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            self.loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self.loop.run_until_complete(self.loop.shutdown_asyncgens())
            self.loop.close()

    async def _start(self, ack_timeout_ms, response):
        async def on_message(_frame):
            pass

        self.conn = ClawChatConnection(
            ClawChatConfig(
                websocket_url="wss://example.invalid/ws",
                token="example-placeholder",
                user_id="usr_agent",
                owner_user_id="usr_owner",
                ack_timeout_ms=ack_timeout_ms,
            ),
            on_message=on_message,
        )
        await self.conn.start()
        self.ws = _FakeWs(self.loop, response)
        self.conn._ws = self.ws
        self.conn._state = ConnectionState.READY
        self.conn._read_task = asyncio.create_task(self.conn._read_loop(self.ws))

    def call(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=2)

    def close(self, *, stop_connection=True):
        if self.loop.is_closed():
            assert not self.errors, self.errors
            return
        try:
            if stop_connection:
                self.call(self.conn.stop())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=2)
            assert not self.thread.is_alive(), "gateway loop thread leaked"
            key = self.conn._supervisor_key
            if ClawChatConnection._live_supervisors.get(key) is self.conn:
                del ClawChatConnection._live_supervisors[key]
        assert not self.errors, self.errors


@pytest.fixture
def gateway_factory(monkeypatch):
    monkeypatch.setenv("CLAWCHAT_DEVICE_ID", "hermes-example-test")
    async def idle_supervisor(_self):
        await asyncio.Future()

    # Exercise real start/stop ownership without opening a network connection.
    monkeypatch.setattr(ClawChatConnection, "_supervisor", idle_supervisor)
    gateways = []

    def make(**kwargs):
        gateway = _Gateway(**kwargs)
        gateways.append(gateway)
        return gateway

    yield make
    for gateway in reversed(gateways):
        gateway.close()


def test_cross_loop_ack_returns_before_ack_timeout(gateway_factory):
    gateway = gateway_factory(ack_timeout_ms=10000)

    async def tool_send():
        token = _CALLER_CONTEXT.set("tool")
        try:
            started = time.monotonic()
            result = await gateway.conn.send_frame(_frame(), wait_for_ack=True)
            return result, time.monotonic() - started
        finally:
            _CALLER_CONTEXT.reset(token)

    result, elapsed = asyncio.run(tool_send())
    assert result is True
    # Well under ack_timeout: before the fix this waited the full 10 s.
    assert elapsed < 5, f"cross-loop ACK took {elapsed:.3f}s (ack_timeout=10.000s)"
    assert gateway.ws.sent_loops == [gateway.loop]
    assert gateway.ws.sent_contexts == ["tool"]


@pytest.mark.parametrize("cross_loop", [False, True], ids=["same-loop", "tool-loop"])
def test_missing_ack_keeps_timeout_semantics(gateway_factory, cross_loop):
    gateway = gateway_factory(ack_timeout_ms=300, response=None)

    async def send():
        started = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await gateway.conn.send_frame(_frame(), wait_for_ack=True)
        return time.monotonic() - started

    elapsed = asyncio.run(send()) if cross_loop else gateway.call(send())
    assert 0.25 <= elapsed < 5, f"ACK timeout took {elapsed:.3f}s"
    assert gateway.ws.sent_loops == [gateway.loop]
    assert not gateway.conn._pending_acks


@pytest.mark.parametrize("cross_loop", [False, True], ids=["same-loop", "tool-loop"])
def test_message_error_keeps_rejection_semantics(gateway_factory, cross_loop):
    gateway = gateway_factory(ack_timeout_ms=10000, response="message.error")

    async def send():
        started = time.monotonic()
        with pytest.raises(RuntimeError, match=r"^message\.error: test rejection$"):
            await gateway.conn.send_frame(_frame(), wait_for_ack=True)
        return time.monotonic() - started

    elapsed = asyncio.run(send()) if cross_loop else gateway.call(send())
    assert elapsed < 5
    assert gateway.ws.sent_loops == [gateway.loop]
    assert not gateway.conn._pending_acks


@pytest.mark.parametrize("unavailable", ["closed", "not-running", "stopped"])
def test_unavailable_connection_drops_without_socket_write(gateway_factory, caplog, unavailable):
    gateway = gateway_factory(response=None)
    if unavailable == "stopped":
        gateway.call(gateway.conn.stop())
    else:
        # Leave READY state intact: the owner-loop check itself must fail closed.
        gateway.close(stop_connection=False)
        if unavailable == "not-running":
            loop = asyncio.new_event_loop()
            gateway.conn._owner_loop = loop

    async def send():
        return await gateway.conn.send_frame(_frame(), wait_for_ack=True)

    try:
        with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
            assert asyncio.run(send()) is False
        assert "send_dropped" in caplog.text
        assert "reason=stopped" in caplog.text
        assert gateway.ws.sent_loops == []
        assert not gateway.conn._pending_acks
        assert not gateway.conn._send_queue
        assert not gateway.errors
    finally:
        if unavailable == "not-running":
            loop.close()


def test_fire_and_forget_send_also_uses_owner_loop(gateway_factory):
    gateway = gateway_factory(response=None)
    frame = {**_frame(), "event": "message.reaction"}
    assert asyncio.run(gateway.conn.send_frame(frame, queue_when_unready=False)) is True
    assert gateway.ws.sent_loops == [gateway.loop]
    assert not gateway.conn._pending_acks


def test_cancelling_tool_send_cancels_owner_waiter(gateway_factory):
    gateway = gateway_factory(response=None)

    async def tool_send():
        task = asyncio.create_task(gateway.conn.send_frame(_frame(), wait_for_ack=True))

        async def pending():
            while not gateway.conn._pending_acks:
                await asyncio.sleep(0.005)

        await asyncio.wait_for(pending(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(tool_send())

    async def cancelled():
        # The cancellation is scheduled onto the owner loop asynchronously.
        async def wait():
            while not gateway.conn._pending_acks[_frame()["trace_id"]].future.cancelled():
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait(), timeout=1)

    gateway.call(cancelled())
    assert gateway.ws.sent_loops == [gateway.loop]
