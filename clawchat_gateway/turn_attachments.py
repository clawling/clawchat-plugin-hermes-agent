"""Local attachment authority from a live turn and consumed tool completions.

Only host hooks record calls/results. Model prose (including MEDIA:) is never
an artifact protocol. No latest-session lookup can recover missing authority.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import threading
import time

from clawchat_gateway import turn_grants

_delivery: ContextVar[tuple[str, str, str] | None] = ContextVar("clawchat_attachment_turn", default=None)
_processing: ContextVar[tuple[str, str, str] | None] = ContextVar("clawchat_processing_turn", default=None)
_lock = threading.Lock()
_calls: dict[tuple, tuple[bool, int]] = {}
_artifacts: dict[tuple, dict[str, FileIdentity]] = {}
completion_hook_registered = False
MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024


def start_processing(nonce: str, account_id: str, chat_id: str) -> None:
    """Called in the host processing task, before its session context exists.

    Media-only replies carry no reply_to on some hosts. Retain their exact
    source nonce through context reset, never infer it from the destination.
    """
    _processing.set((nonce, account_id, chat_id))


def finish_processing(nonce: str) -> None:
    scope = _processing.get()
    if scope and scope[0] == nonce:
        _processing.set(None)


def clear_turn(nonce: str) -> None:
    with _lock:
        for table in (_calls, _artifacts):
            for key in list(table):
                if key[0] == nonce:
                    del table[key]


def current_snapshot(chat_id: str | None = None):
    scope = _delivery.get()
    platform = turn_grants.session_context_value("HERMES_SESSION_PLATFORM")
    if scope is None and not platform:
        scope = _processing.get()
    if scope is not None:
        nonce, account, target = scope
    else:
        if turn_grants.session_context_value("HERMES_SESSION_PLATFORM") != "clawchat":
            return None
        nonce = turn_grants.session_context_value("HERMES_SESSION_MESSAGE_ID")
        target = turn_grants.session_context_value("HERMES_SESSION_CHAT_ID")
        account = ""
    snapshot = turn_grants.bound_snapshot(nonce)
    if (not snapshot or not (snapshot.speakers or snapshot.legacy_owner_tools)
            or not target or snapshot.chat_id != target
            or account and snapshot.account_id != account or chat_id and chat_id != target):
        return None
    return snapshot


@contextmanager
def delivery_turn(nonce: str | None, account_id: str, chat_id: str):
    # An explicit but invalid nonce must not fall back to another live turn.
    if not nonce:
        snapshot = current_snapshot(chat_id)
        nonce = snapshot.nonce if snapshot else ""
    token = _delivery.set((nonce, account_id, chat_id))
    try:
        yield
    finally:
        _delivery.reset(token)


def current_nonce() -> str:
    snapshot = current_snapshot()
    return snapshot.nonce if snapshot else ""


def files_direct(chat_id: str | None = None) -> bool:
    from clawchat_gateway.host_tools_guard import _tool_grants_enabled
    from clawchat_gateway.tool_capabilities import TOOL_CAPABILITIES
    from clawchat_gateway.tool_grants import evaluate_tool_grant
    try:
        snapshot = current_snapshot(chat_id)
        return bool(snapshot and _tool_grants_enabled() and all(
            evaluate_tool_grant(snapshot.grants, tool, now=time.time() * 1000) == "direct"
            for tool in TOOL_CAPABILITIES["files"]["tools"]))
    except Exception:  # noqa: BLE001 - unknown policy grants nothing
        return False


@dataclass(frozen=True)
class FileIdentity:
    realpath: str
    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    nlink: int


def _from_stat(path: str, info) -> FileIdentity:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("attachment is not a single regular file") from None
    return FileIdentity(path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                        info.st_nlink)


def _source_path(source: str) -> str:
    from clawchat_gateway.media_runtime import ensure_allowed_local_path
    # Check before Path can erase empty components or dot segments.
    expanded = os.path.expanduser(source)
    if sys.platform != "win32":
        if not expanded.startswith("/"):
            raise ValueError("attachment requires an absolute path") from None
        if "\0" in expanded or any(part in {"", ".", ".."} for part in expanded[1:].split("/")):
            raise ValueError("attachment path is unavailable") from None
    path = Path(expanded)
    if not path.is_absolute():
        raise ValueError("attachment requires an absolute path") from None
    try:
        # This precheck is diagnostic; the POSIX walk enforces no-follow.
        for component in (path, *path.parents):
            if component.is_symlink():
                raise ValueError("attachment symlink is not allowed") from None
        allowed = str(ensure_allowed_local_path(str(path)))
    except (OSError, RuntimeError):
        raise ValueError("attachment path is unavailable") from None
    if sys.platform != "win32" and allowed != expanded:
        raise ValueError("attachment path changed before open") from None
    return allowed


def _without_windows_prefix(path: str) -> str:
    if path[:8].lower() == "\\\\?\\unc\\":
        return "\\\\" + path[8:]
    return path[4:] if path.startswith("\\\\?\\") else path


def _normalized_path(path: str) -> str:
    """Normalize Windows handle paths, including prefixes, case and short names."""
    resolved = str(Path(_without_windows_prefix(path)).resolve())
    return os.path.normcase(_without_windows_prefix(resolved))


def _descriptor_path(fd: int) -> str:
    """Query a Windows handle's path; never substitute the requested path."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            get_path = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
            get_path.restype = wintypes.DWORD
            handle = msvcrt.get_osfhandle(fd)
            size = get_path(handle, None, 0, 0)
            if not 0 < size <= 32768:
                raise ValueError("attachment handle path is unavailable")
            buffer = ctypes.create_unicode_buffer(size)
            length = get_path(handle, buffer, size, 0)
            if not 0 < length < size:
                raise ValueError("attachment handle path changed")
            path = buffer.value
            path = _without_windows_prefix(path)
        else:
            raise ValueError("attachment descriptor paths are unsupported")
        if not path or not Path(path).is_absolute():
            raise ValueError("attachment descriptor path is unavailable")
        return path
    except Exception:  # noqa: BLE001 - unsupported/failed kernel lookup grants nothing
        raise ValueError("attachment descriptor path is unavailable") from None


def _descriptor_identity(fd: int) -> FileIdentity:
    """Validate a Windows handle's target against policy and fstat identity."""
    from clawchat_gateway.media_runtime import ensure_allowed_local_path

    path = _descriptor_path(fd)
    normalized = _normalized_path(path)
    before = _from_stat(normalized, os.fstat(fd))
    # Reapply the complete policy to the kernel path, including Hermes homes,
    # profiles, credentials and stores. Normalize both sides so native aliases
    # do not reject the same file; identity still comes solely from this fd.
    if _normalized_path(str(ensure_allowed_local_path(path))) != normalized:
        raise ValueError("attachment descriptor path was redirected")
    if (_normalized_path(_descriptor_path(fd)) != normalized
            or _from_stat(normalized, os.fstat(fd)) != before):
        raise ValueError("attachment descriptor changed")
    return before


def _recheck_macos_path(fd: int, expected_path: str) -> None:
    """Reject case aliases using the actual name returned by the macOS kernel."""
    from clawchat_gateway.media_runtime import ensure_allowed_local_path

    try:
        import fcntl
        # F_GETPATH is a Darwin ABI constant, absent from some Python builds.
        path = os.fsdecode(fcntl.fcntl(fd, 50, bytes(1024)).split(b"\0", 1)[0])
        # On case-insensitive volumes, string policy alone can miss case aliases;
        # the kernel path supplies the actual spelling, which must match exactly.
        if path != expected_path or str(ensure_allowed_local_path(path)) != expected_path:
            raise ValueError("attachment descriptor path was redirected") from None
    except Exception:  # noqa: BLE001 - failed kernel queries or policy grant nothing
        raise ValueError("attachment descriptor path is unavailable") from None


def _open_posix_nofollow(path: str) -> int:
    """Walk from the root without following links; transfer the final fd."""
    required = ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC")
    if (os.name != "posix" or os.open not in os.supports_dir_fd
            or any(getattr(os, flag, None) is None for flag in required)):
        raise ValueError("attachment path is unavailable") from None
    path_flags = getattr(os, "O_PATH", None)
    directory_flags = ((path_flags if path_flags is not None else os.O_RDONLY)
                       | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    fd = None
    try:
        parent = os.open("/", directory_flags)
        try:
            parts = path[1:].split("/")
            for name in parts[:-1]:
                child = os.open(name, directory_flags, dir_fd=parent)
                old, parent = parent, child
                os.close(old)
            fd = os.open(parts[-1], file_flags, dir_fd=parent)
        finally:
            os.close(parent)
    except (OSError, ValueError, NotImplementedError):
        try:
            if fd is not None:
                os.close(fd)
        finally:
            raise ValueError("attachment path is unavailable") from None
    # Ownership transfers only after closing the parent has succeeded.
    return fd


def _strict_lstat_chain(path: str, expected: FileIdentity) -> None:
    """Require every ancestor to exist as a real directory and match the leaf."""
    try:
        prefix = "/"
        if not stat.S_ISDIR(os.lstat(prefix).st_mode):
            raise ValueError("attachment changed during read") from None
        for name in path[1:].split("/")[:-1]:
            prefix = os.path.join(prefix, name)
            if not stat.S_ISDIR(os.lstat(prefix).st_mode):
                raise ValueError("attachment changed during read") from None
        if _from_stat(path, os.lstat(path)) != expected:
            raise ValueError("attachment changed during read") from None
    except (OSError, ValueError):
        raise ValueError("attachment changed during read") from None


def _recheck_open_file(source: str, fd: int, expected: FileIdentity) -> None:
    """Reapply policy and compare complete identities without reading new fds."""
    try:
        if sys.platform == "win32":
            if (_descriptor_identity(fd) != expected
                    or _normalized_path(_source_path(source)) != expected.realpath
                    or _descriptor_identity(fd) != expected):
                raise ValueError("attachment changed during read") from None
            return
        path = _source_path(source)
        if path != expected.realpath or _from_stat(path, os.fstat(fd)) != expected:
            raise ValueError("attachment changed during read") from None
        check_fd = _open_posix_nofollow(path)
        try:
            if _from_stat(path, os.fstat(check_fd)) != expected:
                raise ValueError("attachment changed during read") from None
        finally:
            os.close(check_fd)
        if sys.platform == "darwin":
            _recheck_macos_path(fd, path)
        if (_from_stat(path, os.stat(path, follow_symlinks=False)) != expected
                or _source_path(source) != expected.realpath):
            raise ValueError("attachment changed during read") from None
        # The last path sample lstats every prefix, requiring real directories
        # and the expected leaf; persistent moves or links are refused there.
        # Changes during this final walk can escape observation: user-space
        # path queries are not atomic.
        _strict_lstat_chain(path, expected)
        if _from_stat(path, os.fstat(fd)) != expected:
            raise ValueError("attachment changed during read") from None
    except (OSError, RuntimeError):
        raise ValueError("attachment path is unavailable") from None


@contextmanager
def _open_checked_file(source: str):
    path = _source_path(source)
    try:
        if sys.platform == "win32":
            expected_path = _normalized_path(path)
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(str(Path(source).expanduser()), flags)
        else:
            fd = _open_posix_nofollow(path)
    except (OSError, RuntimeError):
        raise ValueError("attachment path is unavailable") from None
    try:
        identity = (_descriptor_identity(fd) if sys.platform == "win32"
                    else _from_stat(path, os.fstat(fd)))
        if sys.platform == "win32" and identity.realpath != expected_path:
            raise ValueError("attachment path changed before open") from None
        if sys.platform == "darwin":
            _recheck_macos_path(fd, path)
        _recheck_open_file(source, fd, identity)
        yield fd, identity
    except OSError:
        raise ValueError("attachment path is unavailable") from None
    finally:
        os.close(fd)


def file_identity(source: str) -> FileIdentity:
    # Registration and preflight identity come from the opened file too.
    with _open_checked_file(source) as (_, identity):
        return identity


def _scope(snapshot):
    return (snapshot.nonce, snapshot.account_id, snapshot.chat_id, snapshot.speakers,
            snapshot.non_owner_speakers)


def _recorded_artifact(source: str, chat_id: str | None = None) -> FileIdentity | None:
    if not completion_hook_registered:
        return None
    snapshot = current_snapshot(chat_id)
    if not snapshot:
        return None
    with _lock:
        return _artifacts.get(_scope(snapshot), {}).get(source)


def artifact_identity(source: str, chat_id: str | None = None) -> FileIdentity | None:
    recorded = _recorded_artifact(source, chat_id)
    try:
        return recorded if recorded and file_identity(source) == recorded else None
    except (ValueError, OSError, RuntimeError):
        return None


def attachment_allowed(source: str | None, chat_id: str | None) -> bool:
    # This is a scope/permit check. The reader verifies the original fd;
    # additional fds only compare identity and never supply upload bytes.
    return files_direct(chat_id) or bool(source and _recorded_artifact(source, chat_id))


def _call_binding(tool_name, args, session_id, turn_id, tool_call_id=""):
    if not completion_hook_registered:
        return None
    snapshot = turn_grants.lookup_snapshot_for_call(
        turn_grants.session_context_value("HERMES_SESSION_MESSAGE_ID"),
        turn_grants.session_context_value("HERMES_SESSION_CHAT_ID"), session_id, turn_id)
    if (not snapshot or not snapshot.speakers or
            turn_grants.session_context_value("HERMES_SESSION_PLATFORM") != "clawchat"):
        return None
    if not isinstance(args, dict) or not isinstance(tool_name, str) or not tool_name:
        return None
    fingerprint = hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode()).hexdigest()
    return _scope(snapshot), (tool_call_id or "", tool_name, fingerprint)


def record_call(*, tool_name, args, decision, session_id="", turn_id="", tool_call_id="", **_):
    try:
        binding = _call_binding(tool_name, args, session_id, turn_id, tool_call_id)
        if binding is None:
            return
        scope, call = binding
        key = (*scope, call)
        permitted = decision is None or decision.get("action") == "approve"
        with _lock:
            if turn_grants.bound_snapshot(scope[0]) is None:
                return
            previous = _calls.get(key)
            # Indistinguishable concurrent calls poison each other, even by ID.
            _calls[key] = (permitted and previous is None, (previous[1] if previous else 0) + 1)
    except Exception:  # noqa: BLE001 - malformed identity adds no permission
        return


_FAILURE = {"error", "failed", "failure", "timeout", "timed_out", "blocked", "denied", "forbidden",
            "unavailable", "disabled", "aborted", "cancelled", "canceled", "killed", "invalid",
            "pending", "running", "queued", "in-progress", "approval-pending", "approval-unavailable"}
_MEDIA_KEYS = ("media", "path", "url", "mediaUrl", "filePath", "fileUrl")


def structured_media_paths(result):
    # Hermes serializes many actual tool results to JSON. Decode only the
    # completion result's JSON object, never content/text fields or MEDIA lines.
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return []
    if not isinstance(result, dict):
        return []
    details = result.get("details")
    if not isinstance(details, dict):
        return []
    for obj in (result, details):
        if (obj.get("isError") is True or obj.get("ok") is False or obj.get("success") is False
                or obj.get("error") or obj.get("timedOut") is True
                or str(obj.get("status", "")).strip().lower() in _FAILURE
                or obj.get("exitCode", 0) != 0):
            return []
    media = details.get("media")
    if not isinstance(media, dict) or media.get("outbound") is False:
        return []
    values = [media.get(key) for key in _MEDIA_KEYS]
    if isinstance(media.get("mediaUrls"), list):
        values.extend(media["mediaUrls"])
    if isinstance(media.get("attachments"), list):
        for entry in media["attachments"]:
            if isinstance(entry, dict):
                values.extend(entry.get(key) for key in _MEDIA_KEYS)
    from clawchat_gateway.media_runtime import normalize_outbound_media_reference
    return list(dict.fromkeys(normalize_outbound_media_reference(value)
                             for value in values if isinstance(value, str) and value.strip()))


def post_tool_call(*, tool_name="", args=None, result=None, session_id="", turn_id="",
                   tool_call_id="", status="", error_type=None, error_message=None, **_):
    from clawchat_gateway.process_origins import record_process_origin
    record_process_origin(tool_name=tool_name, args=args, result=result, session_id=session_id,
                          turn_id=turn_id, status=status, error_type=error_type, error_message=error_message)
    try:
        binding = _call_binding(tool_name, args, session_id, turn_id, tool_call_id)
        if binding is None:
            return
        scope, call = binding
        key = (*scope, call)
        with _lock:
            permitted, count = _calls.pop(key, (False, 0))
            if count > 1:
                _calls[key] = (False, count - 1)
        # Only a successful host completion proves that an approve directive
        # passed the host gate. Consume errors too, so no receipt can be reused.
        if not permitted or status != "ok" or error_type or error_message:
            return
        for source in structured_media_paths(result):
            try:
                identity = file_identity(source)
            except (ValueError, OSError, RuntimeError):
                continue
            with _lock:
                if turn_grants.bound_snapshot(scope[0]) is not None:
                    _artifacts.setdefault(scope, {}).setdefault(source, identity)
    except Exception:  # noqa: BLE001 - unknown completion shapes grant nothing
        return


def read_local_attachment(source: str, chat_id: str | None, *, require_turn_permission: bool = True) -> bytes:
    if not require_turn_permission:
        # Legacy callers already apply their original path policy. Preserve
        # their original read without fd lookup, type checks or size limits.
        return Path(source).read_bytes()
    recorded = None if files_direct(chat_id) else _recorded_artifact(source, chat_id)
    if not files_direct(chat_id) and recorded is None:
        raise ValueError("attachment is not authorized")
    with _open_checked_file(source) as (fd, opened):
        if recorded is not None and recorded != opened:
            raise ValueError("attachment identity changed")
        if opened.size > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment exceeds size limit")
        chunks, remaining = [], opened.size
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError("attachment changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        _recheck_open_file(source, fd, opened)
        _recheck_open_file(source, fd, opened)
        if not attachment_allowed(source, chat_id):
            raise ValueError("attachment changed during read") from None
        return b"".join(chunks)
