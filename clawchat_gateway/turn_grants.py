"""Process-local grants, bound once to the host's actual execution turn."""

from __future__ import annotations

import secrets
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import Mapping

from clawchat_gateway.tool_grants import CompiledGrants, freeze_turn_grants, intersect_turn_grants

NONCE_PREFIX = "clawchat-turn-"


@dataclass(frozen=True)
class RequestOrigin:
    requester_user_id: str
    source_chat_id: str
    source_chat_type: str
    source_session_key: str
    source_message_id: str
    request_summary: str


@dataclass(frozen=True)
class TurnSnapshot:
    nonce: str
    account_id: str
    chat_id: str
    speakers: tuple[str, ...]
    grants: Mapping[str, str]
    created_at: float
    any_granted_tools: frozenset[str]
    speaker_names: tuple[str, ...] = ()
    owner_verified: bool = False
    non_owner_speakers: frozenset[str] = frozenset()
    owner_side: bool = False
    legacy_owner_tools: bool = False
    request_origin: RequestOrigin | None = None
    # Host-started turns that inherit a starter's grants never use approve.
    approve_eligible: bool = True
    # Retained orchestration owner authority: approved_retry or overlapping owner turns.
    orchestrate_retry: bool = False


_lock = threading.Lock()
_snapshots: dict[str, TurnSnapshot] = {}
_bindings: dict[str, tuple[str, str]] = {}
_active: dict[tuple[str, str], str] = {}
_session_nonces: dict[tuple[str, str], set[str]] = {}
_unverified_inputs: set[tuple[str, str]] = set()
_approvals: dict[str, GrantApproval] = {}
_released_turns: dict[str, tuple[str, bool, float]] = {}
_MAX_RELEASED_TURNS = 512
_RELEASED_TURN_TTL = 60 * 60


def register_turn(*, account_id: str, chat_id: str, speakers: tuple[str, ...],
                  grants: Mapping[str, str], session_key: str,
                  any_granted_tools: frozenset[str] = frozenset(),
                  speaker_names: tuple[str, ...] = (), owner_verified: bool = False,
                  owner_side: bool = False, non_owner_speakers: frozenset[str] | None = None,
                  legacy_owner_tools: bool = False, request_origin: RequestOrigin | None = None,
                  approve_eligible: bool = True, orchestrate_retry: bool = False) -> TurnSnapshot:
    with _lock:
        nonce = NONCE_PREFIX + secrets.token_urlsafe(24)
        owner_side = bool(owner_side and speakers and all(isinstance(id_, str) and id_.strip() for id_ in speakers))
        if owner_side and (account_id, session_key) in _unverified_inputs:
            owner_side, grants = False, {}
        frozen = CompiledGrants(owner_only=True) if owner_side else freeze_turn_grants(grants)
        ids = (frozenset(non_owner_speakers) if non_owner_speakers is not None else
               frozen.non_owner_speakers or (frozenset() if owner_side else frozenset(speakers)))
        overlaps = [_snapshots[value] for value in _session_nonces.get((account_id, session_key), ())
                    if value in _snapshots]
        # A trusted plugin turn in the owner's DM retains legacy ordinary
        # tools without representing a speaker or gaining management powers.
        legacy_owner_tools = bool(legacy_owner_tools and not speakers and not owner_verified
                                  and not ids and all(not item.non_owner_speakers for item in overlaps))
        def orch_ok(item):
            return item.owner_side or item.legacy_owner_tools or item.orchestrate_retry

        new_orch = bool((owner_side or orchestrate_retry or legacy_owner_tools) and not ids
                        # A fresh approved_retry receipt may follow a turn the receipt itself revoked.
                        and all(not item.non_owner_speakers and (orchestrate_retry or orch_ok(item))
                                for item in overlaps))
        if overlaps:
            frozen = intersect_turn_grants([frozen, *(item.grants for item in overlaps)])
            ids |= frozenset(id_ for item in overlaps for id_ in item.non_owner_speakers)
            for item in overlaps:
                _snapshots[item.nonce] = replace(item, grants=frozen, non_owner_speakers=ids,
                                                owner_side=item.owner_side and owner_side,
                                                legacy_owner_tools=item.legacy_owner_tools and not ids,
                                                orchestrate_retry=bool(orch_ok(item) and new_orch and not ids))
        snapshot = TurnSnapshot(
            nonce, account_id, chat_id, tuple(speakers),
            frozen, time.monotonic(), frozenset(any_granted_tools),
            tuple(speaker_names), owner_verified is True and (account_id, session_key) not in _unverified_inputs,
            ids, owner_side and all(item.owner_side for item in overlaps),
            legacy_owner_tools, request_origin if not owner_side else None, bool(approve_eligible), new_orch,
        )
        _snapshots[nonce] = snapshot
        _active[account_id, session_key] = nonce
        _session_nonces.setdefault((account_id, session_key), set()).add(nonce)
        return snapshot


def get_snapshot(nonce: str) -> TurnSnapshot | None:
    with _lock:
        return _snapshots.get(nonce)


def get_released_turn_origin(nonce: str) -> tuple[str, bool, float] | None:
    """Read recent attribution only; never restore grants or owner verification."""
    with _lock:
        origin = _released_turns.get(nonce) if nonce else None
        return origin if origin and time.monotonic() - origin[2] < _RELEASED_TURN_TTL else None


def session_key_for_nonce(account_id: str, nonce: str) -> str:
    """Read the plugin session identity; never infer it from host/user text."""
    with _lock:
        return next((key for (account, key), nonces in _session_nonces.items()
                     if account == account_id and nonce in nonces), "")


def active_snapshot(account_id: str, session_key: str) -> TurnSnapshot | None:
    with _lock:
        return _snapshots.get(_active.get((account_id, session_key), ""))


def revoke_owner_verification(account_id: str, session_key: str) -> None:
    """Revoke every live nonce that the host could retain for this session."""
    with _lock:
        _revoke_owner_verification_locked(account_id, session_key)


def _revoke_owner_verification_locked(account_id: str, session_key: str) -> None:
    for nonce in _session_nonces.get((account_id, session_key), ()):
        snapshot = _snapshots.get(nonce)
        if snapshot is not None:
            # Unattributed input may be merged under this nonce by the host.
            # Retain denies, but intersect with the ordinary default row.
            _snapshots[nonce] = replace(snapshot, owner_verified=False, owner_side=False, legacy_owner_tools=False,
                                        orchestrate_retry=False, grants=intersect_turn_grants([snapshot.grants, {}]))


def mark_unverified_input(account_id: str, session_key: str) -> None:
    """Deny existing and future owner nonces until the host session is idle."""
    with _lock:
        _unverified_inputs.add((account_id, session_key))
        _revoke_owner_verification_locked(account_id, session_key)


def clear_unverified_input(account_id: str, session_key: str) -> None:
    """Caller must prove host idle and no plugin dispatch in progress."""
    with _lock:
        _unverified_inputs.discard((account_id, session_key))


def _record_released_turn_locked(snapshot: TurnSnapshot | None) -> None:
    if snapshot is None:
        return
    now = time.monotonic()
    for nonce, (_, _, released_at) in list(_released_turns.items()):
        if now - released_at >= _RELEASED_TURN_TTL:
            del _released_turns[nonce]
    while len(_released_turns) >= _MAX_RELEASED_TURNS:
        oldest = min(_released_turns, key=lambda nonce: _released_turns[nonce][2])
        del _released_turns[oldest]
    _released_turns[snapshot.nonce] = (snapshot.chat_id, snapshot.owner_side, now)


def release_turn(nonce: str) -> None:
    with _lock:
        _record_released_turn_locked(_snapshots.pop(nonce, None))
        _bindings.pop(nonce, None)
        for key, nonces in list(_session_nonces.items()):
            nonces.discard(nonce)
            if not nonces:
                del _session_nonces[key]
            if _active.get(key) == nonce:
                del _active[key]
    from clawchat_gateway.turn_attachments import clear_turn
    clear_turn(nonce)


def release_session(account_id: str, session_key: str) -> None:
    with _lock:
        key = (account_id, session_key)
        _active.pop(key, None)
        nonces = _session_nonces.pop(key, set())
        for nonce in nonces:
            _record_released_turn_locked(_snapshots.pop(nonce, None))
            _bindings.pop(nonce, None)
    from clawchat_gateway.turn_attachments import clear_turn
    for nonce in nonces:
        clear_turn(nonce)


def bound_snapshot(nonce: str) -> TurnSnapshot | None:
    """Attachment delivery requires a live execution binding, even after context reset."""
    with _lock:
        return _snapshots.get(nonce) if nonce in _bindings else None


def bind_turn(nonce: str, session_id: str, turn_id: str) -> None:
    with _lock:
        if nonce in _snapshots and session_id and turn_id:
            _bindings.setdefault(nonce, (session_id, turn_id))


def lookup_for_tool_call(nonce: str, chat_id: str, session_id: str, turn_id: str) -> Mapping[str, str]:
    with _lock:
        snapshot = _snapshots.get(nonce)
        if (not turn_id or not session_id or snapshot is None or snapshot.chat_id != chat_id
                or _bindings.get(nonce) != (session_id, turn_id)):
            return {}
        return snapshot.grants


def session_context_value(name: str) -> str:
    """Use the host reader only for a bound ContextVar; never its env fallback."""
    try:
        from gateway.session_context import _VAR_MAP, get_session_env

        var = _VAR_MAP.get(name)
        context = copy_context()
        if var is None or var not in context or not isinstance(context[var], str):
            return ""
        return str(get_session_env(name, "") or "").strip()
    except Exception:  # noqa: BLE001 - missing host context is no authority
        return ""


@dataclass
class GrantApproval:
    session_key: str
    chat_id: str
    tool: str
    nonce: str
    expires_at: float
    description: str
    routed: bool = False
    delivered: bool = False
    choice: str = ""


def lookup_snapshot_for_call(nonce: str, chat_id: str, session_id: str, turn_id: str) -> TurnSnapshot | None:
    with _lock:
        snapshot = _snapshots.get(nonce)
        if (not session_id or not turn_id or snapshot is None or snapshot.chat_id != chat_id
                or _bindings.get(nonce) != (session_id, turn_id)):
            return None
        return snapshot


def register_approval(session_key: str, chat_id: str, tool: str, description: str, ttl: float) -> GrantApproval:
    with _lock:
        for key, record in list(_approvals.items()):
            if record.expires_at <= time.monotonic():
                del _approvals[key]
        record = GrantApproval(session_key, chat_id, tool, secrets.token_urlsafe(24),
                               time.monotonic() + ttl, description)
        record.description += "\nApproval: " + record.nonce
        _approvals[record.nonce] = record
        return record


def approval_by_nonce(nonce: str) -> GrantApproval | None:
    with _lock:
        record = _approvals.get(nonce)
        return record if record and record.expires_at > time.monotonic() else None


def has_grant_approval(session_key: str) -> bool:
    with _lock:
        return any(r.session_key == session_key and r.routed for r in _approvals.values())


def approval_nonce_for_session(session_key: str) -> str:
    with _lock:
        records = [r for r in _approvals.values() if r.session_key == session_key and r.routed]
        return records[0].nonce if len(records) == 1 else ""


def mark_approval_delivered(nonce: str) -> None:
    with _lock:
        record = _approvals.get(nonce)
        if record and record.routed:
            record.delivered = True


def route_approval(session_key: str, chat_id: str, description: str) -> GrantApproval | None:
    # Correlate by the nonce suffix, since the host may redact the arguments.
    # A lost/changed suffix fails closed. Sharing a session is never enough.
    with _lock:
        records = [r for r in _approvals.values() if r.session_key == session_key
                   and r.chat_id == chat_id and description.endswith("\nApproval: " + r.nonce)
                   and not r.routed and r.expires_at > time.monotonic()]
        if len(records) != 1:
            return None
        records[0].routed = True
        return records[0]


def approval_decision(session_key: str, choice: str, nonce: str = "") -> str:
    with _lock:
        records = [r for r in _approvals.values() if r.session_key == session_key and r.routed
                   and (not nonce or r.nonce == nonce)]
        if not records:
            return choice
        # A session-level decision cannot reliably distinguish parallel asks.
        choice = "once" if choice == "once" and len(records) == 1 and records[0].expires_at > time.monotonic() else "deny"
        for record in records:
            record.choice = choice
        return choice


def finish_approval(nonce: str) -> bool:
    with _lock:
        record = _approvals.pop(nonce, None)
        return bool(record and record.routed and record.delivered and record.choice == "once"
                    and record.expires_at > time.monotonic())
