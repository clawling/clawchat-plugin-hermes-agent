"""The repository's only atomic state-file writer; use it for new state writes."""

from __future__ import annotations

import errno
import os
import stat
import tempfile
from pathlib import Path


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    unsupported = {getattr(errno, name) for name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
                   if hasattr(errno, name)}
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        if exc.errno in unsupported:
            return
        raise
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in unsupported:
                raise
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    opened = False
    try:
        stream = os.fdopen(fd, "wb")
        opened = True
        with stream:
            stream.write(data)
            stream.flush()
            permissions = mode
            if permissions is None:
                try:
                    permissions = stat.S_IMODE(path.stat().st_mode)
                except FileNotFoundError:
                    pass
            if os.name != "nt" and permissions is not None:
                os.chmod(temporary, permissions)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        if not opened:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def atomic_write_text(
    path: Path, text: str, *, encoding: str = "utf-8",
    newline: str | None = "", mode: int | None = None,
) -> None:
    if newline not in (None, "", "\n", "\r", "\r\n"):
        raise ValueError("illegal newline value")
    translation = os.linesep if newline is None else newline
    if translation:
        text = text.replace("\n", translation)
    atomic_write_bytes(path, text.encode(encoding), mode=mode)
