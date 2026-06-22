"""Per-group agent settings: dataclasses, in-memory cache, and effective-settings lookup.

Mirrors the OpenClaw plugin's ``GroupSettingsCache`` in Python.

Usage
-----
1. At startup (and on every WS reconnect / ``agent.config.changed`` signal), call
   ``ApiClient.get_my_group_settings()`` and, ONLY when the result is
   authoritative, pass ``result.rows`` plus a monotonic per-fetch ``sequence``
   (incremented at the call site) to ``GroupSettingsCache.apply_fetched()``. A
   non-authoritative result must be a no-op (preserve cache).
2. When deciding how to handle an inbound group message, call
   ``cache.effective(chat_id, static_fallback)`` to get the merged settings.

Cache contract
--------------
- Keyed by ``conversation_id``.
- Replacement set: an authoritative ``get_my_group_settings()`` returns the
  *complete* snapshot of the agent's rows, so ``apply_fetched`` REPLACES the
  cache and drops conversations absent from it (a reset / left group must not
  linger). An authoritative EMPTY snapshot means "zero overrides" and clears the
  cache.
- Authoritative-only: only HTTP 200 results reach ``apply_fetched``. A
  non-authoritative outcome (404 / endpoint-absent / non-2xx / network error)
  carries no information about the override set and must be a no-op at the call
  site — it must NOT be passed here.
- Sequence-monotonic: each dispatched fetch is tagged with a monotonic
  ``sequence`` at the call site. ``apply_fetched`` drops any pull whose
  ``sequence`` is ``<=`` the last applied one (a stale/out-of-order snapshot,
  empty or not, must never roll the cache back). ``version`` is only an
  in-snapshot tiebreaker when a conversation repeats within one pull.
- Unknown chats (no backend row) fall through to ``static_fallback``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=True, frozen=True)
class GroupSettings:
    """A single per-group backend row.

    The ``agent_id`` field returned by the backend is intentionally omitted here —
    the cache is keyed by ``conversation_id`` and the agent only ever sees its own
    settings.
    """

    conversation_id: str
    muted: bool
    reply_mode: str  # "all" | "mention"
    batch_delay_seconds: int
    version: int


@dataclass(eq=True, frozen=True)
class GroupSettingsFetchResult:
    """Outcome of a ``get_my_group_settings()`` call.

    ``authoritative`` is ``True`` only for an HTTP 200 whose envelope parsed; its
    ``rows`` (possibly empty) are the COMPLETE override snapshot and may replace
    the cache (an empty ``rows`` then means "zero overrides", which clears it).

    ``authoritative`` is ``False`` for every NON-authoritative outcome — HTTP 404
    / endpoint-absent / non-2xx / network error. Such a result carries no
    information about the override set and must be a no-op: ``rows`` is always
    empty and must never be applied as if the agent had "no overrides".
    """

    authoritative: bool
    rows: list[GroupSettings] = field(default_factory=list)


@dataclass(eq=True, frozen=True)
class EffectiveSettings:
    """The resolved (possibly backend-overridden) settings for a given chat."""

    muted: bool
    reply_mode: str  # "all" | "mention"
    batch_delay_seconds: int


class GroupSettingsCache:
    """In-memory, sequence-monotonic cache of per-group agent settings.

    Thread-safety: the adapter runs in a single asyncio event loop, so no
    locking is needed.
    """

    def __init__(self) -> None:
        # conversation_id -> GroupSettings
        self._rows: dict[str, GroupSettings] = {}
        # Highest fetch SEQUENCE applied so far. A pull whose sequence is <= this
        # is a late/out-of-order delivery and is ignored wholesale, so a slow
        # earlier snapshot can never wipe or roll back newer state. Unlike a
        # row-version guard, a sequence orders EMPTY pulls too (an empty pull has
        # no row version to compare), so a stale empty "clear all" can no longer
        # overtake a newer non-empty snapshot.
        self._max_applied_sequence: int = -1

    def apply_fetched(self, rows: list[GroupSettings], sequence: int) -> None:
        """Replace the cache from a COMPLETE, AUTHORITATIVE backend snapshot.

        ``GET /v1/agents/me/group-settings`` is a full pull: an authoritative
        HTTP 200 returns every stored per-group override for this agent, and a
        row that was reset/deleted on the backend simply disappears from the
        response (absent => default). So an authoritative pull REPLACES the
        cache, pruning conversations that vanished — merging would leave reset
        rows cached forever. An authoritative 200 with an EMPTY list means "the
        agent has zero overrides" and clears the cache.

        Callers MUST only invoke this for authoritative HTTP 200 results. A
        non-authoritative outcome (404 / endpoint-absent / non-2xx / network
        error) must be a no-op at the call site and must NOT be passed here,
        because it carries no information about the agent's actual override set.

        Out-of-order protection: each dispatched fetch is tagged with a monotonic
        ``sequence`` at the call site. A pull whose ``sequence`` is ``<=`` the
        last applied one is dropped entirely (a stale snapshot, empty or not,
        must never roll the cache back). Row ``version`` is kept only as a
        within-snapshot tiebreaker when the same conversation repeats.
        """
        # Drop a stale/out-of-order pull. This gates EMPTY pulls too: a late
        # empty "clear all" with a lower sequence can no longer wipe a newer
        # snapshot.
        if sequence <= self._max_applied_sequence:
            return
        rebuilt: dict[str, GroupSettings] = {}
        for row in rows:
            existing = rebuilt.get(row.conversation_id)
            # Within a single snapshot the same conversation should not repeat,
            # but keep the highest-version row defensively if it does.
            if existing is None or row.version >= existing.version:
                rebuilt[row.conversation_id] = row
        self._rows = rebuilt
        self._max_applied_sequence = sequence

    def effective(self, chat_id: str, static_fallback: EffectiveSettings) -> EffectiveSettings:
        """Return the effective settings for *chat_id*.

        If the backend has a row for this chat the backend values win; otherwise
        ``static_fallback`` is returned unchanged.
        """
        row = self._rows.get(chat_id)
        if row is None:
            return static_fallback
        return EffectiveSettings(
            muted=row.muted,
            reply_mode=row.reply_mode,
            batch_delay_seconds=row.batch_delay_seconds,
        )
