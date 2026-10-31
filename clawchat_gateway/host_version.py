"""Best-effort Hermes Agent version detection and compatibility diagnostics."""

from __future__ import annotations

import re

# Hermes v0.20.3–v0.20.6: their install-time scanner rates this plugin
# dangerous (a block --force cannot override). v0.20.2 and older have no
# install scan; v0.21.0+ pass. Half-open range: [low, high).
UNSUPPORTED_HOST_VERSIONS = ((0, 20, 3), (0, 21, 0))


def resolve_host_agent_version() -> str:
    """Best-effort Hermes Agent version; empty on failure (never raises)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("hermes-agent")
        except PackageNotFoundError:
            pass
    except Exception:  # noqa: BLE001 — best-effort, must never break import
        pass
    try:
        from hermes_cli import __version__ as host_version

        return host_version
    except Exception:  # noqa: BLE001
        return ""


def parse_version(s: str) -> tuple[int, int, int] | None:
    if not isinstance(s, str):
        return None
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", s.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def unsupported_host_message(version: str | None = None) -> str | None:
    """Return an upgrade hint for the unsupported range, without raising."""
    try:
        if version is None:
            version = resolve_host_agent_version()
        parsed = parse_version(version)
        low, high = UNSUPPORTED_HOST_VERSIONS
        if parsed is None or not low <= parsed < high:
            return None
        ver = version.strip().removeprefix("v")
        return (
            f"Hermes Agent v{ver} is not supported by the ClawChat plugin: "
            "Hermes v0.20.3–v0.20.6 block this plugin at install time, so it "
            "cannot be installed or updated there. Upgrade Hermes (hermes update) "
            "to v0.21.6 or newer."
        )
    except Exception:  # noqa: BLE001 — diagnostics must never break callers
        return None
