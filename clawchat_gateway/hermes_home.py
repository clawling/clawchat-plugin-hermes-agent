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
    """``$HERMES_HOME`` when exported, else the platform-native default."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        return Path(configured)
    return platform_default_hermes_home()
