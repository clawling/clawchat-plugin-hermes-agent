from __future__ import annotations

from typing import Any

TERMINAL_REPLY_INSTRUCTION = (
    "The mention message has already been sent to ClawChat. "
    "The ClawChat adapter suppresses the same-turn normal follow-up reply."
)


def normalize_mention_targets(mentions: Any) -> list[dict[str, str]]:
    if not isinstance(mentions, list) or not mentions:
        raise ValueError("clawchat_mention_message requires at least one mention")

    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for index, mention in enumerate(mentions):
        if not isinstance(mention, dict):
            raise ValueError(f"clawchat_mention_message requires mentions[{index}].userId")
        raw_user_id = mention.get("userId")
        user_id = raw_user_id.strip() if isinstance(raw_user_id, str) else ""
        if not user_id:
            raise ValueError(f"clawchat_mention_message requires mentions[{index}].userId")
        if user_id in seen:
            continue

        raw_display = mention.get("display")
        display = raw_display.strip() if isinstance(raw_display, str) else ""
        if display.startswith("@"):
            display = display[1:]
        if not display:
            raise ValueError(f"clawchat_mention_message requires mentions[{index}].display")
        seen.add(user_id)
        normalized.append({"userId": user_id, "display": display})
    return normalized


def apply_text_mention_labels(
    mentions: list[dict[str, str]],
    text: str | None,
) -> tuple[list[dict[str, str]], str]:
    remaining = text.strip() if isinstance(text, str) else ""
    return [dict(mention) for mention in mentions], remaining


def build_mention_message_fragments(
    *,
    mentions: Any,
    text: str | None = None,
) -> list[dict[str, Any]]:
    normalized, remaining_text = apply_text_mention_labels(normalize_mention_targets(mentions), text)
    fragments: list[dict[str, Any]] = []
    for mention in normalized:
        fragments.append(
            {
                "kind": "mention",
                "user_id": mention["userId"],
                "display": mention["display"],
            }
        )
    if remaining_text:
        fragments.append({"kind": "text", "text": f" {remaining_text}"})
    return fragments


def build_context_mentions(mentions: Any) -> list[dict[str, str]]:
    context_mentions: list[dict[str, str]] = []
    for mention in normalize_mention_targets(mentions):
        context_mentions.append(
            {
                "kind": "mention",
                "user_id": mention["userId"],
                "display": mention["display"],
            }
        )
    return context_mentions


def _mention_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, str]]:
    mentions: list[dict[str, str]] = []
    for index, fragment in enumerate(fragments):
        if fragment.get("kind") != "mention":
            continue
        user_id = fragment.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError(f"mention fragment requires user_id at index {index}")
        display = fragment.get("display")
        if not isinstance(display, str) or not display.strip():
            raise ValueError(f"mention fragment requires display at index {index}")
        mentions.append({"kind": "mention", "user_id": user_id, "display": display})
    return mentions


def validate_mention_payload(
    fragments: list[dict[str, Any]],
    context_mentions: list[dict[str, Any]],
) -> None:
    fragment_mentions = _mention_fragments(fragments)
    normalized_context_mentions: list[dict[str, str]] = []
    for index, mention in enumerate(context_mentions):
        if not isinstance(mention, dict) or mention.get("kind") != "mention":
            raise ValueError(f"context.mentions requires mention fragment at index {index}")
        user_id = mention.get("user_id")
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError(f"context.mentions requires user_id at index {index}")
        display = mention.get("display")
        if not isinstance(display, str) or not display.strip():
            raise ValueError(f"context.mentions requires display at index {index}")
        normalized_context_mentions.append(
            {"kind": "mention", "user_id": user_id, "display": display}
        )
    if fragment_mentions != normalized_context_mentions:
        raise ValueError("context.mentions must match mention fragments")


def mention_user_ids(mentions: list[dict[str, str]]) -> list[str]:
    return [mention["userId"] for mention in mentions]


def mention_message_text(*, mentions: list[dict[str, str]], text: str | None = None) -> str:
    normalized, remaining_text = apply_text_mention_labels(mentions, text)
    mention_text = "".join(f"@{mention.get('display') or mention['userId']}" for mention in normalized)
    return f"{mention_text} {remaining_text}".strip() if remaining_text else mention_text
