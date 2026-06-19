from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from clawchat_gateway.inbound import InboundMessage

logger = logging.getLogger("clawchat_gateway.group_message_coalescer")


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
    _ = idle_seconds, max_wait_seconds
    lines = ["ClawChat group messages:"]
    for index, message in enumerate(messages, start=1):
        sender_name = message.sender_name or message.sender_id
        label = f"[message {index}] {_message_field(sender_name)}:"
        body = _message_body(message)
        if "\n" in body:
            lines.append(f"{label}\n{body}")
        else:
            lines.append(f"{label} {body}")
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

    def enqueue(self, message: InboundMessage, *, idle_seconds_override: float | None = None) -> None:
        batch = self._pending.setdefault(message.chat_id, [])
        batch.append(message)
        self._reset_idle_task(message.chat_id, idle_seconds_override=idle_seconds_override)
        if message.chat_id not in self._max_wait_tasks:
            self._max_wait_tasks[message.chat_id] = asyncio.create_task(
                self._flush_after_max_wait(message.chat_id),
                name=f"clawchat-group-coalesce-max-{message.chat_id}",
            )

    def _reset_idle_task(self, chat_id: str, *, idle_seconds_override: float | None = None) -> None:
        task = self._tasks.pop(chat_id, None)
        if task is not None:
            task.cancel()
        self._tasks[chat_id] = asyncio.create_task(
            self._flush_after_idle(chat_id, idle_seconds=idle_seconds_override if idle_seconds_override is not None else self._idle_seconds),
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

    async def _flush_after_idle(self, chat_id: str, *, idle_seconds: float | None = None) -> None:
        task = asyncio.current_task()
        try:
            await self._sleep(idle_seconds if idle_seconds is not None else self._idle_seconds)
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
        mentioned_users: list[dict[str, str]] = []
        seen_mention_ids = set()
        for message in batch:
            message_mentions = message.mentioned_users or [
                {"id": user_id} for user_id in message.mentioned_user_ids
            ]
            for mention in message_mentions:
                user_id = mention.get("id")
                if not user_id:
                    continue
                if user_id not in seen_mention_ids:
                    mentioned_user_ids.append(user_id)
                    mentioned_users.append(mention)
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
            mentioned_users=mentioned_users,
        )
        await self._dispatch(merged)
