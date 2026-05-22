from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from clawchat_gateway.inbound import InboundMessage

logger = logging.getLogger("clawchat_gateway.group_message_coalescer")


def _message_id(message: InboundMessage) -> str:
    raw = message.raw_message if isinstance(message.raw_message, dict) else {}
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    value = payload.get("message_id")
    return value if isinstance(value, str) and value else "-"


def format_coalesced_group_text(messages: list[InboundMessage], *, window_seconds: float) -> str:
    seconds = int(window_seconds)
    header = f"ClawChat group batch ({len(messages)} {'message' if len(messages) == 1 else 'messages'}, {seconds}s window):"
    lines = [header]
    for index, message in enumerate(messages, start=1):
        sender_name = message.sender_name or message.sender_id
        body = message.text or "(empty message)"
        lines.append(f"{index}. [{_message_id(message)}] {sender_name} ({message.sender_id}): {body}")
    return "\n".join(lines)


class GroupMessageCoalescer:
    def __init__(
        self,
        *,
        window_seconds: float,
        dispatch: Callable[[InboundMessage], Awaitable[None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        log: logging.Logger = logger,
    ) -> None:
        self._window_seconds = window_seconds
        self._dispatch = dispatch
        self._sleep = sleep
        self._log = log
        self._pending: dict[str, list[InboundMessage]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def enqueue(self, message: InboundMessage) -> None:
        batch = self._pending.setdefault(message.chat_id, [])
        batch.append(message)
        if message.chat_id not in self._tasks:
            self._tasks[message.chat_id] = asyncio.create_task(
                self._flush_later(message.chat_id),
                name=f"clawchat-group-coalesce-{message.chat_id}",
            )

    async def cancel(self) -> None:
        tasks = list(self._tasks.values())
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

    async def flush_now(self, chat_id: str) -> None:
        task = self._tasks.get(chat_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self.flush(chat_id)

    async def _flush_later(self, chat_id: str) -> None:
        task = asyncio.current_task()
        try:
            await self._sleep(self._window_seconds)
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

    async def flush(self, chat_id: str) -> None:
        batch = self._pending.pop(chat_id, [])
        if not batch:
            return
        task = asyncio.current_task()
        if self._tasks.get(chat_id) is task:
            self._tasks.pop(chat_id, None)
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
            text=format_coalesced_group_text(batch, window_seconds=self._window_seconds),
            raw_message=merged_raw,
            media_urls=[url for message in batch for url in message.media_urls],
            media_types=[kind for message in batch for kind in message.media_types],
            was_mentioned=any(message.was_mentioned for message in batch),
            mentioned_user_ids=mentioned_user_ids,
        )
        await self._dispatch(merged)
