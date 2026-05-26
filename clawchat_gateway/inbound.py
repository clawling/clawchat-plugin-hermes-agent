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
    was_mentioned: bool = False
    mentioned_user_ids: list[str] = field(default_factory=list)
    mentioned_users: list[dict[str, str]] = field(default_factory=list)
    sender_relation: str = ""
    sender_profile_type: str = ""


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


def _mention_id(mention: dict[str, Any]) -> str | None:
    for key in ("user_id", "userId", "id"):
        value = mention.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _mention_display(mention: dict[str, Any]) -> str | None:
    for key in ("display", "label", "name", "nick_name", "nickname"):
        value = mention.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().removeprefix("@")
    return None


def _extract_mentioned_users(mentions: Any) -> list[dict[str, str]]:
    if not isinstance(mentions, list):
        return []

    mentioned_users: list[dict[str, str]] = []
    seen: set[str] = set()
    for mention in mentions:
        mention_id: str | None = None
        display: str | None = None
        if isinstance(mention, dict):
            mention_id = _mention_id(mention)
            display = _mention_display(mention)
        elif isinstance(mention, str) and mention:
            mention_id = mention

        if mention_id is not None and mention_id not in seen:
            seen.add(mention_id)
            item = {"id": mention_id}
            if display:
                item["display"] = display
            mentioned_users.append(item)

    return mentioned_users


def _mentioned_user_ids(mentions: list[dict[str, str]]) -> list[str]:
    return [mention["id"] for mention in mentions if mention.get("id")]


def _merge_mentioned_users(*sources: list[dict[str, str]]) -> list[dict[str, str]]:
    merged: dict[str, dict[str, str]] = {}
    for source in sources:
        for mention in source:
            mention_id = mention.get("id")
            if not mention_id:
                continue
            existing = merged.get(mention_id)
            if existing is None or (not existing.get("display") and mention.get("display")):
                merged[mention_id] = mention
    return list(merged.values())


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

    context_mentioned_users = _extract_mentioned_users(context.get("mentions"))
    fragment_mentioned_users: list[dict[str, str]] = []
    for fragment in _coerce_fragments(message):
        if isinstance(fragment, dict) and _fragment_kind(fragment) == "mention":
            mention_id = _mention_id(fragment)
            if mention_id:
                item = {"id": mention_id}
                display = _mention_display(fragment)
                if display:
                    item["display"] = display
                fragment_mentioned_users.append(item)
    mentioned_users = _merge_mentioned_users(fragment_mentioned_users, context_mentioned_users)
    mentioned_user_ids = _mentioned_user_ids(mentioned_users)
    was_mentioned = config.user_id in mentioned_user_ids

    if (
        chat_type == "group"
        and effective_group_mode(config, envelope.get("chat_id") or "") == "mention"
        and not was_mentioned
    ):
        return None

    fragments = _coerce_fragments(message)
    text_parts: list[str] = []
    last_text_part_is_inline = False
    media_urls: list[str] = []
    media_types: list[str] = []

    def append_inline_text(value: str) -> None:
        nonlocal last_text_part_is_inline
        if not value:
            return
        if last_text_part_is_inline and text_parts:
            text_parts[-1] += value
        else:
            text_parts.append(value)
        last_text_part_is_inline = True

    def append_block_text(value: str) -> None:
        nonlocal last_text_part_is_inline
        if not value:
            return
        text_parts.append(value)
        last_text_part_is_inline = False

    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        kind = _fragment_kind(fragment)
        text = _fragment_text(fragment)
        if kind in (None, "text") and text is not None:
            append_inline_text(text)
            continue
        if kind == "mention":
            mention_id = _mention_id(fragment)
            display = _mention_display(fragment)
            if display or mention_id:
                append_inline_text(f"@{display or mention_id}")
            continue
        if kind in {"image", "file", "audio", "video"} and isinstance(
            fragment.get("url"), str
        ):
            media_urls.append(fragment["url"])
            media_types.append(kind)
            label = fragment.get("name") or fragment["url"]
            if kind == "image":
                append_block_text(f"![{label}]({fragment['url']})")
            else:
                append_block_text(f"[{label}]({fragment['url']})")

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
        was_mentioned=was_mentioned,
        mentioned_user_ids=mentioned_user_ids,
        mentioned_users=mentioned_users,
    )
