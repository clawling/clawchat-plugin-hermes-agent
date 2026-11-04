from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

from clawchat_gateway import atomic_file
from clawchat_gateway.state_lock import StateLockTimeout, env_file_lock
from clawchat_gateway.hermes_home import hermes_home


def _hermes_home() -> Path:
    return hermes_home()


def _env_file() -> Path:
    return _hermes_home() / ".env"


def configure_clawchat_allow_all() -> bool:
    """Allow ClawChat users by default without opening every gateway platform."""
    env_path = _env_file()
    with env_file_lock(env_path), ExitStack() as locks:
        try:
            from hermes_cli import config as host_config
        except ImportError:
            host_lock = None
        else:
            host_lock = getattr(host_config, "_env_write_lock", None)
        if host_lock is not None:
            try:
                locks.enter_context(host_lock(env_path))
            except TimeoutError as exc:
                raise StateLockTimeout("timed out waiting for the Hermes .env lock; retry later") from exc

        env_path.parent.mkdir(parents=True, exist_ok=True)
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
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
            atomic_file.atomic_write_text(env_path, "\n".join(lines) + "\n", newline="")
        return changed
