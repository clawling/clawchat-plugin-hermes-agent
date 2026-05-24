from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from clawchat_gateway.inbound import InboundMessage

logger = logging.getLogger("clawchat_gateway.group_message_coalescer")


def _message_time(message: InboundMessage) -> str:
    raw = message.raw_message if isinstance(message.raw_message, dict) else {}
    emitted_at = raw.get("emitted_at")
    if isinstance(emitted_at, (int, float)):
        try:
            return (
                datetime.fromtimestamp(emitted_at / 1000, UTC)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except (OverflowError, OSError, ValueError):
            pass
    return "unknown-time"


def _message_relation(message: InboundMessage) -> str:
    return message.sender_relation or "peer_user"


def _message_profile_type(message: InboundMessage) -> str:
    if message.sender_profile_type:
        return message.sender_profile_type
    if _message_relation(message) in {"self_agent", "peer_agent"}:
        return "agent"
    return "user"


def _message_is_owner(message: InboundMessage) -> str:
    return "true" if _message_relation(message) == "owner" else "false"


def _message_mentions(message: InboundMessage) -> str:
    return ",".join(message.mentioned_user_ids) or "-"


def _message_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _message_body(message: InboundMessage) -> str:
    return message.text or "(empty message)"


def format_coalesced_group_text(
    messages: list[InboundMessage],
    *,
    idle_seconds: float,
    max_wait_seconds: float,
) -> str:
    idle = int(idle_seconds)
    max_wait = int(max_wait_seconds)
    header = f"ClawChat group batch ({len(messages)} {'message' if len(messages) == 1 else 'messages'}, {idle}s idle, {max_wait}s max):"
    lines = [header]
    for message in messages:
        sender_name = message.sender_name or message.sender_id
        if len(lines) > 1:
            lines.append("")
        lines.append("[message]")
        lines.append(f"sender_id: {_message_field(message.sender_id)}")
        lines.append(f"sender_name: {_message_field(sender_name)}")
        lines.append(f"sender_profile_type: {_message_field(_message_profile_type(message))}")
        lines.append(f"sender_is_owner: {_message_is_owner(message)}")
        lines.append(f"mentions_current_agent: {'true' if message.was_mentioned else 'false'}")
        lines.append(f"mentioned_user_ids: {_message_field(_message_mentions(message))}")
        lines.append("text:")
        lines.append(_message_body(message))
    return "\n".join(lines)


class GroupMessageCoalescer:
    def __init__(
        self,
        *,
        idle_seconds: float,
        max_wait_seconds: float,
        dispatch: Callable[[InboundMessage], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        log: logging.Logger = logger,
    ) -> None:
        self._idle_seconds = idle_seconds
        self._max_wait_seconds = max_wait_seconds
        self._dispatch = dispatch
        self._sleep = sleep
        self._log = log
        self._pending: dict[str, list[InboundMessage]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._max_wait_tasks: dict[str, asyncio.Task[None]] = {}

    def enqueue(self, message: InboundMessage) -> None:
        batch = self._pending.setdefault(message.chat_id, [])
        batch.append(message)
        self._reset_idle_task(message.chat_id)
        if message.chat_id not in self._max_wait_tasks:
            self._max_wait_tasks[message.chat_id] = asyncio.create_task(
                self._flush_after_max_wait(message.chat_id),
                name=f"clawchat-group-coalesce-max-{message.chat_id}",
            )

    def _reset_idle_task(self, chat_id: str) -> None:
        task = self._tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
        self._tasks[chat_id] = asyncio.create_task(
            self._flush_after_idle(chat_id),
            name=f"clawchat-group-coalesce-idle-{chat_id}",
        )

    async def cancel(self) -> None:
        tasks = list(self._tasks.values()) + list(self._max_wait_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for chat_id, batch in list(self._pending.items()):
            self._log.info(
                "clawchat dropped pending group batch chat_id=%s count=%d reason=shutdown",
                chat_id,
                len(batch),
            )
        self._pending.clear()
        self._tasks.clear()
        self._max_wait_tasks.clear()

    async def flush_now(self, chat_id: str) -> None:
        task = self._tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
        max_wait_task = self._max_wait_tasks.pop(chat_id, None)
        if max_wait_task is not None:
            max_wait_task.cancel()
        tasks = [item for item in (task, max_wait_task) if item is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.flush(chat_id)

    async def _flush_after_idle(self, chat_id: str) -> None:
        task = asyncio.current_task()
        try:
            await self._sleep(self._idle_seconds)
            await self.flush(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.warning(
                "clawchat group batch dispatch failed chat_id=%s",
                chat_id,
                exc_info=True,
            )
        finally:
            if self._tasks.get(chat_id) is task:
                self._tasks.pop(chat_id, None)

    async def _flush_after_max_wait(self, chat_id: str) -> None:
        task = asyncio.current_task()
        try:
            await self._sleep(self._max_wait_seconds)
            await self.flush(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log.warning(
                "clawchat group batch dispatch failed chat_id=%s",
                chat_id,
                exc_info=True,
            )
        finally:
            if self._max_wait_tasks.get(chat_id) is task:
                self._max_wait_tasks.pop(chat_id, None)

    async def flush(self, chat_id: str) -> None:
        batch = self._pending.pop(chat_id, [])
        if not batch:
            return
        task = asyncio.current_task()
        if self._tasks.get(chat_id) is task:
            self._tasks.pop(chat_id, None)
        elif chat_id in self._tasks:
            idle_task = self._tasks.pop(chat_id)
            idle_task.cancel()
        if self._max_wait_tasks.get(chat_id) is task:
            self._max_wait_tasks.pop(chat_id, None)
        elif chat_id in self._max_wait_tasks:
            max_wait_task = self._max_wait_tasks.pop(chat_id)
            max_wait_task.cancel()
        latest = batch[-1]
        mentioned_user_ids = []
        seen_mention_ids = set()
        for message in batch:
            for user_id in message.mentioned_user_ids:
                if user_id not in seen_mention_ids:
                    mentioned_user_ids.append(user_id)
                    seen_mention_ids.add(user_id)
        merged_raw: dict[str, Any] = {
            "clawchat_group_batch": True,
            "messages": [message.raw_message for message in batch],
        }
        merged = replace(
            latest,
            text=format_coalesced_group_text(
                batch,
                idle_seconds=self._idle_seconds,
                max_wait_seconds=self._max_wait_seconds,
            ),
            raw_message=merged_raw,
            media_urls=[url for message in batch for url in message.media_urls],
            media_types=[kind for message in batch for kind in message.media_types],
            was_mentioned=any(message.was_mentioned for message in batch),
            mentioned_user_ids=mentioned_user_ids,
        )
        await self._dispatch(merged)
