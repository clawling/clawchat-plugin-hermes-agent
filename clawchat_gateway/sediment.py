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
the gateway's session context), together with that conversation's context window
(``context_length``, same hook) and output reservation (``max_tokens``, from
``pre_api_request``), so the compaction sediment can run before the point Hermes
actually compresses at (``compaction_point``).
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
# chat_id -> (context window, output reservation) of the latest request. The
# window comes with post_api_request (context_length), the reservation with
# pre_api_request (max_tokens); either may be unknown on an older host.
_latest_window: dict[str, tuple[int | None, int | None]] = {}

# Hermes' own small-window floor (agent/context_compressor.py) and default
# ratio (hermes_cli/config_defaults.py compression.threshold), used when the
# host's helpers cannot be imported.
_SMALL_WINDOW_LIMIT = 512_000
_SMALL_WINDOW_RATIO = 0.75
_DEFAULT_RATIO = 0.50
_MINIMUM_CONTEXT_LENGTH = 64_000
_MIN_CTX_TRIGGER_RATIO = 0.85


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
        "with mode=append for what is new. If nothing is new, write nothing. If owner.md is "
        "not listed, a fact about your owner goes into the note of whoever said it or this "
        "group's note.",
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
        context_length = _positive_int(kwargs.get("context_length"))
        with _usage_lock:
            _latest_usage[chat_id] = (session_id, tokens, time.monotonic())
            if context_length is not None:
                _, max_tokens = _latest_window.get(chat_id, (None, None))
                _latest_window[chat_id] = (context_length, max_tokens)
            if len(_latest_usage) > _USAGE_MAX_CHATS:
                oldest = min(_latest_usage, key=lambda key: _latest_usage[key][2])
                _latest_usage.pop(oldest, None)
                _latest_window.pop(oldest, None)
    except Exception:  # noqa: BLE001 - an observer must never break a request
        return


def clawchat_pre_api_request(**kwargs: Any) -> None:
    """``pre_api_request`` hook: remember the request's output reservation."""
    try:
        if kwargs.get("platform") != "clawchat":
            return
        chat_id = _session_chat_id()
        if not chat_id:
            return
        max_tokens = _positive_int(kwargs.get("max_tokens"))
        with _usage_lock:
            context_length, _ = _latest_window.get(chat_id, (None, None))
            _latest_window[chat_id] = (context_length, max_tokens)
    except Exception:  # noqa: BLE001 - an observer must never break a request
        return


def latest_window(chat_id: str) -> tuple[int | None, int | None]:
    """``(context_length, max_tokens)`` of the conversation's latest request."""
    with _usage_lock:
        return _latest_window.get(chat_id, (None, None))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _host_threshold_helpers() -> Any:
    """Hermes' ContextCompressor (its threshold maths), or None."""
    try:
        from agent.context_compressor import ContextCompressor

        if callable(getattr(ContextCompressor, "_compute_threshold_tokens", None)) and callable(
            getattr(ContextCompressor, "_effective_threshold_percent", None)
        ):
            return ContextCompressor
    except Exception:  # noqa: BLE001 - absent or different host
        pass
    return None


def _fallback_threshold_tokens(context_length: int, ratio: float, max_tokens: int | None) -> int:
    window = context_length - (max_tokens or 0)
    window = window if window > 0 else context_length
    pct_value = int(window * ratio)
    floored = max(pct_value, _MINIMUM_CONTEXT_LENGTH)
    trigger_cap = int(window * _MIN_CTX_TRIGGER_RATIO)
    if window > 0 and floored > pct_value and floored > trigger_cap:
        floored = max(pct_value, trigger_cap)
    if window > 0 and floored >= window:
        return max(1, min(trigger_cap, window - 1))
    return floored


def compaction_point(
    *,
    context_length: int | None,
    max_tokens: int | None,
    threshold_percent: float | None,
    threshold_tokens: int | None,
) -> int | None:
    """The prompt size at which Hermes will compress this conversation.

    The lower of the ratio threshold (raised to 0.75 below a 512K window,
    applied to the window minus the output reservation) and the absolute
    ``compression.threshold_tokens``. ``None`` when neither is known.
    """
    ratio = threshold_percent if threshold_percent and threshold_percent > 0 else _DEFAULT_RATIO
    ratio_point: int | None = None
    if context_length:
        helpers = _host_threshold_helpers()
        if helpers is not None:
            try:
                effective = helpers._effective_threshold_percent(context_length, ratio)
                ratio_point = int(helpers._compute_threshold_tokens(context_length, effective, max_tokens))
            except Exception:  # noqa: BLE001 - fall back to the same rule
                ratio_point = None
        if ratio_point is None:
            effective = max(ratio, _SMALL_WINDOW_RATIO) if context_length < _SMALL_WINDOW_LIMIT else ratio
            ratio_point = _fallback_threshold_tokens(context_length, effective, max_tokens)
    cap = threshold_tokens if threshold_tokens and threshold_tokens > 0 else None
    if cap is not None and context_length:
        cap = min(cap, context_length)
    candidates = [p for p in (ratio_point, cap) if p]
    return min(candidates) if candidates else None


def latest_usage(chat_id: str) -> tuple[str, int] | None:
    with _usage_lock:
        entry = _latest_usage.get(chat_id)
    return (entry[0], entry[1]) if entry else None


def forget_usage(chat_id: str) -> None:
    """Drop a conversation's size, e.g. once its session was reset."""
    with _usage_lock:
        _latest_usage.pop(chat_id, None)
        _latest_window.pop(chat_id, None)


def reset_usage_registry() -> None:
    with _usage_lock:
        _latest_usage.clear()
        _latest_window.clear()
