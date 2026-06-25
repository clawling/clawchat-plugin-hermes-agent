from pathlib import Path
from typing import Any, Literal, TypedDict


__all__ = [
    "load_clawchat_prompts_from_root",
    "platform_prompt",
    "default_owner_behavior_prompt",
    "default_group_bio_prompt",
]

PromptName = Literal["platform", "default-owner-behavior", "default-group-bio"]


class ClawChatPrompts(TypedDict):
    platform: str
    default_owner_behavior: str
    default_group_bio: str


_REQUIRED_PROMPT_NAMES: tuple[PromptName, ...] = ("platform",)
_OPTIONAL_PROMPT_NAMES: tuple[PromptName, ...] = (
    "default-owner-behavior",
    "default-group-bio",
)


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
        "default_owner_behavior": prompts["default-owner-behavior"],
        "default_group_bio": prompts["default-group-bio"],
    }


def load_clawchat_prompts_from_root(plugin_root: Path) -> ClawChatPrompts:
    return _load_clawchat_prompts_from_dir(plugin_root / "prompts")


_PROMPTS = load_clawchat_prompts_from_root(Path(__file__).resolve().parents[1])


def platform_prompt() -> str:
    return _PROMPTS["platform"]


def default_owner_behavior_prompt() -> str:
    return _PROMPTS["default_owner_behavior"]


def default_group_bio_prompt() -> str:
    return _PROMPTS["default_group_bio"]
