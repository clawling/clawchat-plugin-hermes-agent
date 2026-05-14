"""ClawChat WebSocket connection lifecycle."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
from collections import deque
from typing import Any, Awaitable, Callable

try:
    from websockets.asyncio.client import connect as _ws_connect_impl
except ImportError:  # pragma: no cover
    _ws_connect_impl = None  # type: ignore[assignment]

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.protocol import (
    build_connect_request,
    decode_frame,
    encode_frame,
    extract_nonce,
    is_hello_ok,
    new_frame_id,
)
from clawchat_gateway.device_id import get_device_id
from clawchat_gateway.ws_log import format_ws_log
from clawchat_gateway.ws_state import ReconnectTracker

logger = logging.getLogger("clawchat_gateway.connection")

HANDSHAKE_TIMEOUT_SECONDS = 10.0
SEND_QUEUE_MAX = 128
BACKOFF_RESET_AFTER_SECONDS = 5.0


async def _ws_connect(url: str, **kwargs: Any) -> Any:
    if _ws_connect_impl is None:
        raise RuntimeError("websockets library not available")
    return await _ws_connect_impl(url, **kwargs)


class ConnectionState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    READY = "ready"
    RECONNECTING = "reconnecting"
    AUTH_FAILED = "auth_failed"
    CLOSED = "closed"


OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
OnStateChange = Callable[[ConnectionState], Awaitable[None]]


class ClawChatConnection:
    def __init__(
        self,
        config: ClawChatConfig,
        *,
        on_message: OnMessage,
        on_state_change: OnStateChange | None = None,
        account_id: str = "default",
    ) -> None:
        self._cfg = config
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._account_id = account_id
        self._state = ConnectionState.DISCONNECTED
        self._ws: Any = None
        self._stopping = False
        self._auth_failed = False
        self._tracker = ReconnectTracker()
        self._attempt = 0
        self._reconnect_count = 0
        self._supervisor_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._hello_wait: asyncio.Future[bool] | None = None
        self._pending_connect_id: str | None = None
        self._send_queue: deque[str] = deque()
        self._flushing_send_queue = False

    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._stopping = False
        self._auth_failed = False
        self._supervisor_task = asyncio.create_task(
            self._supervisor(),
            name="clawchat-supervisor",
        )

    async def stop(self) -> None:
        self._stopping = True
        await self._set_state(ConnectionState.CLOSED)
        if self._read_task is not None:
            self._read_task.cancel()
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None

    async def send_frame(self, frame: dict[str, Any]) -> None:
        text = encode_frame(frame)
        event = str(frame.get("event") or frame.get("type") or "unknown")
        frame_id = str(frame.get("id") or frame.get("trace_id") or "")
        if (
            self._state == ConnectionState.READY
            and self._ws is not None
            and not self._send_queue
            and not self._flushing_send_queue
        ):
            try:
                logger.info(
                    "clawchat ws send event=%s id=%s bytes=%d",
                    event,
                    frame_id,
                    len(text),
                )
                await self._ws.send(text)
            except Exception:
                self._enqueue_text(text, front=True)
                logger.warning(
                    "clawchat ws send failed; queued event=%s id=%s queue_size=%d",
                    event,
                    frame_id,
                    len(self._send_queue),
                )
                raise
            return
        self._enqueue_text(text)
        logger.info(
            "clawchat ws queued event=%s id=%s state=%s queue_size=%d",
            event,
            frame_id,
            self._state.value,
            len(self._send_queue),
        )

    @property
    def is_ready(self) -> bool:
        return self._state == ConnectionState.READY

    async def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        if self._on_state_change is None:
            return
        try:
            await self._on_state_change(state)
        except Exception:  # noqa: BLE001
            logger.exception("on_state_change raised")

    async def _supervisor(self) -> None:
        delay_seconds = self._cfg.reconnect_initial_delay_ms / 1000.0
        max_delay_seconds = self._cfg.reconnect_max_delay_ms / 1000.0
        max_retries = self._cfg.reconnect_max_retries
        retries = 0
        reconnect_reason = "-"
        while not self._stopping:
            try:
                await self._set_state(ConnectionState.CONNECTING)
                stable_session = await self._run_one_connection()
                if stable_session:
                    delay_seconds = self._cfg.reconnect_initial_delay_ms / 1000.0
                    retries = 0
                    self._tracker.reset_reconnect_count()
                    snapshot = self._tracker.snapshot()
                    self._attempt = snapshot.attempt
                    self._reconnect_count = snapshot.reconnect_count
                    logger.info(
                        format_ws_log(
                            event="reconnect_backoff_reset",
                            account_id=self._account_id,
                            attempt=snapshot.attempt,
                            reconnect_count=snapshot.reconnect_count,
                            state=ConnectionState.READY.value,
                            action="reset",
                            fields=[("stable_ms", 5000)],
                        )
                    )
                reconnect_reason = "-"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reconnect_reason = str(exc) or type(exc).__name__
                logger.warning(
                    format_ws_log(
                        event="connection_lost",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=self._state.value,
                        action="reconnect",
                        fields=[
                            ("code", "-"),
                            ("reason", reconnect_reason),
                        ],
                    )
                )
            if self._stopping:
                break
            retries += 1
            if retries > max_retries:
                break
            await self._set_state(ConnectionState.RECONNECTING)
            jitter = random.uniform(0.0, delay_seconds * self._cfg.reconnect_jitter_ratio)
            delay_with_jitter = delay_seconds + jitter
            self._tracker.mark_reconnect_scheduled()
            next_reconnect_count = self._tracker.snapshot().reconnect_count + 1
            logger.info(
                format_ws_log(
                    event="reconnect_scheduled",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=next_reconnect_count,
                    state=ConnectionState.RECONNECTING.value,
                    action="wait",
                    fields=[
                        ("delay_ms", int(delay_with_jitter * 1000)),
                        ("max_delay_ms", self._cfg.reconnect_max_delay_ms),
                        ("reason", reconnect_reason),
                    ],
                )
            )
            await asyncio.sleep(delay_with_jitter)
            delay_seconds = min(delay_seconds * 2.0, max_delay_seconds)
        await self._set_state(ConnectionState.CLOSED)

    async def _run_one_connection(self) -> bool:
        attempt, reconnect_count = self._tracker.next_connect()
        self._attempt = attempt
        self._reconnect_count = reconnect_count
        logger.info(
            format_ws_log(
                event="connect_start",
                account_id=self._account_id,
                attempt=attempt,
                reconnect_count=reconnect_count,
                state=ConnectionState.CONNECTING.value,
                action="connect",
                fields=[
                    ("url", self._cfg.websocket_url),
                    ("queue_size", len(self._send_queue)),
                ],
            )
        )
        loop = asyncio.get_running_loop()
        handshake_started_at = loop.time()
        ws = await _ws_connect(
            self._cfg.websocket_url,
            additional_headers={
                "Authorization": f"Bearer {self._cfg.token}",
                "X-Device-Id": get_device_id(),
            },
            subprotocols=["clawchat.v1", f"bearer.{self._cfg.token}"],
            ping_interval=self._cfg.heartbeat_interval_ms / 1000.0,
            ping_timeout=self._cfg.heartbeat_timeout_ms / 1000.0,
        )
        self._ws = ws
        self._pending_connect_id = None
        ready_started_at: float | None = None
        await self._set_state(ConnectionState.HANDSHAKING)

        self._hello_wait = loop.create_future()
        self._read_task = asyncio.create_task(self._read_loop(ws), name="clawchat-read")
        try:
            hello_ok = await asyncio.wait_for(
                self._hello_wait,
                timeout=HANDSHAKE_TIMEOUT_SECONDS,
            )
            if not hello_ok or self._auth_failed:
                return False
            await self._set_state(ConnectionState.READY)
            elapsed_ms = int((loop.time() - handshake_started_at) * 1000)
            logger.info(
                format_ws_log(
                    event="handshake_ok",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="flush_queue",
                    fields=[
                        ("trace_id", self._pending_connect_id),
                        ("elapsed_ms", elapsed_ms),
                        ("queue_size", len(self._send_queue)),
                    ],
                )
            )
            ready_started_at = loop.time()
            await self._flush_send_queue(ws)
            await self._read_task
        finally:
            if self._read_task is not None and not self._read_task.done():
                self._read_task.cancel()
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if not self._stopping and not self._auth_failed:
            logger.info(
                format_ws_log(
                    event="connection_lost",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reconnect",
                    fields=[
                        ("code", "-"),
                        ("reason", "-"),
                    ],
                )
            )
        if ready_started_at is None:
            return False
        return (loop.time() - ready_started_at) >= BACKOFF_RESET_AFTER_SECONDS

    async def _flush_send_queue(self, ws: Any) -> None:
        self._flushing_send_queue = True
        try:
            while self._send_queue:
                text = self._send_queue[0]
                try:
                    frame = decode_frame(text)
                    event = str(frame.get("event") or frame.get("type") or "unknown")
                    frame_id = str(frame.get("id") or frame.get("trace_id") or "")
                except (TypeError, ValueError):
                    event = "malformed"
                    frame_id = ""
                logger.info(
                    "clawchat ws flush queued event=%s id=%s remaining=%d",
                    event,
                    frame_id,
                    len(self._send_queue),
                )
                await ws.send(text)
                self._send_queue.popleft()
        finally:
            self._flushing_send_queue = False

    async def _read_loop(self, ws: Any) -> None:
        async for raw in ws:
            try:
                frame = decode_frame(raw)
            except (TypeError, ValueError) as exc:
                logger.warning("clawchat dropped malformed frame: %s", exc)
                continue
            logger.info(
                "clawchat ws recv event=%s type=%s id=%s state=%s bytes=%d",
                frame.get("event"),
                frame.get("type"),
                frame.get("id") or frame.get("trace_id"),
                self._state.value,
                len(raw) if isinstance(raw, (str, bytes, bytearray)) else 0,
            )
            await self._dispatch_inbound(frame)

    async def _dispatch_inbound(self, frame: dict[str, Any]) -> None:
        ftype = frame.get("type")
        if (
            self._state == ConnectionState.HANDSHAKING
            and ftype in (None, "event")
            and frame.get("event") == "connect.challenge"
        ):
            await self._handle_challenge(frame)
            return
        if (
            self._state == ConnectionState.HANDSHAKING
            and (ftype == "res" or frame.get("event") in {"hello-ok", "hello-fail"})
            and self._hello_wait is not None
            and not self._hello_wait.done()
        ):
            await self._maybe_finish_handshake(frame)
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") in {"message.send", "message.reply", "interaction.submit"}:
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            fragments = message.get("fragments") if isinstance(message.get("fragments"), list) else []
            body = message.get("body")
            body_keys = sorted(body.keys()) if isinstance(body, dict) else []
            body_len = len(body) if isinstance(body, (str, list, dict)) else 0
            inbound_msg_id = (
                payload.get("message_id")
                or message.get("message_id")
                or message.get("id")
            )
            logger.info(
                "clawchat ws dispatch %s chat_id=%s sender_id=%s message_id=%s trace_id=%s fragments=%d payload_keys=%s message_keys=%s body_type=%s body_keys=%s body_len=%d",
                frame.get("event"),
                frame.get("chat_id"),
                (frame.get("sender") or {}).get("id") if isinstance(frame.get("sender"), dict) else None,
                inbound_msg_id,
                frame.get("trace_id"),
                len(fragments),
                sorted(payload.keys()),
                sorted(message.keys()),
                type(body).__name__,
                body_keys,
                body_len,
            )
            await self._on_message(frame)
            return
        logger.info(
            "clawchat ws ignored event=%s type=%s state=%s",
            frame.get("event"),
            ftype,
            self._state.value,
        )

    async def _handle_challenge(self, frame: dict[str, Any]) -> None:
        nonce = extract_nonce(frame)
        if not nonce:
            logger.warning("clawchat ws challenge missing nonce")
            return
        req_id = new_frame_id("trace")
        self._pending_connect_id = req_id
        logger.info(
            format_ws_log(
                event="challenge_received",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=ConnectionState.HANDSHAKING.value,
                action="send_connect",
                fields=[
                    ("challenge_trace_id", frame.get("trace_id")),
                    ("has_nonce", bool(nonce)),
                ],
            )
        )
        connect_req = build_connect_request(
            frame_id=req_id,
            token=self._cfg.token,
            nonce=nonce,
            device_id=get_device_id(),
            capabilities={"multi_device": True, "device_replay": True},
        )
        await self._ws.send(encode_frame(connect_req))
        logger.info(
            format_ws_log(
                event="connect_sent",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=ConnectionState.HANDSHAKING.value,
                action="await_hello",
                fields=[
                    ("trace_id", req_id),
                    ("device_id", get_device_id()),
                ],
            )
        )

    async def _maybe_finish_handshake(self, frame: dict[str, Any]) -> None:
        if self._pending_connect_id and is_hello_ok(frame, self._pending_connect_id):
            if self._hello_wait is not None and not self._hello_wait.done():
                self._hello_wait.set_result(True)
            return
        if (
            self._pending_connect_id
            and frame.get("event") == "hello-fail"
            and frame.get("trace_id") == self._pending_connect_id
        ):
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
            self._auth_failed = True
            self._stopping = True
            await self._set_state(ConnectionState.AUTH_FAILED)
            logger.info(
                format_ws_log(
                    event="auth_failed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.AUTH_FAILED.value,
                    action="stop_reconnect",
                    fields=[
                        ("trace_id", frame.get("trace_id")),
                        ("reason", reason),
                    ],
                )
            )
            if self._hello_wait is not None and not self._hello_wait.done():
                self._hello_wait.set_result(False)
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
            return
        logger.warning(
            "clawchat ws handshake response ignored event=%s trace_id=%s pending_id=%s",
            frame.get("event"),
            frame.get("trace_id"),
            self._pending_connect_id,
        )

    def _enqueue_text(self, text: str, *, front: bool = False) -> None:
        if len(self._send_queue) >= SEND_QUEUE_MAX:
            logger.warning("clawchat send queue full, dropping oldest frame")
            if front:
                self._send_queue.pop()
            else:
                self._send_queue.popleft()
        if front:
            self._send_queue.appendleft(text)
        else:
            self._send_queue.append(text)
