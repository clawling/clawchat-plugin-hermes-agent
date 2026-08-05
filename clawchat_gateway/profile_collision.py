"""Detect two Hermes profiles running one ClawChat identity.

The activation guards refuse to *create* this state — an identity stamped with
another profile is dropped from the replay, and ``--repair`` is refused on one
this profile cannot prove it owns. None of them can undo a collision that
already happened: once a mis-targeted activation writes the source agent's
``user_id`` *and* stamps ``extra.profile`` with the profile it ran in, the
identity is locally indistinguishable from one this profile earned, and every
later guard reads it as legitimate.

So this module detects rather than prevents. At plugin load it compares this
profile's ``extra.user_id`` against every sibling profile's ``config.yaml``
under the Hermes root; a match means both gateways authenticate as the same
agent, which is the end state of every wrong-profile failure. The operator gets
told which two profiles and that ``--new-account`` is the way out.

Read-only and best-effort: only ``extra.user_id`` / ``extra.base_url`` are
read (never a token), unreadable siblings are skipped, and any failure leaves
plugin registration untouched.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clawchat_gateway.hermes_home import hermes_home, platform_default_hermes_home
from clawchat_gateway.storage import _active_profile_name

logger = logging.getLogger(__name__)

CONFIG_FILENAME = "config.yaml"
PROFILES_DIRNAME = "profiles"
DEFAULT_PROFILE = "default"


@dataclass(frozen=True)
class IdentityCollision:
    """Another profile on this host holding the same ClawChat ``user_id``."""

    profile: str
    user_id: str
    config_path: Path


def hermes_root() -> Path:
    """The root that owns ``profiles/``, mirroring Hermes' own resolution.

    Deliberately mirrors ``hermes_constants.get_default_hermes_root`` rather
    than importing it: this module runs from the standalone CLI too, where
    ``hermes_constants`` is not importable. Native layouts (``HERMES_HOME``
    unset, or anywhere under the platform-native home — including
    ``<native>/profiles/<name>``) resolve to the native home; a custom root
    (Docker's ``/opt/data``) resolves to itself, or to its grandparent when
    ``HERMES_HOME`` is ``<root>/profiles/<name>``.
    """
    native = platform_default_hermes_home()
    configured = os.environ.get("HERMES_HOME", "").strip()
    if not configured:
        return native
    home = Path(configured)
    try:
        home.resolve().relative_to(native.resolve())
        return native
    except (ValueError, OSError):
        pass
    if home.parent.name == PROFILES_DIRNAME:
        return home.parent.parent
    return home


def _clawchat_extra(config_path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - a malformed sibling is skipped, never fatal
        logger.debug("clawchat collision check: unreadable config at %s", config_path, exc_info=True)
        return {}
    if not isinstance(loaded, dict):
        return {}
    platforms = loaded.get("platforms")
    clawchat = platforms.get("clawchat") if isinstance(platforms, dict) else None
    extra = clawchat.get("extra") if isinstance(clawchat, dict) else None
    return extra if isinstance(extra, dict) else {}


def _text(extra: dict[str, Any], key: str) -> str:
    return str(extra.get(key) or "").strip()


def _candidate_configs(root: Path, active_profile: str) -> list[tuple[str, Path]]:
    """Every *other* profile's config path on this host, as (profile, path)."""
    candidates: list[tuple[str, Path]] = []
    if active_profile != DEFAULT_PROFILE:
        candidates.append((DEFAULT_PROFILE, root / CONFIG_FILENAME))
    try:
        profile_dirs = sorted((root / PROFILES_DIRNAME).iterdir())
    except (OSError, FileNotFoundError):
        profile_dirs = []
    for entry in profile_dirs:
        if entry.name == active_profile or not entry.is_dir():
            continue
        candidates.append((entry.name, entry / CONFIG_FILENAME))
    return candidates


def find_identity_collisions() -> list[IdentityCollision]:
    """Sibling profiles whose config names this profile's ``user_id``.

    A match requires the same backend: two deployments mint ids independently,
    so an identical id under a different ``extra.base_url`` is not evidence of
    a shared agent. A blank ``base_url`` on either side is treated as "same
    backend" — the default is implicit in older configs.
    """
    active_profile = _active_profile_name()
    own = _clawchat_extra(hermes_home() / CONFIG_FILENAME)
    own_user_id = _text(own, "user_id")
    if not own_user_id:
        return []
    own_base_url = _text(own, "base_url").rstrip("/")

    collisions: list[IdentityCollision] = []
    for profile, config_path in _candidate_configs(hermes_root(), active_profile):
        extra = _clawchat_extra(config_path)
        if _text(extra, "user_id") != own_user_id:
            continue
        other_base_url = _text(extra, "base_url").rstrip("/")
        if own_base_url and other_base_url and own_base_url != other_base_url:
            continue
        collisions.append(
            IdentityCollision(profile=profile, user_id=own_user_id, config_path=config_path)
        )
    return collisions


def warn_on_shared_identity() -> None:
    """Log one warning per colliding profile. Never raises."""
    try:
        collisions = find_identity_collisions()
        if not collisions:
            return
        active_profile = _active_profile_name()
        for collision in collisions:
            logger.warning(
                "ClawChat: Hermes profiles %r and %r are configured with the same ClawChat "
                "identity (%s). Both gateways authenticate as that one agent — the second "
                "profile has no agent of its own, and whichever paired last owns the live "
                "credentials. This is what a cloned config or a mis-targeted activation "
                "leaves behind. To give this profile its own agent, activate a fresh "
                "connect code with --new-account (%s holds the other copy).",
                active_profile,
                collision.profile,
                collision.user_id,
                collision.config_path,
            )
    except Exception:  # noqa: BLE001 - diagnostics must never break registration
        logger.debug("clawchat identity-collision check skipped", exc_info=True)
