from __future__ import annotations

import contextvars
import time
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_TERMINAL_SEND_TTL_SECONDS = 60.0


@dataclass
class TerminalClawChatSendRecord:
    message_id: str
    expires_at: float
    scope_id: str


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
_terminal_send_scope: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "clawchat_terminal_send_scope",
    default=None,
)
_terminal_sends: dict[tuple[str, str, str], TerminalClawChatSendRecord] = {}


def _current_terminal_send_scope(*, create: bool = False) -> str | None:
    scope_id = _terminal_send_scope.get()
    if scope_id is None and create:
        scope_id = f"term-{uuid.uuid4()}"
        _terminal_send_scope.set(scope_id)
    return scope_id


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
    scope_id: str | None = None,
) -> None:
    effective_scope = scope_id or _current_terminal_send_scope(create=True)
    if effective_scope is None:
        return
    _terminal_sends[(account_id, chat_id, effective_scope)] = TerminalClawChatSendRecord(
        message_id=message_id,
        expires_at=(now if now is not None else time.time()) + ttl_seconds,
        scope_id=effective_scope,
    )


def consume_terminal_clawchat_send(
    *,
    account_id: str,
    chat_id: str,
    now: float | None = None,
    scope_id: str | None = None,
) -> TerminalClawChatSendRecord | None:
    effective_scope = scope_id or _current_terminal_send_scope()
    if effective_scope is None:
        return None
    key = (account_id, chat_id, effective_scope)
    record = _terminal_sends.pop(key, None)
    if record is None:
        return None
    if record.expires_at <= (now if now is not None else time.time()):
        return None
    return record


def clear_terminal_clawchat_sends_for_test() -> None:
    _terminal_sends.clear()
    _terminal_send_scope.set(None)
