"""Host-local advisory locks. Files persist; only the kernel lock grants ownership."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from clawchat_gateway import __version__

logger = logging.getLogger(__name__)
WINDOWS_LOCK_OFFSET = 1024 * 1024


def _backend() -> str:
    return "windows" if os.name == "nt" else "posix"


def identity_lock_path(root: Path, user_id: str) -> Path:
    return root / "clawchat-locks" / (hashlib.sha256(user_id.encode()).hexdigest()[:16] + ".lock")


def _lock(fd: int, backend: str, *, release: bool = False) -> None:
    if backend == "windows":
        import msvcrt

        os.lseek(fd, WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@dataclass
class _Entry:
    fd: int
    backend: str
    holder: dict[str, Any]
    refs: int = 1


_registry: dict[Path, _Entry] = {}
_registry_guard = threading.Lock()
_warned: set[Path] = set()


@dataclass
class LockResult:
    outcome: Literal["acquired", "held", "unsupported"]
    path: Path
    kind: str
    holder: dict[str, Any]
    _entry: _Entry | None = None

    def retain(self) -> LockResult | None:
        """Pin ownership for a worker that can outlive its asyncio caller."""
        with _registry_guard:
            if self.outcome == "unsupported":
                return LockResult(self.outcome, self.path, self.kind, dict(self.holder))
            if self._entry is None:
                return None
            self._entry.refs += 1
            return LockResult("acquired", self.path, self.kind, dict(self.holder), self._entry)

    def release(self) -> None:
        with _registry_guard:
            entry, self._entry = self._entry, None
            if entry is None:
                return
            entry.refs -= 1
            if entry.refs:
                return
            _registry.pop(self.path, None)
            try:
                _lock(entry.fd, entry.backend, release=True)
            except OSError as exc:
                logger.warning("instance lock unlock failed path=%s error=%s", self.path, exc)
            finally:
                os.close(entry.fd)
        # Never unlink: another process may already have opened this inode.


def _holder(fd: int) -> dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        value = json.loads(os.read(fd, 8192).decode("utf-8"))
        # Treat file contents as untrusted diagnostics, never credentials.
        return {key: value[key] for key in (
            "pid", "hostname", "started_at", "kind", "profile_home", "version",
        ) if key in value} if isinstance(value, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def acquire(path: Path, *, kind: str, profile_home: Path) -> LockResult:
    """Try once, sharing a held descriptor with other callers in this process."""
    with _registry_guard:
        try:
            path = path.expanduser().resolve()
        except OSError as exc:
            return _unsupported(path, kind, exc)
        entry = _registry.get(path)
        if entry is not None:
            entry.refs += 1
            return LockResult("acquired", path, kind, dict(entry.holder), entry)
        fd = None
        locked = False
        backend = _backend()
        try:
            _mkdir_private(path.parent)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                _lock(fd, backend)
            except OSError as exc:
                if exc.errno in {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES} or (
                    backend == "windows" and exc.errno == errno.EDEADLK
                ):
                    return LockResult("held", path, kind, _holder(fd))
                raise
            locked = True
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            holder = {
                "pid": os.getpid(), "hostname": socket.gethostname(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "kind": kind, "profile_home": str(profile_home), "version": __version__,
            }
            try:
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                with os.fdopen(os.dup(fd), "w", encoding="utf-8") as stream:
                    json.dump(holder, stream)
                    stream.flush()
            except OSError as exc:
                # The kernel lock is what grants ownership; the record is only
                # diagnostics. Keep the lock (a full disk must not unlock us).
                logger.warning("instance lock holder record write failed path=%s error=%s", path, exc)
            entry = _Entry(fd, backend, holder)
            _registry[path] = entry
            fd = None  # Registry owns the descriptor now.
            return LockResult("acquired", path, kind, dict(holder), entry)
        except OSError as exc:
            return _unsupported(path, kind, exc)
        finally:
            if fd is not None:
                try:
                    if locked:
                        _lock(fd, backend, release=True)
                except OSError as exc:
                    logger.warning("instance lock unlock failed path=%s error=%s", path, exc)
                finally:
                    os.close(fd)


def _unsupported(path: Path, kind: str, error: OSError) -> LockResult:
    # Called under the registry guard, including on worker threads.
    if path not in _warned:
        _warned.add(path)
        logger.warning("instance lock unsupported path=%s error=%s; proceeding unlocked", path, error)
    return LockResult("unsupported", path, kind, {})


def _mkdir_private(path: Path) -> None:
    if not path.exists():
        _mkdir_private(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
