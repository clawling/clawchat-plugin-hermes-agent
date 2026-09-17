"""Onboarding telemetry the plugin attaches to ``/v1/agents/connect`` and
``/v1/agents/connect/check``.

All fields are optional and ignored by older backends. The vocabulary mirrors
the public connection wiki's manifest so the backend can join activations with
wiki field reports without an alias table:

* ``agent_kind`` — the wiki ``match.agent`` name for this host
* ``lane`` — ``"self"``: this plugin holds its own WebSocket and token
* ``matched_install`` — the wiki page slug that routes to this plugin
* ``os`` — ``windows | macos | linux`` (omitted otherwise)
* ``wiki_version`` — the build stamp the agent saw on the install page,
  handed in through ``CLAWCHAT_WIKI_VERSION`` (profile ``.env`` first)

Nothing here is a credential and nothing here gates activation.
"""

from __future__ import annotations

import platform
from collections.abc import Callable

AGENT_KIND = "hermes"
LANE = "self"
MATCHED_INSTALL = "install/official-hermes"

START_GUIDE_URL = "https://agent-connection.clawling.com/start.md"
RECONNECT_GUIDE_URL = "https://agent-connection.clawling.com/reconnect.md"

_WIKI_VERSION_MAX = 40
_OS_BY_SYSTEM = {"Windows": "windows", "Darwin": "macos", "Linux": "linux"}


def host_os(system: str | None = None) -> str:
    """Wiki ``os`` enum for ``platform.system()``; ``""`` for anything else."""
    return _OS_BY_SYSTEM.get(system if system is not None else platform.system(), "")


def _default_env_get(name: str) -> str:
    from clawchat_gateway.config import _get_env  # profile .env beats inherited env

    return _get_env(name) or ""


def wiki_version(env_get: Callable[[str], str] | None = None) -> str:
    getter = env_get or _default_env_get
    return (getter("CLAWCHAT_WIKI_VERSION") or "").strip()[:_WIKI_VERSION_MAX]


def onboarding_context(
    *,
    system: str | None = None,
    env_get: Callable[[str], str] | None = None,
) -> dict[str, str]:
    ctx = {"agent_kind": AGENT_KIND, "lane": LANE, "matched_install": MATCHED_INSTALL}
    os_name = host_os(system)
    if os_name:
        ctx["os"] = os_name
    version = wiki_version(env_get)
    if version:
        ctx["wiki_version"] = version
    return ctx
