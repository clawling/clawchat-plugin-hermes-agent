from __future__ import annotations

import os
from pathlib import Path

import yaml


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _env_file() -> Path:
    return _hermes_home() / ".env"


def configure_clawchat_allow_all() -> bool:
    """Allow ClawChat users by default without opening every gateway platform."""
    env_path = _env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    changed = False
    found = False

    for idx, line in enumerate(lines):
        if line.startswith("CLAWCHAT_ALLOW_ALL_USERS="):
            found = True
            if line != "CLAWCHAT_ALLOW_ALL_USERS=true":
                lines[idx] = "CLAWCHAT_ALLOW_ALL_USERS=true"
                changed = True
            break

    if not found:
        lines.append("CLAWCHAT_ALLOW_ALL_USERS=true")
        changed = True

    if changed:
        env_path.write_text("\n".join(lines) + "\n")
    return changed


def configure_clawchat_display_defaults() -> bool:
    config_path = _hermes_home() / "config.yaml"
    if config_path.exists():
        try:
            config = yaml.safe_load(config_path.read_text()) or {}
        except Exception:
            config = {}
    else:
        config = {}

    changed = False
    platforms = config.setdefault("platforms", {})
    clawchat = platforms.setdefault("clawchat", {})

    display = config.setdefault("display", {})
    display_platforms = display.setdefault("platforms", {})
    clawchat_display = display_platforms.setdefault("clawchat", {})
    display_defaults = {
        "tool_progress": "off",
        "long_running_notifications": False,
        "show_reasoning": False,
    }
    for key, value in display_defaults.items():
        if clawchat_display.get(key) != value:
            clawchat_display[key] = value
            changed = True

    if changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(yaml.safe_dump(config, allow_unicode=False, sort_keys=False))
    return changed
