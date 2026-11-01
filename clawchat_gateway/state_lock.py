"""Reentrant advisory locks for complete state-file read/modify/write operations."""

from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DEFAULT_TIMEOUT = 10.0
_RETRY_SECONDS = 0.02
_WINDOWS_LOCK_OFFSET = 1024 * 1024


class StateLockError(RuntimeError):
    """State cannot be changed without lock ownership."""


class StateLockTimeout(StateLockError):
    """Another writer held the lock past the deadline."""


class StateLockUnavailable(StateLockError):
    """The file locking facility is unavailable."""


@dataclass
class _Entry:
    lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0


_registry: dict[tuple[int, Path], _Entry] = {}
_registry_guard = threading.Lock()


def _lock(fd: int, *, release: bool = False) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def state_file_lock(target: Path, *, timeout: float = DEFAULT_TIMEOUT) -> Iterator[None]:
    """Hold one thread/process lock until the complete mutation finishes.

    Nested acquisition by the owning thread shares its descriptor. Lock files
    persist so every competing process continues to lock the same inode.
    """
    target = Path(target)
    deadline = time.monotonic() + timeout
    unavailable = f"cannot lock {target.name} for a ClawChat update; retry later"
    timed_out = (
        f"timed out after {timeout:g}s waiting for another ClawChat process "
        f"to finish updating {target.name}; retry later"
    )
    try:
        path = target.parent.resolve() / f".{target.name.lstrip('.')}.lock"
        with _registry_guard:
            entry = _registry.setdefault((os.getpid(), path), _Entry())
    except (OSError, ImportError, NotImplementedError) as exc:
        raise StateLockUnavailable(unavailable) from exc
    if not entry.lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise StateLockTimeout(timed_out)
    fd = None
    locked = False
    try:
        if entry.depth:
            entry.depth += 1
            try:
                yield
            finally:
                entry.depth -= 1
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            while True:
                if time.monotonic() >= deadline:
                    raise StateLockTimeout(timed_out)
                try:
                    _lock(fd)
                    locked = True
                    break
                except OSError as exc:
                    contended = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
                    if os.name == "nt":
                        contended.add(errno.EDEADLK)
                    if exc.errno not in contended:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise StateLockTimeout(timed_out) from exc
                    time.sleep(min(_RETRY_SECONDS, remaining))
        except (OSError, ImportError, NotImplementedError) as exc:
            raise StateLockUnavailable(unavailable) from exc
        entry.depth = 1
        try:
            yield
        finally:
            entry.depth = 0
    finally:
        try:
            if fd is not None:
                try:
                    if locked:
                        _lock(fd, release=True)
                except (OSError, ImportError, NotImplementedError):
                    # Closing the descriptor below drops the kernel lock anyway;
                    # never mask the mutation's own outcome with an unlock error.
                    pass
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
        finally:
            entry.lock.release()
