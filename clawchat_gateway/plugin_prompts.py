from importlib.resources import files
from pathlib import Path
from typing import Any, Literal, TypedDict


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


_REQUIRED_PROMPT_NAMES: tuple[PromptName, ...] = ("platform",)
_OPTIONAL_PROMPT_NAMES: tuple[PromptName, ...] = ("user", "group")


def _load_clawchat_prompts_from_dir(prompts_root: Any) -> ClawChatPrompts:
    prompts: dict[PromptName, str] = {}

    for name in _REQUIRED_PROMPT_NAMES:
        path = prompts_root.joinpath(f"{name}.md")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(
                f"missing or empty ClawChat prompt: {name} ({path})"
            ) from exc
        if not text:
            raise RuntimeError(f"missing or empty ClawChat prompt: {name} ({path})")
        prompts[name] = text

    for name in _OPTIONAL_PROMPT_NAMES:
        path = prompts_root.joinpath(f"{name}.md")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        prompts[name] = text

    return {
        "platform": prompts["platform"],
        "user": prompts["user"],
        "group": prompts["group"],
    }


def load_clawchat_prompts_from_root(plugin_root: Path) -> ClawChatPrompts:
    return _load_clawchat_prompts_from_dir(plugin_root / "prompts")


_PROMPTS = _load_clawchat_prompts_from_dir(
    files("clawchat_gateway").joinpath("prompts")
)


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
