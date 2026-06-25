from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from collections.abc import Mapping
from typing import Any

from clawchat_gateway.llm_context_debug import ClawChatLlmContextDebug

_PendingItem = dict[str, Any]
_PENDING_INJECTION_PARTS: dict[tuple[str, str], deque[_PendingItem]] = defaultdict(deque)


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", None)
    if isinstance(value, str) and value:
        return value.strip().lower()
    name = getattr(platform, "name", None)
    if isinstance(name, str) and name:
        return name.strip().lower()
    return str(platform or "").strip().lower()


def _pending_key(platform: Any, user_message: Any) -> tuple[str, str]:
    return (_platform_name(platform), str(user_message or ""))


def clear_pending_injection_parts() -> None:
    _PENDING_INJECTION_PARTS.clear()


def remember_injection_parts(
    *,
    platform: str,
    user_message: str,
    parts: list[Mapping[str, Any]],
    trace: Mapping[str, Any] | None = None,
) -> None:
    debugger = ClawChatLlmContextDebug()
    if not debugger.enabled or not debugger.capture_full_input:
        return
    _PENDING_INJECTION_PARTS[_pending_key(platform, user_message)].append(
        {
            "parts": [dict(part) for part in parts],
            "trace": dict(trace or {}),
        }
    )


def _consume_injection_parts(platform: Any, user_message: Any) -> _PendingItem:
    queue = _PENDING_INJECTION_PARTS.get(_pending_key(platform, user_message))
    if not queue:
        return {"parts": [], "trace": {}}
    item = queue.popleft()
    if not queue:
        _PENDING_INJECTION_PARTS.pop(_pending_key(platform, user_message), None)
    return item


def _mapping_from_message(message: Any) -> dict[str, Any]:
    if isinstance(message, Mapping):
        return dict(message)
    data: dict[str, Any] = {}
    for key in ("role", "content", "name", "tool_call_id", "tool_calls"):
        if hasattr(message, key):
            data[key] = getattr(message, key)
    return data or {"value": repr(message)}


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def _request_messages_from_kwargs(kwargs: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("request_messages", "messages"):
        value = kwargs.get(key)
        if isinstance(value, list):
            return [_mapping_from_message(message) for message in value]
    request = kwargs.get("request")
    if isinstance(request, Mapping):
        messages = request.get("messages")
        if isinstance(messages, list):
            return [_mapping_from_message(message) for message in messages]
    messages = getattr(request, "messages", None)
    if isinstance(messages, list):
        return [_mapping_from_message(message) for message in messages]
    return []


def _request_tools_from_kwargs(kwargs: Mapping[str, Any]) -> list[Any]:
    request = kwargs.get("request")
    if isinstance(request, Mapping):
        tools = request.get("tools")
        return list(tools) if isinstance(tools, list) else []
    tools = getattr(request, "tools", None)
    return list(tools) if isinstance(tools, list) else []


def _message_content_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except TypeError:
        return repr(content)


def build_placement_checks(
    *,
    parts: list[Mapping[str, Any]],
    request_messages: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        content = str(part.get("content") or "")
        part_id = str(part.get("id") or f"injection-{index}")
        target = str(part.get("target") or "system.channel_prompt")
        group = str(part.get("group") or "")
        found_index: int | None = None
        found_role: str | None = None
        if content:
            for message_index, message in enumerate(request_messages):
                if content in _message_content_text(message):
                    found_index = message_index
                    found_role = str(message.get("role") or "")
                    break
        check: dict[str, Any] = {
            "partId": part_id,
            "group": group,
            "target": target,
            "found": found_index is not None,
            "messageIndex": found_index,
            "role": found_role,
        }
        checks.append(check)
    return checks


def _full_llm_input_from_kwargs(
    kwargs: Mapping[str, Any],
    request_messages: list[Mapping[str, Any]],
    request_tools: list[Any],
) -> dict[str, Any]:
    request = kwargs.get("request")
    if isinstance(request, Mapping):
        full_input = {str(key): _jsonable(value) for key, value in request.items()}
        full_input["messages"] = request_messages
        if request_tools:
            full_input["tools"] = request_tools
        return full_input
    full_input = {"messages": request_messages}
    if request_tools:
        full_input["tools"] = request_tools
    model = kwargs.get("model") or getattr(request, "model", None)
    if model:
        full_input["model"] = str(model)
    return full_input


def _clawchat_pre_api_request(**kwargs: Any) -> None:
    debugger = ClawChatLlmContextDebug()
    if not debugger.enabled or not debugger.capture_full_input:
        return None

    platform = kwargs.get("platform") or kwargs.get("platform_name") or "clawchat"
    user_message = kwargs.get("user_message") or kwargs.get("message") or ""
    pending = _consume_injection_parts(platform, user_message)
    parts = list(pending.get("parts") or [])
    request_messages = _request_messages_from_kwargs(kwargs)
    request_tools = _request_tools_from_kwargs(kwargs)
    placement_checks = build_placement_checks(
        parts=parts,
        request_messages=request_messages,
    )
    trace = dict(pending.get("trace") or {})
    if kwargs.get("session_id") is not None:
        trace["sessionId"] = str(kwargs.get("session_id"))
    trace["platform"] = str(platform or "")

    debugger.write_snapshot(
        visibility="full_llm_input",
        trace=trace,
        context={"injectionParts": parts},
        input={
            "requestMessages": request_messages,
            "requestTools": request_tools,
            "placementChecks": placement_checks,
            "fullLlmInput": _full_llm_input_from_kwargs(
                kwargs,
                request_messages,
                request_tools,
            ),
        },
    )
    return None
