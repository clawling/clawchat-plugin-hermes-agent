"""Stable installation identity and explicit recovery from installation conflicts."""

from __future__ import annotations

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
from pathlib import Path
from typing import Any

from clawchat_gateway.device_id import _host_device_id

logger = logging.getLogger(__name__)
_cache: dict[Path, str | None] = {}
_guard = threading.RLock()
# A failed write must still block every connection in this process.
_failed_writes: dict[Path, dict[str, Any]] = {}


def get_install_id(profile_home: Path) -> str | None:
    home = profile_home.expanduser().resolve()
    with _guard:
        if home in _cache:
            return _cache[home]
        try:
            path = home / "clawchat" / "install_seed"
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "w", encoding="ascii") as stream:
                    stream.write(uuid.uuid4().hex + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            # An O_EXCL winner may still be writing when another process reads.
            for attempt in range(10):
                seed = path.read_text(encoding="ascii").strip()
                if seed or attempt == 9:
                    break
                time.sleep(0.01)
            if re.fullmatch(r"[0-9a-f]{32}", seed) is None:
                raise ValueError("invalid installation seed")
            material = f"{seed}:{_host_device_id()}:{socket.gethostname()}:{home}"
            result = hashlib.sha256(material.encode()).hexdigest()[:24]
        except (OSError, ValueError, UnicodeError):
            logger.warning("installation identity unavailable; using legacy takeover handling")
            result = None
        _cache[home] = result
        return result


def superseded_path(profile_home: Path) -> Path:
    return profile_home.expanduser().resolve() / "clawchat" / "install_superseded.json"


def read_superseded(profile_home: Path, user_id: str | None = None) -> dict[str, Any] | None:
    """Unreadable or malformed state blocks reconnect; a new identity is exempt."""
    home = profile_home.expanduser().resolve()
    with _guard:
        value = _failed_writes.get(home)
        if value is None:
            try:
                value = json.loads(superseded_path(home).read_text(encoding="utf-8"))
                if not isinstance(value, dict) or not isinstance(value.get("user_id"), str):
                    raise ValueError("invalid superseded state")
            except FileNotFoundError:
                value = None
            except (OSError, ValueError, UnicodeError):
                value = {"unreadable": True}
        if value and user_id and value.get("user_id") and value["user_id"] != user_id:
            return None
        return value


def write_superseded(profile_home: Path, value: dict[str, Any]) -> bool:
    home = profile_home.expanduser().resolve()
    path = superseded_path(home)
    with _guard:
        temporary = None
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".install_superseded-", dir=path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _failed_writes.pop(home, None)
            return True
        except OSError:
            _failed_writes[home] = dict(value)
            logger.error("cannot persist superseded installation; remaining in standby", exc_info=True)
            return False
        finally:
            if temporary:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    logger.warning("cannot remove temporary installation state file")


def clear_superseded(profile_home: Path) -> bool:
    home = profile_home.expanduser().resolve()
    with _guard:
        existed = read_superseded(home) is not None
        if not existed:
            return False
        path = superseded_path(home)
        try:
            path.stat()
        except FileNotFoundError:
            pass
        else:
            path.unlink(missing_ok=True)
        _failed_writes.pop(home, None)
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
    state = read_superseded(home, user_id)
    return {
        "install_id": (get_install_id(home) or "")[:8] or None,
        "is_superseded": state is not None,
        "superseded": state,
        "instance_lock": live.instance_lock_status() if live else {"state": "unknown"},
        # The record persists after exit: it is diagnostic, not proof of ownership.
        "instance_lock_holder_record": holder,
    }


def reclaim_message(cleared: bool) -> str:
    if not cleared:
        return "This installation is not superseded. / 本安装未处于被取代状态。"
    return (
        "Superseded state cleared. A running gateway will reconnect within 30 seconds "
        "(this stops the other installation); otherwise it connects on next startup.\n"
        "已清除；正在运行的 gateway 会在 30 秒内重新连接（这会让另一处安装停止）；"
        "如果 gateway 没在运行，下次启动即连接。"
    )
