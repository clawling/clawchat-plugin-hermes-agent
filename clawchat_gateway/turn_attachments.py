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


def _from_stat(path: str, info) -> FileIdentity:
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("attachment is not a single regular file")
    return FileIdentity(path, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_identity(source: str) -> FileIdentity:
    from clawchat_gateway.media_runtime import ensure_allowed_local_path
    path = Path(source).expanduser()
    if not path.is_absolute():
        raise ValueError("attachment requires an absolute path")
    # Reject symlinks in directories too; resolve() alone would erase evidence.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("attachment symlink is not allowed")
    resolved = ensure_allowed_local_path(str(path))
    return _from_stat(str(resolved), path.lstat())


def _scope(snapshot):
    return (snapshot.nonce, snapshot.account_id, snapshot.chat_id, snapshot.speakers,
            snapshot.non_owner_speakers)


def artifact_identity(source: str, chat_id: str | None = None) -> FileIdentity | None:
    if not completion_hook_registered:
        return None
    snapshot = current_snapshot(chat_id)
    if not snapshot:
        return None
    with _lock:
        recorded = _artifacts.get(_scope(snapshot), {}).get(source)
    try:
        return recorded if recorded and file_identity(source) == recorded else None
    except (ValueError, OSError, RuntimeError):
        return None


def attachment_allowed(source: str | None, chat_id: str | None) -> bool:
    return files_direct(chat_id) or bool(source and artifact_identity(source, chat_id))


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


def read_local_attachment(source: str, chat_id: str | None) -> bytes:
    before = file_identity(source)
    recorded = None if files_direct(chat_id) else artifact_identity(source, chat_id)
    if not files_direct(chat_id) and recorded is None:
        raise ValueError("attachment is not authorized")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(source, flags)
    try:
        opened = _from_stat(before.realpath, os.fstat(fd))
        if opened != before or recorded is not None and recorded != opened:
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
        if (_from_stat(before.realpath, os.fstat(fd)) != opened or file_identity(source) != opened
                or not attachment_allowed(source, chat_id)):
            raise ValueError("attachment changed during read")
        return b"".join(chunks)
    finally:
        os.close(fd)
