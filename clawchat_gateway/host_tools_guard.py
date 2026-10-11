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

The same holds for two more doors: the MCP servers the owner configured for
ClawChat (a filesystem or database server reads the same files), and Hermes'
own ``memory`` tool, whose ``MEMORY.md`` / ``USER.md`` go into the system
prompt of every later conversation — the owner's direct chat included — so a
line someone else got written there would read like the agent's own note.

This module narrows those turns in two layers:

* :func:`toolsets_for_source` — the adapter's per-source toolset override
  (``BasePlatformAdapter.toolsets_for_source``, host 0.20.1+). Every turn but
  the owner's own direct chat loses :data:`RESTRICTED_TOOLSETS` (``memory``
  among them) and gets the host's ``no_mcp`` sentinel, so the model never sees
  those tools. Listing no MCP server would not do: the host then adds back
  every enabled one, the fallback list included. **Every group turn counts**, whoever spoke: a group is
  one shared session, and switching the toolset per speaker would rebuild the
  agent (and drop the prompt cache) each time the owner and someone else
  alternate.
* :func:`clawchat_pre_tool_call` — a ``pre_tool_call`` hook that blocks any of
  those tools still reaching dispatch in such a turn (an older host without the
  override, a toolset the filter could not see into). MCP tools are known by
  their host toolset (``mcp-<server>``) or, failing that, their ``mcp_`` name
  prefix (``mcp_<server>_<tool>`` before 0.18.1, ``mcp__<server>__<tool>``
  since). ``memory`` has only write actions — reading it is the system-prompt
  injection, which stays — so the whole tool goes. With
  ``non-owner-host-tools: approve`` the owner opts in instead: the tools stay
  visible and every call is escalated to the human-approval gate, which smart
  never answers — a friend's direct chat refuses it on the spot, a group's goes
  to the owner (``adapter.send_exec_approval``).

The host swallows exceptions from both places and then *opens up*: an override
that raises falls back to the full platform toolset, and a hook that raises is
ignored. Both therefore fail closed here.

Turn ownership comes from the same host session context the memory tools use
(``memory_scope``): platform, chat id, chat type and user id. Hermes before
0.19.1 binds no chat type; there the owner's activation chat id decides.

Every host that can load the plugin (0.12.0+) has the hook; on one without the
override the tools stay in the schema and the hook blocks them, and the
adapter adds a line to such turns telling the model so
(:func:`blocked_tools_hint`). Should a host ever load the plugin without
``register_hook``, nothing would stand between those turns and the tools, so
the adapter does not take them at all (:func:`refuses_non_owner_turns`).
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
    "memory",
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
        "memory",
    }
)

#: Hermes' sentinel in a platform toolset list: no MCP server at all, even one
#: listed by name (``hermes_cli.tools_config._merge_mcp_servers``, 0.12.0+).
NO_MCP = "no_mcp"
_MCP_TOOLSET_PREFIX = "mcp-"
_MCP_TOOL_PREFIX = "mcp_"

#: What a narrowed turn falls back to when the platform toolset cannot be
#: worked out, or nothing survives the filter: the plugin's own ClawChat tools,
#: and no MCP (without the sentinel the host adds every MCP server back).
#: (An empty override would be read by the host as "no override".)
FALLBACK_TOOLSETS: tuple[str, ...] = ("clawchat", NO_MCP)

SETTING_KEY = "non-owner-host-tools"
SETTING_OFF = "off"
SETTING_APPROVE = "approve"

_DIRECT_CHAT_TYPES = frozenset({"dm", "direct"})
_PLATFORM = "clawchat"

BLOCK_MESSAGE = (
    "This tool is not available here: only your owner's direct chat may use the "
    "terminal, files, code execution, the browser, sub-agents, scheduled jobs, MCP tools "
    "or Hermes' memory tool on this machine. "
    "Do not try to reach them another way; answer without them, or tell the person "
    "to ask your owner."
)
#: For a turn where the tools are in the schema but every call is blocked (a
#: host without the override, or ``approve`` on a host that cannot escalate).
BLOCKED_TOOLS_HINT = (
    "Host tools in this turn: your owner did not start it, so the terminal, files, code "
    "execution, the browser, sub-agents, scheduled jobs, MCP tools and Hermes' memory tool "
    "are blocked even though you can see them. Do not call them; use the ClawChat tools "
    "or answer without them."
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


def _owner_direct_chat_id() -> str:
    """The owner's own direct chat (the activation conversation), or "".

    Raises on a store failure; callers treat that as "not the owner's chat".
    """
    from clawchat_gateway.memory_scope import _owner_direct_chat_id as read_owner_chat

    return read_owner_chat()


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


def _mcp_server_names() -> set[str]:
    """The MCP servers in the host config (``mcp_servers.<name>``)."""
    servers = _host_config().get("mcp_servers") or {}
    return {str(name) for name in servers} if isinstance(servers, dict) else set()


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


def _registry_toolset(name: str) -> str:
    from tools.registry import registry

    return str(registry.get_toolset_for_tool(name) or "")


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


def is_mcp_tool(name: str) -> bool:
    """A tool an MCP server registered: its host toolset, else its name prefix."""
    name = str(name or "")
    try:
        if _registry_toolset(name).startswith(_MCP_TOOLSET_PREFIX):
            return True
    except Exception:  # noqa: BLE001 - registry not importable: name only
        pass
    return name.startswith(_MCP_TOOL_PREFIX)


def is_restricted_tool(name: str) -> bool:
    name = str(name or "")
    return (
        name in restricted_tool_names()
        or name.startswith(_RESTRICTED_TOOL_PREFIXES)
        or is_mcp_tool(name)
    )


def _is_owner_direct(
    chat_id: str,
    chat_type: str,
    user_id: str,
    owner: str,
    owner_chat_lookup: Optional[Callable[[], str]] = None,
) -> bool:
    """A turn in the owner's own direct chat: the owner spoke, or the plugin did.

    The owner's direct chat holds only the owner and the agent, so a turn the
    plugin starts there (permission receipts, the memory-migration hint,
    moment-comment and awareness notes — sender "ClawChat") is the owner's
    turn too. A group never qualifies, whoever spoke; any lookup failure means
    "not the owner's".

    Hermes before 0.19.1 binds no chat type into the session context; with
    none, the chat id alone decides (:func:`_is_owner_chat_without_type`).
    """
    chat_type = (chat_type or "").strip().lower()
    if not chat_type:
        return _is_owner_chat_without_type(chat_id, owner, owner_chat_lookup)
    if chat_type not in _DIRECT_CHAT_TYPES:
        return False
    if _is_known_group(chat_id):
        return False
    if owner and user_id and user_id == owner:
        return True
    if not owner or not chat_id:
        return False
    try:
        owner_chat = (owner_chat_lookup or _owner_direct_chat_id)()
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("clawchat host tools: owner chat lookup failed (%s); treating the turn as not the owner's", exc)
        return False
    return bool(owner_chat) and chat_id == owner_chat


def _is_owner_chat_without_type(
    chat_id: str,
    owner: str,
    owner_chat_lookup: Optional[Callable[[], str]] = None,
) -> bool:
    """No chat type (host < 0.19.1): the owner's activation chat, unless a known group.

    Ids compare case-insensitively. No chat id, an unknown owner or owner chat,
    or any lookup failure means "not the owner's".
    """
    if not chat_id or not owner:
        return False
    try:
        from clawchat_gateway.memory_scope import same_chat_id

        owner_chat = (owner_chat_lookup or _owner_direct_chat_id)()
        return same_chat_id(chat_id, str(owner_chat or "")) and not _is_known_group(chat_id)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning(
            "clawchat host tools: owner chat check without a chat type failed (%s); treating the turn as not the owner's",
            exc,
        )
        return False


def _narrowed_toolsets() -> list[str]:
    restricted_tools = restricted_tool_names()
    try:
        mcp_servers = _mcp_server_names()
    except Exception:  # noqa: BLE001 - NO_MCP still drops them host-side
        mcp_servers = set()
    kept = []
    for name in sorted(_platform_toolsets()):
        if (
            name in RESTRICTED_TOOLSETS
            or name == NO_MCP
            or name in mcp_servers
            or name.startswith(_MCP_TOOLSET_PREFIX)
        ):
            continue
        try:
            tools = set(_resolve_tools(name))
        except Exception:  # noqa: BLE001 - cannot see inside: drop it
            continue
        if tools & restricted_tools or any(
            t.startswith(_RESTRICTED_TOOL_PREFIXES) or t.startswith(_MCP_TOOL_PREFIX) for t in tools
        ):
            continue
        kept.append(name)
    if not kept:
        return list(FALLBACK_TOOLSETS)
    return kept + [NO_MCP]


def toolsets_for_source(
    source: Any,
    *,
    owner_user_id: str | Callable[[], str],
    owner_direct_chat_id: Optional[Callable[[], str]] = None,
) -> Optional[list[str]]:
    """The toolset override for a ClawChat turn, or ``None`` to keep the platform's.

    ``None`` for the owner's own direct chat, and for every turn when the owner
    chose ``non-owner-host-tools: approve`` (the hook escalates instead).
    Otherwise the platform toolset minus :data:`RESTRICTED_TOOLSETS` and every
    MCP server, plus :data:`NO_MCP` — never empty, and on any failure
    :data:`FALLBACK_TOOLSETS`.
    """
    try:
        owner = owner_user_id() if callable(owner_user_id) else owner_user_id
        chat_id = str(getattr(source, "chat_id", "") or "")
        chat_type = str(getattr(source, "chat_type", "") or "")
        user_id = str(getattr(source, "user_id", "") or "")
        if _is_owner_direct(chat_id, chat_type, user_id, str(owner or ""), owner_direct_chat_id):
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


def is_owner_turn(
    chat_id: str,
    chat_type: str,
    user_id: str,
    *,
    owner_user_id: str | Callable[[], str],
    owner_direct_chat_id: Optional[Callable[[], str]] = None,
) -> bool:
    """The owner's own direct chat (see :func:`_is_owner_direct`); False on any failure."""
    try:
        owner = owner_user_id() if callable(owner_user_id) else owner_user_id
        return _is_owner_direct(
            str(chat_id or ""), str(chat_type or ""), str(user_id or ""), str(owner or ""), owner_direct_chat_id
        )
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("clawchat host tools: turn owner check failed (%s); treating the turn as not the owner's", exc)
        return False


def blocked_tools_hint() -> Optional[str]:
    """:data:`BLOCKED_TOOLS_HINT` when a turn the owner did not start sees tools it cannot call.

    That is a host without the per-source override (the schema keeps the tools,
    the hook blocks them), or ``approve`` on a host that cannot escalate (the
    override keeps the tools for the human gate, the hook blocks instead).
    ``approve`` on a host that escalates asks the owner; no hint then.
    """
    try:
        approve = non_owner_host_tools() == SETTING_APPROVE
        if approve and _host_supports_approve():
            return None
        if approve or not host_supports_toolset_override():
            return BLOCKED_TOOLS_HINT
        return None
    except Exception:  # noqa: BLE001 - a hint only
        return BLOCKED_TOOLS_HINT


#: Set by :func:`register_host_tools_guard` when the host could not take the
#: ``pre_tool_call`` hook. ``None`` until the guard was registered.
_hook_missing: Optional[bool] = None


def refuses_non_owner_turns() -> bool:
    """Whether the adapter must not take turns the owner did not start.

    Only when the plugin loaded on a host without ``register_hook``: there no
    layer could keep the host tools, MCP or Hermes' memory out of such a turn.
    """
    return _hook_missing is True


def register_host_tools_guard(ctx: Any) -> None:
    """Register the ``pre_tool_call`` hook and say so when a layer is missing."""
    global _hook_missing
    if not host_supports_toolset_override():
        logger.warning(
            "clawchat host tools: this Hermes has no per-source toolset override "
            "(BasePlatformAdapter.toolsets_for_source, 0.20.1+); turns your owner did not "
            "start keep the host tools, MCP tools and the memory tool in their schema and "
            "rely on the pre_tool_call block"
        )
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        _hook_missing = True
        logger.warning(
            "clawchat host tools: this Hermes cannot register plugin hooks, so nothing could "
            "keep the host tools out of turns your owner did not start; ClawChat will not "
            "answer friends' direct chats or groups on this host. Upgrade Hermes."
        )
        return
    try:
        register_hook("pre_tool_call", clawchat_pre_tool_call)
    except Exception:
        _hook_missing = True
        logger.warning(
            "clawchat host tools: the pre_tool_call hook was not registered; ClawChat will "
            "not answer friends' direct chats or groups on this host"
        )
        raise
    _hook_missing = False


__all__ = [
    "BLOCK_MESSAGE",
    "BLOCKED_TOOLS_HINT",
    "FALLBACK_TOOLSETS",
    "NO_MCP",
    "RESTRICTED_TOOLSETS",
    "SETTING_KEY",
    "blocked_tools_hint",
    "clawchat_pre_tool_call",
    "is_mcp_tool",
    "is_owner_turn",
    "non_owner_host_tools",
    "refuses_non_owner_turns",
    "register_host_tools_guard",
    "is_restricted_tool",
    "restricted_tool_names",
    "toolsets_for_source",
]
