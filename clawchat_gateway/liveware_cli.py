"""Resolve and auto-download the liveware CLI for the ClawChat Hermes plugin.

On startup the plugin ensures a ``liveware`` binary is available; when it is
not on PATH and not already downloaded, it fetches the OS/arch-matched
``liveware`` binary into ``<HERMES_HOME>/clawchat/liveware``.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import logging
import os
import platform
import shutil
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path

logger = logging.getLogger(__name__)

LIVEWARE_BASE_URL = "https://media.clawling.chat/liveware/"
LIVEWARE_CLI_VERSION = "0.0.47-20260921"
# These shipped digests are the trust anchor, never fetched from the download
# server. Updating the CLI requires both constants to change in a plugin release.
LIVEWARE_CLI_SHA256: dict[str, str] = {
    "liveware-darwin-amd64": "28c63d371115889cd8ebeed916af0d5ede62e10016ae34a8984de31ec49d00fe",
    "liveware-darwin-arm64": "eebc9f9a881f9a4da3eb81559290581f0054d15b94eb8c0d2dd21e9472169765",
    "liveware-linux-amd64": "bbbf7c0b74e5633cf869ea3af78997d5ec0f57f3253f1fb3c3cdd75ead5cebba",
    "liveware-linux-arm64": "691b79a8f346e10780e8270d9b759bd749d1df5a3d08bf033c66765c470d6a15",
    "liveware-windows-amd64.exe": "b5fe6fa02cf23d0101af20cc3f8c936ac5d6f32a13896bc1c14622089e3504a1",
    "liveware-windows-arm64.exe": "9400709b2cdd0d2282e478fd8ef6f9953e3190f48653e2cf775817b44f0c34ce",
}
_DOWNLOAD_TIMEOUT = 60
_DOWNLOAD_DEADLINE = 240.0
_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
_DOWNLOAD_RETRY_DELAY = 2.0
_INSTALL_LOCK_TIMEOUT = _DOWNLOAD_DEADLINE + 30.0
_verified: tuple[object, ...] | None = None
_install_lock = threading.Lock()
# A non-default User-Agent is required: the CDN (Cloudflare) returns 403 for the
# stdlib default "Python-urllib/x.y" UA, so requests must identify themselves.
_USER_AGENT = "clawchat-liveware-installer"

_started_lock = threading.Lock()
_started = False


def _hermes_home() -> Path:
    """Process-wide ON PURPOSE — do NOT route this through the scoped resolver.

    This is a host-level binary CACHE. The download runs once per process
    (``ensure_liveware_cli_background``), so under a multiplexing gateway a
    per-profile path would send every named profile looking for a binary only
    the default profile's home ever received. Sharing one copy is the intent;
    nothing per-profile or mutable lives here.
    """
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def liveware_dir() -> Path:
    """Directory holding the downloaded binary: <HERMES_HOME>/clawchat/liveware."""
    return _hermes_home() / "clawchat" / "liveware"


def liveware_account_name(agent_id: str | None) -> str | None:
    """The liveware CLI account that belongs to this ClawChat agent.

    The CLI keeps every login in one host-wide file (``~/.clawling/liveware.json``,
    under the OS home, not ``$HERMES_HOME``) keyed by account, and ``login``
    names the account after the token's ``aid`` claim, lowercased. Two Hermes
    profiles on one host are two agents sharing that file, and a command
    without ``--account`` runs as the DEFAULT account, i.e. whichever agent
    logged in first. So every CLI call names this agent's account explicitly.
    ``None`` when the agent id is unknown: the call then falls back to the
    CLI's default account, the pre-isolation behaviour.
    """
    value = (agent_id or "").strip().lower()
    return value or None


def liveware_account_args(account: str | None) -> list[str]:
    """``["--account", account]`` — appended to every ``liveware`` argv — or ``[]``."""
    return ["--account", account] if account else []


def liveware_binary_name(system: str | None = None) -> str:
    """Local filename for the downloaded binary; Windows needs a .exe suffix."""
    sys_name = (system or platform.system()).lower()
    return "liveware.exe" if sys_name == "windows" else "liveware"


def platform_assets(
    system: str | None = None, machine: str | None = None
) -> str | None:
    """Map host platform/arch to the liveware asset filename, or None."""
    sys_name = (system or platform.system()).lower()
    mach = (machine or platform.machine()).lower()
    os_name = (
        "darwin"
        if sys_name == "darwin"
        else "linux"
        if sys_name == "linux"
        else "windows"
        if sys_name == "windows"
        else None
    )
    arch = (
        "amd64"
        if mach in ("x86_64", "amd64")
        else "arm64"
        if mach in ("arm64", "aarch64")
        else None
    )
    if not os_name or not arch:
        return None
    if os_name == "windows":
        return f"liveware-windows-{arch}.exe"
    return f"liveware-{os_name}-{arch}"


def _verified_local(dest: Path, asset: str | None, *, cached: bool = True) -> bool:
    global _verified
    digest = LIVEWARE_CLI_SHA256.get(asset or "")
    if not digest:
        return False
    try:
        info = dest.stat()
        if not dest.is_file() or (os.name != "nt" and not os.access(dest, os.X_OK)):
            return False
        key = (str(dest), info.st_size, info.st_mtime_ns, digest)
        if cached and key == _verified:
            return True
        with dest.open("rb") as binary:
            actual = hashlib.file_digest(binary, "sha256").hexdigest()
        if actual != digest or dest.stat() != info:
            return False
        _verified = key
        return True
    except (OSError, UnicodeError):
        return False


def resolve_liveware_path() -> str | None:
    """Resolve PATH first; accept the downloaded copy only after verification."""
    if shutil.which("liveware") is not None:
        return "liveware"
    local = liveware_dir() / liveware_binary_name()
    return str(local) if _verified_local(local, platform_assets()) else None


def _lock_install_file(fd: int, *, release: bool = False) -> None:
    """Lock the shared cache with bounded, nonblocking OS lock attempts."""
    if os.name == "nt":
        import msvcrt
    else:
        import fcntl
    if release:
        if os.name == "nt":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    deadline = time.monotonic() + _INSTALL_LOCK_TIMEOUT
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("liveware install lock timed out")
        try:
            if os.name == "nt":
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
                                 errno.EDEADLK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("liveware install lock timed out") from exc
            time.sleep(min(0.05, remaining))


def _stream_download(fd: int, url: str, expected: str, deadline: float,
                     timeout: float, cancelled: threading.Event,
                     monotonic: Callable[[], float], result: Future[None]) -> None:
    try:
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as stream:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise RuntimeError("liveware download non-200 response")
                while True:
                    if cancelled.is_set() or monotonic() >= deadline:
                        raise TimeoutError("liveware download deadline exceeded")
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > _DOWNLOAD_MAX_BYTES:
                        raise RuntimeError("liveware download exceeds size cap")
                    digest.update(chunk)
                    stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if not size:
            raise RuntimeError("liveware download empty body")
        actual = digest.hexdigest()
        if actual != expected:
            logger.warning("ClawChat: liveware digest mismatch asset=%s expected=%s actual=%s",
                           url.rsplit("/", 1)[-1], expected, actual)
            raise RuntimeError("liveware download digest mismatch")
        if cancelled.is_set() or monotonic() >= deadline:
            raise TimeoutError("liveware download deadline exceeded")
        result.set_result(None)
    except Exception as exc:
        result.set_exception(exc)


def _download(url: str, dest: Path, *, sleep: Callable[[float], None] = time.sleep,
              monotonic: Callable[[], float] = time.monotonic) -> None:
    asset = url.rsplit("/", 1)[-1]
    expected = LIVEWARE_CLI_SHA256.get(asset)
    if not expected:
        logger.warning("ClawChat: no pinned liveware digest for %s", asset)
        return
    deadline = monotonic() + _DOWNLOAD_DEADLINE
    for attempt in range(2):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("liveware download deadline exceeded")
        fd, name = tempfile.mkstemp(dir=dest.parent)
        tmp = Path(name)
        # Only this caller installs. A worker blocked in DNS/read can outlive
        # the deadline, but its late completion never publishes a binary.
        result: Future[None] = Future()
        cancelled = threading.Event()
        worker = threading.Thread(
            target=_stream_download,
            args=(fd, url, expected, deadline, min(_DOWNLOAD_TIMEOUT, remaining),
                  cancelled, monotonic, result),
            daemon=True, name="clawchat-liveware-download")
        try:
            try:
                worker.start()
            except Exception:
                os.close(fd)
                raise
            result.result(timeout=max(0.0, deadline - monotonic()))
            if monotonic() >= deadline:
                raise TimeoutError("liveware download deadline exceeded")
            if os.name != "nt":
                os.chmod(tmp, 0o755)
            # Every writer publishes verified bytes. Another pin can replace
            # them later; this process's resolver then fails closed by digest.
            os.replace(tmp, dest)
            if os.name != "nt":
                directory = os.open(dest.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            return
        except Exception as exc:
            if isinstance(exc, FutureTimeout) and (not result.done() or result.exception() is not exc):
                cancelled.set()
                raise TimeoutError("liveware download deadline exceeded") from None
            if attempt or monotonic() + _DOWNLOAD_RETRY_DELAY >= deadline:
                raise
            sleep(_DOWNLOAD_RETRY_DELAY)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except PermissionError:
                # Windows cannot unlink an open file: clean it as soon as the
                # cancelled worker closes its handle. It still cannot install.
                result.add_done_callback(lambda _future, path=tmp: path.unlink(missing_ok=True))


def ensure_liveware_cli() -> None:
    """Ensure a verified CLI on a background thread; never raise."""
    try:
        with _install_lock:
            if shutil.which("liveware") is not None:
                return
            d = liveware_dir()
            asset = platform_assets()
            if asset is None or asset not in LIVEWARE_CLI_SHA256:
                logger.warning("ClawChat: no pinned liveware asset for %s/%s",
                               platform.system(), platform.machine())
                return
            dest = d / liveware_binary_name()
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(d / ".install.lock", os.O_RDWR | os.O_CREAT, 0o600)
            locked = False
            try:
                _lock_install_file(fd)
                locked = True
                if _verified_local(dest, asset, cached=False):
                    return
                _download(LIVEWARE_BASE_URL + asset, dest)
                logger.info("ClawChat: verified liveware CLI downloaded to %s", d)
            finally:
                try:
                    if locked:
                        _lock_install_file(fd, release=True)
                finally:
                    os.close(fd)
                # Keep the lock file: waiters must continue using the same inode.
    except Exception as exc:  # noqa: BLE001
        logger.warning("ClawChat: liveware CLI download skipped: %s", exc)


def ensure_liveware_cli_background() -> None:
    """Run ensure_liveware_cli once on a daemon thread (non-blocking startup)."""
    global _started
    with _started_lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_ensure_and_signal,
        name="clawchat-liveware-cli",
        daemon=True,
    ).start()


def _ensure_and_signal() -> None:
    try:
        ensure_liveware_cli()
    finally:
        _done_event.set()


# Signals wait_liveware_cli_ready: the download attempt finished (either way).
_done_event = threading.Event()
_WAIT_CLI_READY_TIMEOUT = 300  # seconds; downloader is bounded by per-request timeouts


async def wait_liveware_cli_ready() -> None:
    """Await the background CLI download kicked off at plugin load.

    The liveware-sample supervisor must call this before gating on
    resolve_liveware_path(): on a first-ever boot the supervisor starts while
    the ~28MB download is still in flight, and skipping there is permanent
    for the process lifetime. Returns immediately when the download was never
    started (feature disabled at load) or already finished.
    """
    if not _started or _done_event.is_set():
        return
    await asyncio.to_thread(_done_event.wait, _WAIT_CLI_READY_TIMEOUT)
