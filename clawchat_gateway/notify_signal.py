"""Observability hook for reliable ``notify.signal`` frames (§9.4).

The plugin keeps no friend/roster cache (friends are fetched on demand via REST
tools), so there is nothing to invalidate when a signal arrives. This observer is
therefore a pure observability hook: it dedups by ``event_id`` — the live frame
and its reliable-inbox replay carry the same id and collapse to one observation —
and reports the outcome so the caller can structured-log it. It deliberately
takes no action; wire a real reaction at the call site if the product needs one.

Mirrors the OpenClaw plugin's ``createNotifySignalObserver`` to keep the two
Protocol-v2 adapters in parity.
"""

from __future__ import annotations

from typing import Any, Literal

NotifySignalOutcome = Literal["observed", "duplicate", "invalid"]

_DEFAULT_MAX_SEEN = 512


class NotifySignalObserver:
    """Dedups ``notify.signal`` occurrences by ``event_id`` within a bounded window."""

    def __init__(self, max_seen: int = _DEFAULT_MAX_SEEN) -> None:
        self._max_seen = max_seen
        self._seen: set[str] = set()
        self._order: list[str] = []

    def observe(self, frame: dict[str, Any]) -> NotifySignalOutcome:
        """Return whether this signal is newly observed, a duplicate, or malformed.

        A frame is ``invalid`` if it lacks a non-empty ``event_id`` or ``type``.
        A frame whose ``event_id`` was seen within the retained window is a
        ``duplicate``. Otherwise it is recorded and reported as ``observed``.
        """
        payload = frame.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        event_id = payload.get("event_id")
        signal_type = payload.get("type")
        if (
            not isinstance(event_id, str)
            or not event_id
            or not isinstance(signal_type, str)
            or not signal_type
        ):
            return "invalid"
        if event_id in self._seen:
            return "duplicate"
        self._seen.add(event_id)
        self._order.append(event_id)
        while len(self._order) > self._max_seen:
            evicted = self._order.pop(0)
            self._seen.discard(evicted)
        return "observed"
