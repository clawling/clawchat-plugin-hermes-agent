"""Local file delivery policy shared by tools, hooks and reply attachments."""

from __future__ import annotations

import os
from pathlib import Path

LOCAL_PATH_REFUSAL = (
    "Local files can't be sent or uploaded in this conversation. "
    "Only your owner's direct chat can do that."
)


def turn_allows_local_paths() -> bool:
    try:
        from clawchat_gateway import host_tools_guard, memory_scope

        if host_tools_guard._session_turn() == "owner":
            return True
        return memory_scope.is_local_surface() and not any(
            memory_scope._session_value(f"HERMES_SESSION_{key}")
            for key in ("CHAT_ID", "CHAT_TYPE", "USER_ID")
        )
    except Exception:  # noqa: BLE001 - unknown context is not an owner turn
        return False


def destination_is_owner_direct(chat_id: str) -> bool:
    try:
        from clawchat_gateway import host_tools_guard, memory_scope

        return host_tools_guard.is_owner_turn(
            chat_id, "", "", owner_user_id=memory_scope._owner_user_id
        )
    except Exception:  # noqa: BLE001 - fail closed
        return False


_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")
_HOME_FILES = frozenset({".env", "config.yaml", "auth.json"}) | frozenset(
    "clawchat.sqlite" + suffix for suffix in _DB_SUFFIXES
)


def is_denied_hermes_path(resolved: Path) -> bool:
    """Check resolved Hermes homes and existing stores without opening a store."""
    from clawchat_gateway import storage
    from clawchat_gateway.hermes_home import hermes_home, platform_default_hermes_home

    candidates = [hermes_home(), platform_default_hermes_home()]
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        candidates.append(Path(configured))
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # standalone: the host module is optional
        pass
    else:
        candidates.append(Path(get_hermes_home()))
    homes = {Path(home).expanduser().resolve() for home in candidates}
    for home in homes:
        roots = [home]
        # Resolve named profiles too, including profiles linked outside home.
        try:
            roots.extend((home / "profiles").iterdir())
        except (FileNotFoundError, NotADirectoryError):
            pass
        try:
            relative = resolved.relative_to(home)
        except ValueError:
            pass
        else:
            if len(relative.parts) >= 3 and relative.parts[0] == "profiles":
                roots.append(home / "profiles" / relative.parts[1])
        for root in roots:
            data_dir = (root / "clawchat").resolve()
            if resolved == data_dir or data_dir in resolved.parents:
                return True
            if any(resolved == (root / name).resolve() for name in _HOME_FILES):
                return True
    with storage._store_lock:
        db_paths = [store.db_path for store in storage._stores.values()]
    return any(
        resolved == Path(str(db_path) + suffix).expanduser().resolve()
        for db_path in db_paths
        for suffix in _DB_SUFFIXES
    )
