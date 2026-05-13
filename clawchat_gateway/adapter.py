"""ClawChatAdapter — BasePlatformAdapter for the ClawChat WebSocket protocol."""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.group_context import build_group_channel_prompt
from clawchat_gateway.inbound import InboundMessage, parse_inbound_message
from clawchat_gateway.media_runtime import (
    download_inbound_media,
    infer_media_kind_from_mime,
    normalize_outbound_media_reference,
    upload_outbound_media,
)
from clawchat_gateway.protocol import (
    build_message_add_event,
    build_message_created_event,
    build_message_done_event,
    build_message_failed_event,
    build_message_reply_event,
    build_typing_update_event,
    new_frame_id,
)
from clawchat_gateway.stream_buffer import compute_delta

logger = logging.getLogger("clawchat_gateway.adapter")
inbound_trace = logging.getLogger("clawchat_gateway.inbound_trace")

TYPING_REFRESH_SECONDS = 10.0
INBOUND_RATE_WINDOW_SECONDS = 30.0
INBOUND_RATE_WARN_THRESHOLD = 5

_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
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
_ACTIVATION_INTENT_RE = re.compile(
    r"(clawchat|claw\s*chat|激活码|激活|activate|activation|invite\s*code)",
    re.IGNORECASE,
)
_HERMES_STREAM_CURSOR_RE = re.compile(r"[ \t]*▉\Z")
_CLAWCHAT_SKILL_PROMPT = (
    "The user may be activating or configuring ClawChat. Use the installed "
    "clawchat skill instructions. If an activation code is present, run "
    "`python -m clawchat_gateway.activate CODE`; it writes the ClawChat token "
    "and refresh token and schedules a Hermes gateway restart. If no code is "
    "present, ask for the ClawChat activation code."
)


def _clawchat_platform():
    platform = getattr(Platform, "CLAWCHAT", None)
    if platform is not None:
        return platform
    return Platform("clawchat")


@dataclass
class _ActiveRun:
    chat_id: str
    chat_type: str
    message_id: str
    started_order: int
    last_text: str = ""
    reply_to_message_id: str | None = None
    sequence: int = -1


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
        )
        self._active_runs_by_id: dict[str, _ActiveRun] = {}
        self._active_chat_runs: dict[str, str] = {}
        self._typing_state: dict[str, tuple[bool, float]] = {}
        self._run_counter = 0
        self._inbound_window: dict[str, deque[float]] = {}

    async def connect(self) -> bool:
        await self._connection.start()
        return True

    async def disconnect(self) -> None:
        await self._connection.stop()

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
            )
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
            )
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
        logger.info("clawchat state -> %s", state.value)

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
        message_id = (
            payload.get("message_id")
            or message.get("message_id")
            or message.get("id")
        )
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
        if frame.get("event") == "interaction.submit":
            await self._handle_interaction_submit(frame)
            return
        inbound = parse_inbound_message(frame, self._clawchat_config)
        if inbound is None:
            logger.warning(
                "clawchat inbound dropped event=%s chat_id=%s reason=parse_or_filter_failed",
                frame.get("event"),
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
        if self._should_attach_activation_skill(inbound.text):
            event.auto_skill = "clawchat"
        if channel_prompt:
            event.channel_prompt = channel_prompt
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

    async def _handle_interaction_submit(self, frame: dict[str, Any]) -> None:
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        text = self._interaction_submit_text(payload)
        if text is None:
            logger.warning(
                "clawchat interaction submit dropped chat_id=%s reason=unsupported_action",
                frame.get("chat_id"),
            )
            return
        sender = frame.get("sender") if isinstance(frame.get("sender"), dict) else {}
        chat_id = str(frame.get("chat_id") or "")
        sender_id = str(sender.get("id") or "")
        source = self.build_source(
            chat_id=chat_id,
            user_id=sender_id,
            chat_name=chat_id,
            chat_type=self._map_source_chat_type(str(frame.get("chat_type") or "direct")),
        )
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            raw_message={
                "clawchat_chat_type": frame.get("chat_type"),
                "clawchat_interaction_submit": payload,
                "clawchat_raw": frame,
            },
        )
        await self.handle_message(event)

    def _interaction_submit_text(self, payload: dict[str, Any]) -> str | None:
        action_id = str(payload.get("action_id") or "").strip().lower()
        action_payload = payload.get("action_payload")
        decision = ""
        if isinstance(action_payload, dict):
            decision = str(action_payload.get("decision") or "").strip().lower()
        value = decision or action_id
        if value in {"approve", "approved", "allow", "yes"}:
            return "/approve"
        if value in {"deny", "denied", "reject", "rejected", "no"}:
            return "/deny"
        return None

    def _should_attach_activation_skill(self, text: str) -> bool:
        if not text:
            return False
        normalized = text.strip()
        if not _ACTIVATION_INTENT_RE.search(normalized):
            return False
        return True

    def _compose_channel_prompt(self, inbound: InboundMessage) -> str | None:
        prompts: list[str] = []
        if inbound.chat_type == "group":
            group_prompt = build_group_channel_prompt()
            if group_prompt:
                prompts.append(group_prompt)
        if self._should_attach_activation_skill(inbound.text):
            prompts.append(_CLAWCHAT_SKILL_PROMPT)
        return "\n\n".join(prompts) or None

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
        if self._should_suppress_tool_progress(content or ""):
            logger.info("clawchat tool progress suppressed chat_id=%s text_len=%d", chat_id, len(content or ""))
            return SendResult(success=True)
        visible_content = self._filter_output_content(content or "")
        fragments = await self._build_fragments(visible_content, metadata, kwargs)
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
            await self._connection.send_frame(
                build_message_reply_event(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    message_id=message_id,
                    fragments=fragments,
                    reply_to_message_id=reply_to,
                )
            )
            logger.info(
                "clawchat send static reply queued chat_id=%s message_id=%s fragments=%d",
                chat_id,
                message_id,
                len(fragments),
            )
            return SendResult(success=True, message_id=message_id)

        run = _ActiveRun(
            chat_id=chat_id,
            chat_type=chat_type,
            message_id=message_id,
            started_order=self._next_run_order(),
            reply_to_message_id=reply_to,
        )
        self._active_runs_by_id[message_id] = run
        self._active_chat_runs[chat_id] = message_id

        await self._connection.send_frame(
            build_message_created_event(
                chat_id=chat_id,
                chat_type=chat_type,
                message_id=message_id,
            )
        )
        if visible_content:
            run.last_text, delta = compute_delta(run.last_text, visible_content)
            run.sequence += 1
            await self._connection.send_frame(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=chat_type,
                    message_id=message_id,
                    full_text=run.last_text,
                    delta=delta,
                    sequence=run.sequence,
                )
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
        if run is None:
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
        full_text, delta = compute_delta(run.last_text, visible_content)
        if delta:
            await self._connection.send_frame(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=run.chat_type,
                    message_id=run.message_id,
                    full_text=full_text,
                    delta=delta,
                    sequence=run.sequence + 1,
                )
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
        if run is None:
            logger.warning(
                "clawchat run complete skipped chat_id=%s message_id=%s reason=no_active_run",
                chat_id,
                message_id,
            )
            return
        self._discard_run(run)
        logger.info(
            "clawchat run complete chat_id=%s message_id=%s final_len=%d",
            chat_id,
            run.message_id,
            len(self._filter_output_content(final_text or "")),
        )

        visible_final_text = self._filter_output_content(final_text or "")
        full_text, delta = compute_delta(run.last_text, visible_final_text)
        if delta:
            run.sequence += 1
            await self._connection.send_frame(
                build_message_add_event(
                    chat_id=chat_id,
                    chat_type=run.chat_type,
                    message_id=run.message_id,
                    full_text=full_text,
                    delta=delta,
                    sequence=run.sequence,
                )
            )
            run.last_text = full_text

        await self._connection.send_frame(
            build_message_done_event(
                chat_id=chat_id,
                chat_type=run.chat_type,
                message_id=run.message_id,
                fragments=await self._build_fragments(run.last_text),
                sequence=run.sequence,
            )
        )
        await self._connection.send_frame(
            build_message_reply_event(
                chat_id=chat_id,
                chat_type=run.chat_type,
                message_id=run.message_id,
                fragments=await self._build_fragments(run.last_text),
                reply_to_message_id=run.reply_to_message_id,
                include_message_id=True,
            )
        )
        logger.info(
            "clawchat stream done queued chat_id=%s message_id=%s",
            chat_id,
            run.message_id,
        )

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
        await self._connection.send_frame(
            build_message_failed_event(
                chat_id=chat_id,
                chat_type=run.chat_type,
                message_id=run.message_id,
                sequence=max(run.sequence, 0),
                reason=error,
            )
        )
        logger.info(
            "clawchat stream failed queued chat_id=%s message_id=%s",
            chat_id,
            run.message_id,
        )

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

    def _should_suppress_tool_progress(self, content: str) -> bool:
        if self._clawchat_config.show_tool_progress:
            return False
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            return False
        return all(_TOOL_PROGRESS_LINE_RE.match(line) for line in lines)

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
