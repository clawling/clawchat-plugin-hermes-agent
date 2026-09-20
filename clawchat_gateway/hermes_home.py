"""Single resolver for ``$HERMES_HOME``, with Hermes' real per-platform default.

Five modules used to inline ``os.environ.get("HERMES_HOME") or Path.home() /
".hermes"``. That default is the POSIX layout only: Hermes puts the Windows home
under ``%LOCALAPPDATA%\\hermes`` (``hermes_constants._get_platform_default_hermes_home``).
On a Windows desktop where ``HERMES_HOME`` is not exported — the normal case
outside a ``hermes -p <name>`` invocation — the plugin therefore looked for the
``.env``, the SQLite database and the memory root in ``C:\\Users\\<u>\\.hermes``,
a directory Hermes never writes.

Deliberately mirrors ``hermes_constants._hermes_home_from_env`` rather than
importing it: every call site here already resolved the env var directly, and
following Hermes' context-local per-task override would be a wider semantic
change than these paths want.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def platform_default_hermes_home() -> Path:
    """Hermes' native home for this OS, ignoring ``HERMES_HOME``."""
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "hermes"
    return Path.home() / ".hermes"


def hermes_home() -> Path:
    """Multiplex override, then ``$HERMES_HOME``, else the platform-native default.

    Under a multiplexing gateway Hermes runs every profile in one process and
    scopes each profile's work with a context-local home override
    (``hermes_constants.set_hermes_home_override``). Follow it first so
    per-profile storage (.env, sqlite, pairing) resolves to the served
    profile's home instead of the process-wide default home.
    """
    try:
        from hermes_constants import get_hermes_home_override

        override = (get_hermes_home_override() or "").strip()
        if override:
            return Path(override)
    except Exception:  # standalone CLI: hermes_constants is not importable
        pass
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return platform_default_hermes_home()
