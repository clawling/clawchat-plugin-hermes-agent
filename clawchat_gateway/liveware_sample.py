"""Liveware Sample auto-boot for the Hermes clawchat platform.

Port of clawchat-plugin-openclaw/src/liveware-sample.ts. Downloads a
zero-dependency node demo web app from the install-cli repo (GitHub raw +
per-file sha256), runs it locally, binds a liveware tunnel, registers it as a
ClawChat app, and supervises it — deterministically, without the LLM.

Spec: clawchat-plugin-openclaw/docs/superpowers/specs/2026-07-06-liveware-sample-autoboot-design.md
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

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


# ---------------------------------------------------------------------------
# Process runners + liveware CLI wrappers
#
# Port of openclaw's liveware-sample process supervision. `parse_app_create_output`
# and `parse_tunnel_public_url` are calibrated against real captured CLI output
# (see tests/test_liveware_sample_procs.py) — do not "clean up" the regexes
# without re-checking those fixtures.
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s\"']+")
# domain row of the `tunnel bind` table: requires a trailing newline so a
# streamed/partial chunk is never parsed into a truncated host.
_DOMAIN_RE = re.compile(
    r"(?:^|\n)[ \t]*domain\b[ \t:=]+([A-Za-z0-9][A-Za-z0-9._-]*[A-Za-z0-9])[ \t]*\r?\n",
    re.IGNORECASE,
)
_LOCAL_RE = re.compile(
    r"^https?://(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(?:[:/]|$)", re.IGNORECASE
)
_APP_ID_RE = re.compile(
    r"app[ _-]?id\b\s*[:=]?\s*\"?([A-Za-z0-9][A-Za-z0-9_-]*)\"?", re.IGNORECASE
)
_ID_KV_RE = re.compile(r"\bid\s*[:=]\s*\"?([A-Za-z0-9][A-Za-z0-9_-]*)\"?", re.IGNORECASE)
_LONE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,}$")

SpawnFn = Callable[..., "Awaitable"]
ExecFn = Callable[..., "Awaitable"]

_SERVER_START_TIMEOUT = 10.0
_TUNNEL_START_TIMEOUT = 30.0
_CLI_TIMEOUT = 30.0


def parse_tunnel_public_url(output: str) -> str | None:
    """Extract the public URL from `liveware tunnel bind` output.

    The real CLI prints an aligned table whose `domain` row carries the public
    host (no scheme); the only literal URL is the LOCAL upstreamUrl. Prefer the
    domain (→ https://<domain>); fall back to the first non-local http(s) URL.
    Returns None until a full domain line (trailing newline) has arrived.
    """
    text = output or ""
    m = _DOMAIN_RE.search(text)
    if m:
        return f"https://{m.group(1)}"
    for u in _URL_RE.findall(text):
        if not _LOCAL_RE.match(u):
            return u
    return None


def _find_id_deep(value) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("app_id", "appId", "id"):
        v = value.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for v in value.values():
        found = _find_id_deep(v)
        if found:
            return found
    return None


def parse_app_create_output(stdout: str) -> str | None:
    """Parse the app id from `liveware app create` output (calibrated).

    Real output is a whitespace-aligned table row `appId   <id>`; also tolerates
    JSON and `app id: xxx` / `AppID=xxx`.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        found = _find_id_deep(json.loads(text))
        if found:
            return found
    except (ValueError, TypeError):
        pass
    m = _APP_ID_RE.search(text) or _ID_KV_RE.search(text)
    if m:
        return m.group(1)
    for line in text.split("\n"):
        t = line.strip()
        if _LONE_ID_RE.match(t):
            return t
    return None


async def _read_until(proc, match, timeout: float, label: str) -> str:
    """Accumulate proc.stdout until match(acc) is truthy; kill+raise on timeout/exit."""
    acc = ""

    async def _loop() -> str:
        nonlocal acc
        while True:
            line = await proc.stdout.readline()
            if not line:  # EOF → process exited early
                raise LivewareSampleError(f"{label} exited early")
            acc += line.decode() if isinstance(line, (bytes, bytearray)) else line
            hit = match(acc)
            if hit is not None:
                return hit

    try:
        return await asyncio.wait_for(_loop(), timeout=timeout)
    except (asyncio.TimeoutError, LivewareSampleError) as exc:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        if isinstance(exc, asyncio.TimeoutError):
            raise LivewareSampleError(f"{label} timed out after {timeout}s") from exc
        raise


async def _maybe_await(value):
    """spawn injected as a sync lambda returns a proc; real create_subprocess_exec
    returns a coroutine. Support both."""
    if asyncio.iscoroutine(value):
        return await value
    return value


async def start_sample_server(*, app_dir, port: int, spawn: SpawnFn | None = None,
                              timeout: float = _SERVER_START_TIMEOUT):
    spawn = spawn or asyncio.create_subprocess_exec
    # node runs the zero-dependency sample server.
    proc = await _maybe_await(spawn(
        "node",
        str(Path(app_dir) / "server.mjs"), "--dir", str(app_dir), "--port", str(port),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))

    def _match(acc: str):
        for ln in acc.split("\n"):
            s = ln.strip()
            if s.startswith('{"port"'):
                return s
        return None

    line = await _read_until(proc, _match, timeout, "liveware-sample server start")
    return proc, int(json.loads(line)["port"])


async def start_tunnel(*, liveware_path, app_id, port: int,
                       spawn: SpawnFn | None = None,
                       timeout: float = _TUNNEL_START_TIMEOUT):
    spawn = spawn or asyncio.create_subprocess_exec
    proc = await _maybe_await(spawn(
        liveware_path, "tunnel", "bind", app_id, f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    url = await _read_until(proc, parse_tunnel_public_url, timeout, "liveware tunnel bind")
    return proc, url


async def _communicate(proc, timeout: float, label: str):
    """Await proc.communicate() under a timeout; kill+raise on timeout.

    Mirrors `_read_until`'s contract: the constraint "超时/早退必 kill 子进程"
    means a one-shot CLI child must also be killed (not orphaned) when it hangs,
    and the failure must surface as LivewareSampleError so callers catching
    narrowly still see it.
    """
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        raise LivewareSampleError(f"{label} timed out after {timeout}s") from exc


def _scrub(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


async def liveware_login(*, liveware_path, token: str, exec: ExecFn | None = None,
                         timeout: float = _CLI_TIMEOUT) -> None:
    exec = exec or asyncio.create_subprocess_exec
    proc = await _maybe_await(exec(
        liveware_path, "login", "--access-token", token,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    out, err = await _communicate(proc, timeout, "liveware login")
    if proc.returncode:
        detail = _scrub((err or b"").decode(errors="replace")
                        or (out or b"").decode(errors="replace"), token).strip()
        raise LivewareSampleError(f"liveware login failed: {detail}")


async def liveware_app_create(*, liveware_path, name: str, exec: ExecFn | None = None,
                              timeout: float = _CLI_TIMEOUT) -> str:
    exec = exec or asyncio.create_subprocess_exec
    proc = await _maybe_await(exec(
        liveware_path, "app", "create", name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    out, err = await _communicate(proc, timeout, "liveware app create")
    stdout = (out or b"").decode(errors="replace")
    if proc.returncode:
        detail = ((err or b"").decode(errors="replace") or stdout).strip()
        raise LivewareSampleError(f"liveware app create failed: {detail}")
    app_id = parse_app_create_output(stdout)
    if not app_id:
        raise LivewareSampleError(
            f"liveware app create: cannot parse app id from output: {stdout[:500]}"
        )
    return app_id
