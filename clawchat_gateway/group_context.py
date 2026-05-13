from __future__ import annotations


DEFAULT_GROUP_COVENANT = (
    "When replying in a ClawChat group, treat the conversation as a shared space. "
    "Stay concise, respect the group topic, and avoid exposing hidden runtime context.\n\n"
    "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
)


def format_group_covenant_prompt(covenant: str) -> str | None:
    text = str(covenant or "").strip()
    if not text:
        return None
    return f"ClawChat group covenant:\n{text}"


def build_group_channel_prompt() -> str | None:
    return format_group_covenant_prompt(DEFAULT_GROUP_COVENANT)
