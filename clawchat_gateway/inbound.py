from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clawchat_gateway.config import ClawChatConfig, effective_group_mode


@dataclass(frozen=True)
class InboundMessage:
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    text: str
    raw_message: dict[str, Any]
    reply_preview: dict[str, Any] | None = None
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    return None


def _coerce_fragments(message: dict[str, Any]) -> list[Any]:
    fragments = message.get("fragments")
    if isinstance(fragments, list):
        return fragments

    body = message.get("body")
    if isinstance(body, list):
        return body
    if isinstance(body, str):
        return [{"kind": "text", "text": body}]
    if isinstance(body, dict):
        for key in ("fragments", "parts", "items"):
            value = body.get(key)
            if isinstance(value, list):
                return value
        for key in ("text", "content", "value"):
            value = body.get(key)
            if isinstance(value, str):
                return [{"kind": "text", "text": value}]

    return []


def _fragment_kind(fragment: dict[str, Any]) -> str | None:
    value = fragment.get("kind") or fragment.get("type")
    if isinstance(value, str):
        return value
    return None


def _fragment_text(fragment: dict[str, Any]) -> str | None:
    for key in ("text", "content", "value"):
        value = fragment.get(key)
        if isinstance(value, str):
            return value
    return None


def parse_inbound_message(
    envelope: dict[str, Any], config: ClawChatConfig
) -> InboundMessage | None:
    payload = _as_dict(envelope.get("payload") or {})
    if payload is None:
        return None

    message = _as_dict(payload.get("message") or {})
    if message is None:
        return None

    context = _as_dict(message.get("context") or {})
    if context is None:
        return None

    chat_type = envelope.get("chat_type")
    if chat_type not in {"direct", "group"}:
        return None

    if (
        chat_type == "group"
        and effective_group_mode(config, envelope.get("chat_id") or "") == "mention"
    ):
        mentions = context.get("mentions") or []
        if not any(
            mention.get("id") == config.user_id
            for mention in mentions
            if isinstance(mention, dict)
        ):
            return None

    fragments = _coerce_fragments(message)
    text_parts: list[str] = []
    media_urls: list[str] = []
    media_types: list[str] = []

    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        kind = _fragment_kind(fragment)
        text = _fragment_text(fragment)
        if kind in (None, "text") and text is not None:
            text_parts.append(text)
            continue
        if kind in {"image", "file", "audio", "video"} and isinstance(
            fragment.get("url"), str
        ):
            media_urls.append(fragment["url"])
            media_types.append(kind)
            label = fragment.get("name") or fragment["url"]
            if kind == "image":
                text_parts.append(f"![{label}]({fragment['url']})")
            else:
                text_parts.append(f"[{label}]({fragment['url']})")

    sender = _as_dict(envelope.get("sender") or {})
    if sender is None:
        return None

    return InboundMessage(
        chat_id=envelope.get("chat_id") or "",
        chat_type=chat_type,
        sender_id=sender.get("id") or "",
        sender_name=sender.get("nick_name") or "",
        text="\n".join(part for part in text_parts if part),
        raw_message=envelope,
        reply_preview=_as_dict(context.get("reply")),
        media_urls=media_urls,
        media_types=media_types,
    )
