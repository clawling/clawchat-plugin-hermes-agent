"""Per-group agent settings: dataclasses, in-memory cache, and effective-settings lookup.

Mirrors the OpenClaw plugin's ``GroupSettingsCache`` (Task 6) in Python.

Usage
-----
1. At startup (and on every WS reconnect / ``agent.config.changed`` signal), call
   ``ApiClient.get_my_group_settings()`` and pass the result to
   ``GroupSettingsCache.apply_fetched()``.
2. When deciding how to handle an inbound group message, call
   ``cache.effective(chat_id, static_fallback)`` to get the merged settings.

Cache contract
--------------
- Keyed by ``conversation_id``.
- Replacement set: ``get_my_group_settings()`` returns the *complete* snapshot of
  the agent's rows, so ``apply_fetched`` rebuilds the cache from a non-empty fetch
  and drops conversations absent from it (a reset / left group must not linger).
- Empty fetch is a no-op, not a wipe (older backends with no endpoint also return
  an empty list via HTTP 404 — clearing on that would discard valid overrides).
- Version-monotonic: ``apply_fetched`` keeps the cached row when a fetched row's
  ``version`` is strictly less than the cached one (guards a refresh racing behind
  a newer ``agent.config.changed`` signal).
- Unknown chats (no backend row) fall through to ``static_fallback``.
"""

from __future__ import annotations

from dataclasses import dataclass


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
class EffectiveSettings:
    """The resolved (possibly backend-overridden) settings for a given chat."""

    muted: bool
    reply_mode: str  # "all" | "mention"
    batch_delay_seconds: int


class GroupSettingsCache:
    """In-memory, version-monotonic cache of per-group agent settings.

    Thread-safety: the adapter runs in a single asyncio event loop, so no
    locking is needed.
    """

    def __init__(self) -> None:
        # conversation_id -> GroupSettings
        self._rows: dict[str, GroupSettings] = {}

    def apply_fetched(self, rows: list[GroupSettings]) -> None:
        """Replace the cache with a fresh *complete* fetch of backend rows.

        ``GET /v1/agents/me/group-settings`` returns the full snapshot of all
        stored rows for this agent, so a conversation omitted from the fetch no
        longer has an override and must be dropped — otherwise a stale muted /
        reply_mode would linger until restart.

        An **empty** fetch is treated as a no-op rather than a wipe: an empty
        list is also what older backends (no endpoint -> HTTP 404) return, and
        clearing on that signal would discard valid overrides. Per-conversation
        version-monotonicity is preserved: a fetched row whose ``version`` is
        strictly older than the cached one is kept (guards a refresh racing
        behind a newer ``agent.config.changed`` signal).
        """
        if not rows:
            return
        rebuilt: dict[str, GroupSettings] = {}
        for row in rows:
            existing = self._rows.get(row.conversation_id)
            # Keep the newer cached row if this fetch raced behind a signal.
            if existing is not None and row.version < existing.version:
                rebuilt[row.conversation_id] = existing
            else:
                rebuilt[row.conversation_id] = row
        self._rows = rebuilt

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
