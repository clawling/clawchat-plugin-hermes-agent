"""Stable installation identity and explicit recovery from installation conflicts."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import socket
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from clawchat_gateway.device_id import _host_device_id

logger = logging.getLogger(__name__)
_cache: dict[Path, str | None] = {}
_guard = threading.RLock()
# Only failed event creation is cached; notice failures must not revive reclaim.
_failed_writes: dict[Path, dict[str, Any]] = {}
# Reclaim generation sampled before recording an event. Failed creations stay
# only in memory until reclaim; they are never persisted later.
_failed_generations: dict[Path, str | None] = {}
_warned_lock_paths: set[Path] = set()
_WINDOWS_LOCK_OFFSET = 1024 * 1024
STATE_LOCK_TIMEOUT_SECONDS = 120.0
_STATE_LOCK_RETRY_SECONDS = 0.05


class StateLockTimeoutError(TimeoutError):
    """Lock contention exceeded its deadline; never continue without ownership."""


class StateLockUnavailable(OSError):
    """Cross-process ownership cannot be established; refuse state mutations."""


def _state_lock_backend() -> str:
    return "windows" if os.name == "nt" else "posix"


def _lock_is_contended(exc: OSError) -> bool:
    return exc.errno in {
        errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK,
        errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK),
    }


def _derive_install_id(home: Path) -> str:
    seed = (home / "clawchat" / "install_seed").read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{32}", seed) is None:
        raise ValueError("invalid installation seed")
    material = f"{seed}:{_host_device_id()}:{socket.gethostname()}:{home}"
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def peek_install_id(profile_home: Path) -> str | None:
    """Inspect an existing seed without creating files or populating the cache."""
    try:
        return _derive_install_id(profile_home.expanduser().resolve())
    except (OSError, ValueError, UnicodeError):
        return None


def _publish_seed(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".install_seed-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(uuid.uuid4().hex + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish only a complete seed; a concurrent winner must never be replaced.
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            logger.warning("cannot remove temporary installation seed file")


def get_install_id(profile_home: Path) -> str | None:
    home = profile_home.expanduser().resolve()
    with _guard:
        if home in _cache:
            return _cache[home]
        try:
            try:
                result = _derive_install_id(home)
            except FileNotFoundError:
                _publish_seed(home / "clawchat" / "install_seed")
                result = _derive_install_id(home)
        except (OSError, ValueError, UnicodeError, AttributeError, NotImplementedError):
            logger.warning("installation identity unavailable; using legacy takeover handling")
            result = None
        _cache[home] = result
        return result


def superseded_path(profile_home: Path) -> Path:
    return profile_home.expanduser().resolve() / "clawchat" / "install_superseded.json"


def _lock_state_file(fd: int, *, release: bool = False) -> None:
    windows = _state_lock_backend() == "windows"
    if windows:
        import msvcrt
    else:
        import fcntl
    if release:
        if windows:
            os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    deadline = time.monotonic() + STATE_LOCK_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StateLockTimeoutError("superseded state lock contention timed out")
        try:
            if windows:
                os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
                # LK_LOCK can spend about ten seconds retrying internally. Use
                # nonblocking attempts near the deadline to respect the total cap.
                mode = msvcrt.LK_LOCK if remaining >= 10.0 else msvcrt.LK_NBLCK
                msvcrt.locking(fd, mode, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if not _lock_is_contended(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateLockTimeoutError("superseded state lock contention timed out") from exc
            time.sleep(min(_STATE_LOCK_RETRY_SECONDS, remaining))


@contextmanager
def _state_guard(home: Path) -> Iterator[None]:
    """Serialize a complete state mutation across threads and processes."""
    with _guard:
        path = home / "clawchat" / "install_superseded.lock"
        fd = None
        locked = False
        try:
            try:
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                _lock_state_file(fd)
                locked = True
            except StateLockTimeoutError:
                raise
            except (OSError, ImportError, NotImplementedError) as exc:
                if path not in _warned_lock_paths:
                    _warned_lock_paths.add(path)
                    logger.warning("superseded state file lock unavailable; refusing state changes")
                raise StateLockUnavailable("superseded state file lock unavailable") from exc
            yield
        finally:
            if fd is not None:
                try:
                    if locked:
                        _lock_state_file(fd, release=True)
                finally:
                    os.close(fd)
            # Never unlink the lock: other processes may be waiting on its inode.


def _read_disk_superseded_unlocked(home: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(superseded_path(home).read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("user_id"), str):
            raise ValueError("invalid superseded state")
        return value
    except FileNotFoundError:
        return None
    except (OSError, ValueError, UnicodeError):
        return {"unreadable": True}


def _read_superseded_unlocked(home: Path, user_id: str | None = None) -> dict[str, Any] | None:
    value = _failed_writes.get(home)
    if value is None:
        value = _read_disk_superseded_unlocked(home)
    if value and user_id and value.get("user_id") and value["user_id"] != user_id:
        return None
    return value


def read_superseded(profile_home: Path, user_id: str | None = None) -> dict[str, Any] | None:
    """Unreadable or malformed state blocks reconnect; a new identity is exempt."""
    home = profile_home.expanduser().resolve()
    with _guard:
        return _read_superseded_unlocked(home, user_id)


def _write_superseded_unlocked(home: Path, value: dict[str, Any]) -> bool:
    path = superseded_path(home)
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".install_superseded-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                logger.warning("cannot remove temporary installation state file")


def write_superseded(profile_home: Path, value: dict[str, Any]) -> bool:
    home = profile_home.expanduser().resolve()
    with _state_guard(home):
        saved = _write_superseded_unlocked(home, value)
        if not saved:
            logger.warning("cannot write superseded installation state")
        return saved


def _reclaim_generation_path(home: Path) -> Path:
    return home / "clawchat" / "install_reclaim_generation"


def _read_reclaim_generation(home: Path) -> str | None:
    """"" before any reclaim; None when it cannot be read."""
    try:
        return _reclaim_generation_path(home).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        return None


def _bump_reclaim_generation_unlocked(home: Path) -> None:
    path = _reclaim_generation_path(home)
    fd, temporary = tempfile.mkstemp(prefix=".install_reclaim-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(uuid.uuid4().hex + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _remember_failed_creation(home: Path, state: dict[str, Any], snapshot: str | None) -> None:
    _failed_generations[home] = snapshot
    _failed_writes[home] = dict(state)
    logger.error("cannot persist superseded installation; remaining in standby")


_NO_SNAPSHOT: Any = object()


def read_reclaim_generation(profile_home: Path) -> str | None:
    return _read_reclaim_generation(profile_home.expanduser().resolve())


def record_superseded(
    profile_home: Path, state: dict[str, Any], *, snapshot: Any = _NO_SNAPSHOT,
) -> tuple[dict[str, Any], bool]:
    """Claim a durable event once; a failed creation blocks locally without notice.

    ``snapshot`` is the reclaim generation read when the conflict was first
    observed; a reclaim completed after it wins and the event is not recorded.
    """
    home = profile_home.expanduser().resolve()
    if snapshot is _NO_SNAPSHOT:
        snapshot = _read_reclaim_generation(home)
    try:
        with _state_guard(home):
            existing = _read_superseded_unlocked(home, state.get("user_id"))
            if existing is not None:
                return existing, False
            current = _read_reclaim_generation(home)
            # A vanished file ("") proves nothing; only a present, different
            # generation shows that a reclaim completed.
            if snapshot is not None and current and current != snapshot:
                # A reclaim completed after this event began: honour it rather
                # than record an event the operator has already overridden.
                logger.warning("superseded event overtaken by a reclaim; not recording it")
                return state, False
            if not _write_superseded_unlocked(home, state):
                _remember_failed_creation(home, state, snapshot)
                return state, False
            return state, True
    except OSError:
        with _guard:
            existing = _read_superseded_unlocked(home, state.get("user_id"))
            if existing is not None:
                # Already blocking (on disk or cached). Never re-cache a durable
                # event: a later reclaim deletes it and must not be undone.
                logger.error("cannot lock superseded installation state; remaining in standby")
                return existing, False
            _remember_failed_creation(home, state, snapshot)
            return state, False


def update_owner_notice(profile_home: Path, superseded_at: str, result: dict[str, Any]) -> bool:
    """Update only the original event; a failed notice update must never be retried."""
    home = profile_home.expanduser().resolve()
    try:
        with _state_guard(home):
            current = _read_disk_superseded_unlocked(home)
            if current is None or current.get("superseded_at") != superseded_at:
                return False
            if _write_superseded_unlocked(home, {**current, "owner_notice": result}):
                return True
    except OSError:
        logger.warning("cannot update superseded owner notice", exc_info=True)
        return False
    logger.warning("cannot update superseded owner notice")
    return False


def release_reclaimed_cache(profile_home: Path) -> bool:
    """Release memory-only state after an observable reclaim; never write it back."""
    home = profile_home.expanduser().resolve()
    with _guard:
        if home not in _failed_writes:
            return False
        snapshot = _failed_generations.get(home)
        current = _read_reclaim_generation(home)
        # Reclaims only ever write the file; a vanished one proves nothing.
        if snapshot is not None and current and snapshot != current:
            _failed_writes.pop(home, None)
            _failed_generations.pop(home, None)
            return True
        return False


def clear_superseded(profile_home: Path) -> bool:
    home = profile_home.expanduser().resolve()
    with _state_guard(home):
        # Every reclaim advances the generation first, even with no local state:
        # it releases other processes' memory-only state and stops an event
        # still being recorded elsewhere. If that fails the reclaim fails with
        # disk and memory unchanged (restarting the gateway drops memory-only state).
        _bump_reclaim_generation_unlocked(home)
        existed = _read_superseded_unlocked(home) is not None
        path = superseded_path(home)
        path.unlink(missing_ok=True)
        _failed_writes.pop(home, None)
        _failed_generations.pop(home, None)
    return existed


def install_status(profile_home: Path, user_id: str | None = None) -> dict[str, Any]:
    path = profile_home / "clawchat" / "gateway.lock"
    try:
        holder = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        holder = None
    from clawchat_gateway.connection import ClawChatConnection

    home = profile_home.expanduser().resolve()
    live = next((conn for conn in ClawChatConnection._live_supervisors.values()
                 if conn._profile_home == home and not conn._connect_device_id), None)
    state = read_superseded(home)
    stale = bool(state and user_id and state.get("user_id") and state["user_id"] != user_id)
    if stale:
        state = None
    return {
        "install_id": (peek_install_id(home) or "")[:8] or None,
        "identity": user_id[:8] if user_id else "unknown",
        "is_superseded": state is not None,
        "stale_state_for_other_identity": stale,
        "superseded": state,
        "instance_lock": live.instance_lock_status() if live else {"state": "unknown"},
        # The record persists after exit: it is diagnostic, not proof of ownership.
        "instance_lock_holder_record": holder,
    }


def reclaim_message(cleared: bool) -> str:
    if not cleared:
        return (
            "This installation is not superseded (no disk record). If a running gateway "
            "logs 'cannot persist superseded installation', it will observe this reclaim "
            "and reconnect within 30 seconds.\n"
            "本安装未处于被取代状态（磁盘无记录）；若某个正在运行的 gateway 日志显示 "
            "cannot persist superseded installation，它会在 30 秒内看到这次 reclaim 并重连。"
        )
    return (
        "Superseded state cleared. A running gateway will reconnect within 30 seconds "
        "(this stops the other installation); otherwise it connects on next startup.\n"
        "已清除；正在运行的 gateway 会在 30 秒内重新连接（这会让另一处安装停止）；"
        "如果 gateway 没在运行，下次启动即连接。"
    )
