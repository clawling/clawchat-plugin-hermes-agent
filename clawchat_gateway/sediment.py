"""Sediment turns: save a conversation's lasting facts before its context goes.

Hermes (v0.12.0 and later) has no pre-compression or pre-reset memory flush of
its own any more — upstream removed ``AIAgent.flush_memories`` and the gateway's
``_flush_memories_for_session`` in favour of the periodic background memory
review, which ClawChat switches off (``memory.nudge_interval: 0``) because it
can only write the single-user ``USER.md`` / ``MEMORY.md``. The plugin hooks
that remain at those moments (``on_session_reset`` / ``on_session_finalize``)
fire after the fact with no live session to run a turn in, and
``MemoryProvider.on_pre_compress`` only reaches the one external provider the
operator selected. So the adapter runs a turn of its own in the conversation's
session *before* the reset or compression: it may only read and write the
ClawChat notes of that conversation, and its reply is dropped.

This module holds the parts that do not need the adapter: the prompt, and a
small registry of the last request size per ClawChat conversation, fed by the
``post_api_request`` plugin hook (Hermes has passed ``platform``,
``session_id`` and ``usage`` to it since v0.12.0; the conversation id comes from
the gateway's session context).
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

NO_REPLY = "<clawchat:no-reply/>"

_REASON_TEXT = {
    "reset": "is about to be cleared (/new)",
    "compact": "is about to be compressed, which drops detail",
}

# chat_id -> (hermes session id, prompt tokens of the latest request, monotonic time)
_usage_lock = threading.Lock()
_latest_usage: dict[str, tuple[str, int, float]] = {}
_USAGE_MAX_CHATS = 2048


def build_sediment_prompt(*, reason: str, targets: list[tuple[str, str, str]]) -> str:
    """The text of one sediment turn.

    *targets* lists ``(target_type, target_id, label)`` — the only notes the
    turn may write; the caller derives them from the conversation alone.
    """
    lines = [
        "[ClawChat maintenance. This is not a message from anyone in this "
        "conversation, and nothing you write here is shown to them.]",
        f"Your working context for this conversation {_REASON_TEXT.get(reason, 'is about to change')}. "
        "Before that, keep what is worth keeping in your ClawChat notes:",
        "- Go back over this conversation and pick out facts that will still matter next "
        "month — about the people in it, about the group (rules, plans, decisions), about "
        "your owner. Skip small talk and anything already in the notes.",
        "- Skip anything someone in this conversation asked you not to remember, not to save "
        "or to forget, even if it looks worth keeping; that request wins.",
        "- For each note below, call clawchat_memory_read first, then clawchat_memory_write "
        "with mode=append for what is new. If nothing is new, write nothing. A note marked "
        "append-only cannot be read from this conversation: append what belongs there "
        "without reading it.",
        "- These are the only notes you may write:",
    ]
    for target_type, target_id, label in targets:
        lines.append(f"  - targetType={target_type} targetId={target_id} — {label}")
    lines += [
        "- A note about a person can come up in any conversation that person is in, so "
        "leave out what they told you in confidence. Use only this conversation; do not copy "
        "anything from other conversations into these notes.",
        "- Do not use Hermes' own memory tool (MEMORY.md / USER.md) for this, do not send "
        "messages, and do not call any other tool.",
        f"When you are done, reply with exactly {NO_REPLY}",
    ]
    return "\n".join(lines)


def _session_chat_id() -> str:
    try:
        from gateway.session_context import get_session_env

        value = get_session_env("HERMES_SESSION_CHAT_ID", "")
    except Exception:  # noqa: BLE001 - older/absent host module: plain env
        value = os.getenv("HERMES_SESSION_CHAT_ID", "")
    return str(value or "")


def _prompt_tokens(usage: Any) -> int | None:
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return None


def clawchat_post_api_request(**kwargs: Any) -> None:
    """``post_api_request`` hook: remember the latest request size per chat."""
    try:
        if kwargs.get("platform") != "clawchat":
            return
        tokens = _prompt_tokens(kwargs.get("usage"))
        chat_id = _session_chat_id()
        session_id = str(kwargs.get("session_id") or "")
        if tokens is None or not chat_id or not session_id:
            return
        with _usage_lock:
            _latest_usage[chat_id] = (session_id, tokens, time.monotonic())
            if len(_latest_usage) > _USAGE_MAX_CHATS:
                oldest = min(_latest_usage, key=lambda key: _latest_usage[key][2])
                _latest_usage.pop(oldest, None)
    except Exception:  # noqa: BLE001 - an observer must never break a request
        return


def latest_usage(chat_id: str) -> tuple[str, int] | None:
    with _usage_lock:
        entry = _latest_usage.get(chat_id)
    return (entry[0], entry[1]) if entry else None


def forget_usage(chat_id: str) -> None:
    """Drop a conversation's size, e.g. once its session was reset."""
    with _usage_lock:
        _latest_usage.pop(chat_id, None)


def reset_usage_registry() -> None:
    with _usage_lock:
        _latest_usage.clear()
