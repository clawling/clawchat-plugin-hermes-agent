from pathlib import Path
from typing import Literal, TypedDict


__all__ = [
    "load_clawchat_prompts_from_root",
    "platform_prompt",
    "user_prompt",
    "group_prompt",
    "mode_prompt",
]

PromptName = Literal["platform", "user", "group"]


class ClawChatPrompts(TypedDict):
    platform: str
    user: str
    group: str


_PROMPT_NAMES: tuple[PromptName, ...] = ("platform", "user", "group")


def load_clawchat_prompts_from_root(plugin_root: Path) -> ClawChatPrompts:
    prompts: dict[PromptName, str] = {}
    prompts_root = plugin_root / "prompts"

    for name in _PROMPT_NAMES:
        path = prompts_root / f"{name}.md"
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"missing or empty ClawChat prompt: {name} ({path})"
            ) from exc
        if not text:
            raise RuntimeError(f"missing or empty ClawChat prompt: {name} ({path})")
        prompts[name] = text

    return {
        "platform": prompts["platform"],
        "user": prompts["user"],
        "group": prompts["group"],
    }


_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS = load_clawchat_prompts_from_root(_PLUGIN_ROOT)


def platform_prompt() -> str:
    return _PROMPTS["platform"]


def user_prompt() -> str:
    return _PROMPTS["user"]


def group_prompt() -> str:
    return _PROMPTS["group"]


def mode_prompt(mode: str) -> str:
    if mode == "group":
        return group_prompt()
    return user_prompt()
