import asyncio
import logging
import re

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState

from tests.fake_ws import FakeClawChatServer


def _cfg(**overrides) -> ClawChatConfig:
    base = dict(
        websocket_url="ws://fake/ws",
        token="tok",
        user_id="bot",
        reconnect_initial_delay_ms=10,
        reconnect_max_delay_ms=40,
        reconnect_jitter_ratio=0.0,
        reconnect_max_retries=3,
        heartbeat_interval_ms=30_000,
        heartbeat_timeout_ms=60_000,
    )
    base.update(overrides)
    return ClawChatConfig(**base)


async def _wait_until(predicate, *, timeout: float = 1.0, sleep=asyncio.sleep):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await sleep(0.01)
    raise AssertionError("timed out waiting for condition")


async def _wait_for_connect(srv: FakeClawChatServer, *, count: int = 1):
    await _wait_until(lambda: len(srv.connect_calls) >= count)


async def _complete_handshake(srv: FakeClawChatServer) -> dict:
    await _wait_for_connect(srv)
    srv.enqueue_from_server(
        {
            "version": "2",
            "event": "connect.challenge",
            "trace_id": "challenge",
            "payload": {"nonce": "N"},
        }
    )
    req = await srv.read_client_frame(timeout=1.0)
    srv.enqueue_from_server(
        {
            "version": "2",
            "event": "hello-ok",
            "trace_id": req["trace_id"],
            "payload": {},
        }
    )
    return req


async def _wait_for_ready(conn: ClawChatConnection, srv: FakeClawChatServer) -> None:
    await _complete_handshake(srv)
    await _wait_until(lambda: conn.is_ready)


async def test_challenge_endpoint_sends_connect_before_ready(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.get_device_id", lambda: "hermes-test-device")

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(
        _cfg(websocket_url="ws://company.newbaselab.com:10086/ws"),
        on_message=on_message,
    )
    await conn.start()
    try:
        await _wait_for_connect(srv)
        assert conn.is_ready is False

        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "connect.challenge",
                "trace_id": "challenge",
                "payload": {"nonce": "N"},
            }
        )
        req = await srv.read_client_frame(timeout=1.0)
        assert req["event"] == "connect"
        assert req["payload"]["device_id"] == "hermes-test-device"
        assert req["payload"]["nonce"] == "N"
        assert conn.is_ready is False

        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "hello-ok",
                "trace_id": req["trace_id"],
                "payload": {},
            }
        )
        await _wait_until(lambda: conn.is_ready)
    finally:
        await conn.stop()


async def test_connection_reaches_ready_after_challenge_handshake(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen = []

    async def on_message(_frame):
        pass

    async def on_state(state):
        seen.append(state)

    conn = ClawChatConnection(
        _cfg(),
        on_message=on_message,
        on_state_change=on_state,
    )
    await conn.start()
    try:
        req = await _complete_handshake(srv)
        assert req["event"] == "connect"
        await _wait_until(lambda: conn.is_ready)
        assert conn.is_ready is True
        assert "ready" in [s.value for s in seen]
    finally:
        await conn.stop()


async def test_connection_logs_canonical_connect_and_handshake(monkeypatch, caplog):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.get_device_id", lambda: "hermes-test-device")

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.connection"):
        await conn.start()
        try:
            req = await _complete_handshake(srv)
            await _wait_until(lambda: conn.is_ready)
        finally:
            await conn.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "clawchat.ws event=connect_start account_id=default attempt=1 "
        "reconnect_count=0 state=connecting action=connect url=ws://fake/ws queue_size=0"
    ) in messages
    assert any(
        re.fullmatch(
            (
                "clawchat\\.ws event=handshake_ok account_id=default attempt=1 "
                "reconnect_count=0 state=ready action=flush_queue "
                f"trace_id={re.escape(req['trace_id'])} elapsed_ms=\\d+ queue_size=0"
            ),
            message,
        )
        for message in messages
    )


async def test_challenge_frames_are_ignored(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "connect.challenge",
                "trace_id": "challenge",
                "payload": {"nonce": "N"},
            }
        )
        with pytest.raises(asyncio.TimeoutError):
            await srv.read_client_frame(timeout=0.05)
        assert conn.is_ready is True
    finally:
        await conn.stop()


async def test_message_send_dispatches_after_handshake(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen_messages = []

    async def on_message(frame):
        seen_messages.append(frame)

    conn = ClawChatConnection(
        _cfg(),
        on_message=on_message,
    )
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        srv.enqueue_from_server(
            {
                "type": "event",
                "id": "m1",
                "event": "message.send",
                "chat_id": "u1",
                "chat_type": "direct",
                "sender": {"id": "u1"},
                "payload": {"message": {"fragments": [{"kind": "text", "text": "hi"}]}},
            }
        )
        await _wait_until(lambda: len(seen_messages) == 1)

        assert seen_messages[0]["event"] == "message.send"
        assert conn.is_ready is True
    finally:
        await conn.stop()


async def test_bearer_auth_header_is_sent_on_connect(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(token="tok-123"), on_message=on_message)
    await conn.start()
    try:
        await _wait_until(lambda: bool(srv.connect_calls))
        headers = srv.connect_calls[0]["kwargs"].get("additional_headers") or {}
        assert headers["Authorization"] == "Bearer tok-123"
    finally:
        await conn.stop()


async def test_subprotocol_headers_are_sent_on_connect(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.get_device_id", lambda: "hermes-test-device")

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(token="tok-123"), on_message=on_message)
    await conn.start()
    try:
        await _wait_until(lambda: bool(srv.connect_calls))
        kwargs = srv.connect_calls[0]["kwargs"]
        headers = kwargs.get("additional_headers") or {}
        assert headers["X-Device-Id"] == "hermes-test-device"
        assert kwargs["subprotocols"] == ["clawchat.v1", "bearer.tok-123"]
    finally:
        await conn.stop()


async def test_challenge_connect_sends_device_header_and_payload(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.get_device_id", lambda: "hermes-test-device")

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        req = await _complete_handshake(srv)
        headers = srv.connect_calls[0]["kwargs"].get("additional_headers") or {}
        assert headers["X-Device-Id"] == "hermes-test-device"
        assert req["payload"]["device_id"] == "hermes-test-device"
        assert req["payload"]["capabilities"] == {"multi_device": True, "device_replay": True}
        assert "sign" not in req["payload"]
        assert "signature" not in req["payload"]
        assert "client_id" not in req["payload"]
        assert "client_version" not in req["payload"]
    finally:
        await conn.stop()


async def test_rich_interactions_preserve_challenge_handshake(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(
        _cfg(enable_rich_interactions=True),
        on_message=on_message,
    )
    await conn.start()
    try:
        req = await _complete_handshake(srv)
        assert req["event"] == "connect"
        await _wait_until(lambda: conn.is_ready)
    finally:
        await conn.stop()


async def test_hello_fail_frame_does_not_affect_ready_connection(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen_states = []

    async def on_message(_frame):
        pass

    async def on_state(state):
        seen_states.append(state)

    conn = ClawChatConnection(
        _cfg(reconnect_max_retries=0),
        on_message=on_message,
        on_state_change=on_state,
    )
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "hello-fail",
                "trace_id": "wrong-id",
                "payload": {"reason": "wrong id"},
            }
        )
        await asyncio.sleep(0.05)
        assert conn.is_ready is True
        assert ConnectionState.READY in seen_states
        assert ConnectionState.CLOSED not in seen_states
    finally:
        await conn.stop()


async def test_hello_fail_logs_auth_failed_and_stops_reconnect(monkeypatch, caplog):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        await conn.start()
        try:
            await _wait_for_connect(srv)
            srv.enqueue_from_server(
                {
                    "version": "2",
                    "event": "connect.challenge",
                    "trace_id": "challenge",
                    "payload": {"nonce": "N"},
                }
            )
            req = await srv.read_client_frame(timeout=1.0)
            srv.enqueue_from_server(
                {
                    "version": "2",
                    "event": "hello-fail",
                    "trace_id": req["trace_id"],
                    "payload": {"reason": "authentication failed"},
                }
            )
            await _wait_until(lambda: conn._state.value in {"auth_failed", "closed"})
            await asyncio.sleep(0.05)
            assert len(srv.connect_calls) == 1
        finally:
            await conn.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "clawchat.ws event=auth_failed account_id=default attempt=1 reconnect_count=0 "
        f"state=auth_failed action=stop_reconnect trace_id={req['trace_id']} reason=authentication failed"
    ) in messages


async def test_queued_outbound_frame_flushes_after_ready(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        await conn.send_frame({"type": "event", "id": "m1", "event": "message.reply"})
        await _wait_for_ready(conn, srv)
        queued = await srv.read_client_frame(timeout=1.0)
        assert queued["id"] == "m1"
        assert queued["event"] == "message.reply"
    finally:
        await conn.stop()


async def test_connection_logs_receive_dispatch_and_send(monkeypatch, caplog):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen_messages = []

    async def on_message(frame):
        seen_messages.append(frame)

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.connection"):
        await conn.start()
        try:
            await _wait_for_ready(conn, srv)
            await conn.send_frame({"type": "event", "id": "out-1", "event": "message.reply"})
            await srv.read_client_frame(timeout=1.0)
            srv.enqueue_from_server(
                {
                    "type": "event",
                    "id": "m1",
                    "event": "message.send",
                    "chat_id": "u1",
                    "sender": {"id": "u1"},
                    "payload": {
                        "message": {
                            "context": {},
                            "fragments": [{"kind": "text", "text": "hi"}],
                        }
                    },
                }
            )
            await _wait_until(lambda: len(seen_messages) == 1)
        finally:
            await conn.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert any("clawchat ws recv event=message.send" in message for message in messages)
    assert any("clawchat ws dispatch message.send chat_id=u1" in message for message in messages)
    assert any("payload_keys=['message']" in message for message in messages)
    assert any("message_keys=['context', 'fragments']" in message for message in messages)
    assert any("body_type=NoneType" in message for message in messages)
    assert any("clawchat ws send event=message.reply id=out-1" in message for message in messages)


async def test_ready_dispatches_message_reply(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen_messages = []

    async def on_message(frame):
        seen_messages.append(frame)

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "message.reply",
                "chat_id": "u1",
                "sender": {"id": "u1"},
                "payload": {"message": {"context": {}, "fragments": [{"kind": "text", "text": "hi"}]}},
            }
        )
        await _wait_until(lambda: len(seen_messages) == 1)
        assert seen_messages[0]["event"] == "message.reply"
    finally:
        await conn.stop()


async def test_ready_dispatches_interaction_submit(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    seen_messages = []

    async def on_message(frame):
        seen_messages.append(frame)

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        srv.enqueue_from_server(
            {
                "version": "2",
                "event": "interaction.submit",
                "chat_id": "u1",
                "sender": {"id": "u1"},
                "payload": {
                    "message_id": "msg-1",
                    "fragment_index": 0,
                    "fragment_kind": "approval_request",
                    "action_id": "approve",
                },
            }
        )
        await _wait_until(lambda: len(seen_messages) == 1)
        assert seen_messages[0]["event"] == "interaction.submit"
    finally:
        await conn.stop()


async def test_queued_frames_flush_in_order_after_handshake(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.send_frame({"type": "event", "id": "a", "event": "message.reply"})
    await conn.send_frame({"type": "event", "id": "b", "event": "message.reply"})
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)
        first = await srv.read_client_frame(timeout=1.0)
        second = await srv.read_client_frame(timeout=1.0)

        assert first["id"] == "a"
        assert second["id"] == "b"
    finally:
        await conn.stop()


async def test_queued_frame_survives_failed_flush_and_reconnect(monkeypatch):
    srv = FakeClawChatServer()
    failed = False

    async def connect_with_first_send_failure(url: str, **kwargs):
        nonlocal failed
        ws = await srv.connect(url, **kwargs)
        real_send = ws.send

        async def flaky_send(text: str):
            nonlocal failed
            if '"event":"connect"' not in text and not failed:
                failed = True
                raise ConnectionError("flush failed")
            await real_send(text)

        if len(srv.connect_calls) == 1:
            monkeypatch.setattr(ws, "send", flaky_send)
        return ws

    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", connect_with_first_send_failure)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.send_frame({"type": "event", "id": "queued-1", "event": "message.reply"})
    await conn.start()
    try:
        await _complete_handshake(srv)
        await _wait_until(lambda: len(srv.connect_calls) >= 2, timeout=0.3)
        await _wait_for_ready(conn, srv)
        replayed = await srv.read_client_frame(timeout=1.0)
        assert replayed["id"] == "queued-1"
    finally:
        await conn.stop()


async def test_ready_send_failure_requeues_for_next_connection(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    await conn.start()
    try:
        await _wait_for_ready(conn, srv)

        async def broken_send(_text: str):
            raise ConnectionError("socket going away")

        monkeypatch.setattr(conn._ws, "send", broken_send)

        with pytest.raises(ConnectionError):
            await conn.send_frame({"type": "event", "id": "direct-1", "event": "message.reply"})

        await srv.disconnect()
        await _wait_until(lambda: len(srv.connect_calls) >= 2, timeout=0.3)
        await _wait_for_ready(conn, srv)
        replayed = await srv.read_client_frame(timeout=1.0)
        assert replayed["id"] == "direct-1"
    finally:
        await conn.stop()


async def test_backoff_progresses_for_repeated_connect_failures(monkeypatch):
    srv = FakeClawChatServer()
    srv.set_auto_fail(True)
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.random.uniform", lambda _a, _b: 0.0)

    real_sleep = asyncio.sleep
    sleep_calls = []

    async def recording_sleep(secs: float):
        sleep_calls.append(secs)
        await real_sleep(0)

    monkeypatch.setattr("clawchat_gateway.connection.asyncio.sleep", recording_sleep)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(reconnect_max_retries=2), on_message=on_message)
    await conn.start()
    try:
        await real_sleep(0.05)
    finally:
        await conn.stop()

    assert len(srv.connect_calls) >= 3
    assert sleep_calls[:2] == [0.01, 0.02]


async def test_reconnect_scheduled_uses_canonical_log(monkeypatch, caplog):
    srv = FakeClawChatServer()
    srv.set_auto_fail(True)
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.random.uniform", lambda _a, _b: 0.0)

    real_sleep = asyncio.sleep

    async def recording_sleep(_secs: float):
        await real_sleep(0)

    monkeypatch.setattr("clawchat_gateway.connection.asyncio.sleep", recording_sleep)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(reconnect_max_retries=1), on_message=on_message)
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.connection"):
        await conn.start()
        try:
            await _wait_until(lambda: len(srv.connect_calls) >= 2, sleep=real_sleep)
        finally:
            await conn.stop()

    messages = [record.getMessage() for record in caplog.records]
    assert (
        "clawchat.ws event=reconnect_scheduled account_id=default attempt=1 "
        "reconnect_count=1 state=reconnecting action=wait delay_ms=10 "
        "max_delay_ms=40 reason=fake auto-fail"
    ) in messages


async def test_reconnect_backoff_reset_logs_after_stable_ready(monkeypatch, caplog):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.BACKOFF_RESET_AFTER_SECONDS", 0.0)

    async def on_message(_frame):
        pass

    conn = ClawChatConnection(_cfg(), on_message=on_message)
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.connection"):
        await conn.start()
        try:
            await _wait_for_ready(conn, srv)
            await srv.disconnect()
            await _wait_until(
                lambda: any(
                    record.getMessage()
                    == (
                        "clawchat.ws event=reconnect_backoff_reset account_id=default attempt=1 "
                        "reconnect_count=0 state=ready action=reset stable_ms=5000"
                    )
                    for record in caplog.records
                )
            )
        finally:
            await conn.stop()


async def test_backoff_progresses_for_flapping_ready_connections(monkeypatch):
    srv = FakeClawChatServer()
    monkeypatch.setattr("clawchat_gateway.connection._ws_connect", srv.connect)
    monkeypatch.setattr("clawchat_gateway.connection.random.uniform", lambda _a, _b: 0.0)

    real_sleep = asyncio.sleep
    sleep_calls = []
    seen_states = []

    async def recording_sleep(secs: float):
        sleep_calls.append(secs)
        await real_sleep(0)

    monkeypatch.setattr("clawchat_gateway.connection.asyncio.sleep", recording_sleep)

    async def on_message(_frame):
        pass

    async def on_state(state):
        seen_states.append(state)

    conn = ClawChatConnection(
        _cfg(reconnect_max_retries=2),
        on_message=on_message,
        on_state_change=on_state,
    )
    await conn.start()
    try:
        for ready_count in range(2):
            await _wait_until(lambda: len(srv.connect_calls) > ready_count, sleep=real_sleep)
            await _complete_handshake(srv)
            await _wait_until(
                lambda: seen_states.count(ConnectionState.READY) > ready_count,
                sleep=real_sleep,
            )
            await srv.disconnect()

        await _wait_until(lambda: len(sleep_calls) >= 2, sleep=real_sleep)
    finally:
        await conn.stop()

    assert sleep_calls[:2] == [0.01, 0.02]
