"""ClawChat WebSocket connection lifecycle."""

from __future__ import annotations

import asyncio
import contextvars
import enum
import json
import logging
import math
import os
import random
import socket
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable, ClassVar, Literal

try:
    from websockets.asyncio.client import connect as _ws_connect_impl
except ImportError:  # pragma: no cover
    _ws_connect_impl = None  # type: ignore[assignment]

from clawchat_gateway import __version__
from clawchat_gateway.api_client import (
    DIAGNOSTIC_REDACTION_FAILED,
    DIAGNOSTIC_WITHHELD,
    redact_display_text,
    url_has_userinfo,
)
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.hermes_home import hermes_home
from clawchat_gateway import instance_lock, install_identity
from clawchat_gateway.profile_collision import hermes_root
from clawchat_gateway.token_refresh import (
    RefreshManager,
    RefreshOutcome,
    PROACTIVE_JITTER_SECONDS,
    is_token_near_expiry,
)
from clawchat_gateway.protocol import (
    build_connect_request,
    build_offline_ack_event,
    decode_frame,
    encode_frame,
    extract_nonce,
    is_hello_ok,
    new_frame_id,
)
from clawchat_gateway.config import _get_env, _jwt_claim
from clawchat_gateway.device_id import (
    device_id_is_pinned,
    get_device_id,
    resolve_paired_device_id,
    warn_if_device_id_unpinned,
)
from clawchat_gateway.storage import ActivationCredentials, get_clawchat_store, refresh_token_fingerprint
from clawchat_gateway.notify_signal import NotifySignalObserver
from clawchat_gateway.onboarding import RECONNECT_GUIDE_URL
from clawchat_gateway.ws_log import format_ws_log
from clawchat_gateway.ws_state import ReconnectTracker

logger = logging.getLogger("clawchat_gateway.connection")

HANDSHAKE_TIMEOUT_SECONDS = 10.0
SUPERSEDED_CHECK_SECONDS = 30.0
INSTANCE_LOCK_RETRY_SECONDS = 30.0
INSTANCE_LOCK_REMINDER_SECONDS = 600.0
CREDENTIAL_READ_FAILURE_LIMIT = 3
# Upper bound on the graceful ``ws.close()` in ``stop()``. A half-dead socket can
# make ``close()`` block until the library's own close handshake gives up; bounding
# it keeps supersession (``start`` -> prior ``stop``) and the Hermes watcher's ~5s
# disconnect budget responsive, so a slow close never leaves an orphan behind.
_WS_CLOSE_TIMEOUT_SECONDS = 5.0
SEND_QUEUE_MAX = 128
BACKOFF_RESET_AFTER_SECONDS = 5.0
# msghub closes the OLDER socket with application code 4001 when a newer session
# for the same (user_id, device_id) takes over (docs/client-integration.md §3.6).
CLOSE_CODE_REPLACED = 4001
# msghub closes the NEWER socket with application code 4002 when the operator
# enabled the duplicate-session refusal guard and the incumbent is provably
# healthy (docs/client-integration.md §3.6). The close reason is a JSON object
# carrying retry_after_ms; unlike 4001 the server owns the escalation
# (60s -> 300s per consecutive refusal), so the client just honours the value.
CLOSE_CODE_DUPLICATE_THROTTLED = 4002
# Different installations require explicit operator recovery; tokens remain valid.
CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL = 4010
# Reconnect floor after being evicted by a takeover, and its ceiling once the
# eviction keeps repeating.
#
# The eviction loop is mutual: two supervisors sharing a device id each
# reconnect when kicked and kick the other in turn. Ordinary exponential backoff
# cannot break it, because every round reaches READY for longer than
# BACKOFF_RESET_AFTER_SECONDS and resets the delay to
# ``reconnect_initial_delay_ms``. Prod on 2026-08-18 held that pattern flat for
# hours across six devices at 9-163s per eviction.
#
# So a takeover gets its own floor, doubling per consecutive loss: whichever
# side keeps losing withdraws further each round until the other simply stays
# connected. The floor is only ever applied to THIS reconnect's wait — the
# ordinary exponential state is left untouched, so a device that stops being
# evicted returns to the fast path immediately.
TAKEOVER_BACKOFF_FLOOR_SECONDS = 60.0
TAKEOVER_BACKOFF_MAX_SECONDS = 600.0
ACKABLE_EVENTS = {"message.send", "message.reply"}

# Every ClawChat conversation id is minted by member-backend with the `cnv_`
# prefix, and msghub resolves a chat_id only through member-backend. Anything
# else — a `usr_…` user idcode, a host-composed `direct:{self}:{peer}` key, an
# adapter name, a placeholder — is rejected upstream with
# `code=400: invalid conversation id`, which costs a round trip and leaves an
# ERROR line in msghub for a frame that never had a recipient.
CHAT_ID_PREFIX = "cnv_"

# The prefix alone is not the rule. member-backend mints every
# externally-visible id as `<3 lowercase ascii>_<26 Crockford base32 chars>`
# and validates that shape server-side: exactly 30 chars, `_` at index 3, and a
# body decodable as Crockford base32 — an alphabet that drops I, L, O and U to
# stay unambiguous, decoded case-insensitively so a lowercase body is equally
# valid. Checking only the prefix still lets `cnv_`,
# `cnv_<id>:thr_1` and `cnv_<display name>` onto the wire, where member-backend
# answers `code=400: invalid conversation id` all the same. These constants are
# pinned by the shared parity fixture (`invalid-chat-id-outbound.json`).
IDCODE_LENGTH = 30
IDCODE_BODY_LENGTH = 26
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CROCKFORD_CHARS = frozenset(CROCKFORD_ALPHABET + CROCKFORD_ALPHABET.lower())


def is_valid_chat_id(chat_id: Any) -> bool:
    """Whether `chat_id` can name a ClawChat conversation at all.

    A purely *static* check — it says nothing about whether the conversation
    exists, is alive, or admits this sender. Those are server-side questions
    answered by `message.error`; this one is answerable locally and is always
    wrong to send.
    """
    if not isinstance(chat_id, str):
        return False
    if len(chat_id) != IDCODE_LENGTH:
        return False
    if not chat_id.startswith(CHAT_ID_PREFIX):
        return False
    body = chat_id[len(CHAT_ID_PREFIX) :]
    return all(char in _CROCKFORD_CHARS for char in body)


class _ChatRejectedError(RuntimeError):
    """Internal sentinel: a queued frame was dropped by the dead-chat gate.

    Raised only into a queued frame's ack future by ``_flush_send_queue``, and
    caught only by ``send_frame``, which turns it back into ``False``. It never
    escapes the connection layer, so callers keep seeing the plain
    "not delivered" bool they already handle.
    """


ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS = 2.0

# Protocol v2 §14.1: a `hello-fail` whose reason signals that the upstream auth
# backend is *unavailable* (upstream 5xx / timeout) means the
# token may still be valid — the auth service is down. The mandated response is
# to backoff-reconnect with the SAME token, NOT to discard credentials or
# trigger a refresh (a 5xx storm must not become a mass token-refresh storm).
# §3.5 pins the matching contract in BOTH directions: `"authentication failed"`
# is the ONLY terminal reason and is matched by exact equality; every other
# reason — nonce mismatch, invalid connect, and any string msghub adds later —
# is transient and must backoff-reconnect with the same token. Widening the
# terminal branch to a substring breaks that forward-compat guarantee.
_TRANSIENT_AUTH_FAILURE_MARKERS = (
    "remote auth service unavailable",
    "auth service unavailable",
    "service unavailable",
    "temporarily unavailable",
)


def build_connect_capabilities(*, install_conflict: bool = False) -> dict[str, bool]:
    """Return the capability flags advertised in the connect handshake.

    Extracted so tests can assert on the capability set without wiring up a
    full WebSocket session.  Keep every field in sync with the OpenClaw plugin
    (``clawchat-plugin-openclaw``).
    """
    return {
        **({"install_conflict": True} if install_conflict else {}),
        # Agent runtime is single-device: multi_device stays off so the
        # server never self-fans-out this connection's own messages.
        "multi_device": False,
        # device_replay: replay offline messages on reconnect.
        "device_replay": True,
        # chat_meta_events: receive chat metadata invalidation frames.
        "chat_meta_events": True,
        # notify_signals: receive §9.4 reliable system notification frames.
        "notify_signals": True,
        # permission_events: opts this owner-side connection in to the live
        # §9.5 ``permission.request`` / ``permission.resolved`` signals. The
        # server filters at the hub layer, so a client that does not advertise
        # it never receives either frame.
        "permission_events": True,
    }


def _is_transient_auth_failure(reason: str | None) -> bool:
    if not reason:
        return False
    text = reason.lower()
    return any(marker in text for marker in _TRANSIENT_AUTH_FAILURE_MARKERS)


# §3.5: the one reason that is terminal for the current token. Matched by EXACT
# equality — never a substring or prefix. §3.5 names `"invalid token"` and
# `"token expired"` as speculative matchers not to add: msghub does not emit
# them, and matching them turns a future transient reason into a forced re-pair.
_TERMINAL_AUTH_FAILURE_REASON = "authentication failed"


def _is_token_rejected(reason: str | None) -> bool:
    """True only for the exact §3.5 terminal reason (case/whitespace tolerant)."""
    if not reason:
        return False
    return reason.strip().lower() == _TERMINAL_AUTH_FAILURE_REASON


# §C.1 user-visible auto-logout message. MUST be kept identical across both
# plugins (Hermes + OpenClaw). Points the owner at the reconnect prompt (a
# code bound to this identity) rather than at a slash command: a fresh create
# code spent here would mint a second agent.
AUTO_LOGOUT_STATUS_MESSAGE = (
    "ClawChat token expired and could not be refreshed. Ask your owner to send you "
    f"the reconnect prompt from the ClawChat app, then follow {RECONNECT_GUIDE_URL}."
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
class _InboundLane:
    """One chat's inbound frames, handled in arrival order by one worker task."""

    frames: deque[dict[str, Any]]
    task: asyncio.Task[None] | None = None


@dataclass
class _PendingAck:
    event_name: str
    trace_id: str
    chat_id: str
    expected_message_id: str | None
    future: asyncio.Future[dict[str, Any]]
    timeout_task: asyncio.Task[None]


def takeover_backoff_floor(streak: int) -> float:
    """Reconnect floor for ``streak`` consecutive takeover evictions.

    0 losses -> no floor; then TAKEOVER_BACKOFF_FLOOR_SECONDS doubling per
    additional loss, capped at TAKEOVER_BACKOFF_MAX_SECONDS.
    """
    if streak <= 0:
        return 0.0
    return min(TAKEOVER_BACKOFF_FLOOR_SECONDS * (2.0 ** (streak - 1)), TAKEOVER_BACKOFF_MAX_SECONDS)


def refusal_retry_after_seconds(reason: str | None) -> float:
    """Seconds to wait after a 4002 duplicate-session refusal.

    Parses ``retry_after_ms`` out of the JSON close reason msghub sends. Falls
    back to ``TAKEOVER_BACKOFF_FLOOR_SECONDS`` when the reason is missing or
    unparseable, so a refusal never reconnects at the ordinary initial delay
    even if the server text changes shape. Clamped to the takeover ceiling.
    """
    seconds = TAKEOVER_BACKOFF_FLOOR_SECONDS
    if reason:
        try:
            value = json.loads(reason).get("retry_after_ms")
        except (ValueError, TypeError, AttributeError):
            value = None
        try:
            if type(value) in (int, float) and math.isfinite(value) and value > 0:
                seconds = float(value) / 1000.0
        except OverflowError:
            pass
    return min(max(seconds, 0.0), TAKEOVER_BACKOFF_MAX_SECONDS)


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
    SUSPENDED = "suspended"
    STANDBY = "standby"
    SUPERSEDED = "superseded"
    CLOSED = "closed"


OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
OnStateChange = Callable[[ConnectionState], Awaitable[None]]
OnSignal = Callable[[dict[str, Any]], Awaitable[None]]
# Called once on permanent refresh failure (auto-logout) so the adapter can emit
# the user-visible status/chat message (§C.1). Receives the human-readable
# message text.
OnAuthLogout = Callable[[str], Awaitable[None]]
# Called for every freshly-observed ``notify.signal`` frame (after dedup).
# Receives the full raw frame dict.  Use to react to specific ``payload.type``
# values (e.g. ``"agent.config.changed"``).
OnNotifySignal = Callable[[dict[str, Any]], Awaitable[None]]
# Called for every inbound ``message.error`` frame, BEFORE the pending-ack
# lookup — an unmatched frame still carries an authoritative ``chat_id`` +
# ``payload.code`` and must participate in dead-chat decisions.
OnMessageError = Callable[[dict[str, Any]], Awaitable[None]]
# Injected by the adapter. True when the server has rejected this chat_id with
# a terminal message.error code and no outbound frame may be sent for it.
IsChatRejected = Callable[[str], bool]


def _in_connection_context(method):
    """Run lifecycle/credential callbacks in the owning profile, including workers.

    Coroutine creation alone does not capture ContextVars; task creation does.
    A copy also permits overlapping callbacks without re-entering one Context.
    """
    @wraps(method)
    async def scoped(self, *args, **kwargs):
        task = self._profile_context.copy().run(
            asyncio.create_task, method(self, *args, **kwargs)
        )
        return await task
    return scoped


class ClawChatConnection:
    # Process-wide guard: at most one live supervisor per (profile home, account_id). The Hermes
    # reconnect watcher builds a FRESH adapter (=> new ``ClawChatConnection``) on every
    # retry and only best-effort disconnects the old one (a ~5s budget it abandons on
    # timeout; a ``wait_for``-cancelled ``connect()`` never disconnects at all). A
    # leaked supervisor keeps reconnecting with the SAME ``device_id``, so msghub
    # mutually kicks the live connection in an endless reconnect storm. Superseding the
    # prior supervisor when a fresh one starts makes such duplicates impossible.
    _live_supervisors: ClassVar[dict[tuple[str, str], "ClawChatConnection"]] = {}

    def __init__(
        self,
        config: ClawChatConfig,
        *,
        on_message: OnMessage,
        on_state_change: OnStateChange | None = None,
        on_signal: OnSignal | None = None,
        on_auth_logout: OnAuthLogout | None = None,
        on_notify_signal: OnNotifySignal | None = None,
        on_message_error: OnMessageError | None = None,
        is_chat_rejected: IsChatRejected | None = None,
        account_id: str = "default",
        connect_device_id: str | None = None,
        owner_locale: Callable[[], str | None] | None = None,
        _allow_superseded_notice: bool = False,
        proactive_jitter: Callable[[], float] | None = None,
    ) -> None:
        # ``connect_device_id`` overrides the device id presented in the WS
        # connect payload ONLY. REST refresh keeps the canonical resolved id
        # (the backend rejects a mismatched X-Device-Id with a 10003 forced
        # re-login). msghub enforces single-session per (user_id, device_id)
        # via takeover, so a standalone sender must present a sibling device
        # id or it kicks a gateway running in another process off the socket.
        self._connect_device_id = connect_device_id
        self._owner_locale = owner_locale
        self._allow_superseded_notice = _allow_superseded_notice
        self._superseded = False
        # Reclaim generation read when this connection first saw 4010; a reclaim
        # after that point must win over the event it is about to record.
        self._superseded_snapshot: str | None = None
        self._reclaim_event = asyncio.Event()
        self._owner_notice_task: asyncio.Task[None] | None = None
        self._owner_notice_tasks: set[asyncio.Task[None]] = set()
        self._cfg = config
        self._on_message = on_message
        self._on_state_change = on_state_change
        self._on_signal = on_signal
        self._on_auth_logout = on_auth_logout
        self._on_notify_signal = on_notify_signal
        self._on_message_error = on_message_error
        self._is_chat_rejected = is_chat_rejected
        self._account_id = account_id
        self._profile_home = hermes_home().expanduser().resolve()
        self._profile_context = contextvars.copy_context()
        self._supervisor_key = (str(self._profile_home), account_id)
        self._instance_root = hermes_root().expanduser().resolve()
        self._instance_locks: dict[str, instance_lock.LockResult] = {}
        self._instance_lock_waiting: instance_lock.LockResult | None = None
        # Set by ``start``: only a started canonical connection runs the
        # supervisor that acquires the locks, so only it is gated by them.
        self._instance_lock_enforced = False
        # True after a STANDBY wait, until the caller re-reads the credential
        # source (the holder may have rotated tokens meanwhile).
        self._instance_lock_waited = False
        self._state = ConnectionState.DISCONNECTED
        self._ws: Any = None
        self._stopping = False
        self._auth_failed = False
        self._proactive_token: str | None = None
        self._proactive_jitter_offset = 0.0
        self._proactive_jitter = proactive_jitter or (
            lambda: random.uniform(-PROACTIVE_JITTER_SECONDS, PROACTIVE_JITTER_SECONDS))
        self._credential_generation = 0
        self._connection_credential_generation: int | None = None
        self._connection_token: str | None = None
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
        # Close code of the most recent connection, and how many connections in
        # a row ended in a takeover — the input to the takeover backoff floor.
        self._last_close_code: int | None = None
        self._last_close_reason: str | None = None
        self._takeover_streak = 0
        self._supervisor_task: asyncio.Task[None] | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        self._read_task: asyncio.Task[None] | None = None
        self._credential_watch_task: asyncio.Task[None] | None = None
        self._hello_wait: asyncio.Future[bool] | None = None
        self._ready_event = asyncio.Event()
        self._settled_event = asyncio.Event()
        self._pending_connect_id: str | None = None
        self._hello_resolved_device_id: str | None = None
        self._hello_delivery_mode: str | None = None
        self._send_queue: deque[_QueuedFrame] = deque()
        self._flushing_send_queue = False
        self._pending_acks: dict[str, _PendingAck] = {}
        # Inbound message frames are handled OFF the read loop, one lane per
        # chat_id (see ``_dispatch_to_lane``). Lanes outlive a reconnect so a
        # chat's order holds across it; ``stop()`` cancels them.
        self._inbound_lanes: dict[str, _InboundLane] = {}
        self._stable_ready_handle: asyncio.TimerHandle | None = None
        self._stable_ready_reset_done = False
        self._activation_wait_logged = False
        self._using_activation_db_credentials = False
        self._credential_read_failures = 0
        self._activation_source_suspended = False
        self._pending_permanent_credentials: tuple[str, str] | None = None
        self._pending_permanent_reason: str | None = None
        self._rejected_activation_token: str | None = None
        self._rejected_refresh_fingerprint: str | None = None
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

    @_in_connection_context
    async def start(self) -> None:
        if self._supervisor_task is not None:
            return
        self._owner_loop = asyncio.get_running_loop()
        # Supersede any orphaned supervisor for the same account before starting
        # ours, so only one WS session per account is ever live in this process.
        if self._connect_device_id:
            self._supervisor_key = (
                str(self._profile_home), self._account_id + ":" + self._connect_device_id,
            )
        prior = ClawChatConnection._live_supervisors.get(self._supervisor_key)
        if prior is not None and prior is not self:
            # Take a process-local reference BEFORE stop can drop the OS lock.
            # The old connection retains its own references until it has stopped.
            if not self._connect_device_id:
                for kind, held in prior._instance_locks.items():
                    retained = held.retain()
                    if retained is not None:
                        self._instance_locks[kind] = retained
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
            try:
                await prior.stop()
            except BaseException:
                # Hermes may abandon a timed-out start before registering us.
                # Do not strand references owned only by this replacement.
                for held in self._instance_locks.values():
                    held.release()
                self._instance_locks.clear()
                raise
        self._stopping = False
        self._auth_failed = False
        self._instance_lock_enforced = not self._connect_device_id
        self._warn_if_device_id_volatile()
        if self._refresh_manager is None:
            self._refresh_manager = self._build_refresh_manager()
        self._supervisor_task = asyncio.create_task(
            self._supervisor(),
            name="clawchat-supervisor",
        )
        ClawChatConnection._live_supervisors[self._supervisor_key] = self

    def _build_refresh_manager(self) -> RefreshManager:
        # Bind persistence to the pair used for this single-flight request.
        refresh_config: ClawChatConfig | None = None

        def build_client() -> Any:
            nonlocal refresh_config
            refresh_config = self._cfg
            return self._build_refresh_client()

        async def persist_tokens(access_token: str, refresh_token: str) -> bool:
            return await self._persist_rotated_tokens(
                access_token, refresh_token, expected_credentials=refresh_config,
            )

        async def should_continue() -> bool:
            return await self._refresh_should_continue(refresh_config)

        async def persist_rejection(refresh_token: str) -> bool:
            return await self._persist_refresh_rejection(
                refresh_token, expected_credentials=refresh_config,
            )

        async def persist_logout(reason: str) -> bool:
            return await self._persist_auth_logout(
                reason, expected_credentials=refresh_config,
            )

        return RefreshManager(
            build_client=build_client,
            persist_tokens=persist_tokens,
            persist_logout=persist_logout,
            persist_rejection=persist_rejection,
            device_id_provider=self._refresh_device_id,
            should_continue=should_continue,
        )

    async def _refresh_should_continue(self, refresh_config: ClawChatConfig | None) -> bool:
        if (
            self._allow_superseded_notice or self._install_is_blocked()
            or not self._instance_locks_ready() or self._auth_failed
            or self._activation_source_suspended or refresh_config is None
        ):
            return False
        if self._cfg.refresh_token != refresh_config.refresh_token:
            return False
        status = await self._recheck_activation_source(adopt_changes=False)
        return (
            status in ("ok", "unreadable") and not self._activation_source_suspended
            and self._instance_locks_ready() and not self._install_is_blocked()
        )

    def _instance_locks_ready(self) -> bool:
        if self._stopping:
            return False
        # Standalone senders use a sibling WS device and must never wait on the gateway.
        if self._connect_device_id or not self._instance_lock_enforced:
            return True
        profile = self._instance_locks.get("profile")
        identity = self._instance_locks.get("identity")
        return bool(
            profile is not None
            and profile.outcome in ("acquired", "unsupported")
            and (not self._cfg.user_id or (
                identity is not None and identity.outcome in ("acquired", "unsupported")
                and identity.path == instance_lock.identity_lock_path(
                    self._instance_root, self._cfg.user_id,
                )
            ))
        )

    def _take_instance_lock_waited(self) -> bool:
        waited, self._instance_lock_waited = self._instance_lock_waited, False
        return waited

    def instance_lock_status(self) -> dict[str, Any]:
        waiting = self._instance_lock_waiting
        return {
            "state": "standby" if waiting else (
                "exempt" if self._connect_device_id else (
                    "unsupported" if any(lock.outcome == "unsupported" for lock in self._instance_locks.values())
                    else "acquired" if self._instance_locks else "unlocked"
                )
            ),
            "kind": waiting.kind if waiting else None,
            "path": str(waiting.path) if waiting else None,
            "holder": dict(waiting.holder) if waiting else {},
        }

    async def _wait_for_instance_locks(self, *, profile_only: bool = False) -> bool:
        if self._connect_device_id:
            return not self._stopping
        waited_from = None
        last_reminder = 0.0
        while not self._stopping:
            paths = [("profile", self._profile_home / "clawchat" / "gateway.lock")]
            if not profile_only:
                target = (
                    instance_lock.identity_lock_path(self._instance_root, self._cfg.user_id)
                    if self._cfg.user_id else None
                )
                old = self._instance_locks.get("identity")
                if old is not None and old.path != target:
                    self._instance_locks.pop("identity").release()
                if target is not None:
                    paths.append(("identity", target))
            blocked = None
            for kind, path in paths:
                if kind in self._instance_locks:
                    continue
                result = instance_lock.acquire(path, kind=kind, profile_home=self._profile_home)
                if result.outcome == "held":
                    blocked = result
                    break
                self._instance_locks[kind] = result
            if blocked is None:
                self._instance_lock_waiting = None
                if waited_from is not None:
                    logger.info(format_ws_log(
                        event="instance_lock_acquired", account_id=self._account_id,
                        attempt=self._attempt, reconnect_count=self._reconnect_count,
                        state=self._state.value, action="continue",
                        fields=[("waited_seconds", round(time.monotonic() - waited_from, 3))],
                    ))
                    await self._set_state(ConnectionState.DISCONNECTED)
                return True
            self._instance_lock_waiting = blocked
            self._instance_lock_waited = True
            now = time.monotonic()
            first = waited_from is None
            if first:
                waited_from = now
            await self._set_state(ConnectionState.STANDBY)
            if first or now - last_reminder >= INSTANCE_LOCK_REMINDER_SECONDS:
                last_reminder = now
                logger.log(logging.ERROR if first else logging.WARNING, format_ws_log(
                    event="instance_lock_standby", account_id=self._account_id,
                    attempt=self._attempt, reconnect_count=self._reconnect_count,
                    state=self._state.value, action="wait",
                    fields=[("kind", blocked.kind), ("path", str(blocked.path)),
                            ("holder_pid", blocked.holder.get("pid")),
                            ("holder_hostname", blocked.holder.get("hostname")),
                            ("holder_started_at", blocked.holder.get("started_at")),
                            ("retry_seconds", INSTANCE_LOCK_RETRY_SECONDS)],
                ))
            await asyncio.sleep(INSTANCE_LOCK_RETRY_SECONDS)
        return False

    def _clear_pending_permanent(self) -> None:
        self._pending_permanent_credentials = None
        self._pending_permanent_reason = None

    def _apply_activation_credentials(self, credentials: ActivationCredentials) -> None:
        self._clear_pending_permanent()
        self._credential_generation += 1
        self._cfg = replace(
            self._cfg, token=credentials.access_token,
            refresh_token=credentials.refresh_token or "",
            user_id=credentials.user_id, owner_user_id=credentials.owner_user_id,
        )
        self._using_activation_db_credentials = True
        self._auth_failed = False
        self._rejected_activation_token = None
        self._clear_refresh_rejection()
        self._activated_at_ms = credentials.activated_at
        self._credential_read_failures = 0
        self._activation_source_suspended = False
        if self._refresh_manager is not None:
            self._refresh_manager.reset_latch()

    async def _suspend_activation_source(self) -> None:
        if self._activation_source_suspended:
            return
        self._credential_generation += 1
        self._activation_source_suspended = True
        self._refresh_pending_reconnect = False
        logger.warning(
            format_ws_log(
                event="activation_credentials_unreadable",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="suspend",
                fields=[("failures", self._credential_read_failures)],
            )
        )
        await self._set_state(ConnectionState.SUSPENDED)
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.set_result(False)
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=_WS_CLOSE_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001
                logger.warning("clawchat suspended socket close failed")

    async def _recheck_activation_source(
        self, *, adopt_changes: bool = True, include_bootstrap: bool = False,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        permanent_failure_reason: str | None = None,
    ) -> Literal["ok", "changed", "revoked", "unreadable"]:
        if self._activation_source_suspended:
            include_bootstrap = True
        if not self._using_activation_db_credentials and not include_bootstrap:
            return "ok"
        if (
            self._store is None and not self._using_activation_db_credentials
            and not self._activation_source_suspended
        ):
            return "ok"
        try:
            if self._store is None:
                raise RuntimeError("activation credential store unavailable")
            credentials = self._store.get_activation_credentials(
                platform="hermes", account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat activation credential watch failed", exc_info=True)
            if self._using_activation_db_credentials:
                self._credential_read_failures += 1
                if self._credential_read_failures >= CREDENTIAL_READ_FAILURE_LIMIT:
                    if (
                        permanent_failure_reason is not None
                        and not self._auth_failed and not self._stopping
                        and expected_access_token == self._cfg.token
                        and expected_refresh_token == self._cfg.refresh_token
                    ):
                        # Preserve the failure before suspension's state await
                        # can let another task recover the same SQLite pair.
                        self._pending_permanent_credentials = (
                            self._cfg.token, self._cfg.refresh_token,
                        )
                        self._pending_permanent_reason = permanent_failure_reason
                    await self._suspend_activation_source()
            return "unreadable"
        self._credential_read_failures = 0
        if credentials is None:
            if self._using_activation_db_credentials:
                await self._revoke_activation_credentials()
                return "revoked"
            if self._activation_source_suspended and adopt_changes:
                return await self._recheck_bootstrap_env_source()
            return "ok"
        if expected_access_token is not None:
            # Permanent callbacks compare the same request pair as logout's CAS.
            changed = (
                credentials.access_token != expected_access_token
                or (credentials.refresh_token or "") != expected_refresh_token
            )
        else:
            changed = (
                credentials.access_token != self._cfg.token
                or (credentials.refresh_token or "") != self._cfg.refresh_token
                or credentials.user_id != self._cfg.user_id
                or credentials.owner_user_id != self._cfg.owner_user_id
            )
        was_suspended = self._activation_source_suspended
        if was_suspended and adopt_changes and self._pending_permanent_credentials is not None:
            pair = (credentials.access_token, credentials.refresh_token or "")
            if pair == self._pending_permanent_credentials:
                completed = await self._finish_pending_permanent()
                return "revoked" if completed else "unreadable"
        if changed:
            if not adopt_changes:
                return "changed"
            logger.info(
                format_ws_log(
                    event="activation_credentials_changed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reconnect",
                    fields=[("has_refresh_token", bool(credentials.refresh_token))],
                )
            )
            if (
                credentials.access_token == self._rejected_activation_token
                or not self._refresh_credentials_usable(credentials.refresh_token)
            ):
                await self._enter_auth_failed(reason="activation credentials rejected")
                return "revoked"
            self._apply_activation_credentials(credentials)
            if self._ws is not None and not was_suspended:
                await self._ws.close()
        elif was_suspended and adopt_changes:
            if not self._using_activation_db_credentials:
                if (
                    credentials.access_token == self._rejected_activation_token
                    or not self._refresh_credentials_usable(credentials.refresh_token)
                ):
                    await self._enter_auth_failed(reason="activation credentials rejected")
                    return "revoked"
                self._apply_activation_credentials(credentials)
            else:
                self._activation_source_suspended = False
                if self._refresh_manager is not None:
                    self._refresh_manager.reset_latch()
        if was_suspended and adopt_changes:
            logger.info(
                format_ws_log(
                    event="activation_credentials_readable",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="reconnect",
                )
            )
            await self._set_state(ConnectionState.DISCONNECTED)
        return "changed" if changed else "ok"

    async def _recheck_bootstrap_env_source(self) -> Literal["ok", "changed", "revoked", "unreadable"]:
        from clawchat_gateway.activate import _persisted_env_credentials_match

        matches = _persisted_env_credentials_match(self._cfg.token, self._cfg.refresh_token)
        if matches is not True:
            if matches is False:
                logger.warning(
                    format_ws_log(
                        event="activation_credentials_changed",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=self._state.value,
                        action="wait_activation",
                        fields=[("reason", "bootstrap env credentials changed")],
                    )
                )
            return "changed" if matches is False else "unreadable"
        if self._pending_permanent_credentials is not None:
            completed = await self._finish_pending_permanent()
            return "revoked" if completed else "unreadable"
        self._activation_source_suspended = False
        if self._refresh_manager is not None:
            self._refresh_manager.reset_latch()
        logger.info(
            format_ws_log(
                event="activation_credentials_readable",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="reconnect",
                fields=[("source", "bootstrap_env")],
            )
        )
        await self._set_state(ConnectionState.DISCONNECTED)
        return "ok"

    async def _wait_for_activation_source(self) -> bool:
        while self._activation_source_suspended and not self._stopping:
            await asyncio.sleep(ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS)
            await self._recheck_activation_source()
        return not self._stopping

    def _build_refresh_client(self) -> Any:
        from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError, RefreshSkipped

        base_url = self._cfg.base_url or self._cfg.websocket_url
        client = ClawChatApiClient(
            base_url=base_url,
            token="",  # /v1/auth/refresh is unauthenticated; token unused.
            user_id=self._cfg.user_id,
            device_id=self._refresh_device_id(),
        )
        request_config = self._cfg
        request_locks = tuple(self._instance_locks.values())
        refresh_sync = client._auth_refresh_sync

        def guarded_refresh_sync(*args: Any) -> Any:
            # urllib's thread can survive cancellation/timeout of its coroutine.
            # Pin the OS locks through the actual request, including on stop().
            leases = []
            try:
                for held in request_locks:
                    lease = held.retain()
                    if lease is None:
                        raise ClawChatApiError(kind="instance_lock", message="instance locks released", connect_failed=True)
                    leases.append(lease)
                if (
                    not self._instance_locks_ready()
                    or self._cfg.refresh_token != request_config.refresh_token
                ):
                    raise ClawChatApiError(kind="instance_lock", message="instance ownership changed", connect_failed=True)
                # The worker may have been queued before a terminal close arrived.
                # Recheck at the HTTP boundary, after retaining its ownership leases.
                if self._allow_superseded_notice or self._install_is_blocked():
                    raise RefreshSkipped(
                        kind="installation_superseded", message="installation superseded",
                        connect_failed=True,
                    )
                return refresh_sync(*args)
            finally:
                for lease in leases:
                    lease.release()

        client._auth_refresh_sync = guarded_refresh_sync
        return client

    def _resolve_device_id(self) -> tuple[str, bool]:
        """Connect-time device id used as ``X-Device-Id`` on connect AND refresh (§E).

        Returns ``(device_id, durable)`` — ``durable`` is True when the id
        survives container recreation (stored row, token claim, or pinned env),
        False only for the volatile host-fingerprint fallback.

        Resolution order (steps 1-3 are shared with ``activate.py``'s
        ``--repair`` / server-confirmed bound-agent auto-repair path via
        ``device_id.resolve_paired_device_id`` — replaying an EXISTING
        identity must never assign the new per-profile id either; only step 4
        does):

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
        3. ``legacy_host_device_id()`` — a token is present but neither of the
           above yielded an id: this is a named profile paired before device
           ids were persisted and before tokens carried ``did``. The backend
           baked the (pre-agent-scoped) host fingerprint into that session, so
           refresh must keep presenting it, never the new per-profile id. The
           backfill below persists it.
        4. The deterministic, agent-scoped ``get_device_id()`` fingerprint —
           only for a truly unpaired process with no stored row and no token
           yet.
        """
        stored = None
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
                return stored, True
        token_did = _jwt_claim(self._cfg.token, "did")
        if token_did:
            return token_did, True
        resolved = resolve_paired_device_id(stored=None, token=self._cfg.token)
        if resolved:
            # Paired before device ids were persisted and before tokens carried
            # `did`: the backend baked the LEGACY host id into this session, so
            # refresh must keep presenting it. The backfill below persists it.
            return resolved, device_id_is_pinned()
        return get_device_id(), device_id_is_pinned()

    def _refresh_device_id(self) -> str:
        return self._resolve_device_id()[0]

    def _warn_if_device_id_volatile(self) -> None:
        """Boot-time gate for the unpinned-``CLAWCHAT_DEVICE_ID`` warning (§E).

        The warning only applies when resolution actually falls through to the
        volatile host fingerprint. A stored activations-row id or token ``did``
        is read back from SQLite / the token on every boot, so it stays stable
        across container recreation even without the env pin — warning then
        would be a false alarm.
        """
        _, durable = self._resolve_device_id()
        if durable:
            return
        warn_if_device_id_unpinned()

    def session_device_id(self) -> str:
        """Public accessor for the resolved connect-time device id (§E).

        The plugin-version report must key on the SAME device id the WS session
        and refresh use, so the report row links to the device the backend tracks
        (rather than a volatile ``get_device_id()`` fingerprint).
        """
        return self._refresh_device_id()

    def use_sibling_connect_device_id(self, suffix: str) -> str:
        """Present ``<canonical id><suffix>`` as the WS connect device id.

        msghub keys live sessions on (user_id, device_id) with takeover
        semantics: connecting with the canonical id would kick a gateway
        running in another process off its socket. A sibling id coexists —
        the inbox is user-scoped with per-device cursors, so it never steals
        messages from the real device. REST refresh keeps the canonical id.
        Must be called before ``start``. Returns the id that will be presented.
        """
        sibling = f"{self._refresh_device_id()}{suffix}"
        self._connect_device_id = sibling
        return sibling

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
        # Via _get_env, not os.environ: a profile gateway inherits the default
        # profile's exported CLAWCHAT_HOME_CHANNEL, and seeding this profile's
        # row with it points the agent at the main agent's conversation.
        return _get_env("CLAWCHAT_HOME_CHANNEL") or None

    @_in_connection_context
    async def _persist_rotated_tokens(
        self, access_token: str, refresh_token: str,
        *, expected_credentials: ClawChatConfig | None = None,
    ) -> bool:
        from clawchat_gateway.activate import persist_rotated_tokens

        # Supersession stops new requests, but an already completed rotation must
        # be saved. Keep the existing ownership/revocation checks for this pair.
        if self._auth_failed or self._activation_source_suspended or not self._cfg.refresh_token:
            return False
        if expected_credentials is not None and (
            self._cfg.token != expected_credentials.token
            or self._cfg.refresh_token != expected_credentials.refresh_token
        ):
            return False
        request_credentials = expected_credentials or self._cfg
        using_activation_db_credentials = self._using_activation_db_credentials
        if await self._recheck_activation_source(
            adopt_changes=False, include_bootstrap=True,
            expected_access_token=request_credentials.token,
            expected_refresh_token=request_credentials.refresh_token,
        ) != "ok":
            return False

        # §C.2: thread the in-memory identity down so an env-only deployment (no
        # activations row yet) gets its row SEEDED on the first refresh instead of
        # failing the db write and bricking the agent on first rotation. An
        # adopted SQLite row must only be updated, never re-seeded after deletion.
        user_id = None if using_activation_db_credentials else self._cfg.user_id or None
        owner_user_id = None if using_activation_db_credentials else self._cfg.owner_user_id or None
        conversation_id = self._refresh_seed_conversation_id()

        try:
            # Keep the guard and write in this task without an await between
            # them: a background writer could otherwise outlive revocation.
            return persist_rotated_tokens(
                access_token=access_token,
                refresh_token=refresh_token,
                device_id=self._refresh_device_id(),
                account_id=self._account_id,
                user_id=user_id,
                owner_user_id=owner_user_id,
                conversation_id=conversation_id,
                expected_access_token=request_credentials.token,
                expected_refresh_token=request_credentials.refresh_token,
                seed_if_missing=not using_activation_db_credentials,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat rotated-token persistence raised", exc_info=True)
            return False

    @_in_connection_context
    async def _persist_refresh_rejection(
        self, refresh_token: str, *, expected_credentials: ClawChatConfig | None = None,
    ) -> bool:
        if not await self._permanent_refresh_is_current(expected_credentials):
            return False
        if expected_credentials is not None and not self._using_activation_db_credentials:
            # Bootstrap ownership is only confirmed by logout, even if .env
            # was readable here. It can become unreadable between callbacks.
            return True
        if expected_credentials is not None:
            self._record_refresh_rejection(refresh_token)
            return True
        # Latch locally if the marker write fails. Persist before any clears.
        self._rejected_refresh_fingerprint = refresh_token_fingerprint(refresh_token)
        try:
            written = self._store is not None and await asyncio.to_thread(
                self._store.mark_revoked_refresh_token,
                platform="hermes", account_id=self._account_id, refresh_token=refresh_token,
            )
            if not written:
                logger.warning("clawchat revoked-refresh marker write returned false")
        except Exception:  # noqa: BLE001
            logger.warning("clawchat revoked-refresh marker write failed")
        return True

    def _record_refresh_rejection(self, refresh_token: str) -> None:
        self._rejected_refresh_fingerprint = refresh_token_fingerprint(refresh_token)
        try:
            written = self._store is not None and self._store.mark_revoked_refresh_token(
                platform="hermes", account_id=self._account_id, refresh_token=refresh_token,
            )
            if not written:
                logger.warning("clawchat revoked-refresh marker write returned false")
        except Exception:  # noqa: BLE001
            logger.warning("clawchat revoked-refresh marker write failed")

    def _log_discarded_refresh(self, reason: str) -> None:
        logger.info(
            format_ws_log(
                event="refresh_result_discarded",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="ignore",
                fields=[("reason", reason)],
            )
        )

    async def _permanent_refresh_is_current(
        self, expected: ClawChatConfig | None, *, completing_pending: bool = False,
        reason: str = AUTO_LOGOUT_LAST_ERROR,
    ) -> bool:
        # A late rejection during supersession must not clear otherwise valid credentials.
        if self._install_is_blocked():
            return False
        if self._auth_failed or (self._activation_source_suspended and not completing_pending):
            self._log_discarded_refresh("credentials_unavailable")
            return False
        if expected is None:
            return not completing_pending
        if completing_pending and (
            self._stopping
            or self._pending_permanent_credentials != (expected.token, expected.refresh_token)
        ):
            self._log_discarded_refresh("pending_permanent_changed")
            return False
        if (
            self._cfg.token != expected.token
            or self._cfg.refresh_token != expected.refresh_token
        ):
            self._log_discarded_refresh("credentials_changed")
            return False
        status = await self._recheck_activation_source(
            adopt_changes=False, include_bootstrap=True, expected_access_token=expected.token,
            expected_refresh_token=expected.refresh_token, permanent_failure_reason=reason,
        )
        if (
            self._install_is_blocked()
            or self._auth_failed
            or (self._activation_source_suspended and not completing_pending)
            or self._cfg.token != expected.token
            or self._cfg.refresh_token != expected.refresh_token
            or status not in ("ok", "unreadable")
            or (completing_pending and (
                self._stopping
                or self._pending_permanent_credentials != (expected.token, expected.refresh_token)
            ))
        ):
            self._log_discarded_refresh(f"activation_source_{status}")
            return False
        return True

    @_in_connection_context
    async def _persist_auth_logout(
        self, reason: str, *, expected_credentials: ClawChatConfig | None = None,
        completing_pending: bool = False,
    ) -> bool:
        from clawchat_gateway.activate import (
            CredentialClearResult, _clear_persisted_env_credentials,
            _persisted_env_credentials_match, clear_persisted_credentials,
        )

        if not await self._permanent_refresh_is_current(
            expected_credentials, completing_pending=completing_pending, reason=reason,
        ):
            return False

        bootstrap_unreadable = False

        def _clear() -> bool:
            nonlocal bootstrap_unreadable
            if expected_credentials is not None and self._using_activation_db_credentials:
                sqlite_cleared = self._store.clear_activation_credentials(
                    platform="hermes", account_id=self._account_id,
                    expected_access_token=expected_credentials.token,
                    expected_refresh_token=expected_credentials.refresh_token,
                )
                if sqlite_cleared is False:
                    self._log_discarded_refresh("activation_changed_before_logout")
                    return False
                if completing_pending and self._rejected_refresh_fingerprint != refresh_token_fingerprint(
                    expected_credentials.refresh_token,
                ):
                    self._record_refresh_rejection(expected_credentials.refresh_token)
                env_cleared = _clear_persisted_env_credentials(
                    expected_access_token=expected_credentials.token,
                    expected_refresh_token=expected_credentials.refresh_token,
                )
                result = CredentialClearResult(env_cleared, bool(sqlite_cleared))
            elif expected_credentials is not None:
                # Read .env once at logout, confirming ownership before its
                # marker/clear. Missing keys still authorize tombstones (§C.1).
                env_matched = _persisted_env_credentials_match(
                    expected_credentials.token, expected_credentials.refresh_token,
                )
                if env_matched is True:
                    self._record_refresh_rejection(expected_credentials.refresh_token)
                env_cleared = env_matched is True and _clear_persisted_env_credentials()
                sqlite_cleared = False
                try:
                    sqlite_cleared = self._store is not None and self._store.clear_activation_credentials(
                        platform="hermes", account_id=self._account_id,
                        expected_access_token=expected_credentials.token,
                        expected_refresh_token=expected_credentials.refresh_token,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("clawchat logout database clear failed")
                result = CredentialClearResult(bool(env_cleared), bool(sqlite_cleared), env_matched)
                # Without a cleared SQLite row, only a confirmed .env match
                # proves the request still owns the persisted credentials.
                if not result.sqlite_cleared and result.env_matched is not True:
                    bootstrap_unreadable = result.env_matched is None
                    self._log_discarded_refresh("activation_changed_before_logout")
                    return False
                if self._rejected_refresh_fingerprint != refresh_token_fingerprint(
                    expected_credentials.refresh_token,
                ):
                    self._record_refresh_rejection(expected_credentials.refresh_token)
            else:
                result = clear_persisted_credentials(account_id=self._account_id)
            logger.log(
                logging.INFO if result.env_cleared and result.sqlite_cleared else logging.WARNING,
                "clawchat logout clear env_cleared=%s sqlite_cleared=%s",
                result.env_cleared, result.sqlite_cleared,
            )
            return True

        try:
            if expected_credentials is not None:
                if not _clear():
                    if bootstrap_unreadable:
                        self._pending_permanent_credentials = (
                            expected_credentials.token, expected_credentials.refresh_token,
                        )
                        self._pending_permanent_reason = reason
                        await self._suspend_activation_source()
                    return False
            else:
                await asyncio.to_thread(_clear)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat logout persistence raised", exc_info=True)
        # Surface the user-visible notification (§C.1) in addition to logs.
        if self._on_auth_logout is not None:
            try:
                await self._on_auth_logout(AUTO_LOGOUT_STATUS_MESSAGE)
            except Exception:  # noqa: BLE001
                logger.warning("clawchat auth-logout notification failed", exc_info=True)
        return True

    async def stop(self) -> None:
        await self._stop_in_context()
        # Last, and in the CALLER's task (the profile-context wrapper runs the
        # body above in a child task): a handler on an inbound lane may stop the
        # connection itself, and only here can that lane be recognised and
        # spared instead of cancelled while it awaits its own stop().
        await self._cancel_inbound_lanes()

    @_in_connection_context
    async def _stop_in_context(self) -> None:
        self._clear_pending_permanent()
        self._credential_generation += 1
        self._stopping = True
        try:
            self._cancel_stable_ready_reset()
            # Reclaim can start a later conflict while an older notice is still
            # within its deadline. Stop every outstanding notice, not just the latest.
            for task in self._owner_notice_tasks:
                task.cancel()
            if self._owner_notice_tasks:
                await asyncio.gather(*self._owner_notice_tasks, return_exceptions=True)
            self._owner_notice_tasks.clear()
            self._owner_notice_task = None
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
        finally:
            # Also on cancellation (a host disconnect budget or an abandoned
            # supersede): never strand the OS locks. ``_stopping`` already gates
            # every connect / refresh path, and the supervisor is cancelled here
            # if the body above did not get that far.
            if self._supervisor_task is not None and not self._supervisor_task.done():
                self._supervisor_task.cancel()
            # A refresh request already in flight on a worker thread keeps its own
            # lease (``_build_refresh_client``), so releasing here never lets another
            # process refresh concurrently with it.
            for held in self._instance_locks.values():
                held.release()
            self._instance_locks.clear()
            self._instance_lock_waiting = None
        # Release our slot only if a newer ``start`` hasn't already claimed it.
        if ClawChatConnection._live_supervisors.get(self._supervisor_key) is self:
            del ClawChatConnection._live_supervisors[self._supervisor_key]

    async def send_frame(
        self,
        frame: dict[str, Any],
        *,
        wait_for_ack: bool = False,
        queue_when_unready: bool = True,
    ) -> bool:
        owner = self._owner_loop
        if owner is None or owner is asyncio.get_running_loop():
            return await self._send_frame_here(
                frame, wait_for_ack=wait_for_ack, queue_when_unready=queue_when_unready,
            )
        if owner.is_closed() or not owner.is_running():
            self._log_send_dropped(
                self._queued_frame(frame, "", wait_for_ack=False), reason="stopped",
            )
            return False
        # Hermes runs tools on another loop; socket, queue, ACK future and timer
        # must all stay on the connection loop. Submission copies caller ContextVars.
        coro = self._send_frame_here(
            frame, wait_for_ack=wait_for_ack, queue_when_unready=queue_when_unready,
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, owner)
        except RuntimeError:
            # The owner may close between the availability check and submission.
            coro.close()
            self._log_send_dropped(
                self._queued_frame(frame, "", wait_for_ack=False), reason="stopped",
            )
            return False
        try:
            return await asyncio.wrap_future(fut)
        except asyncio.CancelledError:
            fut.cancel()
            raise

    async def _send_frame_here(
        self,
        frame: dict[str, Any],
        *,
        wait_for_ack: bool = False,
        queue_when_unready: bool = True,
    ) -> bool:
        text = encode_frame(frame)
        queued = self._queued_frame(frame, text, wait_for_ack=wait_for_ack)
        # Test KEY PRESENCE, not `frame.get(...) is not None`: `_queued_frame`
        # coerces a missing key to "" and would make an explicitly-empty chat_id
        # indistinguishable from an absent one, while a `None` default would let
        # `{"chat_id": null}` skip the gate entirely. msghub decodes an explicit
        # null into the same empty string as a missing key and rejects every
        # business event carrying one (`reason=missing_chat_id`), so null is an
        # unusable value, not an absent field. The rule is "a frame that CARRIES
        # a chat_id must carry a valid one" — byte-identical to the openclaw
        # plugin's, pinned by the shared parity fixture.
        if "chat_id" in frame and not is_valid_chat_id(frame["chat_id"]):
            # A malformed chat_id is wrong at construction time and can never
            # become valid, so — unlike the dead-chat gate below, which guards a
            # revocable server state and therefore must be re-checked when the
            # reconnect queue replays — rejecting here at the entry point is
            # sufficient: the frame never enters the send queue at all.
            self._log_invalid_chat_id(queued, frame["chat_id"])
            self._log_send_dropped(queued, reason="invalid_chat_id")
            return False
        if self._chat_is_rejected(queued.chat_id):
            # Rule is "any frame carrying a rejected chat_id", not an event-name
            # allowlist: a conversation the server refuses must receive nothing,
            # and handshake / ping / presence frames carry no chat_id anyway.
            self._log_send_dropped(queued, reason="chat_rejected")
            return False
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
                try:
                    await queued.ack_future
                except _ChatRejectedError:
                    # _flush_send_queue dropped this frame after we queued it.
                    return False
            return True
        if not queue_when_unready:
            self._log_send_dropped(queued, reason="not_ready")
            return False
        self._enqueue_frame(queued)
        if queued.ack_future is not None:
            try:
                await queued.ack_future
            except _ChatRejectedError:
                # _flush_send_queue dropped this frame after we queued it.
                return False
        return True

    @property
    def is_ready(self) -> bool:
        return self._state == ConnectionState.READY

    @property
    def is_standby(self) -> bool:
        """Waiting behind another process's instance lock — started, not failed."""
        return self._state == ConnectionState.STANDBY

    @property
    def is_superseded(self) -> bool:
        return self._state == ConnectionState.SUPERSEDED

    def _superseded_user_id(self) -> str | None:
        if self._cfg.user_id:
            return self._cfg.user_id
        if self._store is not None:
            try:
                user_id = self._store.get_activation_user_id(
                    platform="hermes", account_id=self._account_id,
                )
                return str(user_id or "").strip() or None
            except Exception:  # noqa: BLE001 — unknown identity must fail closed
                logger.warning("clawchat superseded identity lookup failed; retaining fail-closed filtering")
        return None

    def _install_is_blocked(self) -> bool:
        # The reader latches before teardown/persistence. Share that short window
        # with sibling senders so none can refresh while the terminal close settles.
        return self._superseded or any(
            conn._superseded and conn._profile_home == self._profile_home
            and conn._cfg.user_id == self._cfg.user_id
            for conn in self._live_supervisors.values()
        ) or install_identity.read_superseded(self._profile_home, self._superseded_user_id()) is not None

    @classmethod
    def wake_reclaimed(cls, profile_home: Path) -> None:
        home = profile_home.expanduser().resolve()
        for conn in list(cls._live_supervisors.values()):
            if conn._profile_home == home:
                # Commands can run on a different thread/loop in Hermes.
                task = conn._supervisor_task
                if task is None or task.done():
                    task = next((notice for notice in list(conn._owner_notice_tasks) if not notice.done()), None)
                if task is not None and not task.get_loop().is_closed():
                    task.get_loop().call_soon_threadsafe(conn._on_install_reclaimed)

    def _cancel_owner_notices(self) -> None:
        """Cancel on the connection's event loop, including earlier pending notices."""
        for task in list(self._owner_notice_tasks):
            # The wake callback and the standby loop can observe the same reclaim.
            # Do not interrupt cleanup already handling the first cancellation.
            if not task.done() and not task.cancelling():
                task.cancel()

    def _on_install_reclaimed(self) -> None:
        self._cancel_owner_notices()
        self._reclaim_event.set()

    def _log_install_state(
        self, event: str, level: int, state: dict[str, Any] | None = None,
    ) -> None:
        state = state or {}
        logger.log(level, format_ws_log(
            event=event, account_id=self._account_id, attempt=self._attempt,
            reconnect_count=self._reconnect_count, state=self._state.value,
            action="wait" if event == "install_superseded" else "connect",
            fields=[("code", CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL),
                    ("install_id", str(state.get("install_id") or "")[:8]),
                    ("by_client_version", state.get("by_client_version")),
                    ("unreadable", state.get("unreadable", False)),
                    ("recovery", "hermes clawchat reclaim or /clawchat-reclaim")],
        ))

    async def _wait_if_superseded(self) -> bool:
        if self._allow_superseded_notice:
            return not self._stopping
        waiting = self._superseded
        last_reminder = time.monotonic() if waiting else 0.0
        while not self._stopping:
            self._reclaim_event.clear()
            install_identity.release_reclaimed_cache(self._profile_home)
            state = install_identity.read_superseded(self._profile_home, self._superseded_user_id())
            if state is None:
                if waiting:
                    self._cancel_owner_notices()
                    self._superseded = False
                    self._superseded_snapshot = None
                    self._log_install_state("install_reclaimed", logging.INFO)
                    await self._set_state(ConnectionState.DISCONNECTED)
                return True
            self._superseded = True
            await self._set_state(ConnectionState.SUPERSEDED)
            now = time.monotonic()
            if not waiting or now - last_reminder >= INSTANCE_LOCK_REMINDER_SECONDS:
                self._log_install_state("install_superseded", logging.WARNING if waiting else logging.ERROR, state)
                last_reminder = now
            waiting = True
            try:
                await asyncio.wait_for(self._reclaim_event.wait(), timeout=SUPERSEDED_CHECK_SECONDS)
            except asyncio.TimeoutError:
                pass
        return False

    def _latch_superseded(self) -> None:
        if not self._superseded:
            # Block refresh first: the generation read below is file I/O, and a
            # queued refresh worker thread must not slip through meanwhile.
            self._superseded = True
            self._superseded_snapshot = install_identity.read_reclaim_generation(self._profile_home)

    async def _handle_install_superseded(self) -> None:
        self._latch_superseded()
        self._refresh_pending_reconnect = False
        self._takeover_streak = 0
        try:
            reason = json.loads(self._last_close_reason or "{}")
        except (ValueError, TypeError):
            reason = {}
        version = reason.get("by_client_version") if isinstance(reason, dict) else None
        state, created = install_identity.record_superseded(self._profile_home, snapshot=self._superseded_snapshot, state={
            "user_id": self._cfg.user_id, "device_id": self.session_device_id(),
            "install_id": install_identity.get_install_id(self._profile_home),
            "superseded_at": datetime.now(timezone.utc).isoformat(),
            "by_client_version": version if isinstance(version, str) else None,
            "owner_notice": {"status": "pending"},
        })
        if created and not self._stopping:
            self._owner_notice_task = asyncio.create_task(
                self._send_superseded_notice(state), name="clawchat-install-owner-notice",
            )
            self._owner_notice_tasks.add(self._owner_notice_task)
            self._owner_notice_task.add_done_callback(self._owner_notice_tasks.discard)
        await self._set_state(ConnectionState.SUPERSEDED)
        self._log_install_state("install_superseded", logging.ERROR, state)

    async def _send_superseded_notice(self, state: dict[str, Any]) -> None:
        from clawchat_gateway.owner_language import resolve_owner_language_if_known
        from clawchat_gateway.standalone_send import standalone_send

        def still_current() -> bool:
            return install_identity.event_is_current(self._profile_home, state["superseded_at"])

        try:
            chat_id = self._store.get_activation_conversation(
                platform="hermes", account_id="default",
            ) if self._store is not None else None
            if not still_current():
                result = {"status": "skipped", "reason": "reclaimed"}
            elif not chat_id:
                result = {"status": "skipped", "reason": "missing_owner_chat_id"}
            else:
                locale = self._owner_locale() if self._owner_locale is not None else None
                language = resolve_owner_language_if_known(locale)
                host = socket.gethostname()
                en = (f"This agent is now online at another installation. This installation on {host} "
                      "has stopped connecting to avoid mutual takeovers. To switch back here, run "
                      "`hermes clawchat reclaim` locally, or send /clawchat-reclaim in this machine's Hermes.")
                zh = (f"这个 agent 已在另一处安装上线，本机（{host}）上的这份安装已停止连接以避免互相顶号。"
                      "如需切回本机，在本机运行 `hermes clawchat reclaim`，"
                      "或在本机的 Hermes 里发送 /clawchat-reclaim。")
                version = state.get("by_client_version")
                if isinstance(version, str):
                    version = "".join(char for char in version if char.isprintable())[:64]
                    if version:
                        en += f" The other installation runs plugin version {version}."
                        zh += f"另一处安装的插件版本为 {version}。"
                text = zh if language in ("zh", "zh_Hant") else en if language else en + "\n" + zh
                # An out-of-process reclaim is only noticed by the 30 s standby
                # poll, so the sender re-checks the event right before sending.
                sent = await asyncio.wait_for(standalone_send(
                    self._cfg, str(chat_id), text, _allow_superseded_notice=True,
                    _still_current=still_current,
                ), timeout=HANDSHAKE_TIMEOUT_SECONDS + 10.0)
                result = ({"status": "sent", "message_id": sent.get("message_id")} if sent.get("success")
                          else {"status": "skipped", "reason": "reclaimed"} if sent.get("stale")
                          else {"status": "failed", "error_type": "SendError"})
        except Exception as exc:  # noqa: BLE001 — deliberately no retry
            result = {"status": "failed", "error_type": type(exc).__name__}
        # A reclaim or a later event must never be overwritten by this background task.
        install_identity.update_owner_notice(self._profile_home, state["superseded_at"], result)

    async def wait_until_ready(self, *, timeout: float) -> bool:
        if self.is_ready:
            return True
        try:
            # STANDBY and SUPERSEDED are settled waits, not startup failures.
            # Callers inspect their corresponding properties after this wakes.
            await asyncio.wait_for(self._settled_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return self.is_ready

    async def _set_state(self, state: ConnectionState) -> None:
        if self._state == state:
            return
        self._state = state
        if state in {ConnectionState.READY, ConnectionState.STANDBY, ConnectionState.SUPERSEDED}:
            self._settled_event.set()
        else:
            self._settled_event.clear()
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
            if not await self._wait_for_instance_locks(profile_only=True):
                break
            if not await self._wait_if_superseded():
                break
            if self._activation_source_suspended:
                if not await self._wait_for_activation_source():
                    break
            if (
                not startup_refresh_done
                and not self._using_activation_db_credentials
                and self._load_startup_activation_credentials()
            ):
                logger.info(
                    format_ws_log(
                        event="activation_loaded",
                        account_id=self._account_id,
                        attempt=self._attempt,
                        reconnect_count=self._reconnect_count,
                        state=self._state.value,
                        action="connect",
                        fields=[("source", "sqlite_startup")],
                    )
                )
            if not self._refresh_credentials_usable(self._cfg.refresh_token):
                await self._enter_auth_failed()
            if not self._has_connect_credentials():
                loaded = await self._wait_for_activation_credentials()
                if not loaded:
                    break
                # _wait_for_activation_credentials already applied §A.4.
                startup_refresh_done = True
            elif not startup_refresh_done:
                if not await self._wait_for_instance_locks():
                    break
                # The holder may have rotated credentials while we waited.
                if self._take_instance_lock_waited():
                    source_status = await self._recheck_activation_source(include_bootstrap=True)
                    if source_status in ("changed", "revoked") or self._activation_source_suspended:
                        continue
                if not self._instance_locks_ready():
                    continue
                self._clear_refresh_rejection()
                # §A.4 startup path (sqlite-first load or env bootstrap):
                # refresh-if-near-expiry before the FIRST connect (the polling
                # wait-for-activation path applies its own §A.4).
                startup_refresh_done = True
                if (
                    self._refresh_manager is not None
                    and self._cfg.refresh_token
                    and is_token_near_expiry(self._cfg.token, self._activated_at_ms)
                ):
                    outcome = await self._attempt_refresh(close_ws_on_success=False)
                    if (
                        outcome.status == "permanent"
                        or self._auth_failed
                        or self._activation_source_suspended
                        or not self._has_connect_credentials()
                    ):
                        # Refresh may have observed revocation; keep waiting.
                        self._refresh_pending_reconnect = False
                        continue
                    self._refresh_pending_reconnect = False
            await self._recheck_activation_source()
            if self._auth_failed or self._activation_source_suspended or not self._has_connect_credentials():
                continue
            if not await self._wait_for_instance_locks():
                break
            if self._take_instance_lock_waited():
                source_status = await self._recheck_activation_source(include_bootstrap=True)
                if source_status == "changed":
                    startup_refresh_done = False
                    continue
            if not self._instance_locks_ready() or self._auth_failed or self._activation_source_suspended or not self._has_connect_credentials():
                continue
            try:
                await self._set_state(ConnectionState.CONNECTING)
                self._last_close_code = None
                self._last_close_reason = None
                refusal_floor = 0.0
                try:
                    await self._run_one_connection()
                except Exception as exc:  # noqa: BLE001
                    # websockets raises ConnectionClosedError for non-1000/1001
                    # closes; a pre-hello 4002 instead times out the handshake.
                    if self._last_close_code not in {
                        CLOSE_CODE_REPLACED, CLOSE_CODE_DUPLICATE_THROTTLED,
                        CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL,
                    }:
                        raise
                    if self._last_close_code != CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL:
                        logger.warning(
                            format_ws_log(
                                event="connection_lost",
                                account_id=self._account_id,
                                attempt=self._attempt,
                                reconnect_count=self._reconnect_count,
                                state=self._state.value,
                                action="reconnect",
                                fields=[
                                    ("code", self._last_close_code),
                                    ("reason", self._safe_error_text(exc)),
                                ],
                            )
                        )
                if self._last_close_code == CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL:
                    await self._handle_install_superseded()
                    continue
                if self._last_close_code == CLOSE_CODE_REPLACED:
                    # Evicted by a newer session for this device. The
                    # stable-ready reset is deliberately NOT taken here: the
                    # eviction period always exceeds BACKOFF_RESET_AFTER_SECONDS,
                    # so resetting is precisely what keeps two supervisors
                    # kicking each other at the initial delay forever.
                    self._takeover_streak += 1
                    reconnect_reason = "replaced"
                elif self._last_close_code == CLOSE_CODE_DUPLICATE_THROTTLED:
                    # Refused by the opt-in duplicate-session guard. Not an
                    # eviction we lost, so leave the takeover streak alone; wait
                    # the server's retry_after_ms (it escalates it per refusal).
                    self._takeover_streak = 0
                    refusal_floor = refusal_retry_after_seconds(self._last_close_reason)
                    reconnect_reason = "duplicate_session_throttled"
                else:
                    self._takeover_streak = 0
                    if self._stable_ready_reset_done or self._refresh_pending_reconnect:
                        # §D: after a successful refresh closed the WS, reconnect
                        # immediately with the new token (reset backoff) even if the
                        # socket did not stay READY long enough for the stable-ready
                        # reset — a refresh close is a planned swap, not a fault.
                        delay_seconds = self._cfg.reconnect_initial_delay_ms / 1000.0
                        retries = 0
                    reconnect_reason = "-"
                self._refresh_pending_reconnect = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._takeover_streak = 0
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
            if self._state == ConnectionState.AUTH_FAILED or self._activation_source_suspended:
                continue
            retries += 1
            if retries > max_retries:
                break
            await self._set_state(ConnectionState.RECONNECTING)
            takeover_floor = self._takeover_backoff_floor()
            effective_delay = max(delay_seconds, takeover_floor, refusal_floor)
            jitter = random.uniform(0.0, effective_delay * self._cfg.reconnect_jitter_ratio)
            delay_with_jitter = effective_delay + jitter
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
                        ("takeover_streak", self._takeover_streak),
                        ("takeover_floor_ms", int(takeover_floor * 1000)),
                    ],
                )
            )
            await asyncio.sleep(delay_with_jitter)
            delay_seconds = min(delay_seconds * 2.0, max_delay_seconds)
        await self._set_state(ConnectionState.CLOSED)

    def _takeover_backoff_floor(self) -> float:
        """Minimum wait before reconnecting after consecutive takeover evictions.

        Zero when the last connection did not end in a takeover, so this never
        slows an ordinary reconnect.
        """
        return takeover_backoff_floor(self._takeover_streak)

    def _has_connect_credentials(self) -> bool:
        return bool(
            self._cfg.websocket_url
            and self._cfg.token
            and self._cfg.user_id
            and self._cfg.owner_user_id
        )

    def _refresh_rejection_fingerprints(self) -> set[str]:
        rejected = set()
        if self._rejected_refresh_fingerprint:
            rejected.add(self._rejected_refresh_fingerprint)
        if self._store is not None:
            try:
                persisted = self._store.get_revoked_refresh_fingerprint(
                    platform="hermes", account_id=self._account_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning("clawchat revoked-refresh guard read failed; using in-memory guard")
            else:
                if persisted:
                    rejected.add(persisted)
        return rejected

    def _refresh_credentials_usable(self, refresh_token: str | None) -> bool:
        rejected = self._refresh_rejection_fingerprints()
        if not rejected:
            return True
        if not refresh_token or not refresh_token.strip():
            return False
        return refresh_token_fingerprint(refresh_token) not in rejected

    def _clear_refresh_rejection(self) -> None:
        if not self._refresh_rejection_fingerprints():
            return
        if self._store is not None:
            try:
                if not self._store.clear_revoked_refresh_token(
                    platform="hermes", account_id=self._account_id,
                ):
                    logger.warning("clawchat revoked-refresh marker clear returned false")
            except Exception:  # noqa: BLE001
                logger.warning("clawchat revoked-refresh marker clear failed")
        self._rejected_refresh_fingerprint = None

    async def _enter_auth_failed(self, *, reason: str = AUTO_LOGOUT_LAST_ERROR) -> None:
        """Revocation and permanent refresh paths wait for a fresh activation."""
        self._clear_pending_permanent()
        self._credential_generation += 1
        self._rejected_activation_token = self._cfg.token or self._rejected_activation_token
        self._cfg = replace(self._cfg, token="", refresh_token="", user_id="", owner_user_id="")
        self._using_activation_db_credentials = False
        self._activation_source_suspended = False
        self._credential_read_failures = 0
        self._refresh_pending_reconnect = False
        self._auth_failed = True
        await self._set_state(ConnectionState.AUTH_FAILED)
        self._finish_current_connection(ConnectionState.AUTH_FAILED.value, error=reason)
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.set_result(False)
        if self._ws is not None:
            try:
                await asyncio.wait_for(self._ws.close(), timeout=_WS_CLOSE_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001
                logger.warning("clawchat auth-failed socket close failed")

    async def _revoke_activation_credentials(self) -> None:
        reason = "activation credentials deleted"
        logger.warning(
            format_ws_log(
                event="activation_credentials_revoked",
                account_id=self._account_id,
                attempt=self._attempt,
                reconnect_count=self._reconnect_count,
                state=self._state.value,
                action="wait_activation",
                fields=[("reason", reason)],
            )
        )
        await self._enter_auth_failed(reason=reason)

    def _load_startup_activation_credentials(self) -> bool:
        """SQLite-first startup: adopt the activation row when one exists.

        The activations row is the durable credential source of truth
        (activation + token rotation land there), so at startup it takes
        precedence over env/extra credentials. env/extra only bootstrap the
        no-row case — they connect once and get seeded into a row on the
        first refresh (§C.2). One-shot and non-blocking: the polling wait
        path stays in `_wait_for_activation_credentials`.
        """
        if self._store is None:
            return False
        try:
            credentials = self._store.get_activation_credentials(
                platform="hermes",
                account_id=self._account_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat activation credential read failed", exc_info=True)
            return False
        self._credential_read_failures = 0
        if credentials is None:
            return False
        if (
            credentials.access_token == self._rejected_activation_token
            or not self._refresh_credentials_usable(credentials.refresh_token)
        ):
            return False
        self._apply_activation_credentials(credentials)
        return True

    async def _wait_for_activation_credentials(self) -> bool:
        if not await self._wait_for_instance_locks(profile_only=True):
            return False
        if self._auth_failed or self._rejected_activation_token or self._refresh_rejection_fingerprints():
            await self._set_state(ConnectionState.AUTH_FAILED)
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
            if self._activation_source_suspended:
                if not await self._wait_for_activation_source():
                    return False
            credentials = None
            if self._store is not None:
                try:
                    credentials = self._store.get_activation_credentials(
                        platform="hermes",
                        account_id=self._account_id,
                    )
                    self._credential_read_failures = 0
                except Exception:  # noqa: BLE001
                    logger.warning("clawchat activation credential read failed", exc_info=True)
            if credentials is not None:
                if (
                    credentials.access_token == self._rejected_activation_token
                    or not self._refresh_credentials_usable(credentials.refresh_token)
                ):
                    await self._set_state(ConnectionState.AUTH_FAILED)
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
                self._apply_activation_credentials(credentials)
                if not await self._wait_for_instance_locks():
                    return False
                # Re-read after standby before using a potentially rotated pair.
                if self._take_instance_lock_waited():
                    status = await self._recheck_activation_source()
                    if status in ("changed", "revoked") or self._activation_source_suspended:
                        continue
                if not self._instance_locks_ready():
                    continue
                self._activation_wait_logged = False
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
                    if (
                        outcome.status == "permanent"
                        or self._auth_failed
                        or self._activation_source_suspended
                        or not self._has_connect_credentials()
                    ):
                        # Refresh may have observed revocation; keep polling.
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
            # Observe deletion/rotation before refreshing with the old pair.
            status = await self._recheck_activation_source(include_bootstrap=True)
            if status in ("changed", "revoked") or self._activation_source_suspended:
                return
            if status == "unreadable":
                continue
            # §A.1: no row is normal for env/extra bootstrap until refresh (§C.2).
            try:
                await self._maybe_proactive_refresh()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning("clawchat proactive refresh failed", exc_info=True)
            if self._state != ConnectionState.READY:
                return

    async def reactive_refresh(self) -> RefreshOutcome:
        """§A.2.1: run one single-flight refresh for a REST 401/403 retry.

        Shares the same single-flight ``RefreshManager`` as proactive/hello-fail
        refreshes (so concurrent REST 401s + a proactive timer coalesce into one
        HTTP refresh). On success the in-memory token is swapped (the caller can
        rebuild its api client from ``self.config.token`` and retry); on permanent
        the manager already cleared creds + emitted the user message and the
        connection closes the WS and drops to wait-for-activation. Successful
        REST refreshes leave socket continuation to the proactive/hello-fail paths.
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
        if self._allow_superseded_notice or self._install_is_blocked():
            return RefreshOutcome(status="skipped", error="installation superseded; run hermes clawchat reclaim")
        if not self._instance_locks_ready():
            return RefreshOutcome(status="skipped", error="instance locks not held")
        if self._activation_source_suspended:
            return RefreshOutcome(status="skipped", error="activation credential source suspended")
        source_status = await self._recheck_activation_source()
        if not self._instance_locks_ready():
            return RefreshOutcome(status="skipped", error="instance locks not held")
        if source_status in ("changed", "revoked") or self._activation_source_suspended:
            return RefreshOutcome(status="skipped", error="activation credential source changed")
        if self._allow_superseded_notice or self._install_is_blocked():
            return RefreshOutcome(status="skipped", error="installation superseded")
        manager = self._refresh_manager
        if not self._refresh_credentials_usable(self._cfg.refresh_token):
            await self._enter_auth_failed()
            return RefreshOutcome(status="permanent", error="unusable refresh credentials")
        if manager is None:
            return RefreshOutcome(status="skipped", error="no refresh manager")
        if self._auth_failed or not self._cfg.refresh_token:
            return RefreshOutcome(status="skipped", error="no refresh credentials")
        token = self._cfg.token
        refresh_token = self._cfg.refresh_token or None
        refresh_task = asyncio.create_task(manager.refresh(access_token=token, refresh_token=refresh_token))
        try:
            outcome = await asyncio.shield(refresh_task)
        except asyncio.CancelledError:
            if not self._install_is_blocked():
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)
                raise
            # Terminal WS teardown cancels its credential watcher. Let an HTTP
            # rotation already in progress finish persisting (the manager writes
            # both stores), then honour the cancellation; reclaim re-reads them.
            await asyncio.gather(refresh_task, return_exceptions=True)
            raise
        superseded = self._install_is_blocked()
        if superseded and outcome.status != "success":
            self._refresh_pending_reconnect = False
            return RefreshOutcome(status="skipped", error="installation superseded")
        if self._activation_source_suspended:
            return RefreshOutcome(status="skipped", error="activation credential source suspended")
        if (
            outcome.status == "success"
            and not self._auth_failed
            and (self._cfg.token != token or self._cfg.refresh_token != refresh_token)
            and self._cfg.token == outcome.access_token
            and self._cfg.refresh_token == outcome.refresh_token
        ):
            # Another single-flight waiter already adopted this result.
            if superseded:
                self._refresh_pending_reconnect = False
                return RefreshOutcome(status="skipped", error="rotation saved while installation superseded")
            self._refresh_pending_reconnect = True
            if close_ws_on_success and self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
            return outcome
        if (
            self._auth_failed
            or self._cfg.token != token
            or self._cfg.refresh_token != refresh_token
        ):
            # Revocation/activation may have happened while awaiting the HTTP
            # result or a coalesced refresh. Never restore the discarded pair.
            return RefreshOutcome(status="skipped", error="refresh credentials changed")
        if outcome.status == "success":
            # Persist already succeeded (§0). Swap in-memory token only now.
            self._credential_generation += 1
            self._cfg = replace(
                self._cfg,
                token=outcome.access_token,
                refresh_token=outcome.refresh_token,
            )
            # §C.2: move the process onto the SQLite-credentials recovery path.
            self._using_activation_db_credentials = True
            if outcome.access_token != self._rejected_activation_token:
                self._rejected_activation_token = None
            self._refresh_pending_reconnect = not superseded
            self._activated_at_ms = int(time.time() * 1000)
            logger.info(
                format_ws_log(
                    event="token_refreshed",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=self._state.value,
                    action="wait" if superseded else "reconnect",
                    fields=[("close_ws", close_ws_on_success and not superseded)],
                )
            )
            if superseded:
                return RefreshOutcome(status="skipped", error="rotation saved while installation superseded")
            if close_ws_on_success and self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:  # noqa: BLE001
                    pass
        elif outcome.status == "permanent":
            # §C: refresh token permanently invalid → auto-logout. Drop creds and
            # latch the dead token so wait-for-activation does not re-load it.
            await self._complete_permanent_logout()
        return outcome

    async def _finish_pending_permanent(self) -> bool:
        pair = self._pending_permanent_credentials
        if pair is None or pair != (self._cfg.token, self._cfg.refresh_token):
            return False
        expected = self._cfg
        generation = self._credential_generation
        reason = self._pending_permanent_reason or AUTO_LOGOUT_LAST_ERROR
        if not await self._persist_auth_logout(
            reason, expected_credentials=expected, completing_pending=True,
        ):
            return False
        # Notification callbacks may adopt a new activation while we await.
        if (
            self._stopping or self._auth_failed
            or self._credential_generation != generation
            or self._pending_permanent_credentials != pair
            or (self._cfg.token, self._cfg.refresh_token) != pair
        ):
            return False
        if self._refresh_manager is not None:
            # The suspended HTTP result was skipped by the manager. Complete
            # its adopted-permanent latch without issuing another HTTP request.
            self._refresh_manager.latch_adopted_permanent(*pair)
        await self._complete_permanent_logout()
        return True

    async def _complete_permanent_logout(self) -> None:
        await self._enter_auth_failed()
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

    async def _maybe_proactive_refresh(self) -> None:
        """§A.1: when the live token nears expiry, refresh + reconnect."""
        if self._activation_source_suspended or self._refresh_manager is None or not self._cfg.refresh_token:
            return
        if self._proactive_token != self._cfg.token:
            self._proactive_jitter_offset = self._proactive_jitter()
            self._proactive_token = self._cfg.token
        if not is_token_near_expiry(self._cfg.token, self._activated_at_ms,
                                    jitter=self._proactive_jitter_offset):
            return
        await self._attempt_refresh(close_ws_on_success=True)

    def _connection_credentials_current(self) -> bool:
        return (
            not self._stopping
            and not self._auth_failed
            and not self._activation_source_suspended
            and (
                self._connection_credential_generation is None
                or self._connection_credential_generation == self._credential_generation
            )
        )

    async def _close_stale_connection(self, ws: Any) -> None:
        if self._hello_wait is not None and not self._hello_wait.done():
            self._hello_wait.set_result(False)
        try:
            await asyncio.wait_for(ws.close(), timeout=_WS_CLOSE_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat stale socket close failed")

    async def _run_one_connection(self) -> bool:
        if self._install_is_blocked() and not self._allow_superseded_notice:
            return False
        if not self._instance_locks_ready():
            return False
        # A source check can invalidate this attempt while connect/hello awaits.
        self._connection_credential_generation = self._credential_generation
        self._connection_token = self._cfg.token
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
                    ("url", redact_display_text(self._cfg.websocket_url, self._cfg.token)),
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
        try:
            if not self._connection_credentials_current():
                return False
            self._ws = ws
            self._pending_connect_id = None
            self._hello_resolved_device_id = None
            self._hello_delivery_mode = None
            await self._set_state(ConnectionState.HANDSHAKING)
            if not self._connection_credentials_current():
                return False

            self._hello_wait = loop.create_future()
            self._read_task = asyncio.create_task(self._read_loop(ws), name="clawchat-read")
            hello_ok = await asyncio.wait_for(
                self._hello_wait,
                timeout=HANDSHAKE_TIMEOUT_SECONDS,
            )
            if not hello_ok or not self._connection_credentials_current():
                return False
            await self._set_state(ConnectionState.READY)
            if not self._connection_credentials_current():
                return False
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
                await asyncio.wait_for(ws.close(), timeout=_WS_CLOSE_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001
                pass
            # Read AFTER close() so the code is settled. This is what tells a
            # takeover eviction apart from an ordinary drop.
            self._last_close_code = getattr(ws, "close_code", None)
            self._last_close_reason = getattr(ws, "close_reason", None)
            if self._last_close_code == CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL:
                # Latch before any task can attempt refresh after teardown.
                self._latch_superseded()
            self._ws = None
            self._read_task = None
            self._connection_credential_generation = None
            self._connection_token = None
            if not self._stopping and not self._auth_failed and not self._activation_source_suspended:
                await self._set_state(ConnectionState.DISCONNECTED)
                self._reject_pending_acks(RuntimeError("connection disconnected"))
            if self._auth_failed:
                self._finish_current_connection(ConnectionState.AUTH_FAILED.value)
            elif self._stopping:
                self._finish_current_connection(ConnectionState.CLOSED.value)
            elif self._activation_source_suspended:
                self._finish_current_connection(ConnectionState.SUSPENDED.value)
            else:
                self._finish_current_connection(ConnectionState.DISCONNECTED.value)
        if not self._stopping and not self._auth_failed and not self._activation_source_suspended:
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
                queued = self._send_queue[0]
                if self._chat_is_rejected(queued.chat_id):
                    # The reconnect flush is the incident's actual shape: frames
                    # queued while offline for a conversation that died in the
                    # meantime. This path never goes through send_frame, so the
                    # gate has to be repeated here.
                    self._log_send_dropped(queued, reason="chat_rejected")
                    self._send_queue.popleft()
                    if queued.ack_timeout_task is not None:
                        queued.ack_timeout_task.cancel()
                    if queued.ack_future is not None and not queued.ack_future.done():
                        # Wake the send_frame coroutine still parked on this
                        # future so it returns False instead of hanging forever.
                        queued.ack_future.set_exception(
                            _ChatRejectedError(f"chat rejected: {queued.chat_id}")
                        )
                    continue
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
                            ("remaining", len(self._send_queue) - 1),
                        ],
                    )
                )
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
        try:
            await self._read_frames(ws)
        finally:
            if getattr(ws, "close_code", None) == CLOSE_CODE_SUPERSEDED_BY_OTHER_INSTALL:
                self._latch_superseded()
                if self._hello_wait is not None and not self._hello_wait.done():
                    self._hello_wait.set_result(False)

    async def _read_frames(self, ws: Any) -> None:
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
            # §9.4 reliable system notification. Dedup by event_id so the live
            # frame and its reliable-inbox replay collapse to one observation.
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
            # Forward freshly-observed signals to the adapter callback so it can
            # react to specific signal types (e.g. agent.config.changed).
            if outcome == "observed" and self._on_notify_signal is not None:
                try:
                    await self._on_notify_signal(frame)
                except Exception:  # noqa: BLE001
                    logger.exception("on_notify_signal raised")
            return
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "replay.done":
            # §11.5 terminal control frame: device replay drained, live begins.
            # Fires on every reconnect (even zero-backlog). Replayed messages are
            # handed to their chat lanes like live ones, so this is a logged
            # boundary marker, not a gate.
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
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "history.truncated":
            # §11.7 replay-start boundary frame: part of the retained history
            # will not be replayed to this device. This plugin advertises no
            # reliable-delivery flag, so the server does not send it today; if
            # one arrives it is logged explicitly. It carries no seq/dseq, so it
            # is never acked, and it is never dispatched to the agent. An absent
            # or unrecognised `reason` means "pruned"; `oldest_seq` is opaque.
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            raw_reason = payload.get("reason")
            known_reason = raw_reason in ("pruned", "cursor_started_above_zero")
            oldest_seq = payload.get("oldest_seq")
            fields: list[tuple[str, Any]] = [
                ("event_name", frame.get("event")),
                ("trace_id", frame.get("trace_id") or frame.get("id")),
                ("oldest_seq", oldest_seq if isinstance(oldest_seq, int) and not isinstance(oldest_seq, bool) else None),
                ("reason", raw_reason if known_reason else "pruned"),
            ]
            if not known_reason and isinstance(raw_reason, str) and raw_reason:
                fields.append(("raw_reason", raw_reason))
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="history_truncated",
                    fields=fields,
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
        if self._state == ConnectionState.READY and ftype in (None, "event") and frame.get("event") == "message.recall":
            # A message its sender took back (client-integration.md §9.8).
            # Ids only — the frame never carries the recalled text — and
            # `payload.message_id` is the server's `rcl:<target>` slot, NOT the
            # id to delete.
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            logger.info(
                format_ws_log(
                    event="inbound_control",
                    account_id=self._account_id,
                    attempt=self._attempt,
                    reconnect_count=self._reconnect_count,
                    state=ConnectionState.READY.value,
                    action="recall",
                    fields=[
                        ("event_name", frame.get("event")),
                        ("trace_id", frame.get("trace_id") or frame.get("id")),
                        ("chat_id", frame.get("chat_id")),
                        ("target_message_id", payload.get("target_message_id")),
                    ],
                )
            )
            # Same lane as the chat's messages, so a recall is never handled
            # before the message it takes back. The lane guards exceptions: a
            # purge that raises must cost one frame, not the connection.
            self._dispatch_to_lane(frame)
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
            self._dispatch_to_lane(frame)
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

    def _dispatch_to_lane(self, frame: dict[str, Any]) -> None:
        """Hand an inbound message frame to its chat's lane; never awaits it.

        The read loop is the only reader of ``message.ack``. Handling a message
        can send a reply and WAIT for its ack — the plugin's own replies to a
        non-owner's confirm, and every command Hermes dispatches inline (an
        approval ``yes``, a group ``/new``). Awaited here, that ack could never
        be read: the send timed out, the read loop died, the connection was
        rebuilt and frames that arrived meanwhile were lost. So each chat gets
        a worker task: frames of one chat are still handled strictly in order,
        one at a time, while acks and other chats keep flowing.
        """
        chat_id = str(frame.get("chat_id") or "")
        lane = self._inbound_lanes.get(chat_id)
        if lane is None:
            lane = _InboundLane(frames=deque())
            self._inbound_lanes[chat_id] = lane
        lane.frames.append(frame)
        if lane.task is None:
            lane.task = asyncio.create_task(
                self._drain_inbound_lane(chat_id, lane), name="clawchat-inbound"
            )

    async def _drain_inbound_lane(self, chat_id: str, lane: _InboundLane) -> None:
        try:
            while lane.frames:
                frame = lane.frames.popleft()
                try:
                    await self._on_message(frame)
                except Exception:  # noqa: BLE001
                    # One bad frame must not stop this chat's later frames.
                    logger.exception(
                        "clawchat inbound handling raised event=%s chat_id=%s trace_id=%s",
                        frame.get("event"),
                        chat_id,
                        frame.get("trace_id") or frame.get("id"),
                    )
        finally:
            lane.task = None
            if not lane.frames and self._inbound_lanes.get(chat_id) is lane:
                del self._inbound_lanes[chat_id]

    async def _cancel_inbound_lanes(self) -> None:
        """Cancel every lane worker and drop queued frames (shutdown only).

        The caller's own task is spared: a handler may stop the connection
        itself (e.g. a ``/restart`` dispatched inline) and must not cancel
        itself halfway through.
        """
        current = asyncio.current_task()
        lanes = list(self._inbound_lanes.values())
        self._inbound_lanes.clear()
        tasks = []
        for lane in lanes:
            lane.frames.clear()
            if lane.task is not None and lane.task is not current and not lane.task.done():
                lane.task.cancel()
                tasks.append(lane.task)
        if tasks:
            await asyncio.wait(tasks, timeout=_WS_CLOSE_TIMEOUT_SECONDS)

    async def _handle_challenge(self, frame: dict[str, Any]) -> None:
        if not self._connection_credentials_current():
            if self._ws is not None:
                await self._close_stale_connection(self._ws)
            return
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
        device_id = self._connect_device_id or self._refresh_device_id()
        # Persist the resolved device id (e.g. the token's `did` for env-booted
        # agents) onto the activations row so it survives container recreation
        # and is observable. Only-if-empty: never clobbers a connect-code value.
        # An override is a sibling session id, never the canonical device id —
        # persisting it would poison refresh (§E), so skip the backfill.
        if self._store is not None and device_id and self._connect_device_id is None:
            try:
                self._store.set_activation_device_id(
                    platform="hermes",
                    account_id=self._account_id,
                    device_id=device_id,
                )
            except Exception:  # noqa: BLE001 — best-effort, must not block connect
                logger.debug("clawchat device id backfill failed")
        install_id = (install_identity.get_install_id(self._profile_home)
                      if not self._connect_device_id else None)
        connect_req = build_connect_request(
            frame_id=req_id,
            token=self._connection_token if self._connection_token is not None else self._cfg.token,
            nonce=nonce,
            device_id=device_id,
            install_id=install_id,
            capabilities=build_connect_capabilities(install_conflict=bool(install_id)),
            client_version=__version__,
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
            if not self._connection_credentials_current():
                if self._ws is not None:
                    await self._close_stale_connection(self._ws)
                return
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            device_id = payload.get("device_id")
            delivery_mode = payload.get("delivery_mode")
            self._hello_resolved_device_id = device_id if isinstance(device_id, str) else None
            self._hello_delivery_mode = delivery_mode if isinstance(delivery_mode, str) else None
            await self._set_state(ConnectionState.READY)
            if not self._connection_credentials_current():
                if self._ws is not None:
                    await self._close_stale_connection(self._ws)
                return
            if self._hello_wait is not None and not self._hello_wait.done():
                self._hello_wait.set_result(True)
            return
        if frame.get("event") == "hello-fail":
            if not self._connection_credentials_current():
                # A late hello-fail for a superseded attempt must not reject or
                # clear the credentials that replaced it; just drop the socket.
                if self._ws is not None:
                    await self._close_stale_connection(self._ws)
                return
            payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
            raw_reason = payload.get("reason") if isinstance(payload.get("reason"), str) else None
            # Classify on the RAW reason and log the sanitized one. The redactor
            # rewrites any occurrence of the token inside the text, which would
            # silently break the §3.5 exact-equality match on
            # "authentication failed" and turn a terminal failure into an
            # endless backoff loop.
            reason = self._sanitize_secret_text(raw_reason)
            frame_trace_id = frame.get("trace_id")
            trace_id_match = bool(
                self._pending_connect_id and frame_trace_id == self._pending_connect_id
            )
            if _is_transient_auth_failure(raw_reason):
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
            token_rejected = _is_token_rejected(raw_reason)
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
                # Permanent handling is shared with startup, proactive and REST paths.
                return
            if not token_rejected:
                # §3.5 forward-compat: the reason is not the exact terminal
                # string, so the token is NOT known to be rejected — this is a
                # nonce mismatch, a malformed connect, or a reason msghub added
                # after this client shipped. Backoff-reconnect with the same
                # token instead of tearing the account down; a terminal branch
                # here would force the owner to re-pair after an ordinary
                # handshake hiccup. (Refresh was either not eligible or not
                # available; either way the credentials stay untouched.)
                logger.warning(
                    format_ws_log(
                        event="hello_fail_transient",
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
            using_activation_db_credentials = self._using_activation_db_credentials
            self._clear_pending_permanent()
            self._credential_generation += 1
            self._auth_failed = True
            if not using_activation_db_credentials:
                self._stopping = True
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
            self._finish_current_connection(
                ConnectionState.AUTH_FAILED.value,
                error=reason,
            )
            hello_wait, ws = self._hello_wait, self._ws
            await self._set_state(ConnectionState.AUTH_FAILED)
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
            if hello_wait is not None and not hello_wait.done():
                hello_wait.set_result(False)
            if ws is not None:
                try:
                    await ws.close()
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

    def _chat_is_rejected(self, chat_id: str) -> bool:
        return (
            self._is_chat_rejected is not None
            and bool(chat_id)
            and self._is_chat_rejected(chat_id)
        )

    def _log_invalid_chat_id(self, queued: _QueuedFrame, raw_chat_id: Any) -> None:
        """Surface a locally-detectable addressing bug at WARNING.

        `_log_send_dropped` alone is INFO and shaped for routine drops; this one
        names the offending value so the caller that composed it can be found
        without correlating against msghub's logs. It logs the RAW value rather
        than `queued.chat_id`, which coerces `None` to "" and would hide an
        explicit null behind an empty string.
        """
        logger.warning(
            "clawchat outbound frame dropped: chat_id is not a ClawChat "
            "conversation id event=%s chat_id=%r trace_id=%s "
            "expected=%s+%d Crockford base32 chars",
            queued.event_name,
            raw_chat_id,
            queued.trace_id,
            CHAT_ID_PREFIX,
            IDCODE_BODY_LENGTH,
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
        if self._on_message_error is not None:
            asyncio.create_task(self._notify_message_error(frame))
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

    async def _notify_message_error(self, frame: dict[str, Any]) -> None:
        assert self._on_message_error is not None
        try:
            await self._on_message_error(frame)
        except Exception:  # noqa: BLE001 — a callback must never break ack handling
            logger.exception("clawchat on_message_error callback failed")

    def _message_error_code(self, payload: dict[str, Any]) -> str:
        """Return ``payload.code`` verbatim.

        Deliberately NOT routed through ``_message_error_reason``: that helper
        ranks ``code`` last, so a non-empty ``reason`` (which the server always
        sends) shadows it. It also sanitizes secrets — irrelevant for a fixed
        protocol token and capable of mangling it.
        """
        value = payload.get("code")
        return value if isinstance(value, str) else ""

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
        try:
            if url_has_userinfo(self._cfg.base_url) or url_has_userinfo(self._cfg.websocket_url):
                return DIAGNOSTIC_WITHHELD
            token = self._cfg.token
            if token:
                text = text.replace(token, "***")
            return redact_display_text(text)
        except Exception:  # noqa: BLE001 - fail closed
            return DIAGNOSTIC_REDACTION_FAILED

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
