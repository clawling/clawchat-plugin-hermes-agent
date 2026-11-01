"""Process-local grants, bound once to the host's actual execution turn."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass, replace
from typing import Mapping

from clawchat_gateway.tool_grants import CompiledGrants, freeze_turn_grants, intersect_turn_grants

logger = logging.getLogger(__name__)

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
    originally_owner_side: bool = False
    downgrade_reason: str = ""
    downgrade_trigger: str = ""
    input_trigger: str = "unknown_sender"
    plugin_notification: bool = False


@dataclass(frozen=True)
class InputRestriction:
    trigger: str
    grants: Mapping[str, str]
    speakers: frozenset[str]


_lock = threading.Lock()
_snapshots: dict[str, TurnSnapshot] = {}
_bindings: dict[str, tuple[str, str]] = {}
_active: dict[tuple[str, str], str] = {}
_session_nonces: dict[tuple[str, str], set[str]] = {}
# Each pending input has its own token; a preparation await cannot associate
# another input's marker with the wrong nonce. None means not registered yet.
_unverified_inputs: dict[tuple[str, str], dict[str, tuple[str | None, str]]] = {}
_execution_nonces: dict[tuple[str, str], str] = {}
_context_execution: dict[str, str] = {}
# A consumed third-party batch retains its frozen restrictions, but cannot
# contaminate registrations for the next batch.
_consumed_batches: set[str] = set()
_approvals: dict[str, GrantApproval] = {}
_released_turns: dict[str, tuple[str, bool, float]] = {}
_MAX_RELEASED_TURNS = 512
_RELEASED_TURN_TTL = 60 * 60


def pending_input_restriction(account_id: str, session_key: str) -> InputRestriction | None:
    """Freeze input already in flight when an owner starts asynchronous preparation."""
    with _lock:
        pending = _unverified_inputs.get((account_id, session_key))
        if not pending:
            return None
        live = [_snapshots[nonce] for nonce in _session_nonces.get((account_id, session_key), ())
                if nonce in _snapshots]
        return InputRestriction(next(iter(pending.values()))[1],
                                intersect_turn_grants([{}, *(item.grants for item in live)]),
                                frozenset(speaker for item in live for speaker in item.non_owner_speakers))


def register_turn(*, account_id: str, chat_id: str, speakers: tuple[str, ...],
                  grants: Mapping[str, str], session_key: str,
                  any_granted_tools: frozenset[str] = frozenset(),
                  speaker_names: tuple[str, ...] = (), owner_verified: bool = False,
                  owner_side: bool = False, non_owner_speakers: frozenset[str] | None = None,
                  legacy_owner_tools: bool = False, request_origin: RequestOrigin | None = None,
                  approve_eligible: bool = True, orchestrate_retry: bool = False,
                  trusted_notification: bool = False, trigger: str = "unknown_sender",
                  unverified_token: str = "", arrival_restriction: InputRestriction | None = None) -> TurnSnapshot:
    with _lock:
        nonce = NONCE_PREFIX + secrets.token_urlsafe(24)
        originally_owner_side = bool(owner_side)
        owner_side = bool(owner_side and speakers and all(isinstance(id_, str) and id_.strip() for id_ in speakers))
        if owner_side and ((account_id, session_key) in _unverified_inputs or arrival_restriction):
            owner_side, grants = False, {}
        notices = _unverified_inputs.get((account_id, session_key), {})
        trusted_notification = bool(trusted_notification and not speakers and not non_owner_speakers)
        frozen = CompiledGrants(owner_only=True) if owner_side else freeze_turn_grants(grants)
        ids = (frozenset(non_owner_speakers) if non_owner_speakers is not None else
               frozen.non_owner_speakers or (frozenset() if owner_side else frozenset(speakers)))
        if arrival_restriction:
            frozen = intersect_turn_grants([frozen, arrival_restriction.grants])
            ids |= arrival_restriction.speakers
        all_overlaps = [_snapshots[value] for value in _session_nonces.get((account_id, session_key), ())
                        if value in _snapshots and (value not in _consumed_batches or notices)]
        # A local notification contributes no guest text to an owner's batch.
        # Keep its own default grants separate; neutrality must not grant it
        # owner tools or propagate its restricted grants into an owner turn.
        overlaps = [item for item in all_overlaps
                    if not (originally_owner_side and item.plugin_notification)]
        # Only the existing owner-action path retains legacy ordinary tools.
        legacy_owner_tools = bool(legacy_owner_tools and not speakers and not owner_verified
                                  and not notices and not arrival_restriction and not ids
                                  and all(not item.non_owner_speakers for item in overlaps))
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
                if trusted_notification and (item.originally_owner_side or item.owner_side or item.legacy_owner_tools):
                    _snapshots[item.nonce] = _withdraw_management(item)
                    continue
                updated = replace(item, grants=frozen, non_owner_speakers=ids,
                                                owner_side=item.owner_side and owner_side,
                                                legacy_owner_tools=item.legacy_owner_tools and not ids,
                                                orchestrate_retry=bool(orch_ok(item) and new_orch and not ids))
                _snapshots[item.nonce] = _downgrade(updated, "overlap", trigger)
        snapshot = TurnSnapshot(
            nonce, account_id, chat_id, tuple(speakers),
            frozen, time.monotonic(), frozenset(any_granted_tools),
            tuple(speaker_names), owner_verified is True and not trusted_notification and not notices and not arrival_restriction
            and not any(item.plugin_notification for item in all_overlaps),
            ids, owner_side and all(item.owner_side for item in overlaps),
            legacy_owner_tools, request_origin if not owner_side else None, bool(approve_eligible), new_orch,
            originally_owner_side, "", "", trigger, trusted_notification,
        )
        if originally_owner_side and not snapshot.owner_side:
            reason = "unverified_input" if notices or arrival_restriction else "overlap"
            cause = next((cause for _, cause in notices.values()),
                         next((item.downgrade_trigger or item.input_trigger for item in overlaps
                               if not (item.owner_side or item.legacy_owner_tools)), trigger))
            snapshot = _downgrade(snapshot, reason, arrival_restriction.trigger if arrival_restriction else cause)
        elif (owner_verified is True and snapshot.owner_side and not snapshot.owner_verified
              and any(item.plugin_notification for item in all_overlaps)):
            logger.info("owner_downgrade reason=management trigger=plugin_notice")
        if unverified_token in notices:
            notices[unverified_token] = (nonce, notices[unverified_token][1])
        _snapshots[nonce] = snapshot
        _active[account_id, session_key] = nonce
        _session_nonces.setdefault((account_id, session_key), set()).add(nonce)
        if arrival_restriction and not notices:
            # This input arrived before consumption but completed preparation
            # afterwards. Keep it restricted without poisoning the next batch.
            _consumed_batches.add(nonce)
        return snapshot


def get_snapshot(nonce: str) -> TurnSnapshot | None:
    with _lock:
        return _snapshots.get(nonce)


def _effective_snapshot_locked(context_nonce: str) -> TurnSnapshot | None:
    target = _snapshots.get(_context_execution.get(context_nonce, ""))
    if target is not None and target.nonce in _bindings:
        return target
    return _snapshots.get(context_nonce)


def effective_snapshot(context_nonce: str) -> TurnSnapshot | None:
    """Resolve a host context's current execution, retaining the raw fallback."""
    with _lock:
        return _effective_snapshot_locked(context_nonce)


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


def has_non_owner_turn(account_id: str, session_key: str) -> bool:
    """Inspect every registered, unreleased execution, including older nonces."""
    with _lock:
        return any(not snapshot.owner_side and not snapshot.legacy_owner_tools
                   for nonce in _session_nonces.get((account_id, session_key), ())
                   if (snapshot := _snapshots.get(nonce)) is not None)


def _withdraw_management(snapshot: TurnSnapshot) -> TurnSnapshot:
    """A local notice keeps ordinary owner tools; only grant management is withdrawn."""
    if snapshot.owner_verified:
        logger.info("owner_downgrade reason=management trigger=plugin_notice")
    return replace(snapshot, owner_verified=False)


def _downgrade(snapshot: TurnSnapshot, reason: str, trigger: str) -> TurnSnapshot:
    if not snapshot.originally_owner_side or snapshot.owner_side or snapshot.downgrade_reason:
        return snapshot
    logger.info("owner_downgrade reason=%s trigger=%s", reason, trigger)
    return replace(snapshot, downgrade_reason=reason, downgrade_trigger=trigger)


def revoke_owner_verification(account_id: str, session_key: str, *,
                              trigger: str = "unknown_sender", management_only: bool = False) -> None:
    """Revoke live nonces before an input can merge under an older nonce."""
    with _lock:
        _revoke_owner_verification_locked(account_id, session_key, trigger=trigger,
                                          management_only=management_only)


def _revoke_owner_verification_locked(account_id: str, session_key: str, *,
                                       trigger: str, management_only: bool = False,
                                       reason: str = "revoke") -> None:
    for nonce in _session_nonces.get((account_id, session_key), ()):
        snapshot = _snapshots.get(nonce)
        if snapshot is not None:
            if management_only:
                _snapshots[nonce] = _withdraw_management(snapshot)
                continue
            updated = replace(snapshot, owner_verified=False, owner_side=False, legacy_owner_tools=False,
                              orchestrate_retry=False, grants=intersect_turn_grants([snapshot.grants, {}]))
            _snapshots[nonce] = _downgrade(updated, reason, trigger)


def mark_unverified_input(account_id: str, session_key: str, *, trigger: str = "unknown_sender") -> str:
    """Track one unconsumed input, including preparation before registration."""
    with _lock:
        token = secrets.token_urlsafe(24)
        _unverified_inputs.setdefault((account_id, session_key), {})[token] = (None, trigger)
        _revoke_owner_verification_locked(account_id, session_key, trigger=trigger, reason="unverified_input")
        return token


def clear_unverified_input(account_id: str, session_key: str) -> None:
    """Caller must prove host idle and no plugin dispatch in progress."""
    with _lock:
        _unverified_inputs.pop((account_id, session_key), None)


def _consume_unverified_locked(nonce: str) -> None:
    for key, pending in list(_unverified_inputs.items()):
        consumed = [token for token, (value, _) in pending.items() if value == nonce]
        if consumed:
            # Do not restore existing snapshots. Only later registrations belong
            # to a new batch after the host consumed this input.
            _consumed_batches.update(_session_nonces.get(key, ()))
            for token in consumed:
                del pending[token]
            if not pending:
                del _unverified_inputs[key]


def _unbind_locked(nonce: str) -> None:
    for context, target in list(_context_execution.items()):
        if context == nonce or target == nonce:
            del _context_execution[context]
    execution = _bindings.pop(nonce, None)
    if execution is not None:
        _execution_nonces.pop(execution, None)
    _consume_unverified_locked(nonce)
    _consumed_batches.discard(nonce)


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
        _unbind_locked(nonce)
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
            _unbind_locked(nonce)
    from clawchat_gateway.turn_attachments import clear_turn
    for nonce in nonces:
        clear_turn(nonce)


def bound_snapshot(nonce: str) -> TurnSnapshot | None:
    """Attachment delivery requires a live execution binding, even after context reset."""
    with _lock:
        snapshot = _effective_snapshot_locked(nonce)
        return snapshot if snapshot is not None and snapshot.nonce in _bindings else None


def bind_turn(nonce: str, session_id: str, turn_id: str) -> None:
    with _lock:
        context_nonce = nonce
        context = _snapshots.get(nonce)
        execution = (session_id, turn_id)
        if context is None or not session_id or not turn_id or execution in _execution_nonces:
            return
        if nonce in _bindings:
            # Queued host follow-ups retain the first input's ContextVar nonce.
            # Never reuse a nonce for another execution, or borrow another chat.
            keys = [key for key, values in _session_nonces.items() if nonce in values]
            if (len(keys) != 1 or _bindings[nonce][0] != session_id
                    or _bindings[nonce][1] == turn_id):
                return
            candidates = [_snapshots[value] for value in _session_nonces[keys[0]]
                          if value in _snapshots and value not in _bindings
                          and _snapshots[value].chat_id == context.chat_id]
            if not candidates:
                return
            # Notifications contain no guest text and are neutral to owners;
            # prefer queued message inputs so a later notice cannot replace the
            # owner's snapshot. Third-party inputs remain in this pool: their
            # registration already narrows same-batch owner snapshots through
            # revocation/intersection, so this choice cannot widen that batch.
            inputs = [item for item in candidates if not item.plugin_notification]
            nonce = max(inputs or candidates, key=lambda item: item.created_at).nonce
        _bindings[nonce] = execution
        _execution_nonces[execution] = nonce
        _context_execution[context_nonce] = nonce
        _consume_unverified_locked(nonce)


def _lookup_snapshot_locked(nonce: str, chat_id: str, session_id: str, turn_id: str) -> TurnSnapshot | None:
    context = _snapshots.get(nonce)
    target = _snapshots.get(_execution_nonces.get((session_id, turn_id), ""))
    if (not session_id or not turn_id or context is None or target is None
            or context.chat_id != chat_id or target.chat_id != chat_id
            or _bindings.get(target.nonce) != (session_id, turn_id)):
        return None
    # Execution IDs alone grant no authority outside the context nonce's session.
    if not any(nonce in values and target.nonce in values for values in _session_nonces.values()):
        return None
    # Only a queued follow-up reads another nonce: its context nonce already ran
    # an earlier execution. An unbound context never borrows a bound execution.
    if target.nonce != nonce and _bindings.get(nonce) in (None, (session_id, turn_id)):
        return None
    return target


def lookup_for_tool_call(nonce: str, chat_id: str, session_id: str, turn_id: str) -> Mapping[str, str]:
    with _lock:
        snapshot = _lookup_snapshot_locked(nonce, chat_id, session_id, turn_id)
        return snapshot.grants if snapshot is not None else {}


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
        return _lookup_snapshot_locked(nonce, chat_id, session_id, turn_id)


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
