from __future__ import annotations

from clawchat_gateway.plugin_prompts import group_prompt


def format_group_covenant_prompt(covenant: str) -> str | None:
    text = str(covenant or "").strip()
    if not text:
        return None
    return f"ClawChat group covenant:\n{text}"


def build_group_channel_prompt() -> str | None:
    return group_prompt()
