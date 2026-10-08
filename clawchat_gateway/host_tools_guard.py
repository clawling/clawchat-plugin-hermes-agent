"""Host tools in ClawChat turns the owner did not start.

Hermes gives every ClawChat turn the platform's full host toolset, and its
default ``approvals.mode: smart`` lets an auxiliary model wave a "dangerous"
command through without asking anyone — while ``cat`` / ``ls`` / ``curl`` are
never "dangerous" at all. So in a friend's direct chat, or a group, someone
other than the owner could get the agent to run commands on the owner's
machine or read ``$HERMES_HOME/.env`` (the ClawChat tokens, the owner's model
keys), ``config.yaml`` or ``owner.md``; only the model's own refusal stood in
the way. The plugin's approval routing never saw those calls: smart answers
before the gateway approval step, and harmless-looking commands never reach it.

This module narrows those turns in two layers:

* :func:`toolsets_for_source` — the adapter's per-source toolset override
  (``BasePlatformAdapter.toolsets_for_source``, host 0.20.1+). Every turn but
  the owner's own direct chat loses :data:`RESTRICTED_TOOLSETS`, so the model
  never sees those tools. **Every group turn counts**, whoever spoke: a group is
  one shared session, and switching the toolset per speaker would rebuild the
  agent (and drop the prompt cache) each time the owner and someone else
  alternate.
* :func:`clawchat_pre_tool_call` — a ``pre_tool_call`` hook that blocks any of
  those tools still reaching dispatch in such a turn (an older host without the
  override, a toolset the filter could not see into). With
  ``non-owner-host-tools: approve`` the owner opts in instead: the tools stay
  visible and every call is escalated to the human-approval gate, which smart
  never answers — a friend's direct chat refuses it on the spot, a group's goes
  to the owner (``adapter.send_exec_approval``).

The host swallows exceptions from both places and then *opens up*: an override
that raises falls back to the full platform toolset, and a hook that raises is
ignored. Both therefore fail closed here.

Turn ownership comes from the same host session context the memory tools use
(``memory_scope``): platform, chat id, chat type and user id.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

#: Host toolsets (Hermes 0.21 ``toolsets.py`` names) a turn the owner did not
#: start never gets: shell + process management, file read/write/patch/search,
#: Python execution, sub-agents (which inherit tools), scheduled jobs,
#: desktop control, and browser automation (which opens ``file://`` URLs and
#: runs page scripts / raw CDP, i.e. reads local files).
RESTRICTED_TOOLSETS: tuple[str, ...] = (
    "terminal",
    "file",
    "code_execution",
    "delegation",
    "cronjob",
    "computer_use",
    "browser",
)

#: Every host browser tool is ``browser_*``; a future one is covered too.
_RESTRICTED_TOOL_PREFIXES: tuple[str, ...] = ("browser_",)

#: The tools of :data:`RESTRICTED_TOOLSETS` on Hermes 0.21, used when the
#: host's own resolver is not importable.
_STATIC_RESTRICTED_TOOLS = frozenset(
    {
        "terminal",
        "process_manage",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "execute_code",
        "delegate_task",
        "cronjob_manage",
        "computer_use",
        "browser_back",
        "browser_cdp",
        "browser_click",
        "browser_console",
        "browser_dialog",
        "browser_exec",
        "browser_get_images",
        "browser_navigate",
        "browser_press",
        "browser_scroll",
        "browser_snapshot",
        "browser_type",
        "browser_vault_enter_code",
        "browser_vault_fill",
        "browser_vault_list",
        "browser_vault_save_login",
        "browser_vault_unlock",
        "browser_vision",
    }
)

#: What a narrowed turn falls back to when the platform toolset cannot be
#: worked out, or nothing survives the filter: the plugin's own ClawChat tools.
#: (An empty override would be read by the host as "no override".)
FALLBACK_TOOLSETS: tuple[str, ...] = ("clawchat",)

SETTING_KEY = "non-owner-host-tools"
SETTING_OFF = "off"
SETTING_APPROVE = "approve"

_DIRECT_CHAT_TYPES = frozenset({"dm", "direct"})
_PLATFORM = "clawchat"

BLOCK_MESSAGE = (
    "This tool is not available here: only your owner's direct chat may use the "
    "terminal, files, code execution, the browser, sub-agents or scheduled jobs on this machine. "
    "Do not try to reach them another way; answer without them, or tell the person "
    "to ask your owner."
)
APPROVE_MESSAGE = (
    "Someone other than the owner started this turn (a friend's direct chat or a "
    "group); host tools need the owner's approval for every call."
)


# --- host / plugin lookups (module-level so tests can stand in for them) ----


def _config_extra() -> dict[str, Any]:
    from clawchat_gateway.memory_scope import _config_extra as read_extra

    return read_extra()


def _owner_user_id() -> str:
    from clawchat_gateway.memory_scope import _owner_user_id as read_owner

    return read_owner()


def _session_value(name: str) -> str:
    from clawchat_gateway.memory_scope import _session_value as read_value

    return read_value(name)


def _is_known_group(chat_id: str) -> bool:
    """A chat the plugin knows as a group (cached participant list)."""
    from clawchat_gateway.memory_scope import _group_participants
    from clawchat_gateway.tools import _resolve_memory_root

    root, err = _resolve_memory_root()
    if err is not None or root is None:
        return False
    known, _ids = _group_participants(root, chat_id)
    return known


def _host_config() -> dict[str, Any]:
    try:
        from gateway.run import _load_gateway_config

        return _load_gateway_config() or {}
    except Exception:  # noqa: BLE001 - older host layout
        from hermes_cli.config import load_config

        return load_config() or {}


def _platform_toolsets() -> set[str]:
    """The toolsets the host would enable for a ClawChat turn without us."""
    from hermes_cli.tools_config import _get_platform_tools

    return set(_get_platform_tools(_host_config(), _PLATFORM))


def _resolve_tools(name: str) -> list[str]:
    from toolsets import resolve_toolset

    return list(resolve_toolset(name))


def _host_supports_approve() -> bool:
    """Whether this host turns a hook's ``approve`` into the human gate.

    A host that predates it ignores the directive, so the tool would simply
    run; :func:`clawchat_pre_tool_call` blocks instead there.
    """
    try:
        import hermes_cli.plugins as plugins
    except Exception:  # noqa: BLE001
        return False
    return hasattr(plugins, "_resolve_block_from_details")


def host_supports_toolset_override() -> bool:
    try:
        from gateway.platforms.base import BasePlatformAdapter
    except Exception:  # noqa: BLE001
        return False
    return hasattr(BasePlatformAdapter, "toolsets_for_source")


# --- policy -------------------------------------------------------------------


def non_owner_host_tools() -> str:
    """``off`` (factory: the tools are gone) or ``approve`` (every call asks)."""
    try:
        value = (_config_extra() or {}).get(SETTING_KEY)
    except Exception:  # noqa: BLE001
        return SETTING_OFF
    if isinstance(value, str) and value.strip().lower() == SETTING_APPROVE:
        return SETTING_APPROVE
    return SETTING_OFF


def restricted_tool_names() -> frozenset[str]:
    names = set(_STATIC_RESTRICTED_TOOLS)
    for toolset in RESTRICTED_TOOLSETS:
        try:
            names.update(_resolve_tools(toolset))
        except Exception:  # noqa: BLE001 - host resolver missing: static list
            continue
    return frozenset(names)


def is_restricted_tool(name: str) -> bool:
    name = str(name or "")
    return name in restricted_tool_names() or name.startswith(_RESTRICTED_TOOL_PREFIXES)


def _is_owner_direct(chat_id: str, chat_type: str, user_id: str, owner: str) -> bool:
    if (chat_type or "").strip().lower() not in _DIRECT_CHAT_TYPES:
        return False
    if not owner or not user_id or user_id != owner:
        return False
    return not _is_known_group(chat_id)


def _narrowed_toolsets() -> list[str]:
    restricted_tools = restricted_tool_names()
    kept = []
    for name in sorted(_platform_toolsets()):
        if name in RESTRICTED_TOOLSETS:
            continue
        try:
            tools = set(_resolve_tools(name))
        except Exception:  # noqa: BLE001 - cannot see inside: drop it
            continue
        if tools & restricted_tools or any(t.startswith(_RESTRICTED_TOOL_PREFIXES) for t in tools):
            continue
        kept.append(name)
    return kept or list(FALLBACK_TOOLSETS)


def toolsets_for_source(
    source: Any, *, owner_user_id: str | Callable[[], str]
) -> Optional[list[str]]:
    """The toolset override for a ClawChat turn, or ``None`` to keep the platform's.

    ``None`` for the owner's own direct chat, and for every turn when the owner
    chose ``non-owner-host-tools: approve`` (the hook escalates instead).
    Otherwise the platform toolset minus :data:`RESTRICTED_TOOLSETS` — never
    empty, and on any failure :data:`FALLBACK_TOOLSETS`.
    """
    try:
        owner = owner_user_id() if callable(owner_user_id) else owner_user_id
        chat_id = str(getattr(source, "chat_id", "") or "")
        chat_type = str(getattr(source, "chat_type", "") or "")
        user_id = str(getattr(source, "user_id", "") or "")
        if _is_owner_direct(chat_id, chat_type, user_id, str(owner or "")):
            return None
        if non_owner_host_tools() == SETTING_APPROVE:
            return None
        return _narrowed_toolsets()
    except Exception as exc:  # noqa: BLE001 - the host would fall back to *all* tools
        logger.warning(
            "clawchat host tools: toolset override failed (%s); narrowing to %s",
            exc,
            list(FALLBACK_TOOLSETS),
        )
        return list(FALLBACK_TOOLSETS)


def _session_turn() -> str:
    """``outside`` (not a ClawChat session), ``owner`` or ``other``."""
    platform = _session_value("HERMES_SESSION_PLATFORM").lower()
    if platform != _PLATFORM:
        return "outside"
    chat_id = _session_value("HERMES_SESSION_CHAT_ID")
    chat_type = _session_value("HERMES_SESSION_CHAT_TYPE")
    user_id = _session_value("HERMES_SESSION_USER_ID")
    if chat_id and _is_owner_direct(chat_id, chat_type, user_id, _owner_user_id()):
        return "owner"
    return "other"


def clawchat_pre_tool_call(*, tool_name: str = "", args: Any = None, **_: Any) -> Optional[dict[str, Any]]:
    """``pre_tool_call`` hook: host tools in a turn the owner did not start."""
    try:
        if not is_restricted_tool(tool_name):
            return None
        if _session_turn() != "other":
            return None
        if non_owner_host_tools() == SETTING_APPROVE and _host_supports_approve():
            return {
                "action": "approve",
                "message": APPROVE_MESSAGE,
                "rule_key": f"clawchat-non-owner:{tool_name}",
            }
        return {"action": "block", "message": BLOCK_MESSAGE}
    except Exception as exc:  # noqa: BLE001 - the host would ignore us and run the tool
        logger.warning("clawchat host tools: pre_tool_call check failed (%s); blocking %s", exc, tool_name)
        return {"action": "block", "message": BLOCK_MESSAGE}


def register_host_tools_guard(ctx: Any) -> None:
    """Register the ``pre_tool_call`` hook and say so when a layer is missing."""
    if not host_supports_toolset_override():
        logger.warning(
            "clawchat host tools: this Hermes has no per-source toolset override "
            "(BasePlatformAdapter.toolsets_for_source, 0.20.1+); turns your owner did not "
            "start keep the host tools in their schema and rely on the pre_tool_call block"
        )
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        logger.warning(
            "clawchat host tools: this Hermes cannot register plugin hooks; host tools in "
            "turns your owner did not start are not guarded by the plugin"
        )
        return
    register_hook("pre_tool_call", clawchat_pre_tool_call)


__all__ = [
    "BLOCK_MESSAGE",
    "FALLBACK_TOOLSETS",
    "RESTRICTED_TOOLSETS",
    "SETTING_KEY",
    "clawchat_pre_tool_call",
    "non_owner_host_tools",
    "register_host_tools_guard",
    "is_restricted_tool",
    "restricted_tool_names",
    "toolsets_for_source",
]
