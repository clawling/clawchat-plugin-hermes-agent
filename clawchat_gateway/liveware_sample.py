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
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from . import skill_update as _skill_update
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


def _safe_kill(proc) -> None:
    """kill() a child, swallowing any error. Used on every abnormal exit path
    (timeout, early-exit, AND task cancellation) so a just-spawned subprocess is
    never orphaned before its caller has assigned it to self._server/_tunnel."""
    try:
        if proc is not None:
            proc.kill()
    except Exception:  # noqa: BLE001
        pass


async def _read_until(proc, match, timeout: float, label: str) -> str:
    """Accumulate proc.stdout+stderr until match(acc) is truthy; kill+raise on
    timeout/early-exit AND on cancellation (proc is a local here, so if we don't
    kill it on an abnormal exit it becomes a true OS orphan).

    Reads BOTH streams concurrently — like openclaw's waitForOutput
    (liveware-sample.ts's stdout+stderr `data` listeners) — rather than
    stdout alone. Two reasons: (1) a real CLI can interleave its startup line
    across stdout/stderr; (2) leaving stderr completely unread lets the OS
    pipe buffer (64 KiB) fill from a chatty child, which blocks the child on
    its next stderr write and hangs the whole flow. Once a match is found the
    caller is expected to hand the still-open proc to `_drain_pipes` as a
    background task so later output doesn't reintroduce the same problem.
    """
    acc = ""

    async def _loop() -> str:
        nonlocal acc
        pending: dict[asyncio.Future, object] = {
            asyncio.ensure_future(proc.stdout.readline()): proc.stdout,
            asyncio.ensure_future(proc.stderr.readline()): proc.stderr,
        }
        try:
            while pending:
                done, _pending_set = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    stream = pending.pop(task)
                    line = task.result()
                    if not line:  # EOF on this stream; the other may still be live
                        continue
                    acc += line.decode() if isinstance(line, (bytes, bytearray)) else line
                    hit = match(acc)
                    if hit is not None:
                        return hit
                    pending[asyncio.ensure_future(stream.readline())] = stream
            # both stdout and stderr hit EOF with no match -> process exited early
            raise LivewareSampleError(f"{label} exited early")
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    try:
        return await asyncio.wait_for(_loop(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        _safe_kill(proc)
        raise LivewareSampleError(f"{label} timed out after {timeout}s") from exc
    except BaseException:  # LivewareSampleError early-exit, CancelledError, etc.
        _safe_kill(proc)
        raise


async def _drain_pipes(proc) -> None:
    """Background task (spawn via supervisor._spawn_task): after a child's
    startup line has matched and _read_until stops actively reading, keep
    reading + discarding any further stdout/stderr to EOF. A long-lived
    chatty child (e.g. the tunnel CLI's own runtime logging) would otherwise
    fill the OS pipe buffer once nothing reads it and block on its next
    write. Naturally ends when the child exits and both fds close (e.g. via
    _kill_children's proc.kill()) — no explicit cancellation required, though
    stop() cancels it anyway like every other tracked task."""

    async def _drain_one(stream) -> None:
        if stream is None:
            return
        while True:
            try:
                line = await stream.readline()
            except Exception:  # noqa: BLE001
                return
            if not line:
                return

    await asyncio.gather(_drain_one(proc.stdout), _drain_one(proc.stderr), return_exceptions=True)


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
    return proc, int(json.loads(line)["port"]), _drain_pipes(proc)


async def tunnel_bind(*, liveware_path, app_id, port: int,
                      exec: "ExecFn | None" = None,
                      timeout: float = _CLI_TIMEOUT) -> str:
    """One-shot upstream registration (CLI v0.0.11+): `tunnel bind` writes the
    app→local-upstream mapping to the control plane, prints the binding table
    and exits 0. The data plane is carried by the persistent `liveware agent`
    daemon (see start_tunnel_agent) — bind itself no longer stays running."""
    exec = exec or asyncio.create_subprocess_exec
    proc = await _maybe_await(exec(
        liveware_path, "tunnel", "bind", app_id, f"http://127.0.0.1:{port}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    out, err = await _communicate(proc, timeout, "liveware tunnel bind")
    stdout = (out or b"").decode(errors="replace")
    stderr = (err or b"").decode(errors="replace")
    if proc.returncode:
        raise LivewareSampleError(
            f"liveware tunnel bind failed: {(stderr or stdout).strip()}")
    url = parse_tunnel_public_url(stdout) or parse_tunnel_public_url(stderr)
    if not url:
        raise LivewareSampleError(
            f"liveware tunnel bind: cannot parse public URL from output: {stdout[:500]}")
    return url


_AGENT_READY_MARKER = "relay grpc control connected"


def parse_agent_ready(output: str) -> str | None:
    """Match the `liveware agent` ready signal: the daemon logs a JSON line
    once its gRPC control channel to the relay is up — only then can the
    public URL actually reach the local upstream."""
    for ln in (output or "").split("\n"):
        if _AGENT_READY_MARKER in ln:
            return ln
    return None


async def start_tunnel_agent(*, liveware_path,
                             spawn: SpawnFn | None = None,
                             timeout: float = _TUNNEL_START_TIMEOUT):
    """Spawn the persistent `liveware agent` data-plane daemon (CLI v0.0.11+;
    replaces the long-lived `tunnel bind` / sibling tunnel-agent binary). It
    authenticates from the token saved by `liveware login` and serves every
    app bound via `tunnel bind` for that account."""
    spawn = spawn or asyncio.create_subprocess_exec
    proc = await _maybe_await(spawn(
        liveware_path, "agent",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    ))
    await _read_until(proc, parse_agent_ready, timeout, "liveware agent start")
    return proc, _drain_pipes(proc)


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
        _safe_kill(proc)
        raise LivewareSampleError(f"{label} timed out after {timeout}s") from exc
    except BaseException:  # CancelledError (stop() mid-CLI) etc. → don't orphan
        _safe_kill(proc)
        raise


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


# ---------------------------------------------------------------------------
# Supervisor
#
# Orchestrates the pieces above: first-boot bootstrap, relaunch on reconnect
# (user-deleted-app detection, URL-refresh re-register, offline fallback to a
# local copy), bounded crash-restart backoff, and retrying intro delivery.
# Port of openclaw's liveware-sample supervisor loop.
# ---------------------------------------------------------------------------

LIVEWARE_SAMPLE_INTRO_TEXT = (
    "我给你安装了一个 liveware 演示应用「Liveware Sample」，它已经出现在我们的聊天里。"
    "点开它看看，然后试试对我说：把标题改成 Hello Liveware。"
    "你在页面上点的按钮、提交的留言我也能看到，随时问我。"
)

_DEFAULT_SAMPLE_PORT = 43110
_RESTART_WINDOW_S = 30 * 60
_MAX_RESTARTS_PER_WINDOW = 5
_INTRO_RETRY_DELAY_S = 30.0
_INTRO_MAX_TRIES = 20
# Backoff for transient start() failures (template CDN 429s, network blips):
# a one-shot bootstrap would otherwise stay dormant until the next restart.
_START_RETRY_DELAYS_S = [30.0, 60.0, 120.0, 300.0]


@dataclass
class LivewareSampleDeps:
    platform: str
    account_id: str
    enabled: bool
    store: object
    sample_root: Path
    resolve_token: Callable[[], str]
    resolve_liveware_path: Callable[[], "str | None"]
    list_apps: Callable[[], Awaitable[dict]]
    register_app: Callable[..., Awaitable]
    notify_owner: Callable[[str], Awaitable[bool]]
    # Awaited by start() before the CLI gate: settles when the background CLI
    # download finished (either way), so a first boot doesn't race it, see the
    # gate to None, and silently skip forever.
    wait_cli_ready: "Callable[[], Awaitable[None]] | None" = None
    fetch: Fetcher = _skill_update._default_fetch
    ref: str = DEFAULT_SKILLS_REF
    spawn: "SpawnFn | None" = None
    exec: "ExecFn | None" = None
    log: "logging.Logger | None" = None


class LivewareSampleSupervisor:
    """Bootstraps, relaunches, and supervises the liveware-sample app for one
    (platform, account_id). Never raises out of start(); stop() tears down
    all background tasks and child processes it owns."""

    def __init__(self, deps: LivewareSampleDeps) -> None:
        self._d = deps
        self._server = None
        self._tunnel = None
        self._stopped = False
        self._restart_times: list[float] = []
        self._tasks: set[asyncio.Task] = set()
        # Bumped every time children are killed (stop, restart, cap-out) so a
        # stale watcher for an already-superseded child can't double-count a
        # crash or trigger a duplicate restart. NOTE: the watch is attached
        # only after upsert (see _watch_child) - a crash of THIS flow's own
        # in-progress children before that point does NOT bump the
        # generation and does NOT trip _bail_if_stale; that was the H8 bug
        # (agent dies right after start_tunnel_agent, before upsert -> the
        # resulting generation bump used to abort the bootstrap before the
        # row was ever persisted, orphaning the app card). _bail_if_stale
        # still matters for stop() responsiveness and for the launch-lock
        # overlap race (a stale in-flight flow superseded by a fresh one).
        self._generation = 0
        # ids of child procs already handed to a watcher, so watching both of
        # THIS launch's children (once, right after upsert) never
        # double-attaches even if called more than once.
        self._watched: set[int] = set()
        # Serializes the bounded spawn->register->upsert->attach-watch body of
        # _bootstrap/_relaunch so two launch flows (e.g. a stale in-flight
        # relaunch still awaiting start_tunnel_agent, and a fresh _delayed_relaunch
        # scheduled by its own crash watcher) never interleave. Without this,
        # the stale flow's post-await _bail_if_stale can fire *after* the new
        # flow has already assigned self._server/self._tunnel, killing the new
        # generation's children with nothing left to reschedule a relaunch.
        # stop() intentionally does NOT acquire this lock: it cancels the task
        # that may be holding it, which releases it via the `async with`
        # block's normal cancellation unwind, then stop() kills children
        # itself — see stop()/start() docstrings.
        self._launch_lock = asyncio.Lock()
        self._log = deps.log or logging.getLogger("clawchat.liveware_sample")

    # --- lifecycle -------------------------------------------------------
    async def start(self) -> None:
        """Entry point. Never raises; transient failures are retried on the
        _START_RETRY_DELAYS_S backoff (a one-shot attempt would otherwise stay
        dormant until the next platform restart)."""
        await self._start_attempt(0)

    async def _start_attempt(self, attempt: int) -> None:
        d = self._d
        try:
            if not d.enabled:
                return
            # The CLI self-download runs on a daemon thread from plugin load;
            # gate the whole flow on it or resolve_liveware_path() races to None.
            if d.wait_cli_ready is not None:
                await d.wait_cli_ready()
            if self._stopped:
                return
            key = dict(platform=d.platform, account_id=d.account_id)
            row = d.store.get_liveware_sample(**key)
            if row is not None and getattr(row, "status", None) == "disabled":
                return
            if row is not None:
                await self._relaunch(row)
            else:
                await self._bootstrap()
        except Exception as exc:  # noqa: BLE001
            self._kill_children()
            delays = _START_RETRY_DELAYS_S
            if attempt >= len(delays) or self._stopped:
                self._log.warning(
                    "liveware-sample start failed (attempt %d, giving up): %s",
                    attempt + 1, exc)
                return
            delay = delays[attempt]
            self._log.warning(
                "liveware-sample start failed (attempt %d, retrying in %.0fs): %s",
                attempt + 1, delay, exc)
            self._spawn_task(self._retry_start(attempt + 1, delay))

    async def _retry_start(self, attempt: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._stopped:
            return
        await self._start_attempt(attempt)

    async def stop(self) -> None:
        """Tear down all background tasks and children. Deliberately does NOT
        acquire ``self._launch_lock``: the caller (adapter._stop_liveware_sample)
        cancels the external start()-task before this runs, so if that task is
        parked mid-launch holding the lock, cancelling it unwinds the
        `async with self._launch_lock:` block (released in its __aexit__) before
        stop() ever needs it. Acquiring the lock here would either deadlock
        waiting on a task stop() itself must cancel, or (if cancellation raced
        ahead) be redundant — so we skip it and rely on _kill_children() below
        for cleanup instead."""
        self._stopped = True
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        self._kill_children()

    # --- helpers ---------------------------------------------------------
    def _kill_children(self) -> None:
        self._generation += 1
        for proc in (self._server, self._tunnel):
            _safe_kill(proc)
        self._server = None
        self._tunnel = None
        self._watched.clear()

    def _bail_if_stale(self, gen: int) -> bool:
        """Abort the current bootstrap/relaunch step if we were stopped, or if
        the generation has moved on for a reason unrelated to THIS flow's own
        in-progress children - e.g. the launch lock let a stale flow resume
        after a fresh relaunch already superseded it. (A crash of this flow's
        own server/tunnel is deliberately NOT visible here: the watch is
        attached only after upsert - see _watch_child - precisely so an early
        mid-bootstrap crash can't trip this check before the row is
        persisted; that was the H8 orphan bug.) Either way kill whatever
        children are live and return True so the caller stops before writing
        status="active" over a supervisor generation that's no longer
        current."""
        if self._stopped or gen != self._generation:
            self._kill_children()
            return True
        return False

    def _spawn_task(self, coro) -> None:
        if self._stopped:
            coro.close()
            return
        t = asyncio.ensure_future(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _download(self, row_version: "str | None"):
        d = self._d
        # relaunch tolerates offline: reuse a local copy if the fetch fails.
        # download_liveware_sample does blocking network + filesystem I/O
        # (urllib); run it off the event loop thread like skill_update does,
        # so a slow/hanging fetch never stalls every other connection this
        # process is servicing.
        try:
            return await asyncio.to_thread(
                download_liveware_sample,
                fetch=d.fetch, sample_root=d.sample_root, ref=d.ref)
        except Exception as exc:  # noqa: BLE001
            local = d.sample_root / "app" / "server.mjs"
            if row_version is not None and local.exists():
                self._log.debug("liveware-sample download failed, reusing local: %s", exc)
                return row_version, d.sample_root / "app"
            raise

    async def _bootstrap(self) -> None:
        d = self._d
        path = d.resolve_liveware_path()
        if not path:
            self._log.warning("liveware-sample liveware CLI not ready; skip bootstrap")
            return
        token = d.resolve_token()
        if not token:
            self._log.debug("liveware-sample no token; skip bootstrap")
            return
        try:
            apps = await d.list_apps()
        except Exception as exc:  # noqa: BLE001
            self._log.debug("liveware-sample list_apps failed; skip bootstrap: %s", exc)
            return
        if apps.get("apps"):
            self._log.debug("liveware-sample user already has liveware apps; skip bootstrap")
            return

        # Serialize the bounded spawn->register->upsert sequence against any
        # other in-flight bootstrap/relaunch (see self._launch_lock docstring
        # in __init__). Intro delivery is intentionally OUTSIDE the lock: its
        # own retry loop can run for up to _INTRO_RETRY_DELAY_S * _INTRO_MAX_TRIES
        # (~10 minutes) and must not hold up other launch flows.
        async with self._launch_lock:
            gen = self._generation
            version, app_dir = await self._download(None)
            if self._bail_if_stale(gen):
                return
            self._server, port, server_drain = await start_sample_server(
                app_dir=app_dir, port=_DEFAULT_SAMPLE_PORT, spawn=d.spawn)
            self._spawn_task(server_drain)
            if self._bail_if_stale(gen):
                return
            await liveware_login(liveware_path=path, token=token, exec=d.exec)
            if self._bail_if_stale(gen):
                return
            app_id = await liveware_app_create(
                liveware_path=path, name=LIVEWARE_SAMPLE_APP_NAME, exec=d.exec)
            if self._bail_if_stale(gen):
                return
            public_url = await tunnel_bind(
                liveware_path=path, app_id=app_id, port=port, exec=d.exec)
            if self._bail_if_stale(gen):
                return
            self._tunnel, agent_drain = await start_tunnel_agent(
                liveware_path=path, spawn=d.spawn)
            self._spawn_task(agent_drain)
            if self._bail_if_stale(gen):
                return
            # From here on we do NOT bail before persisting: once register_app
            # has created the app card, a crash of either child must not abort
            # the flow before the row is written (H8 fix). register -> upsert
            # -> watch, matching openclaw's ordering, so a mid-bootstrap crash
            # (e.g. the agent dying right after start_tunnel_agent returns, before
            # upsert) still yields a recoverable persisted row instead of an
            # orphaned app card with nothing for _delayed_relaunch to find.
            await d.register_app(name=LIVEWARE_SAMPLE_APP_NAME, app_id=app_id, url=public_url)
            d.store.upsert_liveware_sample(
                platform=d.platform, account_id=d.account_id, app_id=app_id,
                app_name=LIVEWARE_SAMPLE_APP_NAME, port=port, public_url=public_url,
                sample_version=version, status="active")
            self._watch_child(self._server)
            self._watch_child(self._tunnel)
        await self._deliver_intro()
        self._log.debug("liveware-sample bootstrap complete at %s", public_url)

    async def _relaunch(self, row) -> None:
        d = self._d
        path = d.resolve_liveware_path()
        if not path:
            self._log.warning("liveware-sample liveware CLI not ready; skip relaunch")
            return
        try:
            apps = await d.list_apps()
            if not any(a.get("app_id") == row.app_id for a in apps.get("apps", [])):
                d.store.update_liveware_sample_status(
                    platform=d.platform, account_id=d.account_id,
                    status="disabled", last_error="app removed by user")
                return
        except Exception as exc:  # noqa: BLE001
            self._log.debug("liveware-sample list_apps failed; skip relaunch: %s", exc)
            return

        # See _bootstrap for why this section is lock-serialized and intro
        # delivery is not.
        async with self._launch_lock:
            gen = self._generation
            version, app_dir = await self._download(row.sample_version)
            if self._bail_if_stale(gen):
                return
            self._server, port, server_drain = await start_sample_server(
                app_dir=app_dir, port=row.port or _DEFAULT_SAMPLE_PORT, spawn=d.spawn)
            self._spawn_task(server_drain)
            if self._bail_if_stale(gen):
                return
            public_url = await tunnel_bind(
                liveware_path=path, app_id=row.app_id, port=port, exec=d.exec)
            if self._bail_if_stale(gen):
                return
            self._tunnel, agent_drain = await start_tunnel_agent(
                liveware_path=path, spawn=d.spawn)
            self._spawn_task(agent_drain)
            if self._bail_if_stale(gen):
                return
            # See _bootstrap: no bail between register/upsert (H8 fix) so a
            # crash of either child in this window can't orphan the row.
            if public_url != row.public_url:
                await d.register_app(
                    name=row.app_name, app_id=row.app_id, url=public_url)
            d.store.upsert_liveware_sample(
                platform=d.platform, account_id=d.account_id, app_id=row.app_id,
                app_name=row.app_name, port=port, public_url=public_url,
                sample_version=version, status="active")
            self._watch_child(self._server)
            self._watch_child(self._tunnel)
        if row.intro_sent == 0:
            await self._deliver_intro()

    def _watch_child(self, proc) -> None:
        """Attach a crash watcher to one of THIS launch's children. Called only
        after upsert (see _bootstrap/_relaunch) so a mid-bootstrap crash of a
        child spawned earlier in the flow can never bump the generation - and
        so trip _bail_if_stale - before the row has been persisted (the H8
        fix: openclaw attaches its equivalent watch at the same point, right
        after upsert). Deduped by proc id so the same child is never
        double-watched (which would let one crash be counted twice).

        If the child already exited during the awaits between its own spawn
        and this attach point, nothing else is driving its wait() future (a
        real/fake proc's exit is only observed by whatever already called
        kill()/reaped it) - so check proc.returncode here and, if already
        set, handle the crash once synchronously instead of awaiting
        proc.wait(), which could hang forever for an exit we didn't ourselves
        trigger. This mirrors openclaw's post-attach
        `exitCode != null || signalCode != null` check."""
        if proc is None or id(proc) in self._watched:
            return
        self._watched.add(id(proc))
        gen = self._generation
        if getattr(proc, "returncode", None) is not None:
            self._handle_child_exit(gen)
            return
        self._spawn_task(self._wait_and_handle_child_exit(proc, gen))

    async def _wait_and_handle_child_exit(self, proc, gen: int) -> None:
        try:
            await proc.wait()
        except asyncio.CancelledError:
            return
        self._handle_child_exit(gen)

    def _handle_child_exit(self, gen: int) -> None:
        """Crash-handling body shared by the live-watch path and the
        already-exited-at-attach-time path in _watch_child above.
        Generation-guarded so the same crash is never counted twice - e.g. if
        both children happen to have already exited by attach time, the
        first call's _kill_children bumps the generation and the second
        call's now-stale `gen` makes this a no-op."""
        if self._stopped or gen != self._generation:
            return
        self._kill_children()
        now = time.monotonic()
        self._restart_times = [t for t in self._restart_times if now - t < _RESTART_WINDOW_S]
        if len(self._restart_times) >= _MAX_RESTARTS_PER_WINDOW:
            self._d.store.update_liveware_sample_status(
                platform=self._d.platform, account_id=self._d.account_id,
                status="failed", last_error="sample process crash-looping; restart cap reached")
            self._log.warning("liveware-sample restart cap reached; marked failed")
            return
        n = len(self._restart_times)
        self._restart_times.append(now)
        delay = min(5 * 2 ** n, 60)
        self._spawn_task(self._delayed_relaunch(delay))

    async def _delayed_relaunch(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if self._stopped:
            return
        row = self._d.store.get_liveware_sample(
            platform=self._d.platform, account_id=self._d.account_id)
        if row is None or row.status == "disabled":
            return
        try:
            await self._relaunch(row)
        except Exception as exc:  # noqa: BLE001
            self._kill_children()
            self._log.warning("liveware-sample relaunch failed: %s", exc)
            self._d.store.update_liveware_sample_status(
                platform=self._d.platform, account_id=self._d.account_id,
                status="failed", last_error=str(exc))

    async def _deliver_intro(self, try_index: int = 0) -> None:
        if self._stopped:
            return
        d = self._d
        delivered = False
        try:
            delivered = await d.notify_owner(LIVEWARE_SAMPLE_INTRO_TEXT)
        except Exception as exc:  # noqa: BLE001
            self._log.debug("liveware-sample intro send error: %s", exc)
        if delivered:
            d.store.mark_liveware_sample_intro_sent(
                platform=d.platform, account_id=d.account_id)
            return
        if try_index + 1 >= _INTRO_MAX_TRIES:
            self._log.debug("liveware-sample intro not delivered; will retry next boot")
            return
        self._spawn_task(self._retry_intro(try_index + 1))

    async def _retry_intro(self, try_index: int) -> None:
        try:
            await asyncio.sleep(_INTRO_RETRY_DELAY_S)
        except asyncio.CancelledError:
            return
        await self._deliver_intro(try_index)
