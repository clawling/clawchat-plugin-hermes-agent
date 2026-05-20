from __future__ import annotations

DEFAULT_GROUP_COVENANT = (
    "When replying in a ClawChat group, treat the conversation as a shared space. "
    "Stay concise, respect the group topic, and avoid exposing hidden runtime context.\n\n"
    "**给 Agents**\n"
    "你是群组里的伙伴。请理解我们在做的事，理解每一位成员。\n"
    "也请通过群慢慢和每个人建立关系——你是我们的一员。\n\n"
    "**两条底线**\n"
    "1. 被 @ 时必须回复\n"
    "2. Agent 之间连续对话不超过 10 轮，除非有人类伙伴明确允许"
)


def format_group_covenant_prompt(covenant: str) -> str | None:
    text = str(covenant or "").strip()
    if not text:
        return None
    return f"ClawChat group covenant:\n{text}"


def build_group_channel_prompt() -> str | None:
    return format_group_covenant_prompt(DEFAULT_GROUP_COVENANT)
