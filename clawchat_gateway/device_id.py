from __future__ import annotations

import functools
import hashlib
import logging
import os
import platform
import re
import socket
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger("clawchat_gateway.device_id")


def _safe_id(prefix: str, value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.:-]+", "-", value.strip())
    return f"{prefix}-{clean}" if clean else ""


def _mac_platform_uuid() -> str:
    if platform.system() != "Darwin":
        return ""
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except Exception:
        return ""
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', result.stdout or "")
    if not match:
        return ""
    return _safe_id("hermes-mac", match.group(1).lower())


def _machine_id() -> str:
    for raw in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        path = Path(raw)
        try:
            value = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if value:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
            return f"hermes-machine-{digest}"
    return ""


def _host_fingerprint() -> str:
    raw = f"{socket.gethostname()}:{uuid.getnode():012x}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return f"hermes-host-{digest}"


@functools.lru_cache(maxsize=1)
def get_device_id() -> str:
    """Return a stable ClawChat device id for this Hermes installation.

    Resolution order:

    1. ``CLAWCHAT_DEVICE_ID`` env var — used **verbatim** when already a
       well-formed ``hermes-`` id, otherwise sanitized to the transport-safe
       charset and ``hermes-`` prefixed. This is the durable, deployment-pinned
       path: the same env value always yields the same device id across pod
       restarts/reschedules. **Deployments MUST set this** (see
       ``docs/configuration.md`` — Device id durability) so the server-side
       per-device cursor stays stable.
    2. Host fingerprint fallback (macOS ``IOPlatformUUID`` → Linux
       ``machine-id`` → hostname+MAC hash) — only when the env var is unset.
       In a container this fingerprint changes on every reschedule, which the
       server treats as a brand-new device (full replay + orphan cursor).
    """
    override = os.getenv("CLAWCHAT_DEVICE_ID", "").strip()
    if override:
        return _safe_id("hermes", override) if not override.startswith("hermes-") else override
    return _mac_platform_uuid() or _machine_id() or _host_fingerprint()


def device_id_is_pinned() -> bool:
    """True iff ``CLAWCHAT_DEVICE_ID`` is set (the durable, deployment-pinned path)."""
    return bool(os.getenv("CLAWCHAT_DEVICE_ID", "").strip())


def warn_if_device_id_unpinned() -> None:
    """Emit a boot warning when the device id is a volatile host fingerprint.

    Token-refresh spec §E (decision): the refresh endpoint requires the
    connect-time ``X-Device-Id``. An unpinned host fingerprint changes on pod
    reschedule, which the backend then treats as a device mismatch (10003) at
    refresh time → spurious auto-logout. Deployments MUST pin
    ``CLAWCHAT_DEVICE_ID``.

    Callers gate this on the actual connect-time resolution
    (``ClawChatConnection._warn_if_device_id_volatile``): a device id read back
    from the SQLite activations row or the token's ``did`` claim is durable
    across container recreation, so the warning is only emitted when resolution
    truly falls through to this module's fingerprint.
    """
    if device_id_is_pinned():
        return
    logger.warning(
        "CLAWCHAT_DEVICE_ID is not pinned; using a derived host fingerprint (%s). "
        "On pod reschedule this changes and the backend rejects /v1/auth/refresh "
        "with a device mismatch (forcing re-pair). Pin CLAWCHAT_DEVICE_ID in any "
        "containerized/Kubernetes deployment (see docs/configuration.md).",
        get_device_id(),
    )
