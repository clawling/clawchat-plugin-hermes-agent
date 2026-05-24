"""ClawChatAdapter — BasePlatformAdapter for the ClawChat WebSocket protocol."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import (
    HANDSHAKE_TIMEOUT_SECONDS,
    ClawChatConnection,
    ConnectionState,
)
from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
from clawchat_gateway.inbound import InboundMessage, parse_inbound_message
from clawchat_gateway.media_runtime import (
    download_inbound_media,
    infer_media_kind_from_mime,
    normalize_outbound_media_reference,
    upload_outbound_media,
)
from clawchat_gateway.plugin_prompts import mode_prompt
from clawchat_gateway.profile_sync import relation_for_sender
from clawchat_gateway.protocol import (
    build_message_add_event,
    build_message_created_event,
    build_message_done_event,
    build_message_failed_event,
    build_message_reply_event,
    build_typing_update_event,
    new_frame_id,
)
from clawchat_gateway.storage import get_clawchat_store
from clawchat_gateway.stream_buffer import compute_delta

logger = logging.getLogger("clawchat_gateway.adapter")
inbound_trace = logging.getLogger("clawchat_gateway.inbound_trace")

TYPING_REFRESH_SECONDS = 10.0
INBOUND_RATE_WINDOW_SECONDS = 30.0
INBOUND_RATE_WARN_THRESHOLD = 5
COMPLETED_RUN_CACHE_MAX = 1024
RECONNECT_REFRESH_LIMIT = 20
DEBUG_PROMPT_INJECTION_ENV = "CLAWCHAT_DEBUG_PROMPT_INJECTION"
DEBUG_PROMPT_INJECTION_BEGIN = "----- BEGIN CLAWCHAT DEBUG PROMPT INJECTION -----"
DEBUG_PROMPT_INJECTION_END = "----- END CLAWCHAT DEBUG PROMPT INJECTION -----"
DEBUG_EVENT_TEXT_BEGIN = "----- BEGIN CLAWCHAT DEBUG EVENT TEXT -----"
DEBUG_EVENT_TEXT_END = "----- END CLAWCHAT DEBUG EVENT TEXT -----"
DEBUG_HERMES_OUTPUT_BEGIN = "----- BEGIN CLAWCHAT DEBUG HERMES OUTPUT -----"
DEBUG_HERMES_OUTPUT_END = "----- END CLAWCHAT DEBUG HERMES OUTPUT -----"
SILENT_RESPONSE_TOKEN = "<clawchat:silent/>"
EMPTY_RESPONSE_TOKEN = '""'
CONVERSATION_SEMANTICS = """## ClawChat Conversation Semantics
- chat_type=dm means a direct message.
- chat_type=group means a group conversation.
- sender_id identifies who sent the current direct message or each group [message].
- sender_profile_type is the sender account type: user or agent.
- sender_is_owner tells whether the sender is this agent's owner.
- In group conversations, each [message] block has its own sender fields."""
GROUP_BATCH_REPLY_GUIDANCE = (
    "Reply only if one or more [message] blocks clearly ask for your participation, mention the current agent, "
    "or your response is clearly useful to the group and allowed by the group profile/regulation. "
    "Mentions of other people are not requests for you."
)
GROUP_BATCH_MENTION_REPLY_GUIDANCE = (
    "You were directly addressed in this group batch. Reply by default, including when the message contains only a mention. "
    "Stay silent only if the group profile/regulation explicitly forbids replying."
)
DIRECT_MESSAGE_REPLY_GUIDANCE = (
    "Direct messages are normally addressed to you. Reply unless the agent behavior says this message should not be answered."
)

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
_THINK_CONTENT_RE = re.compile(r"<think\b[^>]*>(.*?)</think>", re.IGNORECASE | re.DOTALL)
_THINK_OPEN_RE = re.compile(r"<think\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)
_TOOL_TAG_BLOCK_RE = re.compile(
    r"<(?:tool|tools|tool_call|tool_result|function_call|function_result)\b[^>]*>"
    r".*?</(?:tool|tools|tool_call|tool_result|function_call|function_result)>",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_TAG_OPEN_RE = re.compile(
    r"<(?:tool|tools|tool_call|tool_result|function_call|function_result)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_FENCE_BLOCK_RE = re.compile(
    r"```(?:tool|tools|tool_call|tool_result|function_call|function_result)[^\n`]*\n.*?```",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_FENCE_OPEN_RE = re.compile(
    r"```(?:tool|tools|tool_call|tool_result|function_call|function_result)[^\n`]*\n.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_TOOL_PROGRESS_LINE_RE = re.compile(
    r"^\s*(?:[^\w\s`]{1,4}\s*)?[A-Za-z_][\w.-]*(?:\([^)]*\))?"
    r"(?:\.\.\.|: \"|\n)",
)
# Hermes streams append a typing-cursor block character to every intermediate
# chunk's tail. Strip it so compute_delta's prefix check stays stable across
# chunks (otherwise every delta degrades to the full accumulated text).
_STREAMING_CURSOR_RE = re.compile(r"\s*[▀-▟]+\s*\Z")

_APPROVE_COMMAND_RE = re.compile(r"(?<!\w)/approve(?!\w)", re.IGNORECASE)
_DENY_COMMAND_RE = re.compile(r"(?<!\w)/(?:deny|reject)(?!\w)", re.IGNORECASE)
_HERMES_STREAM_CURSOR_RE = re.compile(r"[ \t]*▉\Z")
_CLAWCHAT_ACTIVATION_BOOTSTRAP_PROMPT = (
    "ClawChat activation bootstrap: You are now connected to this ClawChat direct conversation.\n\n"
    "Please do both:\n"
    "1. Send a brief, friendly greeting to the user in this ClawChat direct conversation.\n"
    "2. If you have local profile information for yourself, such as display name, bio, or avatar, "
    "update the connected ClawChat account profile using the available ClawChat tools. Use "
    "`clawchat_update_account_profile` for display name/bio/avatar URL, and use "
    "`clawchat_upload_avatar_image` first if the avatar is only available as a local image path. "
    "If you do not have local profile information, skip profile updates and only greet the user.\n\n"
    "Do not ask the user for profile information just for this bootstrap."
)


def _clawchat_platform():
    platform = getattr(Platform, "CLAWCHAT", None)
    if platform is not None:
        return platform
    return Platform("clawchat")


def _debug_prompt_injection_enabled() -> bool:
    return os.getenv(DEBUG_PROMPT_INJECTION_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass
class _ActiveRun:
    chat_id: str
    chat_type: str
    message_id: str
    started_order: int
    last_text: str = ""
    reply_to_message_id: str | None = None
    sequence: int = -1
    delivery_degraded: bool = False


def check_clawchat_requirements(platform_config: Any) -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        logger.warning("ClawChat: websockets library not installed")
        return False
    cfg = ClawChatConfig.from_platform_config(platform_config)
    if not cfg.websocket_url or not cfg.token:
        logger.warning(
            "ClawChat: websocket_url and token are required in platforms.clawchat.extra"
        )
        return False
    return True


class ClawChatAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    REQUIRES_EDIT_FINALIZE = True
    MAX_MESSAGE_LENGTH = 0

    def __init__(self, platform_config: Any) -> None:
        super().__init__(platform_config, _clawchat_platform())
        self._clawchat_config = ClawChatConfig.from_platform_config(platform_config)
        self._connection: Any = ClawChatConnection(
            self._clawchat_config,
            on_message=self._on_message,
            on_state_change=self._on_state_change,
            on_signal=self._on_signal,
        )
        self._active_runs_by_id: dict[str, _ActiveRun] = {}
        self._active_chat_runs: dict[str, str] = {}
        self._typing_state: dict[str, tuple[bool, float]] = {}
        self._run_counter = 0
        self._inbound_window: dict[str, deque[float]] = {}
        self._completed_run_ids: set[str] = set()
        self._completed_run_order: deque[str] = deque()
        self._auth_failed = False
        self._activation_bootstrap_tasks: set[asyncio.Task[None]] = set()
        self._conversation_refresh_tasks: set[asyncio.Task[None]] = set()
        self._profile_sync_tasks: set[asyncio.Task[None]] = set()
        self._conversation_metadata_versions: dict[str, int] = {}
        self._group_message_coalescer = GroupMessageCoalescer(
            idle_seconds=10.0,
            max_wait_seconds=30.0,
            dispatch=self._handle_inbound,
        )
        try:
            self._store = get_clawchat_store()
        except Exception:  # noqa: BLE001
            self._store = None
            logger.warning("clawchat adapter database unavailable")

    async def connect(self) -> bool:
        await self._connection.start()
        ready = await self._connection.wait_until_ready(
            timeout=HANDSHAKE_TIMEOUT_SECONDS + 1.0,
        )
        if not ready:
            logger.warning("clawchat connect returned before websocket ready")
        return ready

    async def disconnect(self) -> None:
        await self._cancel_activation_bootstrap_tasks()
        await self._cancel_conversation_refresh_tasks()
        await self._cancel_profile_sync_tasks()
        await self._connection.stop()
        await self._group_message_coalescer.cancel()

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "direct", "chat_id": chat_id}

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        chat_type = self._resolve_chat_type(metadata, {})
        if self._should_skip_typing(chat_id, active=True):
            logger.debug("clawchat typing active skipped chat_id=%s reason=already_active", chat_id)
            return
        await self._connection.send_frame(
            build_typing_update_event(
                chat_id=chat_id,
                chat_type=chat_type,
                active=True,
            ),
            queue_when_unready=False,
        )
        logger.info("clawchat typing active sent chat_id=%s chat_type=%s", chat_id, chat_type)

    async def stop_typing(self, chat_id: str, metadata: Any = None) -> None:
        chat_type = self._resolve_chat_type(metadata, {})
        if self._should_skip_typing(chat_id, active=False):
            logger.debug("clawchat typing inactive skipped chat_id=%s reason=already_inactive", chat_id)
            return
        await self._connection.send_frame(
            build_typing_update_event(
                chat_id=chat_id,
                chat_type=chat_type,
                active=False,
            ),
            queue_when_unready=False,
        )
        logger.info("clawchat typing inactive sent chat_id=%s chat_type=%s", chat_id, chat_type)

    def _should_skip_typing(self, chat_id: str, *, active: bool) -> bool:
        now = time.monotonic()
        current = self._typing_state.get(chat_id)
        if current is not None:
            was_active, last_sent_at = current
            if active and was_active and now - last_sent_at < TYPING_REFRESH_SECONDS:
                return True
            if not active and not was_active:
                return True
        self._typing_state[chat_id] = (active, now)
        return False

    async def _on_state_change(self, state: ConnectionState) -> None:
        if state == ConnectionState.AUTH_FAILED:
            self._auth_failed = True
        if state == ConnectionState.READY:
            self._schedule_activation_bootstrap()
            await self._schedule_reconnect_conversation_refresh()
        logger.info("clawchat state -> %s", state.value)

    async def _on_signal(self, frame: dict[str, Any]) -> None:
        if frame.get("event") != "chat.metadata.invalidated":
            return
        await self._handle_metadata_invalidated(frame)

    async def _schedule_reconnect_conversation_refresh(self) -> None:
        if self._store is None:
            return
        await self._cancel_conversation_refresh_tasks()
        task = asyncio.create_task(
            self._refresh_recent_conversations_after_ready(),
            name="clawchat-conversation-refresh",
        )
        self._conversation_refresh_tasks.add(task)
        task.add_done_callback(self._conversation_refresh_task_done)

    async def _cancel_conversation_refresh_tasks(self) -> None:
        tasks = list(self._conversation_refresh_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._conversation_refresh_tasks.discard(task)

    def _conversation_refresh_task_done(self, task: asyncio.Task[None]) -> None:
        self._conversation_refresh_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001
            logger.warning("clawchat reconnect conversation refresh failed", exc_info=True)

    async def _refresh_recent_conversations_after_ready(self) -> None:
        if self._store is None:
            return
        ids: list[str] = []
        try:
            activation_id = self._store.get_activation_conversation(
                platform="hermes",
                account_id="default",
            )
            if activation_id:
                ids.append(str(activation_id))
            cached_ids = self._store.list_cached_conversation_ids(
                platform="clawchat",
                account_id=self._account_id(),
                limit=RECONNECT_REFRESH_LIMIT * 2,
            )
            cached_seen: set[str] = set()
            for cached_id in cached_ids:
                if not cached_id or cached_id in cached_seen or cached_id in ids:
                    continue
                cached_seen.add(cached_id)
                ids.append(str(cached_id))
                if len(cached_seen) >= RECONNECT_REFRESH_LIMIT:
                    break
        except Exception:  # noqa: BLE001
            logger.warning("clawchat reconnect conversation list failed", exc_info=True)
            return
        seen: set[str] = set()
        for conversation_id in ids:
            if not conversation_id or conversation_id in seen:
                continue
            seen.add(conversation_id)
            await self._refresh_conversation_metadata(conversation_id)

    async def _handle_metadata_invalidated(self, frame: dict[str, Any]) -> None:
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        conversation_id = self._signal_conversation_id(frame, payload)
        if not conversation_id:
            logger.warning("clawchat metadata invalidation missing chat_id trace_id=%s", frame.get("trace_id"))
            return
        version = payload.get("version") or payload.get("metadata_version") or payload.get("metadataVersion")
        if isinstance(version, int):
            current_version = self._conversation_metadata_versions.get(conversation_id)
            if current_version is not None and version <= current_version:
                logger.info(
                    "clawchat metadata invalidation stale chat_id=%s version=%s current=%s",
                    conversation_id,
                    version,
                    current_version,
                )
                return
        scopes = self._signal_scopes(payload)
        signal_version = version if isinstance(version, int) else None
        needs_behavior = self._scope_needs_behavior(scopes)
        needs_conversation = self._scope_needs_conversation(scopes)
        required_results: list[bool] = []
        if needs_behavior:
            required_results.append(await self._refresh_agent_behavior(conversation_id))
        if needs_conversation:
            required_results.append(
                await self._refresh_conversation_metadata(
                    conversation_id,
                    advance_version=not needs_behavior,
                )
            )
        if required_results and all(required_results) and signal_version is not None:
            self._conversation_metadata_versions[conversation_id] = signal_version

    def _signal_scopes(self, payload: dict[str, Any]) -> list[str]:
        raw = payload.get("scope")
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, str)]
        return []

    def _scope_needs_behavior(self, scopes: list[str]) -> bool:
        return "behavior" in scopes

    def _scope_needs_conversation(self, scopes: list[str]) -> bool:
        return not scopes or any(scope != "behavior" for scope in scopes)

    def _signal_conversation_id(self, frame: dict[str, Any], payload: dict[str, Any]) -> str | None:
        for value in (
            payload.get("chat_id"),
            payload.get("conversation_id"),
            payload.get("conversationId"),
            frame.get("chat_id"),
        ):
            if isinstance(value, str) and value:
                return value
        return None

    async def _refresh_conversation_metadata(
        self,
        conversation_id: str,
        *,
        signal_version: int | None = None,
        advance_version: bool = True,
    ) -> bool:
        client = ClawChatApiClient(
            base_url=self._clawchat_config.base_url,
            token=self._clawchat_config.token,
            user_id=self._clawchat_config.user_id,
        )
        try:
            result = await client.get_conversation(conversation_id)
        except ClawChatApiError as exc:
            if exc.status in (404, 410) or exc.code in (404, 410):
                self._delete_conversation_cache(conversation_id)
                self._conversation_metadata_versions.pop(conversation_id, None)
                return False
            logger.warning(
                "clawchat metadata refresh failed chat_id=%s status=%s error=%s",
                conversation_id,
                exc.status,
                exc,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "clawchat metadata refresh failed chat_id=%s error=%s",
                conversation_id,
                exc,
            )
            return False
        if not self._cache_conversation_details(result, advance_version=advance_version):
            return False
        return True

    async def _refresh_agent_behavior(self, conversation_id: str) -> bool:
        agent_id = self._clawchat_config.agent_id
        if not agent_id:
            logger.warning(
                "clawchat behavior refresh skipped chat_id=%s reason=missing_agent_id",
                conversation_id,
            )
            return False
        client = ClawChatApiClient(
            base_url=self._clawchat_config.base_url,
            token=self._clawchat_config.token,
            user_id=self._clawchat_config.user_id,
        )
        try:
            result = await client.get_agent_detail(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "clawchat behavior refresh failed chat_id=%s agent_id=%s error=%s",
                conversation_id,
                agent_id,
                exc,
            )
            return False
        return self._cache_agent_profile(result, agent_user_id=self._clawchat_config.user_id)

    def _delete_conversation_cache(self, conversation_id: str) -> None:
        if self._store is None:
            return
        try:
            self._store.delete_conversation_cache(
                platform="clawchat",
                account_id=self._account_id(),
                conversation_id=conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat conversation cache delete failed", exc_info=True)

    def _cache_conversation_details(self, result: dict[str, Any], *, advance_version: bool = True) -> bool:
        if self._store is None:
            return False
        detail = result.get("conversation") if isinstance(result.get("conversation"), dict) else result
        if not isinstance(detail, dict):
            return False
        conversation_id = self._conversation_id(detail)
        if not conversation_id:
            return False
        metadata_version = self._metadata_version(detail)
        participants_raw = detail.get("participants")
        members_raw = participants_raw if isinstance(participants_raw, list) else detail.get("members")
        users_raw = detail.get("users") or detail.get("participants") or []
        members = [
            member
            for item in members_raw or []
            if isinstance(item, dict)
            for member in [self._member_from_raw(item)]
            if member
        ]
        user_profiles = [
            profile
            for item in users_raw or []
            if isinstance(item, dict)
            for profile in [self._profile_from_raw(item)]
            if profile
        ]
        for profile in user_profiles:
            profile["relation"] = self._sender_relation(
                str(profile.get("user_id") or ""),
                profile_type=profile.get("profile_type"),
            )
        members_complete = bool(
            isinstance(members_raw, list)
            and (
                isinstance(participants_raw, list)
                or detail.get("members_complete") is True
                or detail.get("participants_complete") is True
            )
        )
        try:
            self._store.upsert_conversation_details(
                platform="clawchat",
                account_id=self._account_id(),
                conversation_id=conversation_id,
                conversation_type=self._conversation_type(detail),
                metadata_version=metadata_version,
                last_seen_at=self._last_seen_at(detail),
                raw=detail,
                group_profile=self._group_profile_from_conversation(detail),
                user_profiles=user_profiles,
                members=members,
                members_complete=members_complete,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat conversation cache upsert failed", exc_info=True)
            return False
        if advance_version and metadata_version is not None:
            self._conversation_metadata_versions[conversation_id] = metadata_version
        return True

    def _cache_agent_profile(self, result: dict[str, Any], *, agent_user_id: str) -> bool:
        if self._store is None:
            return False
        detail = result.get("agent") if isinstance(result.get("agent"), dict) else result
        if not isinstance(detail, dict):
            return False
        profile_id = str(detail.get("user_id") or detail.get("userId") or detail.get("id") or agent_user_id)
        refreshed_at = self._last_seen_at(detail) or int(time.time() * 1000)
        try:
            kwargs = {
                "platform": "clawchat",
                "account_id": self._account_id(),
                "profile_kind": "agent",
                "profile_id": profile_id,
                "profile_type": str(detail.get("type") or "agent"),
                "raw": detail,
                "last_refreshed_at": refreshed_at,
            }
            if "behavior" in detail:
                kwargs["behavior"] = detail["behavior"]
            metadata_version = self._metadata_version(detail)
            if metadata_version is not None:
                kwargs["metadata_version"] = metadata_version
            self._store.upsert_profile(
                **kwargs,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat agent profile cache upsert failed", exc_info=True)
            return False
        return True

    def _cache_user_profile(self, result: dict[str, Any], *, user_id: str) -> bool:
        if self._store is None:
            return False
        detail = result.get("user") if isinstance(result.get("user"), dict) else result
        if not isinstance(detail, dict):
            return False
        profile = self._profile_from_raw({**detail, "id": detail.get("id") or user_id})
        if profile is None:
            return False
        profile_type = profile.get("profile_type")
        refreshed_at = self._last_seen_at(detail) or int(time.time() * 1000)
        try:
            kwargs = {
                "platform": "clawchat",
                "account_id": self._account_id(),
                "profile_kind": "user",
                "profile_id": str(profile["user_id"]),
                "relation": self._sender_relation(str(profile["user_id"]), profile_type=profile_type),
                "raw": detail,
                "last_refreshed_at": refreshed_at,
            }
            if "profile_type" in profile:
                kwargs["profile_type"] = profile["profile_type"]
            for key in ("nickname", "avatar_url", "bio"):
                if key in profile:
                    kwargs[key] = profile[key]
            self._store.upsert_profile(**kwargs)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat user profile cache upsert failed", exc_info=True)
            return False
        return True

    def _account_id(self) -> str:
        return str(self._clawchat_config.user_id or "")

    def _conversation_id(self, value: dict[str, Any]) -> str | None:
        raw = value.get("conversation_id") or value.get("conversationId") or value.get("id")
        return str(raw) if raw else None

    def _conversation_type(self, value: dict[str, Any]) -> str | None:
        raw = value.get("conversation_type") or value.get("conversationType") or value.get("type")
        return str(raw) if raw else None

    def _metadata_version(self, value: dict[str, Any]) -> int | None:
        raw = value.get("metadata_version") or value.get("metadataVersion")
        return raw if isinstance(raw, int) else None

    def _last_seen_at(self, value: dict[str, Any]) -> int | None:
        for key in ("last_seen_at", "lastSeenAt", "updated_at", "created_at"):
            raw = value.get(key)
            if isinstance(raw, int):
                return raw
        return None

    def _group_profile_from_conversation(self, detail: dict[str, Any]) -> dict[str, Any] | None:
        group = detail.get("group") if isinstance(detail.get("group"), dict) else None
        source = dict(group or {})
        for key in ("title", "description", "metadata_version", "metadataVersion"):
            if key in detail and key not in source:
                source[key] = detail[key]
        if "metadataVersion" in source and "metadata_version" not in source:
            source["metadata_version"] = source["metadataVersion"]
        if not any(key in source for key in ("title", "description", "metadata_version")):
            return None
        source["raw"] = source.get("raw", group or detail)
        return source

    def _profile_from_raw(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        user_id = raw.get("user_id") or raw.get("userId") or raw.get("id")
        if not user_id:
            return None
        profile = {"user_id": str(user_id), "raw": raw}
        for source, target in (
            ("type", "profile_type"),
            ("profile_type", "profile_type"),
            ("nickname", "nickname"),
            ("avatar_url", "avatar_url"),
            ("avatarUrl", "avatar_url"),
            ("bio", "bio"),
        ):
            if source in raw:
                profile[target] = raw[source]
        return profile

    def _member_from_raw(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        user_id = raw.get("user_id") or raw.get("userId") or raw.get("id")
        if not user_id:
            return None
        return {"user_id": str(user_id), "role": raw.get("role"), "raw": raw}

    def _sender_relation(self, sender_id: str, *, profile_type: Any = None) -> str:
        return relation_for_sender(
            sender_id,
            agent_user_id=self._clawchat_config.user_id,
            owner_user_id=self._owner_user_id(),
            profile_type=profile_type if isinstance(profile_type, str) else None,
        )

    def _owner_user_id(self) -> str:
        if self._clawchat_config.owner_user_id:
            return self._clawchat_config.owner_user_id
        if self._store is None:
            return ""
        try:
            owner_user_id = self._store.get_activation_owner_user_id(
                platform="hermes",
                account_id="default",
            )
        except AttributeError:
            return ""
        except Exception:  # noqa: BLE001
            logger.warning("clawchat activation owner cache read failed", exc_info=True)
            return ""
        return str(owner_user_id or "")

    def _upsert_minimal_conversation(
        self,
        *,
        conversation_id: str | None,
        conversation_type: str | None,
        last_seen_at: int | None,
    ) -> bool:
        if self._store is None or not conversation_id:
            return False
        try:
            created = self._store.upsert_conversation_summary(
                platform="clawchat",
                account_id=self._account_id(),
                conversation_id=conversation_id,
                conversation_type=conversation_type,
                last_seen_at=last_seen_at if last_seen_at is not None else int(time.time() * 1000),
                raw=None,
            )
            return bool(created)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat minimal conversation cache upsert failed")
            return False

    def _upsert_minimal_sender_profile(self, inbound: InboundMessage) -> bool:
        if self._store is None or not inbound.sender_id:
            return False
        try:
            kwargs = {
                "platform": "clawchat",
                "account_id": self._account_id(),
                "profile_kind": "user",
                "profile_id": inbound.sender_id,
                "relation": self._sender_relation(inbound.sender_id),
                "now_ms": int(time.time() * 1000),
            }
            if inbound.sender_name and inbound.sender_name != inbound.sender_id:
                kwargs["nickname"] = inbound.sender_name
            created = self._store.upsert_minimal_profile(**kwargs)
            return bool(created)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat minimal user profile cache upsert failed")
            return False

    def _sender_profile_needs_refresh(self, sender_id: str) -> bool:
        profile = self._get_cached_profile("user", sender_id)
        if not isinstance(profile, dict):
            return True
        return profile.get("last_refreshed_at") is None

    def _resolve_sender_name(self, inbound: InboundMessage) -> str:
        if inbound.sender_name and inbound.sender_name != inbound.sender_id:
            return inbound.sender_name
        sender_profile = self._get_cached_profile("user", inbound.sender_id)
        cached_nickname = sender_profile.get("nickname") if isinstance(sender_profile, dict) else None
        if isinstance(cached_nickname, str) and cached_nickname and cached_nickname != inbound.sender_id:
            return cached_nickname
        return inbound.sender_name or inbound.sender_id

    def _sender_batch_identity(self, inbound: InboundMessage) -> tuple[str, str]:
        sender_profile = self._get_cached_profile("user", inbound.sender_id)
        profile_type = ""
        if isinstance(sender_profile, dict) and sender_profile.get("profile_type") is not None:
            profile_type = str(sender_profile.get("profile_type"))
        relation = self._sender_relation(inbound.sender_id, profile_type=profile_type or None)
        if not profile_type:
            profile_type = "agent" if relation in {"self_agent", "peer_agent"} else "user"
        return relation, profile_type

    def _upsert_minimal_group_profile(self, inbound: InboundMessage) -> bool:
        if self._store is None or inbound.chat_type != "group" or not inbound.chat_id:
            return False
        try:
            created = self._store.upsert_minimal_profile(
                platform="clawchat",
                account_id=self._account_id(),
                profile_kind="group",
                profile_id=inbound.chat_id,
                now_ms=int(time.time() * 1000),
            )
            return bool(created)
        except Exception:  # noqa: BLE001
            logger.warning("clawchat minimal group profile cache upsert failed")
            return False

    def _schedule_profile_sync(self, coro: Any) -> None:
        task = asyncio.create_task(coro, name="clawchat-profile-sync")
        self._profile_sync_tasks.add(task)
        task.add_done_callback(self._profile_sync_task_done)

    def _profile_sync_task_done(self, task: asyncio.Task[None]) -> None:
        self._profile_sync_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001
            logger.warning("clawchat profile sync task failed", exc_info=True)

    async def _cancel_profile_sync_tasks(self) -> None:
        tasks = list(self._profile_sync_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._profile_sync_tasks.discard(task)

    async def _refresh_user_profile(self, user_id: str) -> bool:
        client = ClawChatApiClient(
            base_url=self._clawchat_config.base_url,
            token=self._clawchat_config.token,
            user_id=self._clawchat_config.user_id,
        )
        try:
            result = await client.get_user_info(user_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "clawchat user profile refresh failed user_id=%s error=%s",
                user_id,
                exc,
            )
            return False
        return self._cache_user_profile(result, user_id=user_id)

    def _schedule_activation_bootstrap(self) -> None:
        if self._store is None:
            return
        task = asyncio.create_task(
            self._dispatch_activation_bootstrap(),
            name="clawchat-activation-bootstrap",
        )
        self._activation_bootstrap_tasks.add(task)
        task.add_done_callback(self._activation_bootstrap_task_done)

    def _activation_bootstrap_task_done(self, task: asyncio.Task[None]) -> None:
        self._activation_bootstrap_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:  # noqa: BLE001
            logger.warning("clawchat activation bootstrap dispatch failed", exc_info=True)

    async def _cancel_activation_bootstrap_tasks(self) -> None:
        tasks = list(self._activation_bootstrap_tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in tasks:
            self._activation_bootstrap_tasks.discard(task)

    async def _dispatch_activation_bootstrap(self) -> None:
        if self._store is None:
            return
        claim = self._store.claim_pending_activation_bootstrap(
            platform="hermes",
            account_id="default",
        )
        if claim is None:
            return
        conversation_id = str(getattr(claim, "conversation_id", "") or "")
        if not conversation_id:
            return
        owner_user_id = str(getattr(claim, "owner_user_id", "") or "")
        claimed_at = getattr(claim, "claimed_at", None)
        inbound = InboundMessage(
            chat_id=conversation_id,
            chat_type="direct",
            sender_id=owner_user_id,
            sender_name="",
            text=_CLAWCHAT_ACTIVATION_BOOTSTRAP_PROMPT,
            raw_message={
                "synthetic": True,
                "bootstrap": True,
                "conversation_id": conversation_id,
                "owner_user_id": owner_user_id,
            },
        )
        try:
            await self._handle_inbound(inbound)
        except asyncio.CancelledError:
            self._release_activation_bootstrap_claim(
                conversation_id=conversation_id,
                claimed_at=claimed_at,
            )
            raise
        except Exception:
            self._release_activation_bootstrap_claim(
                conversation_id=conversation_id,
                claimed_at=claimed_at,
            )
            raise
        self._store.mark_activation_bootstrap_sent(
            platform="hermes",
            account_id="default",
            conversation_id=conversation_id,
            claimed_at=claimed_at,
        )

    def _release_activation_bootstrap_claim(
        self,
        *,
        conversation_id: str,
        claimed_at: Any,
    ) -> None:
        if self._store is None or claimed_at is None:
            return
        try:
            self._store.release_activation_bootstrap_claim(
                platform="hermes",
                account_id="default",
                conversation_id=conversation_id,
                claimed_at=int(claimed_at),
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat activation bootstrap claim release failed", exc_info=True)

    def _trace_inbound_frame(self, frame: dict[str, Any]) -> None:
        """Pre-parse trace for inbound message.send frames.

        Why: hermes-agent has been observed to enter an interrupt-loop where
        it treats its own outbound chunks as new user input. This emits one
        log line per inbound frame with the fields needed to confirm/refute
        that hypothesis (sender_id vs bot user_id, message_id, text head),
        and warns when the per-chat rate exceeds a sane threshold.
        """
        chat_id = frame.get("chat_id") or ""
        chat_type = frame.get("chat_type") or "direct"
        sender = frame.get("sender") if isinstance(frame.get("sender"), dict) else {}
        sender_id = sender.get("id") if isinstance(sender, dict) else None
        bot_user_id = self._clawchat_config.user_id
        is_self_echo = bool(sender_id) and sender_id == bot_user_id

        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        message_id = payload.get("message_id")
        fragments = message.get("fragments") if isinstance(message.get("fragments"), list) else []
        frag_count = len(fragments)

        text_head = ""
        for frag in fragments:
            if not isinstance(frag, dict):
                continue
            for key in ("text", "content", "value"):
                value = frag.get(key)
                if isinstance(value, str) and value:
                    text_head = value
                    break
            if text_head:
                break
        if not text_head:
            body = message.get("body")
            if isinstance(body, str):
                text_head = body
            elif isinstance(body, dict):
                for key in ("text", "content", "value"):
                    value = body.get(key)
                    if isinstance(value, str) and value:
                        text_head = value
                        break
        text_head = text_head[:80].replace("\n", " ")

        log_fn = inbound_trace.warning if is_self_echo else inbound_trace.info
        log_fn(
            "inbound chat_id=%s chat_type=%s sender_id=%s bot_user_id=%s "
            "is_self_echo=%s message_id=%s trace_id=%s frag_count=%d text_head=%r",
            chat_id,
            chat_type,
            sender_id,
            bot_user_id,
            is_self_echo,
            message_id,
            frame.get("trace_id"),
            frag_count,
            text_head,
        )

        now = time.monotonic()
        window = self._inbound_window.setdefault(chat_id, deque())
        window.append(now)
        cutoff = now - INBOUND_RATE_WINDOW_SECONDS
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= INBOUND_RATE_WARN_THRESHOLD:
            inbound_trace.warning(
                "inbound rate spike chat_id=%s count=%d window_s=%.1f "
                "(possible self-echo / interrupt loop)",
                chat_id,
                len(window),
                INBOUND_RATE_WINDOW_SECONDS,
            )

    async def _on_message(self, frame: dict[str, Any]) -> None:
        self._trace_inbound_frame(frame)
        event_name = str(frame.get("event") or "")
        if event_name == "interaction.submit":
            logger.info(
                "clawchat interaction submit ignored chat_id=%s reason=ws_control_event",
                frame.get("chat_id"),
            )
            return
        protocol_message_id = None
        if event_name in {"message.send", "message.reply"}:
            protocol_message_id = self._extract_protocol_message_id(frame)
            if not protocol_message_id:
                logger.warning(
                    "clawchat inbound dropped event=%s chat_id=%s reason=missing_protocol_message_id",
                    event_name,
                    frame.get("chat_id"),
                )
                return
        inbound = parse_inbound_message(frame, self._clawchat_config)
        if inbound is None:
            logger.warning(
                "clawchat inbound dropped event=%s chat_id=%s reason=parse_or_filter_failed",
                event_name,
                frame.get("chat_id"),
            )
            return
        logger.info(
            "clawchat inbound parsed chat_id=%s chat_type=%s sender_id=%s text_len=%d media=%d",
            inbound.chat_id,
            inbound.chat_type,
            inbound.sender_id,
            len(inbound.text),
            len(inbound.media_urls),
        )
        new_conversation = self._upsert_minimal_conversation(
            conversation_id=inbound.chat_id,
            conversation_type=inbound.chat_type,
            last_seen_at=frame.get("emitted_at") if isinstance(frame.get("emitted_at"), int) else None,
        )
        new_sender = self._upsert_minimal_sender_profile(inbound)
        new_group = self._upsert_minimal_group_profile(inbound)
        if new_conversation:
            self._schedule_profile_sync(self._refresh_conversation_metadata(inbound.chat_id))
        elif new_group:
            self._schedule_profile_sync(self._refresh_conversation_metadata(inbound.chat_id))
        if new_sender or self._sender_profile_needs_refresh(inbound.sender_id):
            self._schedule_profile_sync(self._refresh_user_profile(inbound.sender_id))
        resolved_sender_name = self._resolve_sender_name(inbound)
        if resolved_sender_name != inbound.sender_name:
            inbound = replace(inbound, sender_name=resolved_sender_name)
        if inbound.chat_type == "group":
            sender_relation, sender_profile_type = self._sender_batch_identity(inbound)
            inbound = replace(
                inbound,
                sender_relation=sender_relation,
                sender_profile_type=sender_profile_type,
            )
        if event_name in {"message.send", "message.reply"}:
            claimed = self._claim_message_once(
                kind="message",
                direction="inbound",
                event_type=event_name,
                trace_id=frame.get("trace_id") or frame.get("id"),
                chat_id=inbound.chat_id,
                message_id=protocol_message_id,
                text=inbound.text,
                raw=frame,
            )
            if claimed is False:
                logger.info(
                    "clawchat inbound duplicate skipped chat_id=%s message_id=%s event=%s",
                    inbound.chat_id,
                    protocol_message_id,
                    event_name,
                )
                return
        if inbound.chat_type == "group":
            self._group_message_coalescer.enqueue(inbound)
            if inbound.was_mentioned:
                logger.info(
                    "clawchat flushing group batch immediately chat_id=%s sender_id=%s text_len=%d reason=agent_mention",
                    inbound.chat_id,
                    inbound.sender_id,
                    len(inbound.text),
                )
                await self._group_message_coalescer.flush_now(inbound.chat_id)
                return
            logger.info(
                "clawchat queued group batch chat_id=%s sender_id=%s text_len=%d",
                inbound.chat_id,
                inbound.sender_id,
                len(inbound.text),
            )
            return
        await self._handle_inbound(inbound)

    async def _handle_inbound(self, inbound: InboundMessage) -> None:
        reply_to_message_id, reply_to_text = self._extract_reply_fields(
            inbound.reply_preview
        )
        source = self.build_source(
            chat_id=inbound.chat_id,
            user_id=inbound.sender_id,
            chat_name=inbound.chat_id,
            chat_type=self._map_source_chat_type(inbound.chat_type),
        )
        downloaded_media = await self._download_inbound_media(inbound)
        media_urls = [str(item.local_path) for item in downloaded_media]
        media_types = [item.mime for item in downloaded_media]
        event = MessageEvent(
            text=inbound.text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "clawchat_chat_type": inbound.chat_type,
                "clawchat_reply": inbound.reply_preview,
                "clawchat_raw": inbound.raw_message,
            },
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to_message_id,
            reply_to_text=reply_to_text,
        )
        channel_prompt = self._compose_channel_prompt(inbound)
        if channel_prompt:
            event.channel_prompt = channel_prompt
            if _debug_prompt_injection_enabled():
                logger.warning(
                    "clawchat prompt injection debug chat_id=%s chat_type=%s sender_id=%s\n%s\n%s\n%s\n%s\n%s\n%s",
                    inbound.chat_id,
                    inbound.chat_type,
                    inbound.sender_id,
                    DEBUG_PROMPT_INJECTION_BEGIN,
                    channel_prompt,
                    DEBUG_PROMPT_INJECTION_END,
                    DEBUG_EVENT_TEXT_BEGIN,
                    event.text,
                    DEBUG_EVENT_TEXT_END,
                )
        logger.info(
            "clawchat dispatch to hermes chat_id=%s user_id=%s text_len=%d media=%d downloaded=%d reply_to=%s",
            inbound.chat_id,
            inbound.sender_id,
            len(inbound.text),
            len(inbound.media_urls),
            len(media_urls),
            reply_to_message_id,
        )
        await self.handle_message(event)
        logger.info(
            "clawchat dispatch accepted by hermes chat_id=%s user_id=%s",
            inbound.chat_id,
            inbound.sender_id,
        )

    def _compose_channel_prompt(self, inbound: InboundMessage) -> str | None:
        prompts = [CONVERSATION_SEMANTICS]
        base_prompt = mode_prompt(inbound.chat_type)
        if base_prompt:
            prompts.append(base_prompt)
        if inbound.chat_type == "group":
            group_profile = self._get_cached_profile("group", inbound.chat_id)
            group_section = self._format_group_profile(group_profile)
            if group_section:
                prompts.append(group_section)
        else:
            user_profile = self._get_cached_profile("user", inbound.sender_id)
            user_section = self._format_user_profile(user_profile)
            if user_section:
                prompts.append(user_section)
        prompts.append(self._format_current_turn(inbound))
        prompts.append(self._format_response_protocol(inbound))
        behavior = self._cached_agent_behavior()
        if behavior:
            prompts.append(f"## ClawChat Agent Behavior\n{behavior}")
        return "\n\n".join(prompts) or None

    def _get_cached_profile(self, profile_kind: str, profile_id: str) -> dict[str, Any] | None:
        if self._store is None or not profile_id:
            return None
        try:
            return self._store.get_profile(
                platform="clawchat",
                account_id=self._account_id(),
                profile_kind=profile_kind,
                profile_id=profile_id,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat profile cache read failed", exc_info=True)
            return None

    def _cached_agent_behavior(self) -> str | None:
        seen: set[str] = set()
        for profile_id in (
            self._clawchat_config.user_id,
            self._clawchat_config.agent_id,
        ):
            if not profile_id or profile_id in seen:
                continue
            seen.add(profile_id)
            profile = self._get_cached_profile("agent", profile_id)
            behavior = profile.get("behavior") if isinstance(profile, dict) else None
            if isinstance(behavior, str) and behavior.strip():
                return behavior.strip()
        return None

    def _format_user_profile(self, profile: dict[str, Any] | None) -> str | None:
        if not profile:
            return None
        fields = self._format_fields(
            (
                ("avatar_url", profile.get("avatar_url")),
                ("bio", profile.get("bio")),
            )
        )
        return "## Current ClawChat User Profile\n" + fields if fields else None

    def _format_group_profile(self, profile: dict[str, Any] | None) -> str | None:
        if not profile:
            return None
        fields = self._format_fields(
            (
                ("title", profile.get("title")),
                ("description/rules", profile.get("description")),
                ("metadata_version", profile.get("metadata_version")),
            )
        )
        return "## ClawChat Group Profile/Regulation\n" + fields if fields else None

    def _format_current_turn(self, inbound: InboundMessage) -> str:
        if inbound.chat_type == "group":
            field_items: list[tuple[str, Any]] = [
                ("chat_type", "group"),
                ("group_id", inbound.chat_id),
            ]
            fields = self._format_fields(tuple(field_items), include_empty=True)
            return "## Current ClawChat Message Metadata\n" + fields

        sender_profile = self._get_cached_profile("user", inbound.sender_id)
        sender_profile_type = None
        sender_name = inbound.sender_name
        if isinstance(sender_profile, dict) and sender_profile.get("profile_type") is not None:
            sender_profile_type = str(sender_profile.get("profile_type"))
        if (not sender_name or sender_name == inbound.sender_id) and isinstance(sender_profile, dict):
            cached_nickname = sender_profile.get("nickname")
            if isinstance(cached_nickname, str) and cached_nickname and cached_nickname != inbound.sender_id:
                sender_name = cached_nickname
        sender_relation = self._sender_relation(
            inbound.sender_id,
            profile_type=sender_profile_type or None,
        )
        if not sender_profile_type:
            sender_profile_type = "agent" if sender_relation in {"self_agent", "peer_agent"} else "user"
        field_items: list[tuple[str, Any]] = [
            ("chat_type", "dm"),
            ("sender_id", inbound.sender_id),
            ("sender_name", sender_name),
            ("sender_profile_type", sender_profile_type),
            ("sender_is_owner", "true" if sender_relation == "owner" else "false"),
        ]
        fields = self._format_fields(tuple(field_items), include_empty=True)
        return "## Current ClawChat Message Metadata\n" + fields

    def _format_response_protocol(self, inbound: InboundMessage) -> str:
        if inbound.chat_type == "group":
            reply_guidance = (
                GROUP_BATCH_MENTION_REPLY_GUIDANCE
                if inbound.was_mentioned
                else GROUP_BATCH_REPLY_GUIDANCE
            )
            response_decision = "Decide whether this group input needs a reply from you."
        else:
            reply_guidance = DIRECT_MESSAGE_REPLY_GUIDANCE
            response_decision = "Decide whether this direct message needs a reply from you."
        fields = self._format_fields(
            (
                ("response_decision", response_decision),
                ("allowed_outputs", "normal_reply OR exact_empty_response"),
                ("exact_empty_response", EMPTY_RESPONSE_TOKEN),
                ("reply_guidance", reply_guidance),
                ("no_reply_protocol", 'If you choose not to reply, return exactly "" and nothing else.'),
            ),
            include_empty=True,
        )
        return "## ClawChat Response Protocol\n" + fields

    def _format_fields(
        self,
        fields: tuple[tuple[str, Any], ...],
        *,
        include_empty: bool = False,
    ) -> str:
        lines: list[str] = []
        for key, value in fields:
            if value is None:
                if not include_empty:
                    continue
                value = ""
            text = str(value)
            if not text and not include_empty:
                continue
            text = self._escape_prompt_field(text)
            lines.append(f"{key}: {text}")
        return "\n".join(lines)

    def _escape_prompt_field(self, value: str) -> str:
        return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")

    def _debug_hermes_output(
        self,
        *,
        phase: str,
        chat_id: str,
        message_id: str | None,
        text: str,
    ) -> None:
        if not _debug_prompt_injection_enabled():
            return
        logger.warning(
            "clawchat hermes output debug phase=%s chat_id=%s message_id=%s text_len=%d\n%s\n%s\n%s",
            phase,
            chat_id,
            message_id or "-",
            len(text),
            DEBUG_HERMES_OUTPUT_BEGIN,
            text,
            DEBUG_HERMES_OUTPUT_END,
        )

    async def _download_inbound_media(self, inbound: InboundMessage) -> list[Any]:
        if not inbound.media_urls:
            return []
        downloaded = await download_inbound_media(
            inbound.media_urls,
            base_url=self._clawchat_config.base_url,
            websocket_url=self._clawchat_config.websocket_url,
            token=self._clawchat_config.token,
            download_dir=self._clawchat_config.media_download_dir,
        )
        logger.info(
            "clawchat inbound media downloaded chat_id=%s requested=%d downloaded=%d types=%s",
            inbound.chat_id,
            len(inbound.media_urls),
            len(downloaded),
            [item.mime for item in downloaded],
        )
        return downloaded

    async def send(
        self,
        chat_id: str,
        content: str = "",
        reply_to: str | None = None,
        metadata: Any = None,
        **kwargs: Any,
    ) -> SendResult:
        chat_type = self._resolve_chat_type(metadata, kwargs)
        self._debug_hermes_output(
            phase="send",
            chat_id=chat_id,
            message_id=None,
            text=content or "",
        )
        if self._should_suppress_tool_progress(content or ""):
            logger.info("clawchat tool progress suppressed chat_id=%s text_len=%d", chat_id, len(content or ""))
            return SendResult(success=True)
        visible_content = self._filter_output_content(content or "")
        fragments = await self._build_fragments(visible_content, metadata, kwargs)
        if self._is_pure_silent_response(fragments):
            logger.info("clawchat silent response suppressed chat_id=%s chat_type=%s", chat_id, chat_type)
            return SendResult(success=True)
        message_id = new_frame_id("msg")
        logger.info(
            "clawchat send start chat_id=%s chat_type=%s mode=%s text_len=%d fragments=%d reply_to=%s",
            chat_id,
            chat_type,
            self._clawchat_config.reply_mode,
            len(visible_content),
            len(fragments),
            reply_to,
        )

        if self._should_use_static_mode(fragments):
            frame = build_message_reply_event(
                chat_id=chat_id,
                chat_type=chat_type,
                message_id=message_id,
                fragments=fragments,
                reply_to_message_id=reply_to,
                include_message_id=True,
            )
            self._upsert_minimal_conversation(
                conversation_id=chat_id,
                conversation_type=chat_type,
                last_seen_at=frame.get("emitted_at") if isinstance(frame.get("emitted_at"), int) else None,
            )
            claimed = self._claim_outbound_message(
                event_type="message.reply",
                trace_id=frame.get("trace_id") or frame.get("id"),
                chat_id=chat_id,
                message_id=message_id,
                text=visible_content,
                raw=frame,
            )
            if claimed is False:
                return SendResult(success=True, message_id=message_id)
            if claimed is None:
                return SendResult(
                    success=False,
                    error="clawchat outbound message claim failed",
                    message_id=message_id,
                )
            sent = await self._connection.send_frame(
                frame,
                wait_for_ack=True,
            )
            if not sent:
                error = "clawchat static reply dropped"
                self._update_message_record(
                    kind="message",
                    direction="outbound",
                    event_type="message.failed",
                    trace_id=frame.get("trace_id") or frame.get("id"),
                    chat_id=chat_id,
                    message_id=message_id,
                    text=error,
                    raw=frame,
                )
                logger.warning(
                    "clawchat send static reply dropped chat_id=%s message_id=%s",
                    chat_id,
                    message_id,
                )
                return SendResult(success=False, error=error, message_id=message_id)
            self._record_thinking_if_present(
                event_type="message.reply",
                trace_id=frame.get("trace_id") or frame.get("id"),
                chat_id=chat_id,
                message_id=message_id,
                content=content or "",
                raw=frame,
            )
            logger.info(
                "clawchat send static reply queued chat_id=%s message_id=%s fragments=%d",
                chat_id,
                message_id,
                len(fragments),
            )
            return SendResult(success=True, message_id=message_id)

        created_frame = build_message_created_event(
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
        )
        self._upsert_minimal_conversation(
            conversation_id=chat_id,
            conversation_type=chat_type,
            last_seen_at=created_frame.get("emitted_at") if isinstance(created_frame.get("emitted_at"), int) else None,
        )
        claimed = self._claim_outbound_message(
            event_type="message.created",
            trace_id=created_frame.get("trace_id") or created_frame.get("id"),
            chat_id=chat_id,
            message_id=message_id,
            text=visible_content,
            raw=created_frame,
        )
        if claimed is False:
            return SendResult(success=True, message_id=message_id)
        if claimed is None:
            return SendResult(
                success=False,
                error="clawchat outbound message claim failed",
                message_id=message_id,
            )

        run = _ActiveRun(
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
            started_order=self._next_run_order(),
            reply_to_message_id=reply_to,
        )
        self._active_runs_by_id[message_id] = run
        self._active_chat_runs[chat_id] = message_id

        await self._send_best_effort(created_frame, run)
        if visible_content:
            run.last_text, delta = compute_delta(run.last_text, visible_content)
            run.sequence += 1
            await self._send_best_effort(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    message_id=message_id,
                    full_text=run.last_text,
                    delta=delta,
                    sequence=run.sequence,
                ),
                run,
            )
            logger.info(
                "clawchat stream delta queued chat_id=%s message_id=%s delta_len=%d",
                chat_id,
                message_id,
                len(delta),
            )
        return SendResult(success=True, message_id=message_id)

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        finalize: bool = False,
        **kwargs: Any,
    ) -> SendResult:
        run = self._resolve_active_run(chat_id=chat_id, message_id=message_id)
        self._debug_hermes_output(
            phase="edit_message",
            chat_id=chat_id,
            message_id=message_id,
            text=content or "",
        )
        if run is None:
            if message_id and message_id in self._completed_run_ids:
                logger.info(
                    "clawchat edit skipped chat_id=%s message_id=%s reason=run_already_complete",
                    chat_id,
                    message_id,
                )
                return SendResult(success=True, message_id=message_id)
            logger.warning(
                "clawchat edit skipped chat_id=%s message_id=%s reason=no_active_run",
                chat_id,
                message_id,
            )
            return SendResult(success=False, error="no active run for message_id")

        if self._should_suppress_tool_progress(content or "") and not finalize:
            logger.info("clawchat tool progress edit suppressed chat_id=%s message_id=%s text_len=%d", chat_id, message_id, len(content or ""))
            return SendResult(success=True, message_id=run.message_id)

        visible_content = self._filter_output_content(content or "")
        if finalize and not run.last_text and self._is_noop_response_text(visible_content):
            self._discard_run(run)
            self._remember_completed_run(run.message_id)
            logger.info(
                "clawchat silent response edit suppressed chat_id=%s message_id=%s",
                chat_id,
                run.message_id,
            )
            return SendResult(success=True, message_id=run.message_id)

        full_text, delta = compute_delta(run.last_text, visible_content)
        if delta:
            await self._send_best_effort(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=run.chat_type,
                    message_id=run.message_id,
                    full_text=full_text,
                    delta=delta,
                    sequence=run.sequence + 1,
                ),
                run,
            )
            run.sequence += 1
            run.last_text = full_text

        if finalize:
            await self.on_run_complete(
                chat_id=chat_id,
                final_text=content or "",
                message_id=run.message_id,
            )

        return SendResult(success=True, message_id=run.message_id)

    async def on_run_complete(
        self,
        chat_id: str,
        final_text: str,
        message_id: str | None = None,
    ) -> None:
        run = self._resolve_active_run(chat_id=chat_id, message_id=message_id)
        self._debug_hermes_output(
            phase="on_run_complete",
            chat_id=chat_id,
            message_id=message_id,
            text=final_text or "",
        )
        if run is None:
            if message_id and message_id in self._completed_run_ids:
                logger.info(
                    "clawchat run complete skipped chat_id=%s message_id=%s reason=run_already_complete",
                    chat_id,
                    message_id,
                )
                return
            logger.warning(
                "clawchat run complete skipped chat_id=%s message_id=%s reason=no_active_run",
                chat_id,
                message_id,
            )
            return
        self._discard_run(run)
        self._remember_completed_run(run.message_id)
        logger.info(
            "clawchat run complete chat_id=%s message_id=%s final_len=%d",
            chat_id,
            run.message_id,
            len(self._filter_output_content(final_text or "")),
        )

        visible_final_text = self._filter_output_content(final_text or "")
        if not run.last_text and self._is_noop_response_text(visible_final_text):
            logger.info("clawchat silent response final suppressed chat_id=%s message_id=%s", chat_id, run.message_id)
            return
        full_text, delta = compute_delta(run.last_text, visible_final_text)
        if delta:
            run.sequence += 1
            await self._send_best_effort(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=run.chat_type,
                    message_id=run.message_id,
                    full_text=full_text,
                    delta=delta,
                    sequence=run.sequence,
                ),
                run,
            )
            run.last_text = full_text

        frame = build_message_done_event(
            chat_id=chat_id,
            chat_type=run.chat_type,
            message_id=run.message_id,
            fragments=await self._build_fragments(run.last_text),
            sequence=run.sequence,
        )
        await self._send_best_effort(frame, run)
        self._update_message_record(
            kind="message",
            direction="outbound",
            event_type="message.done",
            trace_id=frame.get("trace_id") or frame.get("id"),
            chat_id=chat_id,
            message_id=run.message_id,
            text=run.last_text,
            raw=frame,
        )
        self._record_thinking_if_present(
            event_type="message.done",
            trace_id=frame.get("trace_id") or frame.get("id"),
            chat_id=chat_id,
            message_id=run.message_id,
            content=final_text or "",
            raw=frame,
        )
        if run.delivery_degraded:
            await self._send_stream_fallback_reply(run)
        logger.info(
            "clawchat stream done queued chat_id=%s message_id=%s",
            chat_id,
            run.message_id,
        )

    def _remember_completed_run(self, message_id: str) -> None:
        if message_id in self._completed_run_ids:
            return
        self._completed_run_ids.add(message_id)
        self._completed_run_order.append(message_id)
        while len(self._completed_run_order) > COMPLETED_RUN_CACHE_MAX:
            old_message_id = self._completed_run_order.popleft()
            self._completed_run_ids.discard(old_message_id)

    async def on_run_failed(
        self,
        chat_id: str,
        error: str,
        message_id: str | None = None,
    ) -> None:
        run = self._resolve_active_run(chat_id=chat_id, message_id=message_id)
        if run is None:
            logger.warning(
                "clawchat run failed skipped chat_id=%s message_id=%s reason=no_active_run",
                chat_id,
                message_id,
            )
            return
        self._discard_run(run)
        frame = build_message_failed_event(
            chat_id=chat_id,
            chat_type=run.chat_type,
            message_id=run.message_id,
            sequence=max(run.sequence, 0),
            reason=error,
        )
        await self._send_best_effort(frame, run)
        self._record_message(
            kind="error",
            direction="outbound",
            event_type="message.failed",
            trace_id=frame.get("trace_id") or frame.get("id"),
            chat_id=chat_id,
            message_id=run.message_id,
            text=error,
            raw=frame,
        )
        logger.info(
            "clawchat stream failed queued chat_id=%s message_id=%s",
            chat_id,
            run.message_id,
        )

    async def _send_best_effort(
        self,
        frame: dict[str, Any],
        run: _ActiveRun | None = None,
    ) -> bool:
        sent = await self._connection.send_frame(frame, queue_when_unready=False)
        if run is not None and not sent:
            run.delivery_degraded = True
        return sent

    async def _send_stream_fallback_reply(self, run: _ActiveRun) -> None:
        frame = build_message_reply_event(
            chat_id=run.chat_id,
            chat_type=run.chat_type,
            message_id=run.message_id,
            fragments=await self._build_fragments(run.last_text),
            reply_to_message_id=run.reply_to_message_id,
            include_message_id=True,
        )
        await self._connection.send_frame(frame, wait_for_ack=True)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: str | None = None,
        reply_to: str | None = None,
        metadata: Any = None,
    ) -> SendResult:
        merged_metadata = dict(metadata or {})
        merged_metadata["media_urls"] = [normalize_outbound_media_reference(image_url)]
        return await self.send(
            chat_id=chat_id,
            content=caption or "",
            reply_to=reply_to,
            metadata=merged_metadata,
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
        **kwargs: Any,
    ) -> SendResult:
        merged_metadata = dict(kwargs.get("metadata") or {})
        merged_metadata["media_urls"] = [normalize_outbound_media_reference(image_path)]
        return await self.send(
            chat_id=chat_id,
            content=caption or "",
            reply_to=reply_to,
            metadata=merged_metadata,
        )

    def _resolve_chat_type(self, metadata: Any, kwargs: dict[str, Any]) -> str:
        if isinstance(metadata, dict) and isinstance(metadata.get("chat_type"), str):
            return metadata["chat_type"]
        if isinstance(kwargs.get("chat_type"), str):
            return kwargs["chat_type"]
        return "direct"

    def _map_source_chat_type(self, chat_type: str) -> str:
        if chat_type == "direct":
            return "dm"
        return chat_type

    def _next_run_order(self) -> int:
        self._run_counter += 1
        return self._run_counter

    def _resolve_active_run(
        self,
        *,
        chat_id: str,
        message_id: str | None = None,
    ) -> _ActiveRun | None:
        if message_id:
            run = self._active_runs_by_id.get(message_id)
            if run is None or run.chat_id != chat_id:
                return None
            return run
        latest_message_id = self._active_chat_runs.get(chat_id)
        if latest_message_id is None:
            return None
        return self._active_runs_by_id.get(latest_message_id)

    def _discard_run(self, run: _ActiveRun) -> None:
        self._active_runs_by_id.pop(run.message_id, None)
        latest_message_id = self._active_chat_runs.get(run.chat_id)
        if latest_message_id != run.message_id:
            return
        replacement = self._find_latest_run_for_chat(run.chat_id)
        if replacement is None:
            self._active_chat_runs.pop(run.chat_id, None)
            return
        self._active_chat_runs[run.chat_id] = replacement.message_id

    def _find_latest_run_for_chat(self, chat_id: str) -> _ActiveRun | None:
        candidates = [
            run for run in self._active_runs_by_id.values() if run.chat_id == chat_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda run: run.started_order)

    def _should_use_static_mode(self, fragments: list[dict[str, Any]]) -> bool:
        has_media = any(fragment.get("kind") != "text" for fragment in fragments)
        return self._clawchat_config.reply_mode != "stream" or has_media

    def _filter_output_content(self, content: str) -> str:
        filtered = content
        if not self._clawchat_config.show_think_output:
            filtered = _THINK_BLOCK_RE.sub("", filtered)
            filtered = _THINK_OPEN_RE.sub("", filtered)
        if not self._clawchat_config.show_tools_output:
            filtered = _TOOL_FENCE_BLOCK_RE.sub("", filtered)
            filtered = _TOOL_FENCE_OPEN_RE.sub("", filtered)
            filtered = _TOOL_TAG_BLOCK_RE.sub("", filtered)
            filtered = _TOOL_TAG_OPEN_RE.sub("", filtered)
        filtered = _HERMES_STREAM_CURSOR_RE.sub("", filtered)
        filtered = _STREAMING_CURSOR_RE.sub("", filtered)
        return filtered

    def _is_noop_response_text(self, content: str) -> bool:
        text = content.strip()
        return text == EMPTY_RESPONSE_TOKEN

    def _is_pure_silent_response(self, fragments: list[dict[str, Any]]) -> bool:
        return (
            len(fragments) == 1
            and fragments[0].get("kind") == "text"
            and self._is_noop_response_text(str(fragments[0].get("text") or ""))
        )

    def _should_suppress_tool_progress(self, content: str) -> bool:
        if self._clawchat_config.show_tool_progress:
            return False
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        return all(_TOOL_PROGRESS_LINE_RE.match(line) for line in lines)

    def _record_message(
        self,
        *,
        kind: str,
        direction: str,
        event_type: str,
        trace_id: Any,
        chat_id: str | None,
        message_id: str | None,
        text: str | None,
        raw: Any,
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.insert_message(
                platform="hermes",
                account_id="default",
                kind=kind,
                direction=direction,
                event_type=event_type,
                trace_id=str(trace_id) if trace_id is not None else None,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                raw=raw,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat message database persistence failed")

    def _claim_message_once(
        self,
        *,
        kind: str,
        direction: str,
        event_type: str,
        trace_id: Any,
        chat_id: str | None,
        message_id: str | None,
        text: str | None,
        raw: Any,
    ) -> bool | None:
        if self._store is None:
            return None
        try:
            return self._store.claim_message_once(
                platform="hermes",
                account_id="default",
                kind=kind,
                direction=direction,
                event_type=event_type,
                trace_id=str(trace_id) if trace_id is not None else None,
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                raw=raw,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat message database claim failed")
            return None

    def _claim_outbound_message(
        self,
        *,
        event_type: str,
        trace_id: Any,
        chat_id: str,
        message_id: str,
        text: str | None,
        raw: Any,
    ) -> bool | None:
        claimed = self._claim_message_once(
            kind="message",
            direction="outbound",
            event_type=event_type,
            trace_id=trace_id,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            raw=raw,
        )
        if claimed is False:
            logger.info(
                "clawchat outbound duplicate skipped chat_id=%s message_id=%s event=%s",
                chat_id,
                message_id,
                event_type,
            )
            return False
        if claimed is None:
            logger.warning(
                "clawchat outbound skipped chat_id=%s message_id=%s reason=claim_unavailable",
                chat_id,
                message_id,
            )
            return None
        return True

    def _update_message_record(
        self,
        *,
        kind: str,
        direction: str,
        event_type: str,
        trace_id: Any,
        chat_id: str | None,
        message_id: str | None,
        text: str | None,
        raw: Any,
    ) -> None:
        if self._store is None or not message_id:
            return
        try:
            self._store.update_message_by_identity(
                account_id="default",
                kind=kind,
                direction=direction,
                message_id=message_id,
                event_type=event_type,
                trace_id=str(trace_id) if trace_id is not None else None,
                chat_id=chat_id,
                text=text,
                raw=raw,
            )
        except Exception:  # noqa: BLE001
            logger.warning("clawchat message database update failed")

    def _record_thinking_if_present(
        self,
        *,
        event_type: str,
        trace_id: Any,
        chat_id: str,
        message_id: str | None,
        content: str,
        raw: Any,
    ) -> None:
        if not message_id:
            return
        thinking = self._extract_thinking_content(content)
        if thinking is None:
            return
        self._record_message(
            kind="thinking",
            direction="outbound",
            event_type=event_type,
            trace_id=trace_id,
            chat_id=chat_id,
            message_id=message_id,
            text=thinking,
            raw=raw,
        )

    def _extract_thinking_content(self, content: str) -> str | None:
        parts = [match.strip() for match in _THINK_CONTENT_RE.findall(content) if match.strip()]
        return "\n\n".join(parts) or None

    def _extract_protocol_message_id(self, frame: dict[str, Any]) -> str | None:
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        value = payload.get("message_id")
        return value if isinstance(value, str) and value else None

    async def _build_fragments(
        self,
        content: str = "",
        metadata: Any = None,
        kwargs: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        rich_fragment = self._build_interaction_fragment(content, metadata, kwargs)
        if rich_fragment is not None:
            fragments.append(rich_fragment)
        elif content:
            fragments.append({"kind": "text", "text": content})

        merged_kwargs = kwargs or {}
        media_urls: list[str] = []
        if isinstance(metadata, dict):
            raw_urls = metadata.get("media_urls") or []
            if isinstance(raw_urls, list):
                media_urls.extend(
                    normalize_outbound_media_reference(url)
                    for url in raw_urls
                    if isinstance(url, str)
                )
        raw_kw_urls = merged_kwargs.get("media_urls") or []
        if isinstance(raw_kw_urls, list):
            media_urls.extend(
                normalize_outbound_media_reference(url)
                for url in raw_kw_urls
                if isinstance(url, str)
            )

        uploaded_fragments = await self._build_media_fragments(
            media_urls=media_urls,
            metadata=metadata,
            kwargs=merged_kwargs,
        )
        fragments.extend(uploaded_fragments)

        if not fragments:
            fragments.append({"kind": "text", "text": ""})
        return fragments

    def _build_interaction_fragment(
        self,
        content: str,
        metadata: Any,
        kwargs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not self._clawchat_config.enable_rich_interactions:
            return None
        explicit = self._extract_interaction(metadata, kwargs)
        if explicit is not None:
            return explicit
        if not (_APPROVE_COMMAND_RE.search(content) and _DENY_COMMAND_RE.search(content)):
            return None
        return {
            "kind": "approval_request",
            "title": "Approval required",
            "fallback_text": content,
            "state": "pending",
            "actions": [
                {
                    "id": "approve",
                    "label": "Approve",
                    "style": "primary",
                    "payload": {"decision": "approve"},
                },
                {
                    "id": "deny",
                    "label": "Deny",
                    "style": "danger",
                    "payload": {"decision": "deny"},
                },
            ],
        }

    def _extract_interaction(
        self,
        metadata: Any,
        kwargs: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        for carrier in (metadata, kwargs or {}):
            if not isinstance(carrier, dict):
                continue
            raw = carrier.get("clawchat_interaction") or carrier.get("interaction")
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            fallback_text = raw.get("fallback_text")
            actions = raw.get("actions")
            if kind not in {"approval_request", "action_card"}:
                continue
            if not isinstance(fallback_text, str) or not fallback_text:
                continue
            if not isinstance(actions, list) or not all(isinstance(item, dict) for item in actions):
                continue
            fragment: dict[str, Any] = {
                "kind": kind,
                "fallback_text": fallback_text,
                "actions": actions,
            }
            if isinstance(raw.get("title"), str):
                fragment["title"] = raw["title"]
            if isinstance(raw.get("state"), str):
                fragment["state"] = raw["state"]
            return fragment
        return None

    async def _build_media_fragments(
        self,
        *,
        media_urls: list[str],
        metadata: Any,
        kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if not media_urls:
            return []

        return await upload_outbound_media(
            media_urls,
            base_url=self._clawchat_config.base_url,
            websocket_url=self._clawchat_config.websocket_url,
            token=self._clawchat_config.token,
            media_local_roots=self._clawchat_config.media_local_roots,
        )

    def _infer_media_kind(
        self,
        *,
        media_url: str,
        index: int,
        metadata: Any,
        kwargs: dict[str, Any],
    ) -> str:
        mime_hint = self._extract_media_mime_hint(
            media_url=media_url,
            index=index,
            metadata=metadata,
            kwargs=kwargs,
        )
        if mime_hint:
            return infer_media_kind_from_mime(mime_hint)

        path = urlparse(media_url).path.lower()
        if path.endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".heic")
        ):
            return "image"
        if path.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
            return "audio"
        if path.endswith((".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v")):
            return "video"
        return "file"

    def _extract_media_mime_hint(
        self,
        *,
        media_url: str,
        index: int,
        metadata: Any,
        kwargs: dict[str, Any],
    ) -> str | None:
        for carrier in (metadata, kwargs):
            hint = self._lookup_media_mime_hint(carrier, media_url, index)
            if hint:
                return hint
        return None

    def _lookup_media_mime_hint(
        self,
        carrier: Any,
        media_url: str,
        index: int,
    ) -> str | None:
        if not isinstance(carrier, dict):
            return None
        for key in ("media_content_types", "media_mime_types"):
            raw = carrier.get(key)
            if isinstance(raw, Mapping):
                hint = raw.get(media_url)
                if isinstance(hint, str):
                    return hint
            if isinstance(raw, list) and index < len(raw) and isinstance(raw[index], str):
                return raw[index]
        return None


    def _extract_reply_fields(
        self,
        reply_preview: dict[str, Any] | None,
    ) -> tuple[str | None, str | None]:
        if not isinstance(reply_preview, dict):
            return None, None

        nested_preview = reply_preview.get("reply_preview")
        preview = nested_preview if isinstance(nested_preview, dict) else reply_preview

        reply_to_message_id = None
        for key in ("id", "reply_to_msg_id"):
            value = preview.get(key)
            if isinstance(value, str) and value:
                reply_to_message_id = value
                break
            value = reply_preview.get(key)
            if isinstance(value, str) and value:
                reply_to_message_id = value
                break

        fragments = preview.get("fragments")
        text_parts: list[str] = []
        if isinstance(fragments, list):
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    continue
                if fragment.get("kind") == "text" and isinstance(fragment.get("text"), str):
                    text_parts.append(fragment["text"])

        reply_to_text = "".join(text_parts) or None
        return reply_to_message_id, reply_to_text
