from __future__ import annotations

import re
from typing import Any

TERMINAL_REPLY_INSTRUCTION = (
    'The mention message has already been sent to ClawChat. Return exactly "" '
    "and do not send a normal follow-up reply."
)


MENTION_LABEL_RE = re.compile(r"^@(?P<label>\S+)(?P<rest>(?:\s+.*)?)$", re.DOTALL)


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
        seen.add(user_id)
        normalized_mention = {"userId": user_id}
        if display:
            normalized_mention["display"] = display
        normalized.append(normalized_mention)
    return normalized


def apply_text_mention_labels(
    mentions: list[dict[str, str]],
    text: str | None,
) -> tuple[list[dict[str, str]], str]:
    remaining = text.strip() if isinstance(text, str) else ""
    if not remaining:
        return mentions, ""

    normalized = [dict(mention) for mention in mentions]
    for mention in normalized:
        if mention.get("display"):
            continue
        match = MENTION_LABEL_RE.match(remaining)
        if not match:
            break
        label = match.group("label").strip()
        if not label:
            break
        mention["display"] = label
        remaining = (match.group("rest") or "").strip()
    return normalized, remaining


def build_mention_message_fragments(
    *,
    mentions: list[dict[str, str]],
    text: str | None = None,
) -> list[dict[str, Any]]:
    normalized, remaining_text = apply_text_mention_labels(mentions, text)
    fragments: list[dict[str, Any]] = []
    for mention in normalized:
        fragment = {"kind": "mention", "user_id": mention["userId"]}
        if mention.get("display"):
            fragment["display"] = mention["display"]
        fragments.append(fragment)
    if remaining_text:
        fragments.append({"kind": "text", "text": f" {remaining_text}"})
    return fragments


def mention_user_ids(mentions: list[dict[str, str]]) -> list[str]:
    return [mention["userId"] for mention in mentions]


def mention_context_entries(mentions: list[dict[str, str]]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for mention in mentions:
        entry = {"kind": "mention", "user_id": mention["userId"]}
        if mention.get("display"):
            entry["display"] = mention["display"]
        entries.append(entry)
    return entries


def mention_message_text(*, mentions: list[dict[str, str]], text: str | None = None) -> str:
    normalized, remaining_text = apply_text_mention_labels(mentions, text)
    mention_text = "".join(f"@{mention.get('display') or mention['userId']}" for mention in normalized)
    return f"{mention_text} {remaining_text}".strip() if remaining_text else mention_text
