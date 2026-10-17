"""Process-local grants, bound once to the host's actual execution turn."""

from __future__ import annotations

import secrets
import threading
import time
from contextvars import copy_context
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

NONCE_PREFIX = "clawchat-turn-"


@dataclass(frozen=True)
class TurnSnapshot:
    nonce: str
    account_id: str
    chat_id: str
    speakers: tuple[str, ...]
    grants: Mapping[str, str]
    created_at: float
    any_granted_tools: frozenset[str]


_lock = threading.Lock()
_snapshots: dict[str, TurnSnapshot] = {}
_bindings: dict[str, tuple[str, str]] = {}
_active: dict[tuple[str, str], str] = {}
_session_nonces: dict[tuple[str, str], set[str]] = {}


def register_turn(*, account_id: str, chat_id: str, speakers: tuple[str, ...],
                  grants: Mapping[str, str], session_key: str,
                  any_granted_tools: frozenset[str] = frozenset()) -> TurnSnapshot:
    with _lock:
        nonce = NONCE_PREFIX + secrets.token_urlsafe(24)
        snapshot = TurnSnapshot(
            nonce, account_id, chat_id, tuple(speakers),
            MappingProxyType(dict(grants)), time.monotonic(), frozenset(any_granted_tools),
        )
        _snapshots[nonce] = snapshot
        _active[account_id, session_key] = nonce
        _session_nonces.setdefault((account_id, session_key), set()).add(nonce)
        return snapshot


def get_snapshot(nonce: str) -> TurnSnapshot | None:
    with _lock:
        return _snapshots.get(nonce)


def active_snapshot(account_id: str, session_key: str) -> TurnSnapshot | None:
    with _lock:
        return _snapshots.get(_active.get((account_id, session_key), ""))


def release_turn(nonce: str) -> None:
    with _lock:
        _snapshots.pop(nonce, None)
        _bindings.pop(nonce, None)
        for key, nonces in list(_session_nonces.items()):
            nonces.discard(nonce)
            if not nonces:
                del _session_nonces[key]
            if _active.get(key) == nonce:
                del _active[key]


def release_session(account_id: str, session_key: str) -> None:
    with _lock:
        key = (account_id, session_key)
        _active.pop(key, None)
        for nonce in _session_nonces.pop(key, set()):
            _snapshots.pop(nonce, None)
            _bindings.pop(nonce, None)


def bind_turn(nonce: str, session_id: str, turn_id: str) -> None:
    with _lock:
        if nonce in _snapshots and session_id and turn_id:
            _bindings.setdefault(nonce, (session_id, turn_id))


def lookup_for_tool_call(nonce: str, chat_id: str, session_id: str, turn_id: str) -> dict[str, str]:
    with _lock:
        snapshot = _snapshots.get(nonce)
        if (not turn_id or not session_id or snapshot is None or snapshot.chat_id != chat_id
                or _bindings.get(nonce) != (session_id, turn_id)):
            return {}
        return dict(snapshot.grants)


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
