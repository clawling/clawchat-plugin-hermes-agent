from __future__ import annotations

from typing import Any

TERMINAL_REPLY_INSTRUCTION = (
    'The mention message has already been sent to ClawChat. Return exactly "" '
    "and do not send a normal follow-up reply."
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
        if not display:
            display = user_id
        elif display.startswith("@"):
            display = display[1:]
        seen.add(user_id)
        normalized.append({"userId": user_id, "display": display})
    return normalized


def build_mention_message_fragments(
    *,
    mentions: list[dict[str, str]],
    text: str | None = None,
) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = [
        {"kind": "mention", "user_id": mention["userId"], "display": mention["display"]}
        for mention in mentions
    ]
    trimmed_text = text.strip() if isinstance(text, str) else ""
    if trimmed_text:
        fragments.append({"kind": "text", "text": f" {trimmed_text}"})
    return fragments


def mention_user_ids(mentions: list[dict[str, str]]) -> list[str]:
    return [mention["userId"] for mention in mentions]


def mention_context_entries(mentions: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {"kind": "mention", "user_id": mention["userId"], "display": mention["display"]}
        for mention in mentions
    ]


def mention_message_text(*, mentions: list[dict[str, str]], text: str | None = None) -> str:
    mention_text = "".join(f"@{mention['display']}" for mention in mentions)
    trimmed_text = text.strip() if isinstance(text, str) else ""
    return f"{mention_text} {trimmed_text}".strip() if trimmed_text else mention_text
