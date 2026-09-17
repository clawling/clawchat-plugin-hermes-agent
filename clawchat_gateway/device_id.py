from __future__ import annotations

import functools
import hashlib
import logging
import platform
import re
import socket
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger("clawchat_gateway.device_id")


def _env(name: str) -> str:
    """Profile-scoped env read, imported lazily to break a cycle.

    ``config`` pulls ``api_client`` for the default URLs, and ``api_client``
    imports this module for the connect-time ``X-Device-Id`` — so the import
    has to happen at call time, not at module load.
    """
    from clawchat_gateway.config import _get_env

    return _get_env(name)


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


def _profile_name() -> str:
    """Active Hermes profile ("default" when unnamed). Imported lazily: storage
    imports config which imports nothing from here, so no cycle at call time."""
    try:
        from clawchat_gateway.storage import _active_profile_name

        return _active_profile_name() or "default"
    except Exception:  # noqa: BLE001 — a broken host resolver must not break the id
        return "default"


def profile_scope() -> str:
    """12-hex scope for a NAMED profile; "" for the default profile.

    Hashed rather than embedded so a profile name never leaks into a wire
    header and the id stays in the transport-safe charset.
    """
    name = _profile_name()
    if not name or name == "default":
        return ""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]


def legacy_host_device_id() -> str:
    """The pre-agent-scoped id: host fingerprint only, byte-identical to what
    every profile derived before device ids became agent-scoped. Used for
    already-paired named profiles that persisted no device id (see
    ``connection._resolve_device_id``)."""
    override = _env("CLAWCHAT_DEVICE_ID")
    if override:
        return override if override.startswith("hermes-") else _safe_id("hermes", override)
    return _mac_platform_uuid() or _machine_id() or _host_fingerprint()


@functools.lru_cache(maxsize=1)
def get_device_id() -> str:
    """Return a stable ClawChat device id for this Hermes agent (profile).

    Resolution order:

    1. ``CLAWCHAT_DEVICE_ID`` — verbatim when already a well-formed ``hermes-``
       id, else sanitized + ``hermes-`` prefixed. Deployments MUST set this
       (``docs/configuration.md`` — Device id durability). Read through
       ``_get_env`` so a named profile's own ``.env`` beats the default
       profile's inherited environment.
    2. Host fingerprint (macOS ``IOPlatformUUID`` → Linux ``machine-id`` →
       hostname+MAC hash), **plus ``-p<scope>`` for a named profile**.

    The id is agent-scoped since 0.14.0-86: one Hermes profile is one agent,
    and the redeem safety gate (``paired_device_id``) and the plugin-report row
    are keyed on device id alone, so two agents on one host must not share one.
    The default profile keeps the exact legacy value, and an already-paired
    agent always reuses the id it connected with (persisted on its activations
    row / the token's ``did``) — only a NEW activation ever sees the new
    derivation.
    """
    override = _env("CLAWCHAT_DEVICE_ID")
    if override:
        return override if override.startswith("hermes-") else _safe_id("hermes", override)
    host = _mac_platform_uuid() or _machine_id() or _host_fingerprint()
    scope = profile_scope()
    return f"{host}-p{scope}" if scope else host


def device_id_is_pinned() -> bool:
    """True iff ``CLAWCHAT_DEVICE_ID`` is set (the durable, deployment-pinned path)."""
    return bool(_env("CLAWCHAT_DEVICE_ID"))


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
