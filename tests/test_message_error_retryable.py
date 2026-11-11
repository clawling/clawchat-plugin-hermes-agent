"""Bounded negative-ack retries and terminal recovered-reply suppression."""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace

import pytest

from clawchat_gateway import adapter as adapter_mod, connection, storage
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState, MessageError
from clawchat_gateway.protocol import build_message_reply_event

CHAT = "cnv_" + "0" * 26
RECOVERED = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"


@pytest.fixture(autouse=True)
def homes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    reset = getattr(storage, "reset_clawchat_store_for_test", lambda: None)
    reset()
    yield
    reset()


def peer(monkeypatch, errors):
    conn = ClawChatConnection(ClawChatConfig(websocket_url="wss://example.invalid/ws"),
                             on_message=lambda *_: None)
    conn._state = ConnectionState.READY
    frames, sleeps = [], []
    responses = iter(errors)

    class Socket:
        async def send(self, text):
            frame = json.loads(text)
            frames.append(frame)
            payload = next(responses, None)
            response = dict(trace_id=frame["trace_id"], chat_id=CHAT,
                            payload=payload or {"message_id": "reply-placeholder"})
            handler = conn._handle_message_error if payload else conn._handle_ack
            asyncio.get_running_loop().call_soon(handler, response)

    async def sleep(delay):
        if asyncio.current_task().get_name() == "clawchat-ack-timeout":
            await asyncio.Event().wait()
        else:
            sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    monkeypatch.setattr(connection.random, "uniform", lambda *_: 0.0)
    conn._ws = Socket()
    return conn, frames, sleeps


def frame():
    return build_message_reply_event(chat_id=CHAT, chat_type="direct",
                                    message_id="reply-placeholder", include_message_id=True,
                                    fragments=[{"kind": "text", "text": "Report ready"}])


def limited(**extra):
    return dict(code="send_rate_limited", retryable=True, retry_after_ms=1500, **extra)


async def test_rate_limited_retries_same_message_new_trace(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [limited()])
    assert await conn.send_frame(frame(), wait_for_ack=True)
    assert sleeps == [1.5]
    assert len(frames) == 2
    assert frames[0]["payload"] == frames[1]["payload"]
    assert frames[0]["trace_id"] != frames[1]["trace_id"]
    assert not conn._pending_acks


async def test_retry_limit(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [limited()] * 4)
    with pytest.raises(MessageError) as error:
        await conn.send_frame(frame(), wait_for_ack=True)
    assert error.value.retryable is True
    assert error.value.retry_after_ms == 1500
    assert len(frames) == 4
    assert sleeps == [1.5, 2, 4]
    assert len({f["trace_id"] for f in frames}) == 4


@pytest.mark.parametrize("payload", [
    {"code": "agent_turn_limit", "retryable": False},
    {"code": "agent_muted", "retryable": False},
    {"code": "send_rate_limited"},
    {"code": "send_rate_limited", "retryable": "true"},
    {"code": "future_code", "retryable": True},
])
async def test_terminal_errors_do_not_retry_or_gate(monkeypatch, payload):
    conn, frames, sleeps = peer(monkeypatch, [payload])
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    await adapter._on_message_error({"chat_id": CHAT, "payload": payload})
    assert not getattr(adapter, "_server_rejected_chats", {})
    with pytest.raises(MessageError) as error:
        await conn.send_frame(frame(), wait_for_ack=True)
    assert error.value.retryable is (payload.get("retryable") is True)
    assert len(frames) == 1
    assert sleeps == []


@pytest.mark.parametrize("delay", [None, 0, -1, True, 1.5, "1000"])
async def test_invalid_hint_uses_conservative_backoff(monkeypatch, delay):
    payload = {"code": "send_rate_limited", "retryable": True, "retry_after_ms": delay}
    conn, frames, sleeps = peer(monkeypatch, [payload])
    assert await conn.send_frame(frame(), wait_for_ack=True)
    assert sleeps == [1]
    assert MessageError("refused", payload).retry_after_ms is None


async def test_wait_caps_preserve_server_minimum(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [dict(limited(), retry_after_ms=30001)])
    with pytest.raises(MessageError):
        await conn.send_frame(frame(), wait_for_ack=True)
    assert len(frames) == 1 and not sleeps
    conn, frames, sleeps = peer(monkeypatch, [dict(limited(), retry_after_ms=30000)] * 4)
    with pytest.raises(MessageError):
        await conn.send_frame(frame(), wait_for_ack=True)
    assert sleeps == [30, 30] and len(frames) == 3


async def test_disconnect_cancels_retry_wait(monkeypatch):
    conn, frames, _ = peer(monkeypatch, [limited()])
    entered = asyncio.Event()

    async def sleep(delay):
        if asyncio.current_task().get_name() != "clawchat-ack-timeout":
            entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(conn.send_frame(frame(), wait_for_ack=True))
    await entered.wait()
    conn._reject_pending_acks(RuntimeError("connection disconnected"))
    # The send fails with the original negative ack, not a CancelledError that
    # callers would mistake for their own cancellation.
    with pytest.raises(MessageError):
        await task
    assert len(frames) == 1
    assert not conn._message_retry_waiters


async def test_wait_message_retry_succeeds_and_unregisters_waiter(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [])
    assert await conn.wait_message_retry(1.5) is True
    assert sleeps == [1.5]
    assert not frames and not conn._message_retry_waiters


async def test_wait_message_retry_disconnect_cancellation_returns_false(monkeypatch):
    conn, _, _ = peer(monkeypatch, [])
    entered = asyncio.Event()

    async def sleep(_delay):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    task = asyncio.create_task(conn.wait_message_retry(1.5))
    await entered.wait()
    assert len(conn._message_retry_waiters) == 1
    # Even if reconnect has already restored READY, the cancelled waiter fails.
    conn._reject_pending_acks(RuntimeError("connection disconnected"))
    assert await task is False
    assert not conn._message_retry_waiters


@pytest.mark.parametrize("change", ["generation", "socket", "stopping", "state"])
async def test_wait_message_retry_rechecks_connection_after_wait(monkeypatch, change):
    conn, _, _ = peer(monkeypatch, [])

    async def sleep(_delay):
        assert asyncio.current_task() in conn._message_retry_waiters
        if change == "generation":
            conn._delivery_generation += 1
        elif change == "socket":
            conn._ws = object()
        elif change == "stopping":
            conn._stopping = True
        else:
            conn._state = ConnectionState.DISCONNECTED

    monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await conn.wait_message_retry(1.5) is False
    assert not conn._message_retry_waiters


@pytest.mark.parametrize("unready", ["stopping", "state", "socket"])
async def test_wait_message_retry_refuses_unready_connection(monkeypatch, unready):
    conn, _, sleeps = peer(monkeypatch, [])
    if unready == "stopping":
        conn._stopping = True
    elif unready == "state":
        conn._state = ConnectionState.DISCONNECTED
    else:
        conn._ws = None
    assert await conn.wait_message_retry(1.5) is False
    assert not sleeps and not conn._message_retry_waiters


@pytest.mark.parametrize("changed", ["before", "during"])
async def test_wait_message_retry_honors_supplied_generation(monkeypatch, changed):
    conn, _, sleeps = peer(monkeypatch, [])
    generation = conn.delivery_generation
    if changed == "before":
        conn._delivery_generation += 1
    else:
        async def sleep(delay):
            sleeps.append(delay)
            conn._delivery_generation += 1

        monkeypatch.setattr(asyncio, "sleep", sleep)
    assert await conn.wait_message_retry(1.5, generation=generation) is False
    assert sleeps == ([] if changed == "before" else [1.5])
    assert not conn._message_retry_waiters


@pytest.mark.parametrize("disconnect", [False, True])
async def test_wait_message_retry_uses_owner_loop_from_another_thread(monkeypatch, disconnect):
    conn, _, _ = peer(monkeypatch, [])
    owner = asyncio.new_event_loop()
    started = threading.Event()
    loops = []

    def run_owner():
        asyncio.set_event_loop(owner)
        owner.call_soon(started.set)
        try:
            owner.run_forever()
        finally:
            tasks = asyncio.all_tasks(owner)
            for task in tasks:
                task.cancel()
            owner.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            owner.close()

    async def sleep(delay):
        loops.append(asyncio.get_running_loop())
        assert loops[-1] is owner
        assert delay == 1.5
        assert asyncio.current_task() in conn._message_retry_waiters
        assert all(waiter.get_loop() is owner for waiter in conn._message_retry_waiters)
        if disconnect:
            # Both waiter creation and disconnect cancellation happen on owner.
            owner.call_soon(conn._reject_pending_acks, RuntimeError("connection disconnected"))
            await asyncio.Event().wait()

    monkeypatch.setattr(asyncio, "sleep", sleep)
    thread = threading.Thread(target=run_owner, name="test-retry-owner-loop", daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=2)
        conn._owner_loop = owner
        assert asyncio.get_running_loop() is not owner
        result = await asyncio.wait_for(
            conn.wait_message_retry(1.5, generation=conn.delivery_generation), timeout=2,
        )
        assert result is (not disconnect)
        assert loops == [owner]
        assert not conn._message_retry_waiters
    finally:
        owner.call_soon_threadsafe(owner.stop)
        thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize("closed", [False, True])
async def test_wait_message_retry_refuses_unavailable_owner_loop(monkeypatch, closed):
    conn, _, sleeps = peer(monkeypatch, [])
    owner = asyncio.new_event_loop()
    conn._owner_loop = owner
    try:
        if closed:
            owner.close()
        assert await conn.wait_message_retry(1.5, generation=conn.delivery_generation) is False
        assert not sleeps and not conn._message_retry_waiters
    finally:
        owner.close()


async def test_stream_add_does_not_retry(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [limited()])
    async def refuse(*args, **kwargs):
        raise MessageError("refused", limited())
    monkeypatch.setattr(conn, "_send_frame_once", refuse)
    with pytest.raises(MessageError):
        await conn.send_frame(dict(frame(), event="message.add"), wait_for_ack=True)
    assert not sleeps


async def test_terminal_recovered_content_is_blocked_even_without_old_id(monkeypatch):
    adapter = ClawChatAdapter({})
    adapter._clawchat_config = replace(adapter._clawchat_config,
                                     user_id="usr_placeholder", owner_user_id="usr_owner_placeholder")
    adapter._known_chat_types[CHAT] = "direct"
    calls = []

    async def refuse(frame, **kwargs):
        calls.append(frame)
        raise MessageError("turn limit", {"code": "agent_turn_limit", "retryable": False})

    monkeypatch.setattr(adapter._connection, "send_frame", refuse)
    with pytest.raises(MessageError):
        await adapter.send(CHAT, "Report ready")
    monkeypatch.setattr(adapter, "_earlier_attempt_message_id", lambda *_: None)
    result = await adapter.send(CHAT, RECOVERED + "Report ready")
    assert not result.success
    assert len(calls) == 1
    assert not adapter._server_rejected_chats
    for n in range(260):
        with pytest.raises(MessageError):
            await adapter._send_final_reply_frame(frame(), CHAT, str(n))
    assert len(adapter._rejected_reply_cache()) == 256


async def test_exhausted_rate_limit_blocks_host_recovery_until_cooldown_expires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock[0])
    conn, frames, sleeps = peer(monkeypatch, [limited()] * 4)
    adapter = ClawChatAdapter({})
    adapter._clawchat_config = replace(adapter._clawchat_config,
                                     user_id="usr_placeholder", owner_user_id="usr_owner_placeholder")
    adapter._known_chat_types[CHAT] = "direct"
    monkeypatch.setattr(adapter._connection, "send_frame", conn.send_frame)
    monkeypatch.setattr(adapter, "_earlier_attempt_message_id", lambda *_: None)
    with pytest.raises(MessageError):
        await adapter.send(CHAT, "Report ready")
    assert len(frames) == 4 and sleeps == [1.5, 2, 4]
    key = adapter._reply_content_key(CHAT, "Report ready")
    deadline = 1000.0 + connection.MESSAGE_ERROR_RETRY_TOTAL_WAIT_SECONDS
    assert adapter._rejected_reply_cache()[key] == deadline

    async def accept(frame, **kwargs):
        assert kwargs["wait_for_ack"] is True
        frames.append(frame)
        return True

    monkeypatch.setattr(adapter._connection, "send_frame", accept)
    for now in (1000.0, deadline - 0.001):
        clock[0] = now
        result = await adapter.send(CHAT, RECOVERED + "Report ready")
        assert not result.success
        assert len(frames) == 4
        assert adapter._rejected_reply_cache()[key] == deadline  # Recovery cannot renew the budget.
    clock[0] = deadline
    result = await adapter.send(CHAT, RECOVERED + "Report ready")
    assert result.success
    assert len(frames) == 5
    assert key not in adapter._rejected_reply_cache()


async def test_exhausted_rate_limit_cooldown_preserves_long_server_hint(monkeypatch):
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: 1000.0)
    adapter = ClawChatAdapter({})

    async def refuse(frame, **kwargs):
        raise MessageError("refused", dict(limited(), retry_after_ms=120000))

    monkeypatch.setattr(adapter._connection, "send_frame", refuse)
    with pytest.raises(MessageError):
        await adapter._send_final_reply_frame(frame(), CHAT, "Report ready")
    key = adapter._reply_content_key(CHAT, "Report ready")
    assert adapter._rejected_reply_cache()[key] == 1120.0


def test_rejected_reply_cache_refreshes_lru_and_preserves_terminal_entries():
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._cache_rejected_reply(frame(), CHAT, "Report ready")
    adapter._cache_rejected_reply(frame(), CHAT, "Report ready", 1200.0)
    key = adapter._reply_content_key(CHAT, "Report ready")
    assert adapter._rejected_reply_cache()[key] is None
    for n in range(255):
        adapter._cache_rejected_reply(frame(), CHAT, str(n))
    assert adapter._reply_content_rejected(CHAT, "Report ready")
    adapter._cache_rejected_reply(frame(), CHAT, "new reply")
    assert len(adapter._rejected_reply_cache()) == 256
    assert key in adapter._rejected_reply_cache()
    assert adapter._reply_content_key(CHAT, "0") not in adapter._rejected_reply_cache()


async def test_replaced_socket_fences_retry(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [limited()])

    async def replace_socket(delay):
        if asyncio.current_task().get_name() == "clawchat-ack-timeout":
            await asyncio.Event().wait()
        else:
            conn._delivery_generation += 1
            conn._ws = object()

    monkeypatch.setattr(asyncio, "sleep", replace_socket)
    with pytest.raises(MessageError):
        await conn.send_frame(frame(), wait_for_ack=True)
    assert len(frames) == 1


async def test_jitter_is_added_after_server_minimum(monkeypatch):
    conn, frames, sleeps = peer(monkeypatch, [limited()])
    monkeypatch.setattr(connection.random, "uniform", lambda *_: 0.2)
    assert await conn.send_frame(frame(), wait_for_ack=True)
    assert sleeps == [pytest.approx(1.8)]


async def test_rejected_reply_with_attachment_or_no_text_is_not_cached(monkeypatch):
    adapter = ClawChatAdapter({})

    async def refuse(frame, **kwargs):
        raise MessageError("turn limit", {"code": "agent_turn_limit", "retryable": False})

    monkeypatch.setattr(adapter._connection, "send_frame", refuse)
    media = build_message_reply_event(
        chat_id=CHAT, chat_type="direct", message_id="reply-placeholder", include_message_id=True,
        fragments=[{"kind": "text", "text": "Report ready"},
                   {"kind": "image", "url": "https://example.invalid/a.png"}])
    with pytest.raises(MessageError):
        await adapter._send_final_reply_frame(media, CHAT, "Report ready")
    with pytest.raises(MessageError):
        await adapter._send_final_reply_frame(frame(), CHAT, "   ")
    # Only the visible text is keyed, so neither may suppress a later reply.
    assert not adapter._rejected_reply_cache()


async def test_rejected_reply_with_mention_is_not_resent_on_recovery(monkeypatch):
    adapter = ClawChatAdapter({})
    adapter._clawchat_config = replace(adapter._clawchat_config,
                                     user_id="usr_placeholder", owner_user_id="usr_owner_placeholder")
    adapter._known_chat_types[CHAT] = "group"
    calls = []

    async def refuse(frame, **kwargs):
        calls.append(frame)
        raise MessageError("turn limit", {"code": "agent_turn_limit", "retryable": False})

    monkeypatch.setattr(adapter._connection, "send_frame", refuse)
    mention = build_message_reply_event(
        chat_id=CHAT, chat_type="group", message_id="reply-placeholder", include_message_id=True,
        fragments=[{"kind": "mention", "user_id": "usr_member_placeholder", "display": "@Member"},
                   {"kind": "text", "text": " report ready"}])
    with pytest.raises(MessageError):
        await adapter._send_final_reply_frame(mention, CHAT, "@Member report ready")
    assert adapter._rejected_reply_cache()
    monkeypatch.setattr(adapter, "_earlier_attempt_message_id", lambda *_: None)
    result = await adapter.send(CHAT, RECOVERED + "@Member report ready")
    assert not result.success
    assert len(calls) == 1
