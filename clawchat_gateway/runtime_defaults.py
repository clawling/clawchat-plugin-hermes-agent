from __future__ import annotations

import os
from pathlib import Path


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
