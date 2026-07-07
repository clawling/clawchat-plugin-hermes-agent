"""Liveware Sample auto-boot for the Hermes clawchat platform.

Port of clawchat-plugin-openclaw/src/liveware-sample.ts. Downloads a
zero-dependency node demo web app from the install-cli repo (GitHub raw +
per-file sha256), runs it locally, binds a liveware tunnel, registers it as a
ClawChat app, and supervises it — deterministically, without the LLM.

Spec: clawchat-plugin-openclaw/docs/superpowers/specs/2026-07-06-liveware-sample-autoboot-design.md
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .skill_update import DEFAULT_SKILLS_REF, OFFICIAL_SKILLS_BASE, Fetcher

LIVEWARES_TARGET = "hermes"
LIVEWARE_SAMPLE_ID = "liveware-sample"
LIVEWARE_SAMPLE_APP_NAME = "Liveware Sample"
MAX_SAMPLE_FILE_BYTES = 512 * 1024

# Files the agent owns at runtime; preserved across sample upgrades.
_USER_DATA_FILES = ("state.json", "events.jsonl")

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


class LivewareSampleError(Exception):
    """Raised on any manifest/sample validation or fetch failure."""


@dataclass(frozen=True)
class LivewareSampleFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class LivewareSampleManifest:
    version: str
    files: list[LivewareSampleFile]


def _livewares_base(ref: str) -> str:
    return f"{OFFICIAL_SKILLS_BASE.rstrip('/')}/{ref}/livewares"


def livewares_manifest_url(ref: str = DEFAULT_SKILLS_REF) -> str:
    return f"{_livewares_base(ref)}/manifest.json"


def liveware_file_url(file_path: str, ref: str = DEFAULT_SKILLS_REF) -> str:
    return f"{_livewares_base(ref)}/{file_path.lstrip('/')}"


def parse_livewares_manifest(raw: bytes | str) -> LivewareSampleManifest:
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
    try:
        parsed = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise LivewareSampleError(f"failed to parse livewares manifest: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LivewareSampleError("livewares manifest must be a JSON object")
    livewares = parsed.get("livewares")
    if not isinstance(livewares, dict):
        raise LivewareSampleError("livewares manifest missing `livewares`")
    target = livewares.get(LIVEWARES_TARGET)
    entry = target.get(LIVEWARE_SAMPLE_ID) if isinstance(target, dict) else None
    if not isinstance(entry, dict):
        raise LivewareSampleError(
            f"livewares manifest missing {LIVEWARES_TARGET}/{LIVEWARE_SAMPLE_ID}"
        )
    version = str(entry.get("version") or "").strip()
    if not version:
        raise LivewareSampleError("livewares manifest entry missing version")
    raw_files = entry.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise LivewareSampleError("livewares manifest entry missing files")
    files: list[LivewareSampleFile] = []
    for i, f in enumerate(raw_files):
        p = str((f or {}).get("path") or "").strip()
        sha = str((f or {}).get("sha256") or "").strip().lower()
        n = (f or {}).get("bytes")
        if not p or ".." in p:
            raise LivewareSampleError(f"livewares manifest file[{i}] bad path")
        if not _SHA_RE.match(sha):
            raise LivewareSampleError(f"livewares manifest file[{i}] bad sha256")
        if (
            not isinstance(n, int)
            or isinstance(n, bool)
            or n < 0
            or n > MAX_SAMPLE_FILE_BYTES
        ):
            raise LivewareSampleError(f"livewares manifest file[{i}] bad bytes")
        files.append(LivewareSampleFile(path=p, sha256=sha, bytes=n))
    return LivewareSampleManifest(version=version, files=files)


def _fetch_verified(fetch: Fetcher, url: str) -> bytes:
    try:
        raw = fetch(url)
    except LivewareSampleError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise LivewareSampleError(f"fetch {url} failed: {exc}") from exc
    if not isinstance(raw, (bytes, bytearray)):
        raise LivewareSampleError(f"fetch {url} did not return bytes")
    if len(raw) > MAX_SAMPLE_FILE_BYTES:
        raise LivewareSampleError(f"fetch {url} exceeds {MAX_SAMPLE_FILE_BYTES} bytes")
    return bytes(raw)


def download_liveware_sample(
    *, fetch: Fetcher, sample_root: Path, ref: str = DEFAULT_SKILLS_REF
) -> tuple[str, Path]:
    """Download + verify + atomically install the sample under sample_root/app.

    Preserves _USER_DATA_FILES from an existing install. Raises on any failure;
    never leaves a partially-written app/ dir behind.
    """
    sample_root = Path(sample_root)
    manifest = parse_livewares_manifest(
        _fetch_verified(fetch, livewares_manifest_url(ref))
    )
    app_dir = sample_root / "app"
    tmp_dir = sample_root / ".app.tmp"
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        for f in manifest.files:
            raw = _fetch_verified(fetch, liveware_file_url(f.path, ref))
            actual = hashlib.sha256(raw).hexdigest()
            if actual != f.sha256:
                raise LivewareSampleError(
                    f"sha256 mismatch for {f.path}: expected {f.sha256} got {actual}"
                )
            # Flatten to basename (the sample is a flat dir; matches openclaw).
            (tmp_dir / Path(f.path).name).write_bytes(raw)
        # Preserve agent/user-owned data files from a previous install.
        for name in _USER_DATA_FILES:
            prev = app_dir / name
            if prev.exists():
                shutil.copyfile(prev, tmp_dir / name)
        if app_dir.exists():
            shutil.rmtree(app_dir, ignore_errors=True)
        tmp_dir.replace(app_dir)
        return manifest.version, app_dir
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
