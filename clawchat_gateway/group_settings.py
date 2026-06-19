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
- Version-monotonic: ``apply_fetched`` silently ignores any row whose ``version``
  is strictly less than the currently cached version for that conversation.
  Rows with ``version >= cached`` replace the stored entry.
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
        """Merge a fresh list of backend rows into the cache.

        For each row, if the cache already holds a newer-or-equal version for
        the same ``conversation_id`` the incoming row is silently discarded.
        Rows with ``version >= cached_version`` replace the stored entry.
        """
        for row in rows:
            existing = self._rows.get(row.conversation_id)
            if existing is None or row.version >= existing.version:
                self._rows[row.conversation_id] = row

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
