"""ClawChat WebSocket connection lifecycle."""

from __future__ import annotations

import asyncio
import enum
import logging
import os
import random
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, ClassVar

try:
    from websockets.asyncio.client import connect as _ws_connect_impl
except ImportError:  # pragma: no cover
    _ws_connect_impl = None  # type: ignore[assignment]

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.token_refresh import (
    RefreshManager,
    RefreshOutcome,
    is_token_near_expiry,
)
from clawchat_gateway.protocol import (
    build_connect_request,
    build_offline_ack_event,
    build_pong_event,
    decode_frame,
    encode_frame,
    extract_nonce,
    is_hello_ok,
    new_frame_id,
)
from clawchat_gateway.config import _jwt_claim
from clawchat_gateway.device_id import get_device_id, warn_if_device_id_unpinned
from clawchat_gateway.storage import get_clawchat_store
from clawchat_gateway.notify_signal import NotifySignalObserver
from clawchat_gateway.ws_log import format_ws_log
from clawchat_gateway.ws_state import ReconnectTracker

logger = logging.getLogger("clawchat_gateway.connection")

HANDSHAKE_TIMEOUT_SECONDS = 10.0
# Upper bound on the graceful ``ws.close()` in ``stop()``. A half-dead socket can
# make ``close()`` block until the library's own close handshake gives up; bounding
# it keeps supersession (``start`` -> prior ``stop``) and the Hermes watcher's ~5s
# disconnect budget responsive, so a slow close never leaves an orphan behind.
_WS_CLOSE_TIMEOUT_SECONDS = 5.0
SEND_QUEUE_MAX = 128
BACKOFF_RESET_AFTER_SECONDS = 5.0
ACKABLE_EVENTS = {"message.send", "message.reply"}
ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS = 2.0

# Protocol v2 §14.1: a `hello-fail` whose reason signals that the upstream auth
# backend (member-backend) is *unavailable* (upstream 5xx / timeout) means the
# token may still be valid — the auth service is down. The mandated response is
# to backoff-reconnect with the SAME token, NOT to discard credentials or
# trigger a refresh (a 5xx storm must not become a mass token-refresh storm).
# Every other reason (nonce mismatch, authentication failed, invalid connect)
# stays terminal per §3.5.
_TRANSIENT_AUTH_FAILURE_MARKERS = (
    "remote auth service unavailable",
    "auth service unavailable",
    "service unavailable",
    "temporarily unavailable",
)


def _is_transient_auth_failure(reason: str | None) -> bool:
    if not reason:
        return False
    text = reason.lower()
    return any(marker in text for marker in _TRANSIENT_AUTH_FAILURE_MARKERS)


# §A.2: a `hello-fail` reason that names a genuine *token* rejection (the access
# token is bad / expired) — distinct from the transient auth-backend-unavailable
# markers above. On these, attempt a single-flight refresh.
_TOKEN_REJECTED_MARKERS = (
    "authentication failed",
    "invalid token",
    "token expired",
    "expired token",
    "unauthorized",
)


def _is_token_rejected(reason: str | None) -> bool:
    if not reason:
        return False
    text = reason.lower()
    return any(marker in text for marker in _TOKEN_REJECTED_MARKERS)


# §C.1 user-visible auto-logout message. MUST be kept identical across both
# plugins (Hermes + OpenClaw).
AUTO_LOGOUT_STATUS_MESSAGE = (
    "ClawChat token expired and could not be refreshed. "
    "Re-pair with `/clawchat-activate <code>`."
)
AUTO_LOGOUT_LAST_ERROR = "token expired — re-pair required"


@dataclass
class _QueuedFrame:
    text: str
    event_name: str
    trace_id: str
    chat_id: str
    expected_message_id: str | None = None
    ack_future: asyncio.Future[dict[str, Any]] | None = None
    ack_timeout_task: asyncio.Task[None] | None = None


@dataclass
class _PendingAck:
    event_name: str
    trace_id: str
    chat_id: str
    expected_message_id: str | None
    future: asyncio.Future[dict[str, Any]]
    timeout_task: asyncio.Task[None]


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
OnSignal = Callable[[dict[str, Any]], Awaitable[None]]
# Called once on permanent refresh failure (auto-logout) so the adapter can emit
# the user-visible status/chat message (§C.1). Receives the human-readable
# message text.
OnAuthLogout = Callable[[str], Awaitable[None]]


class ClawChatConnection:
    # Process-wide guard: at most one live supervisor per ``account_id``. The Hermes
    # reconnect watcher builds a FRESH adapter (=> new ``ClawChatConnection``) on every
    # retry and only best-effort disconnects the old one (a ~5s budget it abandons on
    # timeout; a ``wait_for``-cancelled ``connect()`` never disconnects at all). A
    # leaked supervisor keeps reconnecting with the SAME ``device_id``, so msghub
    # mutually kicks the live connection in an endless reconnect storm. Superseding the
    # prior supervisor when a fresh one starts makes such duplicates impossible.
    _live_supervisors: ClassVar[dict[str, "ClawChatConnection"]] = {}

    def __init__(
        self,
        config: ClawChatConfig,
        *,
        on_message: OnMessage,
        on_state_change: OnStateChange | None = None,
        on_signal: OnSignal | None = None,
        on_auth_logout: OnAuthLogout | None = None,
        account_id: str = "default",
    ) -> None:
        self._cfg = config
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._on_signal = on_signal
        self._on_auth_logout = on_auth_logout
        self._account_id = account_id
        self._state = ConnectionState.DISCONNECTED
        self._ws: Any = None
        self._stopping = False
        self._auth_failed = False
        self._tracker = ReconnectTracker()
        self._attempt = 0
        self._reconnect_count = 0
        self._notify_signal_observer = NotifySignalObserver()
        try:
            self._store = get_clawchat_store()
        except Exception:  # noqa: BLE001
            self._store = None
            logger.warning("clawchat connection database unavailable")
        self._connection_row_id: int | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._credential_watch_task: asyncio.Task[None] | None = None
        self._hello_wait: asyncio.Future[bool] | None = None
        self._ready_event = asyncio.Event()
        self._pending_connect_id: str | None = None
        self._hello_resolved_device_id: str | None = None
        self._hello_delivery_mode: str | None = None
        self._send_queue: deque[_QueuedFrame] = deque()
        self._flushing_send_queue = False
        self._pending_acks: dict[str, _PendingAck] = {}
        self._stable_ready_handle: asyncio.TimerHandle | None = None
        self._stable_ready_reset_done = False
        self._activation_wait_logged = False
        self._using_activation_db_credentials = False
        self._rejected_activation_token: str | None = None
        # Token refresh (token-refresh spec §A/§B/§C). The manager is built on the
        # supervisor's running loop in ``start`` so its asyncio primitives bind to
        # the right loop; ``_activated_at_ms`` feeds the exp fallback (§A.0).
        self._refresh_manager: RefreshManager | None = None
        self._activated_at_ms: int | None = None
        # Latched True after persist succeeds and the in-memory token is swapped,
        # so the supervisor reconnects immediately with the fresh token (§D).
        self._refresh_pending_reconnect = False

    @property
    def config(self) -> ClawChatConfig:
        return self._cfg

    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        # Supersede any orphaned supervisor for the same account before starting
        # ours, so only one WS session per account is ever live in this process.
        prior = ClawChatConnection._live_supervisors.get(self._account_id)
        if prior is not None and prior is not self:
            logger.info(
                format_ws_log(
                    event="supervisor_superseded",
                    account_id=self._account_id,
                    attempt=prior._attempt,
                    reconnect_count=prior._reconnect_count,
                    state=prior._state.value,
                    action="stop",
                    fields=[("reason", "replaced by fresh connection")],
                )
            )
            await prior.stop()
        self._stopping = False
        self._auth_failed = False
        warn_if_device_id_unpinned()
        if self._refresh_manager is None:
            self._refresh_manager = self._build_refresh_manager()
        self._supervisor_task = asyncio.create_task(
            self._supervisor(),
            name="clawchat-supervisor",
        )
        ClawChatConnection._live_supervisors[self._account_id] = self

    def _build_refresh_manager(self) -> RefreshManager:
        return RefreshManager(
            build_client=self._build_refresh_client,
            persist_tokens=self._persist_rotated_tokens,
            persist_logout=self._persist_auth_logout,
            device_id_provider=self._refresh_device_id,
        )

    def _build_refresh_client(self) -> Any:
        from clawchat_gateway.api_client import ClawChatApiClient

        base_url = self._cfg.base_url or self._cfg.websocket_url
        return ClawChatApiClient(
            base_url=base_url,
            token="",  # /v1/auth/refresh is unauthenticated; token unused.
            user_id=self._cfg.user_id,
            device_id=self._refresh_device_id(),
        )

    def _refresh_device_id(self) -> str:
        """Connect-time device id used as ``X-Device-Id`` on connect AND refresh (§E).

        Resolution order:

        1. The value persisted on the activations row (the exact id a connect-code
           activation presented at connect).
        2. The ``did`` claim of the current access token. Env-booted deployments
           have no activations row, but the token carries the device id the
           backend baked at login — the EXACT id ``/v1/auth/refresh`` expects as
           ``X-Device-Id``. Using it avoids a 10003 device-mismatch (forced
           re-login) when the local host fingerprint has drifted, e.g. the
           container was recreated and ``CLAWCHAT_DEVICE_ID`` is not pinned. It
           also equals a pinned ``CLAWCHAT_DEVICE_ID`` (that pin was the did at
           login time).
        3. The deterministic ``get_device_id()`` fingerprint — only for a truly
           unpaired process with no stored row and no token yet.
        """
        if self._store is not None:
            try:
                credentials = self._store.get_activation_credentials(
                    platform="hermes",
                    account_id=self._account_id,
                )
            except Exception:  # noqa: BLE001
                credentials = None
            stored = getattr(credentials, "device_id", None) if credentials else None
            if stored:
                return stored
        token_did = _jwt_claim(self._cfg.token, "did")
        if token_did:
            return token_did
        return get_device_id()

    def session_device_id(self) -> str:
        """Public accessor for the resolved connect-time device id (§E).

        The plugin-version report must key on the SAME device id the WS session
        and refresh use, so the report row links to the device the backend tracks
        (rather than a volatile ``get_device_id()`` fingerprint).
        """
        return self._refresh_device_id()

    def _refresh_seed_conversation_id(self) -> str | None:
        """Conversation id used to seed an env-only activations row (§C.2).

        Prefer the conversation id already persisted on the activations row;
        fall back to the ``CLAWCHAT_HOME_CHANNEL`` env var an env-booted process
        was configured with. May be ``None`` (the column is then left empty,
        which is fine — env deployments derive the home channel from env vars).
        """
        if self._store is not None:
            try:
                stored = self._store.get_activation_conversation(
                    platform="hermes",
                    account_id=self._account_id,
                )
            except Exception:  # noqa: BLE001
                stored = None
            if stored:
                return stored
        env_home = os.environ.get("CLAWCHAT_HOME_CHANNEL")
        return env_home.strip() if isinstance(env_home, str) and env_home.strip() else None

    async def _persist_rotated_tokens(self, access_token: str, refresh_token: str) -> bool:
        from clawchat_gateway.activate import persist_rotated_tokens

        # §C.2: thread the in-memory identity down so an env-only deployment (no
        # activations row yet) gets its row SEEDED on the first refresh instead of
        # failing the db write and bricking the agent on first rotation.
        user_id = self._cfg.user_id or None
        owner_user_id = self._cfg.owner_user_id or None
        conversation_id = self._refresh_seed_conversation_id()

        def _write() -> bool:
            return persist_rotated_tokens(
                access_token=access_token,
                refresh_token=refresh_token,
                device_id=self._refresh_device_id(),
                account_id=self._account_id,
                user_id=user_id,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
            )

        try:
            return await asyncio.to_thread(_write)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat rotated-token persistence raised", exc_info=True)
            return False

    async def _persist_auth_logout(self, reason: str) -> None:
        from clawchat_gateway.activate import clear_persisted_credentials

        def _clear() -> None:
            clear_persisted_credentials(account_id=self._account_id)

        try:
            await asyncio.to_thread(_clear)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat logout persistence raised", exc_info=True)
        # Surface the user-visible notification (§C.1) in addition to logs.
        if self._on_auth_logout is not None:
            try:
                await self._on_auth_logout(AUTO_LOGOUT_STATUS_MESSAGE)
            except Exception:  # noqa: BLE001
                logger.warning("clawchat auth-logout notification failed", exc_info=True)

    async def stop(self) -> None:
        self._stopping = True
        self._cancel_stable_ready_reset()
        await self._set_state(ConnectionState.CLOSED)
        self._reject_pending_acks(RuntimeError("connection stopped"))
        self._reject_queued_ack_waiters(RuntimeError("connection stopped"), clear_queue=True)
        if self._read_task is not None:
            self._read_task.cancel()
        if self._credential_watch_task is not None:
            self._credential_watch_task.cancel()
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.cancel()
        if self._ws is not None:
            try:
                await asyncio.wait_for(
                    self._ws.close(), timeout=_WS_CLOSE_TIMEOUT_SECONDS
                )
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                # A half-dead socket may never complete the close handshake; don't
                # let it stall supersession or the host's disconnect budget.
                pass
        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._supervisor_task = None
        # Release our slot only if a newer ``start`` hasn't already claimed it.
        if ClawChatConnection._live_supervisors.get(self._account_id) is self:
            del ClawChatConnection._live_supervisors[self._account_id]

    async def send_frame(
        self,
        frame: dict[str, Any],
        *,
        wait_for_ack: bool = False,
        queue_when_unready: bool = True,
    ) -> bool:
        text = encode_frame(frame)
        queued = self._queued_frame(frame, text, wait_for_ack=wait_for_ack)
        if self._stopping or self._state == ConnectionState.CLOSED:
            self._log_send_dropped(queued, reason="stopped")
            return False
        if (
            self._state == ConnectionState.READY
            and self._ws is not None
            and not self._send_queue
            and not self._flushing_send_queue
        ):
            try:
                logger.info(
                    format_ws_log(
                        event="send_flush",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=ConnectionState.READY.value,
                        action="send",
                        fields=[
                            ("event_name", queued.event_name),
                            ("trace_id", queued.trace_id),
                            ("chat_id", queued.chat_id),
                            ("remaining", 0),
                        ],
                    )
                )
                await self._ws.send(text)
                self._start_ack_timer_if_needed(queued)
            except Exception:
                if not queue_when_unready:
                    self._log_send_dropped(queued, reason="send_failed")
                    return False
                self._enqueue_frame(queued, front=True, log_queued=False)
                self._log_send_failed(queued)
                raise
            if queued.ack_future is not None:
                await queued.ack_future
            return True
        if not queue_when_unready:
            self._log_send_dropped(queued, reason="not_ready")
            return False
        self._enqueue_frame(queued)
        if queued.ack_future is not None:
            await queued.ack_future
        return True

    @property
    def is_ready(self) -> bool:
        return self._state == ConnectionState.READY

    async def wait_until_ready(self, *, timeout: float) -> bool:
        if self.is_ready:
            return True
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.is_ready

    async def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        if state == ConnectionState.READY:
            self._ready_event.set()
        else:
            self._ready_event.clear()
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
        startup_refresh_done = False
        while not self._stopping:
            if not self._has_connect_credentials():
                loaded = await self._wait_for_activation_credentials()
                if not loaded:
                    break
                # _wait_for_activation_credentials already applied §A.4.
                startup_refresh_done = True
            elif not startup_refresh_done:
                # §A.4 env-credential path: refresh-if-near-expiry before the
                # FIRST connect (SQLite path is handled in wait-for-activation).
                startup_refresh_done = True
                if (
                    self._refresh_manager is not None
                    and self._cfg.refresh_token
                    and is_token_near_expiry(self._cfg.token, self._activated_at_ms)
                ):
                    outcome = await self._attempt_refresh(close_ws_on_success=False)
                    if outcome.status == "permanent":
                        # Auto-logout already done; drop to wait-for-activation.
                        self._refresh_pending_reconnect = False
                        continue
                    self._refresh_pending_reconnect = False
            try:
                await self._set_state(ConnectionState.CONNECTING)
                await self._run_one_connection()
                if self._stable_ready_reset_done or self._refresh_pending_reconnect:
                    # §D: after a successful refresh closed the WS, reconnect
                    # immediately with the new token (reset backoff) even if the
                    # socket did not stay READY long enough for the stable-ready
                    # reset — a refresh close is a planned swap, not a fault.
                    delay_seconds = self._cfg.reconnect_initial_delay_ms / 1000.0
                    retries = 0
                self._refresh_pending_reconnect = False
                reconnect_reason = "-"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reconnect_reason = self._safe_error_text(exc)
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

    def _has_connect_credentials(self) -> bool:
        return bool(
            self._cfg.websocket_url
            and self._cfg.token
            and self._cfg.user_id
            and self._cfg.owner_user_id
        )

    async def _wait_for_activation_credentials(self) -> bool:
        if not self._activation_wait_logged:
            logger.info(
                format_ws_log(
                    event="activation_wait",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="wait",
                    fields=[
                        ("has_websocket_url", bool(self._cfg.websocket_url)),
                        ("has_token", bool(self._cfg.token)),
                        ("has_user_id", bool(self._cfg.user_id)),
                        ("has_owner_user_id", bool(self._cfg.owner_user_id)),
                    ],
                )
            )
            self._activation_wait_logged = True
        while not self._stopping:
            credentials = None
            if self._store is not None:
                try:
                    credentials = self._store.get_activation_credentials(
                        platform="hermes",
                        account_id=self._account_id,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("clawchat activation credential read failed", exc_info=True)
            if credentials is not None:
                if credentials.access_token == self._rejected_activation_token:
                    logger.debug(
                        format_ws_log(
                            event="activation_poll",
                            account_id=self._account_id,
                            attempt=self._attempt,
                            reconnect_count=self._reconnect_count,
                            state=self._state.value,
                            action="wait",
                            fields=[
                                ("status", "rejected_token_unchanged"),
                                ("interval_seconds", ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS),
                            ],
                        )
                    )
                    await asyncio.sleep(ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS)
                    continue
                self._cfg = replace(
                    self._cfg,
                    token=credentials.access_token,
                    refresh_token=credentials.refresh_token or "",
                    user_id=credentials.user_id,
                    owner_user_id=credentials.owner_user_id,
                )
                self._using_activation_db_credentials = True
                self._rejected_activation_token = None
                self._activation_wait_logged = False
                self._activated_at_ms = credentials.activated_at
                if self._refresh_manager is not None:
                    self._refresh_manager.reset_latch()
                logger.info(
                    format_ws_log(
                        event="activation_loaded",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=self._state.value,
                        action="connect",
                        fields=[
                            ("has_refresh_token", bool(credentials.refresh_token)),
                        ],
                    )
                )
                # §A.4 startup refresh-if-near-expiry: before the first connect,
                # if the stored access token is past/within the proactive margin
                # and a refresh token exists, refresh synchronously and connect
                # with the fresh token (recovers a long-stopped pod, no re-pair).
                # PERMANENT → auto-logout immediately and keep waiting (skip the
                # doomed connect).
                if (
                    self._refresh_manager is not None
                    and self._cfg.refresh_token
                    and is_token_near_expiry(self._cfg.token, self._activated_at_ms)
                ):
                    outcome = await self._attempt_refresh(close_ws_on_success=False)
                    if outcome.status == "permanent":
                        # Creds cleared + user notified; keep polling for re-pair.
                        self._refresh_pending_reconnect = False
                        continue
                    # success/transient/skipped: connect with whatever token we
                    # now hold (success swapped it; transient keeps the old one).
                    self._refresh_pending_reconnect = False
                return True
            logger.debug(
                format_ws_log(
                    event="activation_poll",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="wait",
                    fields=[
                        ("status", "waiting"),
                        ("has_store", self._store is not None),
                        ("interval_seconds", ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS),
                    ],
                )
            )
            await asyncio.sleep(ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS)
        return False

    async def _watch_activation_credentials(self) -> None:
        while not self._stopping:
            await asyncio.sleep(ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS)
            if self._state != ConnectionState.READY:
                continue
            # §A.1 proactive refresh: when the live token nears expiry, refresh
            # and reconnect with the new token. On success this closes the WS,
            # which ends this watch task (re-armed on the next READY).
            try:
                await self._maybe_proactive_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("clawchat proactive refresh failed", exc_info=True)
            if self._state != ConnectionState.READY:
                # A proactive refresh closed the socket; stop watching.
                return
            if self._store is None:
                continue
            try:
                credentials = self._store.get_activation_credentials(
                    platform="hermes",
                    account_id=self._account_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning("clawchat activation credential watch failed", exc_info=True)
                continue
            if credentials is None:
                continue
            changed = (
                credentials.access_token != self._cfg.token
                or credentials.user_id != self._cfg.user_id
                or credentials.owner_user_id != self._cfg.owner_user_id
            )
            if not changed:
                continue
            logger.info(
                format_ws_log(
                    event="activation_credentials_changed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reconnect",
                    fields=[
                        ("has_refresh_token", bool(credentials.refresh_token)),
                    ],
                )
            )
            self._cfg = replace(
                self._cfg,
                token="",
                refresh_token="",
                user_id="",
                owner_user_id="",
            )
            self._using_activation_db_credentials = False
            self._rejected_activation_token = None
            if self._ws is not None:
                await self._ws.close()
            return

    async def reactive_refresh(self) -> RefreshOutcome:
        """§A.2.1: run one single-flight refresh for a REST 401/403 retry.

        Shares the same single-flight ``RefreshManager`` as proactive/hello-fail
        refreshes (so concurrent REST 401s + a proactive timer coalesce into one
        HTTP refresh). On success the in-memory token is swapped (the caller can
        rebuild its api client from ``self.config.token`` and retry); on permanent
        the manager already cleared creds + emitted the user message and the
        connection drops to wait-for-activation. The WS is NOT force-closed here
        — a REST-path refresh leaves an otherwise-healthy socket alone; a parallel
        proactive/hello-fail path handles WS continuation (§D).
        """
        return await self._attempt_refresh(close_ws_on_success=False)

    async def _attempt_refresh(self, *, close_ws_on_success: bool) -> RefreshOutcome:
        """Run one single-flight token refresh and apply its result.

        On success: the manager has already persisted to BOTH stores (§0). Swap
        the in-memory token AFTER persistence, mark the SQLite-credentials path
        (§C.2), and — when ``close_ws_on_success`` — close the live socket so the
        supervisor reconnects with the new token in a fresh ``connect`` (§D).
        On permanent failure: the manager cleared creds + emitted the user
        message; here we drop the in-memory creds and latch the rejected token so
        the supervisor stops opening sockets with the dead token.
        """
        manager = self._refresh_manager
        if manager is None:
            return RefreshOutcome(status="skipped", error="no refresh manager")
        token = self._cfg.token
        refresh_token = self._cfg.refresh_token or None
        outcome = await manager.refresh(access_token=token, refresh_token=refresh_token)
        if outcome.status == "success":
            # Persist already succeeded (§0). Swap in-memory token only now.
            self._cfg = replace(
                self._cfg,
                token=outcome.access_token,
                refresh_token=outcome.refresh_token,
            )
            # §C.2: move the process onto the SQLite-credentials recovery path.
            self._using_activation_db_credentials = True
            self._rejected_activation_token = None
            self._refresh_pending_reconnect = True
            self._activated_at_ms = int(time.time() * 1000)
            logger.info(
                format_ws_log(
                    event="token_refreshed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reconnect",
                    fields=[("close_ws", close_ws_on_success)],
                )
            )
            if close_ws_on_success and self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
        elif outcome.status == "permanent":
            # §C: refresh token permanently invalid → auto-logout. Drop creds and
            # latch the dead token so wait-for-activation does not re-load it.
            self._rejected_activation_token = self._cfg.token or None
            self._cfg = replace(
                self._cfg,
                token="",
                refresh_token="",
                user_id="",
                owner_user_id="",
            )
            self._using_activation_db_credentials = False
            logger.warning(
                format_ws_log(
                    event="auth_logout",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="re_pair_required",
                    fields=[("reason", AUTO_LOGOUT_LAST_ERROR)],
                )
            )
        return outcome

    async def _maybe_proactive_refresh(self) -> None:
        """§A.1: when the live token nears expiry, refresh + reconnect."""
        if self._refresh_manager is None or not self._cfg.refresh_token:
            return
        if not is_token_near_expiry(self._cfg.token, self._activated_at_ms):
            return
        await self._attempt_refresh(close_ws_on_success=True)

    async def _run_one_connection(self) -> bool:
        attempt, reconnect_count = self._tracker.next_connect()
        self._attempt = attempt
        self._reconnect_count = reconnect_count
        self._connection_row_id = self._record_connection(
            "start_connection",
            platform="hermes",
            account_id=self._account_id,
            attempt=attempt,
            reconnect_count=reconnect_count,
        )
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
        try:
            ws = await _ws_connect(
                self._cfg.websocket_url,
                ping_interval=self._cfg.heartbeat_interval_ms / 1000.0,
                ping_timeout=self._cfg.heartbeat_timeout_ms / 1000.0,
            )
        except Exception as exc:
            self._finish_current_connection(
                ConnectionState.DISCONNECTED.value,
                error=self._safe_error_text(exc),
            )
            raise
        self._ws = ws
        self._pending_connect_id = None
        self._hello_resolved_device_id = None
        self._hello_delivery_mode = None
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
            self._record_connection(
                "mark_connection_ready",
                self._connection_row_id,
                resolved_device_id=self._hello_resolved_device_id,
                delivery_mode=self._hello_delivery_mode,
            )
            self._schedule_stable_ready_reset()
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
            await self._flush_send_queue(ws)
            self._credential_watch_task = asyncio.create_task(
                self._watch_activation_credentials(),
                name="clawchat-credential-watch",
            )
            await self._read_task
        finally:
            self._cancel_stable_ready_reset()
            credential_watch_task = self._credential_watch_task
            if credential_watch_task is not None:
                if not credential_watch_task.done():
                    credential_watch_task.cancel()
                try:
                    await credential_watch_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            self._credential_watch_task = None
            read_task = self._read_task
            if read_task is not None:
                if not read_task.done():
                    read_task.cancel()
                try:
                    await read_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
            self._read_task = None
            if not self._stopping and not self._auth_failed:
                await self._set_state(ConnectionState.DISCONNECTED)
                self._reject_pending_acks(RuntimeError("connection disconnected"))
            if self._auth_failed:
                self._finish_current_connection(ConnectionState.AUTH_FAILED.value)
            elif self._stopping:
                self._finish_current_connection(ConnectionState.CLOSED.value)
            else:
                self._finish_current_connection(ConnectionState.DISCONNECTED.value)
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
        return False

    async def _flush_send_queue(self, ws: Any) -> None:
        self._flushing_send_queue = True
        try:
            while self._send_queue:
                logger.info(
                    format_ws_log(
                        event="send_flush",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=ConnectionState.READY.value,
                        action="send",
                        fields=[
                            ("event_name", self._send_queue[0].event_name),
                            ("trace_id", self._send_queue[0].trace_id),
                            ("chat_id", self._send_queue[0].chat_id),
                            ("remaining", len(self._send_queue) - 1),
                        ],
                    )
                )
                queued = self._send_queue[0]
                try:
                    await ws.send(queued.text)
                    self._start_ack_timer_if_needed(queued)
                    self._send_queue.popleft()
                except Exception:
                    self._log_send_failed(queued)
                    raise
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
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "typing.update":
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="typing",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("chat_id", frame.get("chat_id")),
                    ],
                )
            )
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "chat.metadata.invalidated":
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="signal",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("chat_id", frame.get("chat_id")),
                    ],
                )
            )
            on_signal = getattr(self, "_on_signal", None)
            if on_signal is not None:
                await on_signal(frame)
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "notify.signal":
            # §9.4 reliable system notification. The plugin holds no friend/roster
            # cache (REST-on-demand), so observe + dedup only — no side effect. The
            # live frame and its reliable-inbox replay share an event_id and
            # collapse to one observation. Wire a reaction here if ever needed.
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            outcome = self._notify_signal_observer.observe(frame)
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="notify_signal",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("signal_type", payload.get("type")),
                        ("entity_id", payload.get("entity_id")),
                        ("event_id", payload.get("event_id")),
                        ("outcome", outcome),
                    ],
                )
            )
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "replay.done":
            # §11.5 terminal control frame: device replay drained, live begins.
            # Fires on every reconnect (even zero-backlog). Replayed messages are
            # processed inline, so this is a logged boundary marker, not a gate.
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="replay_done",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                    ],
                )
            )
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") in {"presence.snapshot", "presence.update"}:
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="presence",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("chat_id", frame.get("chat_id")),
                    ],
                )
            )
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") in {"message.send", "message.reply"}:
            sender = frame.get("sender") if isinstance(frame.get("sender"), dict) else {}
            logger.info(
                format_ws_log(
                    event="inbound_dispatch",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="dispatch",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("chat_id", frame.get("chat_id")),
                        ("sender_id", sender.get("id") if isinstance(sender, dict) else None),
                    ],
                )
            )
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            fragments = message.get("fragments") if isinstance(message.get("fragments"), list) else []
            body = message.get("body")
            body_keys = sorted(body.keys()) if isinstance(body, dict) else []
            body_len = len(body) if isinstance(body, (str, list, dict)) else 0
            inbound_msg_id = payload.get("message_id")
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
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "message.ack":
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="ack",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                    ],
                )
            )
            self._handle_ack(frame)
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "message.error":
            self._handle_message_error(frame)
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "ping":
            trace_id = frame.get("trace_id")
            logger.info(
                format_ws_log(
                    event="protocol_ping_received",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="send_pong",
                    fields=[("trace_id", trace_id)],
                )
            )
            if self._ws is not None:
                emitted_at = frame.get("emitted_at")
                if not isinstance(trace_id, str) or not trace_id:
                    return
                if type(emitted_at) is not int:
                    return
                await self._ws.send(encode_frame(build_pong_event(trace_id=trace_id, emitted_at=emitted_at)))
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "pong":
            logger.info(
                format_ws_log(
                    event="protocol_pong_received",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="ignore",
                    fields=[("trace_id", frame.get("trace_id") or frame.get("id"))],
                )
            )
            return
        if (
            self._state == ConnectionState.READY
            and ftype in (None, "event")
            and frame.get("event") in {"offline.batch", "offline.ack", "offline.done"}
        ):
            await self._handle_legacy_offline(frame)
            return
        if self._state == ConnectionState.READY and ftype in (None, "event"):
            logger.info(
                format_ws_log(
                    event="inbound_ignored",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="ignore",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                    ],
                )
            )
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
        device_id = self._refresh_device_id()
        # Persist the resolved device id (e.g. the token's `did` for env-booted
        # agents) onto the activations row so it survives container recreation
        # and is observable. Only-if-empty: never clobbers a connect-code value.
        if self._store is not None and device_id:
            try:
                self._store.set_activation_device_id(
                    platform="hermes",
                    account_id=self._account_id,
                    device_id=device_id,
                )
            except Exception:  # noqa: BLE001 — best-effort, must not block connect
                logger.debug("clawchat device id backfill failed")
        connect_req = build_connect_request(
            frame_id=req_id,
            token=self._cfg.token,
            nonce=nonce,
            device_id=device_id,
            capabilities={
                # Agent runtime is single-device: multi_device stays off so the
                # server never self-fans-out this connection's own messages.
                # notify_signals is advertised now that we handle the frame (§9.4).
                "multi_device": False,
                "device_replay": True,
                "chat_meta_events": True,
                "notify_signals": True,
            },
        )
        await self._ws.send(encode_frame(connect_req))
        self._record_connection(
            "mark_connect_sent",
            self._connection_row_id,
        )
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
                    ("device_id", device_id),
                ],
            )
        )

    async def _maybe_finish_handshake(self, frame: dict[str, Any]) -> None:
        if self._pending_connect_id and is_hello_ok(frame, self._pending_connect_id):
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            device_id = payload.get("device_id")
            delivery_mode = payload.get("delivery_mode")
            self._hello_resolved_device_id = device_id if isinstance(device_id, str) else None
            self._hello_delivery_mode = delivery_mode if isinstance(delivery_mode, str) else None
            await self._set_state(ConnectionState.READY)
            if self._hello_wait is not None and not self._hello_wait.done():
                self._hello_wait.set_result(True)
            return
        if frame.get("event") == "hello-fail":
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
            reason = self._sanitize_secret_text(reason)
            frame_trace_id = frame.get("trace_id")
            trace_id_match = bool(
                self._pending_connect_id and frame_trace_id == self._pending_connect_id
            )
            if _is_transient_auth_failure(reason):
                # §14.1 (5xx / auth backend unavailable): keep the token, do NOT
                # stop or discard credentials — let the supervisor backoff and
                # reconnect with the same token.
                logger.warning(
                    format_ws_log(
                        event="auth_unavailable",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=ConnectionState.HANDSHAKING.value,
                        action="backoff_reconnect",
                        fields=[
                            ("trace_id", frame_trace_id),
                            ("pending_id", self._pending_connect_id),
                            ("trace_id_match", trace_id_match),
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
            # §A.2 reactive refresh on hello-fail. Refresh ONLY on a genuine token
            # rejection, or on a generic/unattributed reason WHEN the local exp
            # shows the access token is actually at/near expiry (prevents a
            # refresh storm during a backend outage emitting a generic reason).
            token_rejected = _is_token_rejected(reason)
            near_expiry = is_token_near_expiry(self._cfg.token, self._activated_at_ms)
            refresh_eligible = bool(
                self._refresh_manager is not None
                and self._cfg.refresh_token
                and (token_rejected or near_expiry)
            )
            if refresh_eligible:
                logger.info(
                    format_ws_log(
                        event="hello_fail_refresh",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=ConnectionState.HANDSHAKING.value,
                        action="refresh",
                        fields=[
                            ("trace_id", frame_trace_id),
                            ("token_rejected", token_rejected),
                            ("near_expiry", near_expiry),
                            ("reason", reason),
                        ],
                    )
                )
                outcome = await self._attempt_refresh(close_ws_on_success=False)
                if outcome.status == "success":
                    # New token persisted + swapped; reconnect with it (§D). The
                    # server already closed this socket on hello-fail.
                    if self._hello_wait is not None and not self._hello_wait.done():
                        self._hello_wait.set_result(False)
                    if self._ws is not None:
                        try:
                            await self._ws.close()
                        except Exception:  # noqa: BLE001
                            pass
                    return
                if outcome.status != "permanent":
                    # Transient / skipped (min-interval, in-flight, no rotation):
                    # keep the current token and backoff-reconnect (§B/§D). Do NOT
                    # discard creds — the old refresh token is still valid.
                    logger.warning(
                        format_ws_log(
                            event="hello_fail_refresh_transient",
                            account_id=self._account_id,
                            attempt=self._attempt,
                            reconnect_count=self._reconnect_count,
                            state=ConnectionState.HANDSHAKING.value,
                            action="backoff_reconnect",
                            fields=[
                                ("status", outcome.status),
                                ("reason", outcome.error),
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
                # outcome.status == "permanent": _attempt_refresh already cleared
                # in-memory creds, latched the rejected token, persisted the
                # logout (both stores), and emitted the user message. Route to
                # wait-for-activation so a re-pair recovers WITHOUT a restart
                # (§C.2/§D): set AUTH_FAILED, finish the row, and let the
                # supervisor poll SQLite (now empty → waits for re-pair).
                await self._set_state(ConnectionState.AUTH_FAILED)
                self._finish_current_connection(
                    ConnectionState.AUTH_FAILED.value,
                    error=reason,
                )
                logger.info(
                    format_ws_log(
                        event="auth_logout",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=ConnectionState.AUTH_FAILED.value,
                        action="wait_activation",
                        fields=[
                            ("trace_id", frame_trace_id),
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
            using_activation_db_credentials = self._using_activation_db_credentials
            self._auth_failed = True
            if not using_activation_db_credentials:
                self._stopping = True
            await self._set_state(ConnectionState.AUTH_FAILED)
            self._finish_current_connection(
                ConnectionState.AUTH_FAILED.value,
                error=reason,
            )
            if using_activation_db_credentials:
                self._rejected_activation_token = self._cfg.token or None
                self._cfg = replace(
                    self._cfg,
                    token="",
                    refresh_token="",
                    user_id="",
                    owner_user_id="",
                )
                self._using_activation_db_credentials = False
                self._auth_failed = False
            logger.info(
                format_ws_log(
                    event="auth_failed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.AUTH_FAILED.value,
                    action=(
                        "wait_activation"
                        if using_activation_db_credentials
                        else "stop_reconnect"
                    ),
                    fields=[
                        ("trace_id", frame_trace_id),
                        ("pending_id", self._pending_connect_id),
                        ("trace_id_match", trace_id_match),
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

    async def _handle_legacy_offline(self, frame: dict[str, Any]) -> None:
        event_name = frame.get("event")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        logger.info(
            format_ws_log(
                event="inbound_control",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=ConnectionState.READY.value,
                action="legacy_offline",
                fields=[
                    ("event_name", event_name),
                    ("trace_id", frame.get("trace_id") or frame.get("id")),
                ],
            )
        )
        if event_name != "offline.batch":
            return
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("event") in {
                "message.send",
                "message.reply",
                "typing.update",
            }:
                await self._dispatch_inbound(item)
        batch_id = payload.get("batch_id")
        if isinstance(batch_id, int) and self._ws is not None:
            await self._ws.send(encode_frame(build_offline_ack_event(batch_id=batch_id)))

    def _queued_frame(
        self,
        frame: dict[str, Any],
        text: str,
        *,
        wait_for_ack: bool,
    ) -> _QueuedFrame:
        event_name = str(frame.get("event") or frame.get("type") or "unknown")
        trace_id = str(frame.get("trace_id") or frame.get("id") or "")
        chat_id = str(frame.get("chat_id") or "")
        expected_message_id = None
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if isinstance(payload.get("message_id"), str):
            expected_message_id = payload["message_id"]
        ack_future = None
        if wait_for_ack and event_name in ACKABLE_EVENTS:
            ack_future = asyncio.get_running_loop().create_future()
        return _QueuedFrame(
            text=text,
            event_name=event_name,
            trace_id=trace_id,
            chat_id=chat_id,
            expected_message_id=expected_message_id,
            ack_future=ack_future,
        )

    def _enqueue_frame(
        self,
        queued: _QueuedFrame,
        *,
        front: bool = False,
        log_queued: bool = True,
    ) -> None:
        if len(self._send_queue) >= SEND_QUEUE_MAX:
            dropped = self._send_queue.pop() if front else self._send_queue.popleft()
            if dropped.ack_future is not None and not dropped.ack_future.done():
                dropped.ack_future.set_exception(asyncio.QueueFull())
            logger.info(
                format_ws_log(
                    event="send_queue_drop",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="drop_oldest",
                    fields=[
                        ("event_name", dropped.event_name),
                        ("trace_id", dropped.trace_id),
                        ("chat_id", dropped.chat_id),
                        ("queue_size", len(self._send_queue)),
                        ("queue_max", SEND_QUEUE_MAX),
                    ],
                )
            )
        if front:
            self._send_queue.appendleft(queued)
        else:
            self._send_queue.append(queued)
        if log_queued:
            logger.info(
                format_ws_log(
                    event="send_queued",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="queue",
                    fields=[
                        ("event_name", queued.event_name),
                        ("trace_id", queued.trace_id),
                        ("chat_id", queued.chat_id),
                        ("queue_size", len(self._send_queue)),
                    ],
                )
            )

    def _log_send_failed(self, queued: _QueuedFrame) -> None:
        logger.info(
            format_ws_log(
                event="send_failed",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="requeue_reconnect",
                fields=[
                    ("event_name", queued.event_name),
                    ("trace_id", queued.trace_id),
                    ("chat_id", queued.chat_id),
                    ("queue_size", len(self._send_queue)),
                ],
            )
        )

    def _log_send_dropped(self, queued: _QueuedFrame, *, reason: str) -> None:
        logger.info(
            format_ws_log(
                event="send_dropped",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="drop",
                fields=[
                    ("event_name", queued.event_name),
                    ("trace_id", queued.trace_id),
                    ("chat_id", queued.chat_id),
                    ("reason", reason),
                    ("queue_size", len(self._send_queue)),
                ],
            )
        )

    def _start_ack_timer_if_needed(self, queued: _QueuedFrame) -> None:
        if queued.ack_future is None:
            return
        if queued.trace_id in self._pending_acks:
            return

        async def timeout_ack() -> None:
            try:
                await asyncio.sleep(self._cfg.ack_timeout_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            pending = self._pending_acks.pop(queued.trace_id, None)
            if pending is None or pending.future.done():
                return
            logger.info(
                format_ws_log(
                    event="ack_timeout",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reject_no_reconnect",
                    fields=[
                        ("event_name", pending.event_name),
                        ("trace_id", pending.trace_id),
                        ("chat_id", pending.chat_id),
                        ("timeout_ms", self._cfg.ack_timeout_ms),
                    ],
                )
            )
            pending.future.set_exception(asyncio.TimeoutError())

        timeout_task = asyncio.create_task(timeout_ack(), name="clawchat-ack-timeout")
        queued.ack_timeout_task = timeout_task
        self._pending_acks[queued.trace_id] = _PendingAck(
            event_name=queued.event_name,
            trace_id=queued.trace_id,
            chat_id=queued.chat_id,
            expected_message_id=queued.expected_message_id,
            future=queued.ack_future,
            timeout_task=timeout_task,
        )

    def _handle_ack(self, frame: dict[str, Any]) -> None:
        trace_id = str(frame.get("trace_id") or frame.get("id") or "")
        chat_id = str(frame.get("chat_id") or "")
        pending = self._pending_acks.pop(trace_id, None)
        if pending is None:
            logger.info(
                format_ws_log(
                    event="ack_unmatched",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="ignore",
                    fields=[
                        ("trace_id", trace_id),
                        ("chat_id", chat_id),
                    ],
                )
            )
            return
        pending.timeout_task.cancel()
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        message_id = payload.get("message_id") if isinstance(payload.get("message_id"), str) else None
        if pending.expected_message_id and message_id != pending.expected_message_id:
            if not pending.future.done():
                pending.future.set_exception(
                    RuntimeError(
                        "ack message_id mismatch: "
                        f"expected {pending.expected_message_id} got {message_id}"
                    )
                )
            return
        logger.info(
            format_ws_log(
                event="ack_received",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="resolve",
                fields=[
                    ("event_name", pending.event_name),
                    ("trace_id", trace_id),
                    ("chat_id", pending.chat_id or chat_id),
                    ("message_id", message_id),
                ],
            )
        )
        if not pending.future.done():
            pending.future.set_result(frame)

    def _handle_message_error(self, frame: dict[str, Any]) -> None:
        trace_id = str(frame.get("trace_id") or frame.get("id") or "")
        chat_id = str(frame.get("chat_id") or "")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        message_id = payload.get("message_id") if isinstance(payload.get("message_id"), str) else None
        pending = self._pending_acks.pop(trace_id, None)
        if pending is None:
            logger.info(
                format_ws_log(
                    event="message_error_unmatched",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="ignore",
                    fields=[
                        ("trace_id", trace_id),
                        ("chat_id", chat_id),
                        ("message_id", message_id),
                    ],
                )
            )
            return
        pending.timeout_task.cancel()
        reason = self._message_error_reason(payload)
        logger.info(
            format_ws_log(
                event="message_error_received",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="reject_ack",
                fields=[
                    ("event_name", pending.event_name),
                    ("trace_id", trace_id),
                    ("chat_id", pending.chat_id or chat_id),
                    ("message_id", message_id),
                    ("reason", reason),
                ],
            )
        )
        if not pending.future.done():
            pending.future.set_exception(RuntimeError(f"message.error: {reason}"))

    def _message_error_reason(self, payload: dict[str, Any]) -> str:
        for key in ("reason", "error", "message", "code"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return self._sanitize_secret_text(value.strip()) or "unknown"
        return "unknown"

    def _reject_pending_acks(self, exc: Exception) -> None:
        pending_acks = list(self._pending_acks.values())
        self._pending_acks.clear()
        for pending in pending_acks:
            pending.timeout_task.cancel()
            if not pending.future.done():
                pending.future.set_exception(exc)

    def _reject_queued_ack_waiters(self, exc: Exception, *, clear_queue: bool = False) -> None:
        retained: deque[_QueuedFrame] = deque()
        for queued in self._send_queue:
            if queued.ack_timeout_task is not None:
                queued.ack_timeout_task.cancel()
            if queued.ack_future is not None and not queued.ack_future.done():
                queued.ack_future.set_exception(exc)
            if not clear_queue or queued.ack_future is None:
                retained.append(queued)
        if clear_queue:
            self._send_queue = retained

    async def _handle_heartbeat_timeout(self) -> None:
        logger.info(
            format_ws_log(
                event="heartbeat_timeout",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="reconnect",
                fields=[("timeout_ms", self._cfg.heartbeat_timeout_ms)],
            )
        )
        if self._ws is not None:
            await self._ws.close()

    def _record_connection(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        if self._store is None:
            return None
        try:
            return getattr(self._store, operation)(*args, **kwargs)
        except Exception:  # noqa: BLE001
            logger.warning(
                "clawchat connection database persistence failed operation=%s",
                operation,
            )
            return None

    def _safe_error_text(self, exc: BaseException) -> str:
        return self._sanitize_secret_text(str(exc) or type(exc).__name__) or type(exc).__name__

    def _sanitize_secret_text(self, text: str | None) -> str | None:
        if text is None:
            return None
        token = self._cfg.token
        if token:
            return text.replace(token, "***")
        return text

    def _finish_current_connection(
        self,
        state: str,
        *,
        close_code: int | None = None,
        close_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        if self._connection_row_id is None:
            return
        connection_row_id = self._connection_row_id
        self._connection_row_id = None
        kwargs: dict[str, Any] = {"state": state}
        if close_code is not None:
            kwargs["close_code"] = close_code
        if close_reason is not None:
            kwargs["close_reason"] = close_reason
        if error is not None:
            kwargs["error"] = error
        self._record_connection(
            "finish_connection",
            connection_row_id,
            **kwargs,
        )

    def _schedule_stable_ready_reset(self) -> None:
        self._cancel_stable_ready_reset()
        self._stable_ready_reset_done = False
        loop = asyncio.get_running_loop()
        attempt = self._attempt
        self._stable_ready_handle = loop.call_later(
            BACKOFF_RESET_AFTER_SECONDS,
            self._reset_reconnect_count_after_stable_ready,
            attempt,
        )

    def _cancel_stable_ready_reset(self) -> None:
        if self._stable_ready_handle is None:
            return
        self._stable_ready_handle.cancel()
        self._stable_ready_handle = None

    def _reset_reconnect_count_after_stable_ready(self, attempt: int) -> None:
        self._stable_ready_handle = None
        if self._state != ConnectionState.READY or self._attempt != attempt:
            return
        self._tracker.reset_reconnect_count()
        snapshot = self._tracker.snapshot()
        self._attempt = snapshot.attempt
        self._reconnect_count = snapshot.reconnect_count
        self._stable_ready_reset_done = True
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
