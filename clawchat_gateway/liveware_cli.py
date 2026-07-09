"""Resolve and auto-download the liveware CLI for the ClawChat Hermes plugin.

On startup the plugin ensures a ``liveware`` binary is available; when it is
not on PATH and not already downloaded, it fetches the OS/arch-matched
``liveware`` binary into ``<HERMES_HOME>/clawchat/liveware``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import shutil
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LIVEWARE_BASE_URL = "https://media.clawling.chat/liveware/"
_DOWNLOAD_TIMEOUT = 60  # seconds
# A non-default User-Agent is required: the CDN (Cloudflare) returns 403 for the
# stdlib default "Python-urllib/x.y" UA, so requests must identify themselves.
_USER_AGENT = "clawchat-liveware-installer"

_started_lock = threading.Lock()
_started = False


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def liveware_dir() -> Path:
    """Directory holding the downloaded binary: <HERMES_HOME>/clawchat/liveware."""
    return _hermes_home() / "clawchat" / "liveware"


def platform_assets(
    system: str | None = None, machine: str | None = None
) -> str | None:
    """Map host platform/arch to the liveware asset filename, or None."""
    sys_name = (system or platform.system()).lower()
    mach = (machine or platform.machine()).lower()
    os_name = "darwin" if sys_name == "darwin" else "linux" if sys_name == "linux" else None
    arch = (
        "amd64"
        if mach in ("x86_64", "amd64")
        else "arm64"
        if mach in ("arm64", "aarch64")
        else None
    )
    if not os_name or not arch:
        return None
    return f"liveware-{os_name}-{arch}"


def resolve_liveware_path() -> str | None:
    """Resolve the liveware executable: PATH first, then the downloaded copy."""
    if shutil.which("liveware") is not None:
        return "liveware"
    local = liveware_dir() / "liveware"
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    return None


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:  # noqa: S310
        status = getattr(resp, "status", 200)
        if status != 200:
            raise RuntimeError(f"download {url} failed: HTTP {status}")
        data = resp.read()
    if not data:
        raise RuntimeError(f"download {url} returned empty body")
    tmp = Path(str(dest) + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o755)
    os.replace(tmp, dest)


def ensure_liveware_cli() -> None:
    """Ensure the liveware CLI is present, downloading it when absent.

    Synchronous and blocking — intended to run on a background thread. Never
    raises: any failure is logged and swallowed.
    """
    try:
        if shutil.which("liveware") is not None:
            return  # PATH wins
        d = liveware_dir()
        if (d / "liveware").exists():
            return
        liveware_asset = platform_assets()
        if liveware_asset is None:
            logger.warning(
                "ClawChat: unsupported platform %s/%s; skipping liveware download",
                platform.system(),
                platform.machine(),
            )
            return
        d.mkdir(parents=True, exist_ok=True)
        _download(LIVEWARE_BASE_URL + liveware_asset, d / "liveware")
        logger.info("ClawChat: liveware CLI downloaded to %s", d)
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
