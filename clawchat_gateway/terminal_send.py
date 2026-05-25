from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TERMINAL_SEND_TTL_SECONDS = 60.0


@dataclass
class TerminalClawChatSendRecord:
    message_id: str
    expires_at: float


class ClawChatMentionSender(Protocol):
    async def send_mention_message(
        self,
        *,
        chat_id: str,
        chat_type: str = "group",
        text: str | None = None,
        mentions: list[dict[str, Any]],
        reply_to_message_id: str | None = None,
    ) -> dict[str, Any]:
        ...


_active_sender: ClawChatMentionSender | None = None
_terminal_sends: dict[tuple[str, str], TerminalClawChatSendRecord] = {}


def set_clawchat_mention_sender(sender: ClawChatMentionSender) -> None:
    global _active_sender
    _active_sender = sender


def clear_clawchat_mention_sender(sender: ClawChatMentionSender | None = None) -> None:
    global _active_sender
    if sender is None or _active_sender is sender:
        _active_sender = None


async def send_clawchat_mention_message(
    *,
    chat_id: str,
    chat_type: str = "group",
    text: str | None = None,
    mentions: list[dict[str, Any]],
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    if _active_sender is None:
        raise RuntimeError("ClawChat websocket sender is not ready")
    return await _active_sender.send_mention_message(
        chat_id=chat_id,
        chat_type=chat_type,
        text=text,
        mentions=mentions,
        reply_to_message_id=reply_to_message_id,
    )


def mark_terminal_clawchat_send(
    *,
    account_id: str,
    chat_id: str,
    message_id: str,
    ttl_seconds: float = DEFAULT_TERMINAL_SEND_TTL_SECONDS,
    now: float | None = None,
) -> None:
    _terminal_sends[(account_id, chat_id)] = TerminalClawChatSendRecord(
        message_id=message_id,
        expires_at=(now if now is not None else time.time()) + ttl_seconds,
    )


def consume_terminal_clawchat_send(
    *,
    account_id: str,
    chat_id: str,
    now: float | None = None,
) -> TerminalClawChatSendRecord | None:
    key = (account_id, chat_id)
    record = _terminal_sends.pop(key, None)
    if record is None:
        return None
    if record.expires_at <= (now if now is not None else time.time()):
        return None
    return record


def clear_terminal_clawchat_sends_for_test() -> None:
    _terminal_sends.clear()
