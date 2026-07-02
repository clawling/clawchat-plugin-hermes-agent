"""Conversational, adapter-driven hot-update of ClawChat skill markdown.

This module is the Python (Hermes) re-implementation of the cross-language
skill-update contract documented in
``ops/agent-plugin/skill-dynamic-update-plan.md`` (§6, "H1 variant").
It re-implements the semantics of the install-cli TypeScript reference
(``packages/core/src/skills/check-update.ts`` + ``installers/metadata.ts``)
WITHOUT taking any dependency on it.

Scope (deliberately narrow):
- Only ``SKILL.md`` markdown is ever fetched/written — never executable ``.py``
  tool code.
- The official source is a hard-coded constant; a trigger signal never carries
  a URL or ref. The git ref is derived locally from a target version, defaulting
  to ``main``.
- Writes are ALWAYS atomic (tmp + ``os.replace``). The registered SKILL.md is
  never deleted-then-written: ``hermes`` treats a missing registered file as
  stale and clears the registration.

The module owns no network policy of its own beyond a size cap + sha256 check;
the adapter owns the owner-consent flow and decides when to call ``apply``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# --- Fixed contract constants (other repos depend on these) -----------------

# Canonical official skill source. A trigger signal NEVER carries a URL; the
# adapter only ever fetches from this base. ``clawling`` is a public org so the
# raw fetch is unauthenticated.
OFFICIAL_SKILLS_BASE = (
    "https://raw.githubusercontent.com/clawling/clawchat-plugin-install-cli"
)
# Default git ref for the skills tree. Production SHOULD pin an immutable
# ``skills-vX.Y.Z`` tag (see ``ref_for_target_version``); ``main`` is the
# fallback when no target version is supplied.
DEFAULT_SKILLS_REF = "main"
# Defence in depth: refuse an absurdly large response before hashing/writing.
MAX_SKILL_BYTES = 256 * 1024
# Which host's skill set this plugin consumes from ``skills.<target>``.
TARGET = "hermes"
# Skill ids this Hermes plugin manages.
HERMES_SKILL_IDS = ("clawchat", "liveware-app")
# Managed (writable) skills root, relative to ``$HERMES_HOME``.
MANAGED_DIRNAME = "clawchat-skills"
# How long an unanswered owner-consent prompt stays pending.
PENDING_TTL_SECONDS = 30 * 60

# A fetcher takes an absolute URL and returns the raw response bytes.
Fetcher = Callable[[str], bytes]


class SkillUpdateError(Exception):
    """Raised on any manifest/skill validation or fetch failure."""


# --- Version comparison (mirrors install-cli parseComparableVersion) --------

_VERSION_RE = re.compile(r"^(\d+(?:\.\d+){1,3})(?:-(\d+))?$")


def _parse_comparable_version(version: str) -> tuple[list[int], int]:
    match = _VERSION_RE.match(str(version or "").strip())
    if not match:
        raise SkillUpdateError(f"unsupported version: {version!r}")
    parts = [int(p) for p in match.group(1).split(".")]
    build = int(match.group(2)) if match.group(2) else 0
    return parts, build


def compare_version(a: str, b: str) -> int:
    """Return -1/0/1 for a<b / a==b / a>b. Matches install-cli ``compareVersions``."""
    left_parts, left_build = _parse_comparable_version(a)
    right_parts, right_build = _parse_comparable_version(b)
    width = max(len(left_parts), len(right_parts))
    for i in range(width):
        left = left_parts[i] if i < len(left_parts) else 0
        right = right_parts[i] if i < len(right_parts) else 0
        if left != right:
            return 1 if left > right else -1
    if left_build != right_build:
        return 1 if left_build > right_build else -1
    return 0


def is_version_older(current: str, candidate: str) -> bool:
    """True if ``current`` is strictly older than ``candidate``."""
    return compare_version(current, candidate) < 0


# --- Frontmatter parsing ----------------------------------------------------


def parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the leading ``---`` YAML frontmatter block of a SKILL.md.

    Returns an empty dict when there is no frontmatter or it cannot be parsed.
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end = idx
            break
    if end is None:
        return {}
    block = "\n".join(lines[1:end])
    try:
        import yaml

        data = yaml.safe_load(block) or {}
    except Exception:  # noqa: BLE001 — tolerate malformed frontmatter
        return {}
    return data if isinstance(data, dict) else {}


def skill_version(path: Path) -> str | None:
    """Read the frontmatter ``version`` of a SKILL.md file, or None."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    version = parse_frontmatter(text).get("version")
    if version is None:
        return None
    version = str(version).strip()
    return version or None


# --- Managed-dir layout -----------------------------------------------------


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def managed_skills_dir() -> Path:
    return hermes_home() / MANAGED_DIRNAME


def managed_skill_path(skill_id: str) -> Path:
    return managed_skills_dir() / skill_id / "SKILL.md"


def managed_manifest_path() -> Path:
    return managed_skills_dir() / "manifest.json"


def pending_path() -> Path:
    return managed_skills_dir() / "pending.json"


# --- Atomic IO --------------------------------------------------------------


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (tmp in same dir + ``os.replace``).

    NEVER delete-then-write the destination: a transient missing file at a
    registered skill path makes ``hermes`` clear the registration.

    Opens the tmp file with ``newline=""`` for raw-byte fidelity: the bytes
    written to disk must exactly match the bytes that were downloaded and
    checksum-verified, because sha convergence (``local_skill_sha`` vs. the
    manifest ``sha256``) depends on it — with the default universal-newline
    translation, ``"\\n"`` would be rewritten to ``os.linesep`` on hosts where
    that differs from ``"\\n"``, permanently diverging the on-disk sha from the
    manifest sha and livelocking the update check as "needs update" forever.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".skillmd-", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --- Local managed manifest -------------------------------------------------


def read_local_manifest() -> dict[str, str]:
    """Return ``{"<skill_id>": "<version>"}`` from the managed manifest."""
    try:
        text = managed_manifest_path().read_text(encoding="utf-8")
        data = json.loads(text)
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001 — a corrupt manifest reseeds from scratch
        logger.warning("clawchat skill manifest unreadable; treating as empty")
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, (str, int, float))}


def write_local_manifest(manifest: dict[str, str]) -> None:
    atomic_write_text(
        managed_manifest_path(),
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _set_local_version(skill_id: str, version: str) -> None:
    manifest = read_local_manifest()
    manifest[skill_id] = version
    write_local_manifest(manifest)


def local_skill_sha(skill_id: str) -> str | None:
    """sha256 hex of the managed SKILL.md raw bytes, or None when missing."""
    try:
        raw = managed_skill_path(skill_id).read_bytes()
    except OSError:
        return None
    return hashlib.sha256(raw).hexdigest()


# --- Seeding managed skills from the bundled snapshot -----------------------


def seed_managed_skill(skill_id: str, bundled_path: Path) -> Path:
    """Ensure a writable managed copy of ``skill_id`` exists; return its path.

    Seeds the managed copy from the bundled snapshot when the managed file is
    missing, corrupt (no parseable version), or older than the bundled version.
    A managed file that is equal-or-newer is kept (it may already hold a
    hot-update). On ANY error, falls back to the read-only bundled path so the
    skill mechanism can never crash plugin load.
    """
    bundled_path = Path(bundled_path)
    try:
        managed = managed_skill_path(skill_id)
        bundled_version = skill_version(bundled_path)
        managed_version = skill_version(managed) if managed.exists() else None

        need_seed = False
        if not managed.exists():
            need_seed = True
        elif managed_version is None:
            # Corrupt/unversioned managed file — reseed from bundled.
            need_seed = True
        elif bundled_version is not None and is_version_older(
            managed_version, bundled_version
        ):
            need_seed = True

        if need_seed:
            content = bundled_path.read_text(encoding="utf-8")
            atomic_write_text(managed, content)
            seeded_version = skill_version(managed) or bundled_version
            if seeded_version:
                _set_local_version(skill_id, seeded_version)
            logger.info(
                "clawchat seeded managed skill %s -> %s (version=%s)",
                skill_id,
                managed,
                seeded_version,
            )
        else:
            # Managed kept; make sure the manifest reflects what is on disk.
            if managed_version and read_local_manifest().get(skill_id) != managed_version:
                _set_local_version(skill_id, managed_version)
        return managed
    except Exception:  # noqa: BLE001 — never let seeding break plugin load
        logger.warning(
            "clawchat managed-skill seeding failed for %s; using bundled path",
            skill_id,
            exc_info=True,
        )
        return bundled_path


# --- Remote source ----------------------------------------------------------


def ref_for_target_version(target_version: str | None) -> str:
    """Map a target version to the immutable git tag, or fall back to ``main``."""
    version = (target_version or "").strip()
    if not version:
        return DEFAULT_SKILLS_REF
    return f"skills-v{version}"


def _skills_base(ref: str) -> str:
    return f"{OFFICIAL_SKILLS_BASE.rstrip('/')}/{ref}/skills"


def manifest_url(ref: str = DEFAULT_SKILLS_REF) -> str:
    return f"{_skills_base(ref)}/manifest.json"


def skill_content_url(entry_path: str, ref: str = DEFAULT_SKILLS_REF) -> str:
    return f"{_skills_base(ref)}/{entry_path.lstrip('/')}"


def _default_fetch(url: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        status = getattr(response, "status", 200)
        if status and int(status) >= 400:
            raise SkillUpdateError(f"fetch {url} returned status {status}")
        return response.read(MAX_SKILL_BYTES + 1)


@dataclass(frozen=True)
class SkillManifestEntry:
    version: str
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class ParsedSkillsManifest:
    """Validated ``skills/manifest.json``: live entries + per-target tombstones."""

    skills: dict[str, dict[str, SkillManifestEntry]]
    removed: dict[str, tuple[str, ...]]


def _as_entry(value: Any, where: str) -> SkillManifestEntry:
    if not isinstance(value, dict):
        raise SkillUpdateError(f"skills manifest entry {where} is not an object")
    version = str(value.get("version") or "").strip()
    path = str(value.get("path") or "").strip()
    sha256 = str(value.get("sha256") or "").strip().lower()
    raw_bytes = value.get("bytes")
    if not version:
        raise SkillUpdateError(f"skills manifest entry {where} missing version")
    if not path:
        raise SkillUpdateError(f"skills manifest entry {where} missing path")
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise SkillUpdateError(f"skills manifest entry {where} has invalid sha256")
    if not isinstance(raw_bytes, int) or isinstance(raw_bytes, bool) or raw_bytes < 0:
        raise SkillUpdateError(f"skills manifest entry {where} has invalid bytes")
    return SkillManifestEntry(version=version, path=path, sha256=sha256, bytes=raw_bytes)


def parse_skills_manifest(text: str) -> ParsedSkillsManifest:
    """Parse + validate the raw ``skills/manifest.json`` text."""
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise SkillUpdateError(f"failed to parse skills manifest: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SkillUpdateError("skills manifest must be a JSON object")
    if parsed.get("schema") != 1:
        raise SkillUpdateError(
            f"unsupported skills manifest schema: {parsed.get('schema')!r}"
        )
    skills = parsed.get("skills")
    if not isinstance(skills, dict):
        raise SkillUpdateError("skills manifest missing `skills`")
    out: dict[str, dict[str, SkillManifestEntry]] = {}
    for target, entries in skills.items():
        if not isinstance(entries, dict):
            raise SkillUpdateError(f"skills manifest target {target} is not an object")
        out[target] = {
            skill_id: _as_entry(entry, f"{target}.{skill_id}")
            for skill_id, entry in entries.items()
        }

    removed: dict[str, tuple[str, ...]] = {}
    removed_raw = parsed.get("removed")
    if removed_raw is not None:
        if not isinstance(removed_raw, dict):
            raise SkillUpdateError("skills manifest `removed` must be an object")
        for target, ids in removed_raw.items():
            if not isinstance(ids, list) or not all(
                isinstance(i, str) and i.strip() for i in ids
            ):
                raise SkillUpdateError(
                    f"skills manifest removed[{target}] must be a list of skill ids"
                )
            cleaned = tuple(i.strip() for i in ids)
            for skill_id in cleaned:
                if skill_id in out.get(target, {}):
                    raise SkillUpdateError(
                        f"skill {target}.{skill_id} is in both skills and removed"
                    )
            removed[str(target)] = cleaned
    return ParsedSkillsManifest(skills=out, removed=removed)


def _fetch_text(url: str, fetcher: Fetcher) -> str:
    try:
        raw = fetcher(url)
    except SkillUpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SkillUpdateError(f"fetch {url} failed: {exc}") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillUpdateError(
            f"response {url} is over the {MAX_SKILL_BYTES} byte cap"
        )
    return raw.decode("utf-8")


@dataclass(frozen=True)
class PendingSkillUpdate:
    """One skill whose content differs from the official source."""

    skill_id: str
    current: str | None
    target: str
    path: str
    sha256: str
    bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "current": self.current,
            "target": self.target,
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingSkillUpdate":
        return cls(
            skill_id=str(data["skill_id"]),
            current=(data.get("current") if data.get("current") is None else str(data.get("current"))),
            target=str(data["target"]),
            path=str(data["path"]),
            sha256=str(data["sha256"]),
            bytes=int(data["bytes"]),
        )


@dataclass(frozen=True)
class PendingSkillRemoval:
    """One locally installed skill tombstoned by the official source."""

    skill_id: str
    current: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"skill_id": self.skill_id, "current": self.current}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingSkillRemoval":
        current = data.get("current")
        return cls(
            skill_id=str(data["skill_id"]),
            current=None if current is None else str(current),
        )


@dataclass
class SkillCheckResult:
    updates: list[PendingSkillUpdate] = field(default_factory=list)
    removals: list[PendingSkillRemoval] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.updates or self.removals)


def check_skill_update(
    *,
    fetcher: Fetcher | None = None,
    ref: str | None = None,
    local_manifest: dict[str, str] | None = None,
    local_sha: Callable[[str], str | None] | None = None,
) -> SkillCheckResult:
    """Fetch the official manifest; report skills to converge and to remove.

    Convergence is sha-based: a skill whose LOCAL managed file bytes hash
    differently from the manifest entry (or is missing) is an update — this
    covers upgrades, rollbacks, and same-version content fixes alike. A skill
    listed in ``removed[hermes]`` that is locally installed (per the local
    manifest) and not bundled is reported for removal. Raises
    ``SkillUpdateError`` on any manifest problem.
    """
    fetch = fetcher or _default_fetch
    use_ref = ref or DEFAULT_SKILLS_REF
    local = local_manifest if local_manifest is not None else read_local_manifest()
    sha_of = local_sha or local_skill_sha
    text = _fetch_text(manifest_url(use_ref), fetch)
    manifest = parse_skills_manifest(text)
    target_skills = manifest.skills.get(TARGET)
    if target_skills is None:
        raise SkillUpdateError(f"skills manifest has no entry for target {TARGET}")

    updates: list[PendingSkillUpdate] = []
    for skill_id, entry in target_skills.items():
        if sha_of(skill_id) == entry.sha256:
            continue
        updates.append(
            PendingSkillUpdate(
                skill_id=skill_id,
                current=local.get(skill_id),
                target=entry.version,
                path=entry.path,
                sha256=entry.sha256,
                bytes=entry.bytes,
            )
        )

    removals: list[PendingSkillRemoval] = []
    for skill_id in manifest.removed.get(TARGET, ()):
        if skill_id in HERMES_SKILL_IDS:
            logger.warning(
                "clawchat ignoring tombstone for bundled skill %s", skill_id
            )
            continue
        if skill_id not in local:
            continue
        removals.append(
            PendingSkillRemoval(skill_id=skill_id, current=local.get(skill_id))
        )
    return SkillCheckResult(updates=updates, removals=removals)


def fetch_skill_markdown(
    update: PendingSkillUpdate,
    *,
    fetcher: Fetcher | None = None,
    ref: str | None = None,
) -> str:
    """Download + integrity-check one skill markdown file.

    Validates: size cap, exact sha256 (lowercase hex) against the manifest
    entry, and frontmatter ``name == skill_id`` and ``version == entry.target``.
    Raises ``SkillUpdateError`` on any mismatch.
    """
    fetch = fetcher or _default_fetch
    use_ref = ref or DEFAULT_SKILLS_REF
    url = skill_content_url(update.path, use_ref)
    try:
        raw = fetch(url)
    except SkillUpdateError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SkillUpdateError(f"fetch {url} failed: {exc}") from exc
    if len(raw) > MAX_SKILL_BYTES:
        raise SkillUpdateError(
            f"skill {update.path} is {len(raw)} bytes, over the {MAX_SKILL_BYTES} cap"
        )
    digest = hashlib.sha256(raw).hexdigest()
    if digest != update.sha256.lower():
        raise SkillUpdateError(
            f"skill {update.path} sha256 mismatch: got {digest}, expected {update.sha256}"
        )
    text = raw.decode("utf-8")
    front = parse_frontmatter(text)
    name = str(front.get("name") or "").strip()
    version = str(front.get("version") or "").strip()
    if name != update.skill_id:
        raise SkillUpdateError(
            f"skill {update.path} frontmatter name {name!r} != expected {update.skill_id!r}"
        )
    if version != update.target:
        raise SkillUpdateError(
            f"skill {update.path} frontmatter version {version!r} != expected {update.target!r}"
        )
    return text


def apply_skill_update(
    updates: list[PendingSkillUpdate],
    *,
    fetcher: Fetcher | None = None,
    ref: str | None = None,
) -> list[str]:
    """Fetch, validate, then atomically overwrite the managed SKILL.md files.

    Validates EVERY update before writing ANY (all-or-nothing on validation),
    then writes each managed file atomically (tmp + ``os.replace``) and updates
    the local manifest. Idempotent: an update whose target is already the local
    version is skipped. Returns the list of applied skill ids.
    """
    if not updates:
        return []
    local = read_local_manifest()

    # Phase 1: fetch + validate everything up front.
    staged: list[tuple[PendingSkillUpdate, str]] = []
    for update in updates:
        if local_skill_sha(update.skill_id) == update.sha256:
            # Idempotent: on-disk bytes already converged to the manifest.
            # Keep the local manifest's version in sync anyway.
            if local.get(update.skill_id) != update.target:
                local[update.skill_id] = update.target
                write_local_manifest(local)
            continue
        content = fetch_skill_markdown(update, fetcher=fetcher, ref=ref)
        staged.append((update, content))

    if not staged:
        return []

    # Phase 2: atomic writes + manifest update.
    applied: list[str] = []
    for update, content in staged:
        atomic_write_text(managed_skill_path(update.skill_id), content)
        local[update.skill_id] = update.target
        applied.append(update.skill_id)
    write_local_manifest(local)
    logger.info("clawchat applied skill updates: %s", applied)
    return applied


def apply_skill_removal(removals: list[PendingSkillRemoval]) -> list[str]:
    """Delete managed files + local manifest entries for tombstoned skills.

    This is the ONE legitimate delete of a registered skill path: the Hermes
    host lazily clears a registration whose file is missing, and the next
    plugin load skips manifest-absent ids. Bundled ids are refused (deleting
    them would only trigger a reseed). Idempotent: an already-absent file
    still clears the manifest entry. Returns the removed skill ids.
    """
    if not removals:
        return []
    local = read_local_manifest()
    removed: list[str] = []
    for removal in removals:
        skill_id = removal.skill_id
        if skill_id in HERMES_SKILL_IDS:
            logger.warning("clawchat refusing to remove bundled skill %s", skill_id)
            continue
        skill_path = managed_skill_path(skill_id)
        try:
            skill_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "clawchat could not delete managed skill %s", skill_id, exc_info=True
            )
            continue
        try:
            skill_path.parent.rmdir()
        except OSError:
            pass  # non-empty or already gone — best-effort
        local.pop(skill_id, None)
        removed.append(skill_id)
    if removed:
        write_local_manifest(local)
        logger.info("clawchat removed tombstoned skills: %s", removed)
    return removed


# --- Host hot-registration ---------------------------------------------------

# The host's ``ctx.register_skill``, captured at plugin load. Registration is a
# plain registry write on the Hermes side, so calling it after load is safe and
# makes a just-applied skill resolvable via ``skill_view`` without a restart.
_skill_registrar: Callable[..., None] | None = None


def set_skill_registrar(registrar: Callable[..., None] | None) -> None:
    """Capture ``ctx.register_skill`` for post-apply hot registration."""
    global _skill_registrar
    _skill_registrar = registrar if callable(registrar) else None


def skill_description(path: Path) -> str:
    """Frontmatter ``description`` of a SKILL.md, or empty string."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    description = parse_frontmatter(text).get("description")
    return str(description).strip() if description else ""


def hot_register_new_skills(updates: list[PendingSkillUpdate]) -> list[str]:
    """Register brand-new applied skills with the host; returns registered ids.

    Only skills the local manifest had never seen (``current is None``) need
    registration — pre-existing ids were registered at plugin load and their
    managed file was overwritten in place. Per-skill failures are logged and
    skipped so one bad skill can never break the consent flow.
    """
    registrar = _skill_registrar
    if registrar is None:
        return []
    registered: list[str] = []
    for update in updates:
        if update.current is not None:
            continue
        path = managed_skill_path(update.skill_id)
        try:
            registrar(update.skill_id, path, description=skill_description(path))
        except Exception as exc:  # noqa: BLE001 — never break the consent flow
            logger.warning(
                "clawchat hot-register failed for skill %s: %s", update.skill_id, exc
            )
            continue
        registered.append(update.skill_id)
    return registered


# --- Owner-consent classification -------------------------------------------

_AFFIRM_EXACT = {
    "更新",
    "好",
    "好的",
    "确认",
    "确定",
    "同意",
    "是",
    "升级",
    "可以",
    "要",
    "yes",
    "y",
    "ok",
    "okay",
    "sure",
    "update",
    "confirm",
}
_DENY_EXACT = {
    "取消",
    "不",
    "否",
    "不要",
    "不用",
    "拒绝",
    "算了",
    "no",
    "n",
    "cancel",
    "deny",
    "skip",
}

# Punctuation/whitespace trimmed when normalizing a short reply.
_STRIP_CHARS = " \t\r\n。.!！?？,，、~～;；:：\"'「」“”()（）"


def _normalize_consent(text: str) -> str:
    return str(text or "").strip().strip(_STRIP_CHARS).strip().lower()


def classify_consent(text: str) -> str:
    """Conservatively classify an owner reply as affirm / deny / ambiguous.

    Only a short, unambiguous reply counts as consent. Anything long or mixed is
    treated as ``ambiguous`` so it is NOT consumed and flows to the normal LLM.
    """
    normalized = _normalize_consent(text)
    if not normalized:
        return "ambiguous"
    if normalized in _DENY_EXACT:
        return "deny"
    if normalized in _AFFIRM_EXACT:
        return "affirm"
    # Short replies that clearly contain exactly one polarity keyword.
    if len(normalized) <= 8:
        has_affirm = any(k in normalized for k in _AFFIRM_EXACT if len(k) >= 2)
        has_deny = any(k in normalized for k in _DENY_EXACT if len(k) >= 2)
        if has_affirm and not has_deny:
            return "affirm"
        if has_deny and not has_affirm:
            return "deny"
    return "ambiguous"


# --- Pending-consent record (in-memory mirror + small file) -----------------


def _describe_update(update: PendingSkillUpdate) -> str:
    if update.current is None:
        return f"「{update.skill_id}」v— → v{update.target}"
    try:
        cmp = compare_version(update.current, update.target)
    except SkillUpdateError:
        cmp = -1
    if cmp == 0:
        return f"「{update.skill_id}」v{update.target}(内容修订)"
    if cmp > 0:
        return f"「{update.skill_id}」v{update.current} → v{update.target}(回滚)"
    return f"「{update.skill_id}」v{update.current} → v{update.target}"


@dataclass
class PendingConsent:
    updates: list[PendingSkillUpdate]
    owner_user_id: str
    chat_id: str
    created_at: float = field(default_factory=time.time)
    ttl_seconds: float = PENDING_TTL_SECONDS
    removals: list[PendingSkillRemoval] = field(default_factory=list)

    def is_expired(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return now - self.created_at > self.ttl_seconds

    def summary(self) -> str:
        return "、".join(_describe_update(u) for u in self.updates)

    def removal_summary(self) -> str:
        return "、".join(f"「{r.skill_id}」" for r in self.removals)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updates": [u.to_dict() for u in self.updates],
            "owner_user_id": self.owner_user_id,
            "chat_id": self.chat_id,
            "created_at": self.created_at,
            "ttl_seconds": self.ttl_seconds,
            "removals": [r.to_dict() for r in self.removals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingConsent":
        return cls(
            updates=[PendingSkillUpdate.from_dict(u) for u in data.get("updates", [])],
            owner_user_id=str(data.get("owner_user_id") or ""),
            chat_id=str(data.get("chat_id") or ""),
            created_at=float(data.get("created_at") or time.time()),
            ttl_seconds=float(data.get("ttl_seconds") or PENDING_TTL_SECONDS),
            removals=[PendingSkillRemoval.from_dict(r) for r in data.get("removals", [])],
        )


def write_pending(pending: PendingConsent) -> None:
    atomic_write_text(
        pending_path(),
        json.dumps(pending.to_dict(), ensure_ascii=False, indent=2) + "\n",
    )


def read_pending() -> PendingConsent | None:
    try:
        text = pending_path().read_text(encoding="utf-8")
        data = json.loads(text)
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        logger.warning("clawchat pending skill-update record unreadable; clearing")
        clear_pending()
        return None
    if not isinstance(data, dict) or not (data.get("updates") or data.get("removals")):
        return None
    try:
        return PendingConsent.from_dict(data)
    except Exception:  # noqa: BLE001
        return None


def clear_pending() -> None:
    try:
        pending_path().unlink()
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("clawchat could not clear pending skill-update record")
