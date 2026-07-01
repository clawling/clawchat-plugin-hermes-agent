"""Permission policy cache for agent operation permissions.

Mirrors the OpenClaw plugin's ``PermissionCache`` in Python.

The backend endpoint ``GET /v1/agents/me/permissions`` returns a flat object
mapping operation names to states (``"allow"`` / ``"ask"`` / ``"deny"``), plus
one special array key ``"moment.visibility"``.  This module provides:

- :class:`PermissionPolicy` — parsed snapshot of the permission response.
- :class:`PermissionCache` — lightweight in-memory store that defaults any
  unknown operation to ``"ask"``.

Usage
-----
At startup (and on every WS reconnect) call
``ApiClient.get_my_permissions()`` and pass the result to
``PermissionCache.set()``.  When deciding whether an action is permitted,
call ``cache.state_of(operation)`` which returns the stored state or
``"ask"`` when the operation is not present.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PermissionPolicy:
    """Parsed snapshot of the permissions response.

    ``by_operation`` maps operation name → state (``"allow"`` / ``"ask"`` /
    ``"deny"``).  ``moment_visibility`` holds the ``"moment.visibility"``
    array (e.g. ``["owner", "owner_friends"]``).
    """

    by_operation: dict[str, str] = field(default_factory=dict)
    moment_visibility: list[str] = field(default_factory=list)


class PermissionCache:
    """In-memory cache of agent permission policy.

    Thread-safety: the adapter runs in a single asyncio event loop, so no
    locking is needed.
    """

    def __init__(self) -> None:
        self._p = PermissionPolicy()

    def get(self) -> PermissionPolicy:
        """Return the current cached policy."""
        return self._p

    def set(self, p: PermissionPolicy) -> None:
        """Replace the cached policy with *p*."""
        self._p = p

    def state_of(self, op: str) -> str:
        """Return the permission state for *op*, defaulting to ``"ask"``."""
        return self._p.by_operation.get(op, "ask")
