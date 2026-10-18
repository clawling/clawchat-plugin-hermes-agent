"""Process-local, single-use provenance for explicitly notified completions.

Restart, seven-day expiry or eviction at 512 origins loses inheritance: future
notifications use the ordinary default row. No persisted text restores trust.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re
import threading
import time

from clawchat_gateway import turn_grants


@dataclass(frozen=True)
class ProcessOrigin:
    account_id: str
    nonce: str
    chat_id: str
    session_key: str
    non_owner_speakers: frozenset[str]
    proc_ids: frozenset[str]
    gateway_session_key: str
    registered_at: float


_lock = threading.Lock()
_origins: dict[tuple[str, str], ProcessOrigin] = {}
_MAX_ORIGINS = 512
_TTL = 7 * 24 * 60 * 60
_COMPLETION = re.compile(r"^\[IMPORTANT: Background process (proc_[A-Za-z0-9]+) .*\(exit code ")


def _expire(now: float) -> None:
    for key, origin in list(_origins.items()):
        if now - origin.registered_at >= _TTL:
            del _origins[key]


def record_process_origin(*, tool_name="", args=None, result=None, session_id="", turn_id="",
                          status="", error_type=None, error_message=None, **_) -> None:
    from clawchat_gateway.host_tools_guard import _tool_grants_enabled

    try:
        if (not _tool_grants_enabled() or tool_name != "terminal" or not isinstance(args, dict)
                or args.get("background") is not True or args.get("notify_on_complete") is not True
                or status != "ok" or error_type or error_message
                or turn_grants.session_context_value("HERMES_SESSION_PLATFORM") != "clawchat"):
            return
        data = json.loads(result) if isinstance(result, str) else result
        if (not isinstance(data, dict) or data.get("error") or data.get("isError")
                or data.get("success") is False or data.get("ok") is False):
            return
        proc_id = data.get("session_id")
        if not isinstance(proc_id, str) or not proc_id.startswith("proc_"):
            return
        nonce = turn_grants.session_context_value("HERMES_SESSION_MESSAGE_ID")
        chat_id = turn_grants.session_context_value("HERMES_SESSION_CHAT_ID")
        snapshot = turn_grants.lookup_snapshot_for_call(nonce, chat_id, session_id, turn_id)
        if (not snapshot or snapshot.owner_side or snapshot.legacy_owner_tools
                or not snapshot.non_owner_speakers or not all(
                    isinstance(id_, str) and id_.strip() for id_ in snapshot.non_owner_speakers)):
            return
        key = turn_grants.session_key_for_nonce(snapshot.account_id, nonce)
        if not key:
            return
        gateway_key = turn_grants.session_context_value("HERMES_SESSION_KEY")
        if not gateway_key:
            # The completion must name the same host session; without it there
            # is nothing trusted to compare, so the completion stays unknown.
            return
        now = time.monotonic()
        with _lock:
            _expire(now)
            identity = (snapshot.account_id, nonce)
            old = _origins.get(identity)
            if old:
                # A nonce cannot change its captured route or speaker set.
                if (old.chat_id, old.session_key, old.non_owner_speakers, old.gateway_session_key) != (
                        chat_id, key, snapshot.non_owner_speakers, gateway_key):
                    return
                _origins[identity] = replace(old, proc_ids=old.proc_ids | {proc_id})
            else:
                while len(_origins) >= _MAX_ORIGINS:
                    oldest = min(_origins, key=lambda item: _origins[item].registered_at)
                    del _origins[oldest]
                _origins[identity] = ProcessOrigin(snapshot.account_id, nonce, chat_id, key,
                    frozenset(snapshot.non_owner_speakers), frozenset({proc_id}), gateway_key, now)
    except Exception:  # noqa: BLE001 - unreadable host results carry no provenance
        return


def lookup_process_origin(event, *, account_id: str | None = None, consume: bool = False) -> ProcessOrigin | None:
    from clawchat_gateway.host_tools_guard import _tool_grants_enabled

    if getattr(event, "internal", False) is not True:
        return None
    try:
        if not _tool_grants_enabled():
            return None
    except Exception:  # noqa: BLE001 - an unreadable config cannot enable inheritance
        return None
    metadata = getattr(event, "metadata", None)
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    if (not isinstance(metadata, dict) or metadata.get("notification_origin") != "process_registry_synthetic"
            or getattr(platform, "value", platform) != "clawchat"):
        return None
    nonce = metadata.get("original_trigger_message_id")
    if not isinstance(nonce, str) or not nonce.startswith(turn_grants.NONCE_PREFIX):
        return None
    text = getattr(event, "text", None)
    first = text.splitlines()[0] if isinstance(text, str) and text else ""
    match = _COMPLETION.match(first)
    # Watch patterns can themselves contain '(exit code '; they are not exits.
    if not match or " matched watch pattern " in first:
        return None
    proc_id = match.group(1)
    with _lock:
        _expire(time.monotonic())
        candidates = [item for (account, token), item in _origins.items()
                      if token == nonce and (account_id is None or account == account_id)]
        if len(candidates) != 1:
            return None
        origin = candidates[0]
        if (getattr(source, "chat_id", None) != origin.chat_id or proc_id not in origin.proc_ids
                or not origin.gateway_session_key
                or metadata.get("gateway_session_key") != origin.gateway_session_key):
            return None
        if consume:
            key = (origin.account_id, nonce)
            remaining = origin.proc_ids - {proc_id}
            if remaining:
                _origins[key] = replace(origin, proc_ids=remaining)
            else:
                del _origins[key]
        return origin
