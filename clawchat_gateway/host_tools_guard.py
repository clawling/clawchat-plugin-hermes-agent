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

Two more read everything the owner has: ``session_search`` reads every
conversation in Hermes' session store (the owner's direct chat, other friends,
other groups, other platforms — and other profiles' stores) with no way to
limit it to the current chat, and ``connections`` (``manage_connections``,
Hermes 0.21.3+) both installs or enables MCP servers in the owner's config and
opens the owner's connected hosted accounts (``connectors__<app>__<tool>``:
mail, calendars, issue trackers) to the turn.

Kanban tasks run in a separate process with the assignee profile's full tools
and without the initiating turn's session identity. The hook cannot identify
that caller in the worker, so every ``kanban_*`` tool is restricted at dispatch
in the initiating turn.

This module narrows those turns in two layers. Per-person grants can add
individual tools back: the adapter holds new input while a granted turn is
busy, and a nonce snapshot must match the first pre_llm_call session/turn
binding. No session-level fallback supplies execution authority.

* :func:`toolsets_for_source` — the adapter's per-source toolset override
  (``BasePlatformAdapter.toolsets_for_source``, host 0.20.1+). Every turn but
  the owner's own direct chat loses ungranted :data:`RESTRICTED_TOOLSETS`
  (``memory`` among them) by default and gets the host's ``no_mcp`` sentinel,
  so the model does not see those tools. Listing no MCP server would not do: the host then adds back
  every enabled one, the fallback list included. Group authority comes only
  from the trusted batch snapshot: a nonempty owner-only batch has full
  ordinary tools; mixed batches intersect their non-owner speakers' grants.
  Changing the toolset can rebuild the agent and its prompt cache.
* :func:`clawchat_pre_tool_call` — a ``pre_tool_call`` hook that blocks any of
  those tools still reaching dispatch in such a turn (an older host without the
  override, a toolset the filter could not see into). MCP tools are known by
  their host toolset (``mcp-<server>``) or, failing that, their ``mcp_`` name
  prefix (``mcp_<server>_<tool>`` before 0.18.1, ``mcp__<server>__<tool>``
  since). ``memory`` has only write actions — reading it is the system-prompt
  injection, which stays — so the whole tool goes. With
  ``non-owner-host-tools: approve`` the owner opts in instead: the tools stay
  visible and ungranted restricted calls are escalated to the human-approval
  gate, which smart never answers — a friend's direct chat refuses those calls
  on the spot, a group's goes to the owner (``adapter.send_exec_approval``).
  Effective direct grants execute directly; ask grants route each call,
  including otherwise open tools, to the owner's direct chat. Grant management
  has its own toolset, hidden from non-owner turns regardless of this setting.

  The hook also limits a few tools such turns keep, by what a call asks for
  (:func:`_limited_call`): ``skill_manage`` (a skill it writes is in the
  owner's direct chat from then on, scripts included); ``skill_view`` of
  anything but ClawChat's own skills when the owner turned on
  ``skills.inline_shell`` (the host then runs a skill's ``!`cmd``` snippets);
  ``vision_analyze`` / ``video_analyze`` / ``image_generate`` on a local path
  outside the host's media caches and the plugin's inbound attachments (they
  read any image on the machine); ``text_to_speech`` with an ``output_path``
  (it writes anywhere). And the tools of an external memory provider
  (``memory.provider``: honcho, mem0, ...) — the host only gates them with the
  ``memory`` toolset since 0.18.1, and only the override (0.20.1+) drops that
  toolset. Their names come from the provider itself; when it cannot be
  loaded, a turn the override does not narrow gets no tool the host registry
  does not know (:func:`_strict_unregistered`).

The host swallows exceptions from both places and then *opens up*: an override
that raises falls back to the full platform toolset, and a hook that raises is
ignored. Both therefore fail closed here.

Turn ownership comes from the same host session context the memory tools use
(``memory_scope``): platform, chat id, chat type and user id. Hermes before
0.19.1 binds no chat type; there the owner's activation chat id decides.

Every host that can load the plugin (0.12.0+) has the hook; on one without the
override the tools stay in the schema and the hook blocks them, and the
adapter adds a line to such turns telling the model so
(:func:`blocked_tools_hint`); the limits above get a line of their own
(:func:`limited_tools_hint`). Should a host ever load the plugin without
``register_hook``, nothing would stand between those turns and the tools, so
the adapter does not take them at all (:func:`refuses_non_owner_turns`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

GRANT_MANAGEMENT_TOOLSET = "clawchat-grants"
GRANT_REQUEST_TOOLSET = "clawchat-requests"
_GRANT_APPROVAL_GATE_LOCK = threading.Lock()
_GRANT_CATALOG_LOCK = threading.Lock()
_GUEST_SEARCH_ENTRY_POINTS = ("connector_entries_by_group", "remote_schemas_for", "_registry_entry")
_UNNARROWED_CATALOG = ContextVar("clawchat_unnarrowed_catalog", default=False)

#: Host toolsets (Hermes 0.21 ``toolsets.py`` names) removed by default from
#: turns the owner did not start: shell + process management, file read/write/patch/search,
#: Python execution, sub-agents (which inherit tools), scheduled jobs, kanban tasks
#: (separate workers lose the initiating turn's identity and use full profile tools),
#: desktop control, browser automation (which opens ``file://`` URLs and
#: runs page scripts / raw CDP, i.e. reads local files), Hermes' memory tool,
#: the search over every past conversation, and the owner's connected accounts.
RESTRICTED_TOOLSETS: tuple[str, ...] = (
    "terminal",
    "file",
    "code_execution",
    "delegation",
    "cronjob",
    "kanban",
    "computer_use",
    "browser",
    "memory",
    "session_search",
    "connections",
)

#: Every host browser tool is ``browser_*``, every kanban tool ``kanban_*``,
#: and every hosted connector tool
#: ``connectors__<app>__<tool>`` (reached through ``tool_search`` /
#: ``tool_call``, which hand the hook the underlying name); a future one is
#: covered too.
_RESTRICTED_TOOL_PREFIXES: tuple[str, ...] = ("browser_", "kanban_", "connectors__")

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
        "kanban_show",
        "kanban_list",
        "kanban_complete",
        "kanban_block",
        "kanban_schedule",
        "kanban_request_review",
        "kanban_request_changes",
        "kanban_heartbeat",
        "kanban_comment",
        "kanban_create",
        "kanban_link",
        "kanban_unblock",
        "kanban_attach",
        "kanban_attach_url",
        "kanban_attachments",
        # Bot Mode's message_agent is injected per session, outside any toolset;
        # the receiving agent runs with its own full tools and no session identity.
        "message_agent",
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
        "session_search",
        "manage_connections",
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
    "terminal, files, code execution, the browser, sub-agents, scheduled jobs, kanban tools, MCP tools, "
    "Hermes' memory tool, the search over past conversations or the owner's connected "
    "accounts on this machine. "
    "Do not try to reach them another way; answer without them, or tell the person "
    "to ask your owner."
)
#: For a turn where the tools are in the schema but every call is blocked (a
#: host without the override, or ``approve`` on a host that cannot escalate).
BLOCKED_TOOLS_HINT = (
    "Host tools in this turn: your owner did not start it, so the terminal, files, code "
    "execution, the browser, sub-agents, scheduled jobs, kanban tools, MCP tools, Hermes' memory tool, "
    "session_search and manage_connections are blocked even though you can see them. "
    "Do not call them; use the ClawChat tools or answer without them."
)
#: For every turn the owner did not start where the hook limits these calls.
LIMITED_TOOLS_HINT = (
    "Also limited in this turn: skill_manage is blocked; vision_analyze, video_analyze and "
    "image_generate take images from the web or this conversation's attachments only, no "
    "other file on this machine; text_to_speech takes no output_path."
)
#: Added to :data:`LIMITED_TOOLS_HINT` when the owner turned on inline shell in skills.
LIMITED_SKILL_VIEW_HINT = "skill_view opens only ClawChat's own skills (clawchat:<name>)."
LIMITED_BLOCK_MESSAGE = (
    "This call is not available here: in a turn your owner did not start, skill_manage is "
    "blocked, skill_view may open only ClawChat's own skills while inline shell is on, "
    "images must come from the web or this conversation's attachments, and text_to_speech "
    "takes no output_path. Do not try another way; answer without it, or tell the person "
    "to ask your owner."
)
APPROVE_MESSAGE = (
    "Someone other than the owner started this turn (a friend's direct chat or a "
    "group); host tools need the owner's approval for every call."
)


#: A call the hook judges by its arguments: tool -> the arguments naming a
#: local or remote media source (one value or a list).
_MEDIA_SOURCE_ARGS: dict[str, tuple[str, ...]] = {
    "vision_analyze": ("image_url",),
    "video_analyze": ("video_url",),
    "image_generate": ("image_url", "reference_image_urls"),
}
_SKILL_WRITE_TOOL = "skill_manage"
_SKILL_VIEW_TOOL = "skill_view"
_TTS_TOOL = "text_to_speech"
_PLUGIN_SKILL_PREFIX = "clawchat:"
_REMOTE_MEDIA_PREFIXES = ("http://", "https://", "data:")
_FILE_URI = "file://"
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
#: The host's media cache roots under ``$HERMES_HOME`` (0.21
#: ``tools.image_source._MEDIA_CACHE_SUBDIRS`` and the gateway's caches), used
#: when the host's own list is not importable.
_STATIC_MEDIA_CACHE_SUBDIRS = (
    "cache",
    "images",
    "image_cache",
    "audio_cache",
    "video_cache",
    "document_cache",
    "temp_vision_images",
    "temp_video_files",
)
#: ``memory.provider`` values that mean Hermes' built-in store (no provider tools).
_CORE_MEMORY_PROVIDERS = frozenset({"", "default", "builtin", "built-in", "none"})
_DEFAULT_MEDIA_DOWNLOAD_DIR = "/tmp/clawchat-media"


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


def _registry_knows(name: str) -> bool:
    from tools.registry import registry

    return bool(registry.get_toolset_for_tool(name))


def host_tool_catalog() -> tuple[frozenset[str], frozenset[str]]:
    """Registered names and the host's effective, uncollapsed ClawChat catalog.

    Always start from platform settings, never our per-source override or the
    process-global last-resolved list (which may belong to a narrowed turn).
    Host schema construction applies dependency/credential and platform checks.
    """
    import inspect
    import model_tools
    from tools.registry import registry

    config = _host_config()
    platform = sorted(_platform_toolsets())
    disabled = (config.get("agent") or {}).get("disabled_toolsets") or []
    known = frozenset(registry.get_all_tool_names())
    token = _UNNARROWED_CATALOG.set(True)
    previous = getattr(model_tools, "_last_resolved_tool_names", None)
    try:
        if "skip_tool_search_assembly" in inspect.signature(model_tools.get_tool_definitions).parameters:
            definitions = model_tools.get_tool_definitions(
                enabled_toolsets=platform, disabled_toolsets=disabled, quiet_mode=True,
                skip_tool_search_assembly=True)
        else:
            # Older hosts have no progressive disclosure; their ordinary catalog is complete.
            definitions = model_tools.get_tool_definitions(
                enabled_toolsets=platform, disabled_toolsets=disabled, quiet_mode=True)
        available = frozenset(item["function"]["name"] for item in definitions)
    finally:
        _UNNARROWED_CATALOG.reset(token)
        if previous is not None:
            model_tools._last_resolved_tool_names = previous
    return known, available


def _filter_grant_definitions(definitions, snapshot):
    from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS
    from clawchat_gateway.tool_grants import tool_grant_visible

    def allowed(tool):
        if tool == "clawchat_tool_request":
            return _tool_grants_enabled() and not (snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools))
        if tool in AUTHORIZATION_TOOLS:
            return bool(snapshot and snapshot.owner_side and snapshot.owner_verified and tool != "clawchat_tool_request")
        if snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools):
            return True
        default = not _guarded_call(tool, {})
        if not _host_supports_pre_llm_call():
            # A nonce can exist on an old host without an execution binding.
            # Its explicit grants cannot replace the legacy approval policy.
            return default or non_owner_host_tools() == SETTING_APPROVE
        if snapshot is None:
            return default
        return tool_grant_visible(snapshot.grants, tool, default_allowed=default, now=time.time() * 1000)
    return [item for item in definitions if allowed(item["function"]["name"])]


def _guest_catalog_turn():
    """Only a matching owner snapshot can bypass ClawChat catalog isolation."""
    try:
        from clawchat_gateway.turn_grants import get_snapshot, session_context_value

        if session_context_value("HERMES_SESSION_PLATFORM") != "clawchat":
            return False
        snapshot = get_snapshot(session_context_value("HERMES_SESSION_MESSAGE_ID"))
        return not (snapshot and snapshot.chat_id == session_context_value("HERMES_SESSION_CHAT_ID")
                    and (snapshot.owner_side or snapshot.legacy_owner_tools))
    except Exception:  # noqa: BLE001 - uncertain authority must not expose extra catalog entries
        return True


def _guest_search_catalog_module():
    try:
        import tools.tool_search_catalog as catalog
        return catalog
    except Exception:  # noqa: BLE001 - hosts without the module have no source summaries
        return None


def _install_guest_search_guard() -> bool:
    """Neutralize host search paths that can bypass the filtered guest catalog."""
    try:
        import tools.tool_search as ts

        def wrap_entry(original, name):
            @wraps(original)
            def entry(*args, **kwargs):
                if not _guest_catalog_turn():
                    return original(*args, **kwargs)
                if name == "connector_entries_by_group":
                    queries = args[0] if args else kwargs.get("queries", ())
                    return ([[] for _ in queries], None)
                if name == "remote_schemas_for":
                    return ({}, None)
                if name == "hidden_declared_sources":
                    return []  # Declared but unavailable sources are scanned from the whole registry.
                return None  # No global registry fallback for guest descriptions.
            entry._clawchat_guest = True
            return entry

        with _GRANT_CATALOG_LOCK:
            for name in _GUEST_SEARCH_ENTRY_POINTS:
                if not hasattr(ts, name):
                    continue
                original = getattr(ts, name)
                if not callable(original):
                    return False
                if not getattr(original, "_clawchat_guest", False):
                    setattr(ts, name, wrap_entry(original, name))
            catalog = _guest_search_catalog_module()
            if catalog is not None and hasattr(catalog, "hidden_declared_sources"):
                original = catalog.hidden_declared_sources
                if not callable(original):
                    return False
                if not getattr(original, "_clawchat_guest", False):
                    setattr(catalog, "hidden_declared_sources", wrap_entry(original, "hidden_declared_sources"))
        return True
    except Exception:  # noqa: BLE001 - unknown hosts cannot safely open search bridges
        return False


def _guest_search_guard_complete() -> bool:
    """Whether every known host path that bypasses the filtered catalog is neutralized."""
    try:
        import tools.tool_search as ts

        if any(not getattr(getattr(ts, name), "_clawchat_guest", False)
               for name in _GUEST_SEARCH_ENTRY_POINTS if hasattr(ts, name)):
            return False
        catalog = _guest_search_catalog_module()
        if (catalog is not None and hasattr(catalog, "hidden_declared_sources")
                and not getattr(catalog.hidden_declared_sources, "_clawchat_guest", False)):
            return False
        # Connector hosts must expose both known remote entry points so the
        # guard can neutralize them even if a deny expires before dispatch.
        return not ("manage_connections" in host_tool_catalog()[0]
                    and any(not hasattr(ts, name) for name in _GUEST_SEARCH_ENTRY_POINTS[:2]))
    except Exception:  # noqa: BLE001 - unverifiable guards count as missing
        return False


def _grant_search_bridge_allowed(tool_name, snapshot):
    """Catalog reads are safe only through the filter for this bound authority."""
    try:
        import model_tools
        import tools.tool_search as ts
        from clawchat_gateway.turn_grants import get_snapshot, session_context_value

        if (tool_name == "tool_call" or tool_name not in ts.BRIDGE_TOOL_NAMES
                or not getattr(model_tools.get_tool_definitions, "_clawchat_grants", False)):
            return False
        if not _guest_search_guard_complete():
            return False
        current = get_snapshot(session_context_value("HERMES_SESSION_MESSAGE_ID"))
        if current is None or current.chat_id != session_context_value("HERMES_SESSION_CHAT_ID"):
            return False
        # Compare complete compiled rules: Mapping equality sees only finite
        # candidates and can miss a different all/future-tool policy.
        return current is snapshot or (
            current.non_owner_speakers == snapshot.non_owner_speakers
            and current.grants.plans == snapshot.grants.plans
            and current.grants.owner_only == snapshot.grants.owner_only)
    except Exception:  # noqa: BLE001 - old hosts and unreadable catalogs stay closed
        return False


def _grant_tool_describe_block(args, snapshot):
    """Prevent the host's registry fallback from describing hidden tools."""
    try:
        from tools.tool_search import BRIDGE_TOOL_NAMES
        from clawchat_gateway.tool_grants import grant_failure_code

        raw = args.get("names") if isinstance(args, dict) else None
        raw = [raw] if isinstance(raw, str) else raw
        if not isinstance(raw, list):
            return None  # Let the host return its argument error.
        # Match the host's string coercion, trimming and deduplication. Check
        # every name whatever the host's per-call limit is.
        names = list(dict.fromkeys(text for item in raw if (text := str(item or "").strip())))
        known, available = host_tool_catalog()
        for name in names:
            if name not in known or name in BRIDGE_TOOL_NAMES:
                continue
            if name not in available or not _filter_grant_definitions([{"function": {"name": name}}], snapshot):
                guarded = _guarded_call(name, {})
                strict = not guarded and _strict_unregistered(name)
                context = {"chat_id": snapshot.chat_id, "default_allowed": not guarded and not strict}
                reason = grant_failure_code(snapshot.grants, name, available=name in available,
                    args={}, context=context, now=time.time() * 1000, speakers=snapshot.non_owner_speakers)
                return _grant_block(reason or "not-available")
        return None
    except Exception:  # noqa: BLE001 - a failed check must not reach the host registry
        return _grant_block("not-available")


def _install_grant_catalog_filter() -> bool:
    """Filter after host caching and before search assembly, using this turn's rules.

    The host caches catalogs by toolsets, which cannot distinguish exact grants
    within the same toolset. Never cache the per-turn filtered result there.
    """
    try:
        import inspect
        import model_tools
        from clawchat_gateway.turn_grants import get_snapshot, session_context_value

        with _GRANT_CATALOG_LOCK:
            original = model_tools.get_tool_definitions
            if getattr(original, "_clawchat_grants", False):
                return True
            signature = inspect.signature(original)
            modern = "skip_tool_search_assembly" in signature.parameters

            @wraps(original)
            def definitions(*args, **kwargs):
                if (_UNNARROWED_CATALOG.get()
                        or session_context_value("HERMES_SESSION_PLATFORM") != "clawchat"):
                    return original(*args, **kwargs)
                try:
                    if not _tool_grants_enabled():
                        return original(*args, **kwargs)
                except Exception:  # noqa: BLE001 - unavailable policy cannot expose a broad catalog
                    return []
                snapshot = get_snapshot(session_context_value("HERMES_SESSION_MESSAGE_ID"))
                if snapshot and snapshot.chat_id != session_context_value("HERMES_SESSION_CHAT_ID"):
                    snapshot = None
                bound = signature.bind(*args, **kwargs)
                skip_assembly = bound.arguments.get("skip_tool_search_assembly", False)
                if modern:
                    bound.arguments["skip_tool_search_assembly"] = True
                raw = original(*bound.args, **bound.kwargs)
                try:
                    filtered = _filter_grant_definitions(raw, snapshot)
                except Exception:  # noqa: BLE001 - filter errors never restore the full toolset
                    return []
                # Assembly writes source summaries into the tool_search schema
                # itself; a later bridge refusal cannot take them back.
                if modern and not skip_assembly and (not _guest_catalog_turn() or _guest_search_guard_complete()):
                    try:
                        from tools.tool_search import assemble_tool_defs, load_config
                        config = load_config()
                        if config.enabled != "off":
                            filtered = assemble_tool_defs(
                                filtered, context_length=model_tools._resolve_active_context_length(),
                                config=config).tool_defs
                    except Exception:  # noqa: BLE001 - ordinary filtered schemas are sufficient
                        pass
                model_tools._last_resolved_tool_names = [item["function"]["name"] for item in filtered]
                return filtered
            definitions._clawchat_grants = True
            model_tools.get_tool_definitions = definitions
            return True
    except Exception:  # noqa: BLE001 - old hosts retain toolset narrowing plus execution checks
        return False


def public_tool_names(available):
    from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS
    return frozenset(tool for tool in available if tool not in AUTHORIZATION_TOOLS and not _guarded_call(tool, {}))


def can_ask_each_time(rule_key: str = "clawchat-grant-ask:probe") -> bool:
    """Check real per-call routing at card creation and confirmation as well as execution."""
    try:
        from clawchat_gateway.tools import get_clawchat_sender
        sender = get_clawchat_sender()
        return bool(_host_supports_approve() and not _approval_bypass(rule_key)
                    and sender and sender._owner_direct_chat_id() and _install_grant_approval_gate())
    except Exception:  # noqa: BLE001 - uncertainty removes ask, never direct
        return False


def ask_grants_available() -> bool:
    """Compatibility entry point for cards and pending confirmations."""
    return can_ask_each_time()


def _skills_inline_shell() -> bool:
    """Whether the host runs a skill's ``!`cmd``` snippets (``skills.inline_shell``).

    Read like the host does (any truthy value); unreadable counts as on.
    """
    try:
        skills = _host_config().get("skills") or {}
        return bool(skills.get("inline_shell", False)) if isinstance(skills, dict) else False
    except Exception:  # noqa: BLE001 - fail closed
        return True


def _plugin_skill_ids() -> set[str]:
    """ClawChat's own skills: bundled with the plugin, or delivered by a skill update."""
    from clawchat_gateway.skill_update import bundled_skill_ids, read_local_manifest

    ids = set(bundled_skill_ids())
    try:
        ids.update(read_local_manifest())
    except Exception:  # noqa: BLE001 - the bundled ones still count
        pass
    return ids


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - older host layout
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _media_roots() -> list[Path]:
    """Where a turn the owner did not start may read media from: the host's
    media caches and the plugin's inbound attachment directory."""
    roots: list[Path] = []
    try:
        from tools.image_source import _media_cache_roots

        roots.extend(Path(root) for root in _media_cache_roots())
    except Exception:  # noqa: BLE001 - older host: the static list below
        pass
    home = _hermes_home()
    roots.extend(home / sub for sub in _STATIC_MEDIA_CACHE_SUBDIRS)
    try:
        download_dir = (_config_extra() or {}).get("media_download_dir") or _DEFAULT_MEDIA_DOWNLOAD_DIR
    except Exception:  # noqa: BLE001
        download_dir = _DEFAULT_MEDIA_DOWNLOAD_DIR
    roots.append(Path(os.path.expanduser(str(download_dir))))
    return roots


def _load_memory_provider_tools(name: str) -> frozenset[str]:
    """The tool names the host's memory provider *name* exposes; raises if unknown."""
    from plugins.memory import load_memory_provider

    provider = load_memory_provider(name)
    if provider is None:
        raise LookupError(f"memory provider {name!r} not found")
    names = set()
    for schema in provider.get_tool_schemas() or []:
        if not isinstance(schema, dict):
            continue
        fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
        if fn.get("name"):
            names.add(str(fn["name"]))
    if not names:
        raise LookupError(f"memory provider {name!r} listed no tools")
    return frozenset(names)


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
    from clawchat_gateway.tool_capabilities import canonical_tool_name

    name = canonical_tool_name(str(name or ""))
    return (
        name in restricted_tool_names()
        or name.startswith(_RESTRICTED_TOOL_PREFIXES)
        or is_mcp_tool(name)
    )


#: ``memory.provider`` -> its tool names, or ``None`` when they could not be
#: worked out. One lookup per process: the host picks the provider at start.
_provider_tools: dict[str, Optional[frozenset[str]]] = {}
_UNSET: Any = object()
_provider_tools_result: Any = _UNSET


def memory_provider_tools() -> Optional[frozenset[str]]:
    """The external memory provider's tool names; empty without one, ``None`` if unknown."""
    global _provider_tools_result
    if _provider_tools_result is _UNSET:
        result = _memory_provider_tools()
        if result is not _UNSET:
            _provider_tools_result = result
        return None if result is _UNSET else result
    return _provider_tools_result


def _memory_provider_tools() -> Any:
    """:func:`memory_provider_tools` uncached; ``_UNSET`` when the config was unreadable."""
    try:
        memory = _host_config().get("memory") or {}
        name = str((memory.get("provider") if isinstance(memory, dict) else "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - unknown; try again next call
        logger.warning("clawchat host tools: could not read memory.provider (%s)", exc)
        return _UNSET
    if name.lower() in _CORE_MEMORY_PROVIDERS:
        return frozenset()
    if name not in _provider_tools:
        try:
            _provider_tools[name] = _load_memory_provider_tools(name)
        except Exception as exc:  # noqa: BLE001 - unknown: the strict rule applies
            logger.warning(
                "clawchat host tools: could not list the tools of memory provider %r (%s); "
                "turns your owner did not start get no tool the host registry does not know "
                "where the toolset override does not narrow them",
                name,
                exc,
            )
            _provider_tools[name] = None
    return _provider_tools[name]


def _call_args(args: Any) -> dict[str, Any]:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _is_plugin_skill(name: Any) -> bool:
    name = str(name or "").strip()
    if name.startswith(_PLUGIN_SKILL_PREFIX):
        name = name[len(_PLUGIN_SKILL_PREFIX) :]
    return bool(name) and name in _plugin_skill_ids()


def _media_source_allowed(source: Any) -> bool:
    """A web / data: source, or a file inside :func:`_media_roots`. Anything else no."""
    if source is None:
        return True
    if not isinstance(source, str):
        return False
    text = source.strip()
    if not text:
        return True
    lower = text.lower()
    if lower.startswith(_REMOTE_MEDIA_PREFIXES):
        return True
    if lower.startswith(_FILE_URI):
        text = text[len(_FILE_URI) :]
    elif _SCHEME_RE.match(text) and not re.match(r"^[a-zA-Z]:[\\/]", text):
        return False
    path = Path(os.path.expanduser(text)).resolve()
    for root in _media_roots():
        try:
            if path.is_relative_to(root.resolve()):
                return True
        except Exception:  # noqa: BLE001 - an unresolvable root admits nothing
            continue
    return False


def _limited_call(tool_name: str, args: Any) -> bool:
    """A call of a tool such turns keep that reaches what is the owner's (see module doc)."""
    if tool_name == _SKILL_WRITE_TOOL:
        return True
    call = _call_args(args)
    if tool_name == _SKILL_VIEW_TOOL:
        return _skills_inline_shell() and not _is_plugin_skill(call.get("name"))
    if tool_name == _TTS_TOOL:
        return bool(str(call.get("output_path") or "").strip())
    if tool_name in _MEDIA_SOURCE_ARGS:
        for key in _MEDIA_SOURCE_ARGS[tool_name]:
            value = call.get(key)
            values = value if isinstance(value, (list, tuple)) else [value]
            if not all(_media_source_allowed(v) for v in values):
                return True
    return False


def _override_narrows() -> bool:
    """Whether layer 1 takes the restricted toolsets out of such turns."""
    return host_supports_toolset_override() and non_owner_host_tools() != SETTING_APPROVE


def _strict_unregistered(tool_name: str) -> bool:
    """A tool the host registry does not know, while the provider's tools are unknown.

    Only where layer 1 does not narrow the turn: there an external memory
    provider's tools may be in the schema, and without their names the hook
    cannot tell them from anything else the registry does not hold.
    """
    if _override_narrows():
        return False
    if memory_provider_tools() is not None:
        return False
    return not _registry_knows(tool_name)


def _guarded_call(tool_name: str, args: Any) -> bool:
    """Whether a call needs the turn check at all (cheap tests first)."""
    if is_restricted_tool(tool_name) or _limited_call(tool_name, args):
        return True
    provider = memory_provider_tools()
    if provider and tool_name in provider:
        return True
    return False


def _is_owner_direct(
    chat_id: str,
    chat_type: str,
    user_id: str,
    owner: str,
    owner_chat_lookup: Optional[Callable[[], str]] = None,
) -> bool:
    """Legacy session classification, subordinate to the bound turn snapshot.

    Synthetic events obtain authority only from the adapter's audited origin
    marker. An owner-chat location alone cannot authorize a synthetic turn.
    A group never qualifies; any lookup failure means "not the owner's".

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
    from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS

    restricted_tools = restricted_tool_names() | set(AUTHORIZATION_TOOLS)
    try:
        mcp_servers = _mcp_server_names()
    except Exception:  # noqa: BLE001 - NO_MCP still drops them host-side
        mcp_servers = set()
    kept = []
    for name in sorted(_platform_toolsets()):
        if (
            name in RESTRICTED_TOOLSETS
            or name == GRANT_MANAGEMENT_TOOLSET
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


def _legacy_approve_toolsets():
    """Keep the host's original approval catalog while hiding management tools."""
    from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS

    platform = _platform_toolsets()
    kept = {name for name in platform if name not in {GRANT_MANAGEMENT_TOOLSET, GRANT_REQUEST_TOOLSET}
            and not set(_resolve_tools(name)) & set(AUTHORIZATION_TOOLS)}
    return None if kept == platform else sorted(kept) or list(FALLBACK_TOOLSETS)


def toolsets_for_source(source: Any, *, owner_user_id, owner_direct_chat_id=None):
    from clawchat_gateway.turn_grants import get_snapshot
    result = _toolsets_for_source(source, owner_user_id=owner_user_id, owner_direct_chat_id=owner_direct_chat_id)
    try:
        snapshot = get_snapshot(str(getattr(source, "message_id", "") or ""))
        if snapshot and snapshot.chat_id != str(getattr(source, "chat_id", "") or ""):
            snapshot = None
        if _tool_grants_enabled() and result is not None and not (snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools)):
            return list(dict.fromkeys([GRANT_REQUEST_TOOLSET, *result]))
    except Exception:
        pass
    return result


def _toolsets_for_source(source: Any, *, owner_user_id: str | Callable[[], str],
                        owner_direct_chat_id: Optional[Callable[[], str]] = None) -> Optional[list[str]]:
    """Offer toolsets from the effective host catalog using the turn's frozen rules."""
    from clawchat_gateway.tool_grants import tool_grant_visible
    from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS
    from clawchat_gateway.turn_grants import get_snapshot
    narrowed = list(FALLBACK_TOOLSETS)
    try:
        owner = owner_user_id() if callable(owner_user_id) else owner_user_id
        chat_id = str(getattr(source, "chat_id", "") or "")
        snapshot = get_snapshot(str(getattr(source, "message_id", "") or ""))
        if snapshot is not None and snapshot.chat_id != chat_id:
            snapshot = None
        if snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools):
            if snapshot.owner_verified:
                return None
            return sorted(name for name in _platform_toolsets() if name not in {GRANT_MANAGEMENT_TOOLSET, GRANT_REQUEST_TOOLSET}
                          and not set(_resolve_tools(name)) & set(AUTHORIZATION_TOOLS)) or list(FALLBACK_TOOLSETS)
        if snapshot is None and not hasattr(source, "message_id") and _is_owner_direct(
                chat_id, str(getattr(source, "chat_type", "") or ""),
                str(getattr(source, "user_id", "") or ""), str(owner or ""), owner_direct_chat_id):
            return None
        narrowed = _narrowed_toolsets()
        if not _tool_grants_enabled():
            return _legacy_approve_toolsets() if non_owner_host_tools() == SETTING_APPROVE else narrowed
        if not _host_supports_pre_llm_call():
            return _legacy_approve_toolsets() if non_owner_host_tools() == SETTING_APPROVE else narrowed
        if snapshot is None:
            # Old sources have no grant nonce. A source that carries the field
            # but loses its snapshot must keep the fail-closed catalog.
            if not hasattr(source, "message_id") and non_owner_host_tools() == SETTING_APPROVE:
                return _legacy_approve_toolsets()
            return narrowed
        _, available = host_tool_catalog()
        public = public_tool_names(available)
        allowed = {tool for tool in available if tool_grant_visible(
            snapshot.grants, tool, default_allowed=tool in public, now=time.time() * 1000)}
        # A toolset can contain several tools. The execution hook still checks
        # each concrete member against the same rules, including explicit deny.
        servers = _mcp_server_names()
        kept = set()
        for name in _platform_toolsets():
            if name in {NO_MCP, GRANT_MANAGEMENT_TOOLSET}:
                continue
            tools = set(_resolve_tools(name))
            if tools & set(AUTHORIZATION_TOOLS):
                continue
            if tools & allowed:
                kept.add(name)
        for tool in allowed:
            toolset = _registry_toolset(tool)
            if not toolset or toolset == GRANT_MANAGEMENT_TOOLSET:
                continue
            if set(_resolve_tools(toolset)) & set(AUTHORIZATION_TOOLS):
                continue
            if toolset.startswith(_MCP_TOOLSET_PREFIX):
                server = toolset[len(_MCP_TOOLSET_PREFIX):]
                if server in servers:
                    kept.add(server)
            elif toolset in _platform_toolsets():
                kept.add(toolset)
        if not kept & servers:
            kept.add(NO_MCP)
        return sorted(kept) if kept - {NO_MCP} else narrowed
    except Exception as exc:  # noqa: BLE001 - host fallback would otherwise expose everything
        logger.warning("clawchat host tools: toolset override failed (%s); retaining restrictions", exc)
        return narrowed


def _session_turn() -> str:
    """``outside`` (not a ClawChat session), ``owner`` or ``other``."""
    from clawchat_gateway.memory_scope import SessionContextUnavailable

    try:
        platform = _session_value("HERMES_SESSION_PLATFORM").lower()
        if platform != _PLATFORM:
            return "outside"
        chat_id = _session_value("HERMES_SESSION_CHAT_ID")
        chat_type = _session_value("HERMES_SESSION_CHAT_TYPE")
        user_id = _session_value("HERMES_SESSION_USER_ID")
        if chat_id and _is_owner_direct(chat_id, chat_type, user_id, _owner_user_id()):
            return "owner"
        return "other"
    except SessionContextUnavailable:
        return "other"


_PATH_FIELDS = {
    "path", "filepath", "file_path", "images", "iconpath", "icon_path",
    "media", "media_files", "attachments", "avatar", "image",
}


def has_local_path(value: Any, path_field: bool = False, *, any_string: bool = False) -> bool:
    """Check attachment path fields, or all nested strings for a direct grant."""
    if isinstance(value, str):
        text = value.strip()
        if not any_string and text.lower().startswith(("https://", "http://")):
            return False
        if any_string:
            text = re.sub(r"https?://[^\s\"'<>]+", "", text, flags=re.IGNORECASE)
            if path_field:
                return bool(text.strip())
            return bool(re.search(
                r"(?i)(?:file:|(?:^|[\s\"'`=:(\[{,;&|<>])(?:/|\\|[a-z]:[/\\]|~)|(?:^|[/\\\s])\.\.(?:[/\\\s]|$))",
                text,
            ))
        if path_field:
            return bool(text)
        return text.lower().startswith("file:")
    if isinstance(value, list) or (any_string and isinstance(value, tuple)):
        return any(has_local_path(item, path_field, any_string=any_string) for item in value)
    if isinstance(value, dict):
        return any(
            has_local_path(item, path_field or str(key).lower() in _PATH_FIELDS, any_string=any_string)
            for key, item in value.items()
        )
    return False


def _tool_grants_enabled() -> bool:
    from clawchat_gateway.config import _read_tool_grants

    return _read_tool_grants(_config_extra().get("tool-grants")) == "on"

MANAGEMENT_UPGRADE_MESSAGE = "The owner needs to upgrade Hermes to 0.19.1 or later to manage tool grants."


def request_snapshot(session_id: str, turn_id: str):
    from clawchat_gateway.turn_grants import lookup_snapshot_for_call, session_context_value
    if not _tool_grants_enabled() or session_context_value("HERMES_SESSION_PLATFORM") != "clawchat":
        return None
    snapshot = lookup_snapshot_for_call(session_context_value("HERMES_SESSION_MESSAGE_ID"),
        session_context_value("HERMES_SESSION_CHAT_ID"), session_id, turn_id)
    return snapshot if snapshot and not snapshot.owner_side and not snapshot.legacy_owner_tools else None


def management_snapshot(session_id: str, turn_id: str):
    from clawchat_gateway.turn_grants import lookup_snapshot_for_call, session_context_value

    if not _tool_grants_enabled() or session_context_value("HERMES_SESSION_PLATFORM") != "clawchat":
        return None
    snapshot = lookup_snapshot_for_call(
        session_context_value("HERMES_SESSION_MESSAGE_ID"),
        session_context_value("HERMES_SESSION_CHAT_ID"), session_id, turn_id,
    )
    return snapshot if snapshot and snapshot.owner_side and snapshot.owner_verified else None


def _approval_config_read() -> dict[str, Any]:
    # The host's _get_approval_config catches read errors and returns {}.
    # Check its underlying reader as well so that fallback cannot authorize ask.
    from hermes_cli.config import load_config_readonly

    config = load_config_readonly()
    # Recent hosts return a dict subclass containing defaults or a last-known
    # good fallback after a failed read. Do not require that newer host class
    # to be importable on older hosts.
    if (any(cls.__name__ == "FailedConfigRead" for cls in type(config).__mro__)
            or hasattr(config, "read_error")
            or (isinstance(config, dict) and "read_error" in config)):
        raise ValueError("Unreadable approval policy")
    if not isinstance(config, dict) or not isinstance(config.get("approvals", {}), dict):
        raise ValueError("Unreadable approval policy")
    allowlist = config.get("command_allowlist", [])
    if not isinstance(allowlist, list) or any(not isinstance(key, str) for key in allowlist):
        raise ValueError("Unreadable approval allowlist")
    return config


def _approval_bypass(rule_key: str) -> bool:
    """Read exactly the predicates used by the host's arbitrary-tool gate."""
    try:
        from tools import approval

        _approval_config_read()
        if hasattr(approval, "_yolo_active"):
            yolo = approval._yolo_active()
        else:  # 0.21.x before approval_context.py was extracted
            yolo = approval._YOLO_MODE_FROZEN or approval.is_current_session_yolo_enabled()
        context = getattr(approval, "approval_context", approval)
        return (yolo or context._get_approval_mode() not in {"manual", "smart"}
                or approval.is_approved(approval.get_current_session_key(), "plugin_rule:" + rule_key))
    except Exception:  # noqa: BLE001 - unreadable policy is not a human gate
        return True


def _install_grant_approval_gate() -> bool:
    """Narrow shim: enforce a delivered, once-only ask and normalize denial text.

    The host otherwise returns its own denial prose and may auto-approve before
    contacting the adapter. Ordinary plugin rules keep the original function.
    """
    try:
        from tools import approval
        from clawchat_gateway.turn_grants import approval_by_nonce, finish_approval

        with _GRANT_APPROVAL_GATE_LOCK:
            original = approval.request_tool_approval
            if getattr(original, "_clawchat_grants", False):
                return True
            if not callable(getattr(approval, "list_gateway_approvals", None)):
                return False
            persist = getattr(approval, "_persist_choice", None)
            approve_session = approval.approve_session
            approve_permanent = approval.approve_permanent

            def grant_gate(tool_name, reason, *, rule_key="", **kwargs):
                if not rule_key.startswith("clawchat-grant-ask:"):
                    return original(tool_name, reason, rule_key=rule_key, **kwargs)
                nonce = rule_key.removeprefix("clawchat-grant-ask:")
                record = approval_by_nonce(nonce)
                result = _grant_block("ask-unavailable")
                try:
                    if (record and record.tool == tool_name and record.description == reason
                            and record.session_key == approval.get_current_session_key()
                            and can_ask_each_time(rule_key)):
                        outcome = original(tool_name, reason, rule_key=rule_key, **kwargs)
                        if record.routed and record.delivered:
                            result = _grant_block("ask-declined")
                        if finish_approval(nonce) and outcome.get("approved"):
                            return outcome
                except Exception:  # noqa: BLE001 - unavailable approval never becomes direct
                    pass
                finally:
                    finish_approval(nonce)
                result.pop("action")
                return {"approved": False, **result}

            def grant_persist(session_key, choice, keys):
                # Also prevent a decision delivered outside our adapter from ever
                # persisting these single-call keys. The shim then refuses it.
                ordinary = [key for key in keys if not key.startswith("plugin_rule:clawchat-grant-ask:")]
                if ordinary:
                    return persist(session_key, choice, ordinary)

            def grant_session(session_key, pattern_key):
                if not pattern_key.startswith("plugin_rule:clawchat-grant-ask:"):
                    return approve_session(session_key, pattern_key)

            def grant_permanent(pattern_key):
                if not pattern_key.startswith("plugin_rule:clawchat-grant-ask:"):
                    return approve_permanent(pattern_key)

            grant_gate._clawchat_grants = True
            if persist is not None:
                approval._persist_choice = grant_persist
            approval.approve_session = grant_session
            approval.approve_permanent = grant_permanent
            approval.request_tool_approval = grant_gate
            return True
    except Exception:  # noqa: BLE001 - unsupported host stays blocked
        return False


def _ask_directive(snapshot, tool_name: str, args: Any):
    import json
    from tools import approval
    from clawchat_gateway.turn_grants import register_approval

    denied = _grant_block("ask-unavailable")
    if not can_ask_each_time():
        return denied
    session_key = approval.get_current_session_key()
    if not session_key or session_key == "default":
        return denied
    names = snapshot.speaker_names or snapshot.speakers
    chat_type = "group" if _session_value("HERMES_SESSION_CHAT_TYPE").lower() == "group" else "direct chat"
    description = f"Speaker: {', '.join(names)}\nConversation: {chat_type}\nTool: {tool_name}\nArguments: {json.dumps(args, ensure_ascii=False)}"
    # The lock finalizes the nonce suffix before exposing the directive.
    record = register_approval(session_key, snapshot.chat_id, tool_name, description, 600)
    rule_key = "clawchat-grant-ask:" + record.nonce
    if not can_ask_each_time(rule_key):
        from clawchat_gateway.turn_grants import finish_approval
        finish_approval(record.nonce)
        return denied
    return {"action": "approve", "message": record.description, "rule_key": rule_key}

def clawchat_pre_llm_call(*, session_id: str = "", turn_id: str = "", **_: Any) -> None:
    from clawchat_gateway.turn_grants import bind_turn, session_context_value

    bind_turn(session_context_value("HERMES_SESSION_MESSAGE_ID"), session_id, turn_id)


def _host_supports_pre_llm_call() -> bool:
    try:
        from hermes_cli.plugins import VALID_HOOKS

        return "pre_llm_call" in VALID_HOOKS
    except Exception:  # noqa: BLE001 - do not assume an unknown hook runs
        return False


def _ordinary_non_owner_decision(tool_name, args, *, grants_enabled, approve=False):
    """Retain the first version's dedicated messages for ordinary host policy."""
    if approve and _host_supports_approve():
        return {"action": "approve", "message": APPROVE_MESSAGE,
                "rule_key": f"clawchat-non-owner:{tool_name}"}
    from clawchat_gateway.tool_grants import GRANT_BLOCK_MESSAGE

    message = GRANT_BLOCK_MESSAGE if grants_enabled else BLOCK_MESSAGE
    if not is_restricted_tool(tool_name) and _limited_call(tool_name, args):
        message = LIMITED_BLOCK_MESSAGE
    return {"action": "block", "message": message}


def _grant_block(reason):
    from clawchat_gateway.tool_grants import GRANT_FAILURE_MESSAGES

    message = GRANT_FAILURE_MESSAGES[reason]
    return {"action": "block", "reasonCode": reason, "message": message, "blockReason": message}


def _cross_session_decision(tool_name, args, session_id=""):
    process_session_id = ""
    if tool_name in {"process", "process_manage"} and isinstance(args, dict):
        target = args.get("session_id")
        if target is not None and str(target):
            try:
                from tools.process_registry import process_registry
                from tools.approval import get_current_session_key

                process = process_registry.get(str(target))
                current_key = get_current_session_key(default="")
                # parent_session_id is stamped at spawn; session_key changes
                # on ownership transfer. Both must still identify this caller.
                if not (current_key and process is not None and process.session_key == current_key):
                    return _grant_block("cross-session")
                process_session_id = process.parent_session_id if process is not None else ""
            except Exception:  # Missing/older registry or unreadable ownership.
                return _grant_block("cross-session")

    from clawchat_gateway.tool_capabilities import host_tool_targets_current_session

    return None if host_tool_targets_current_session(
        tool_name, args, session_id=session_id, process_session_id=process_session_id,
    ) else _grant_block("cross-session")


def clawchat_pre_tool_call(*, tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[dict[str, Any]]:
    from clawchat_gateway.turn_attachments import record_call
    decision = _clawchat_pre_tool_decision(tool_name=tool_name, args=args, **kwargs)
    record_call(tool_name=tool_name, args=args, decision=decision, **kwargs)
    return decision


def _clawchat_pre_tool_decision(*, tool_name: str = "", args: Any = None, **kwargs: Any) -> Optional[dict[str, Any]]:
    """``pre_tool_call`` hook: host tools in a turn the owner did not start."""
    try:
        tool_name = str(tool_name or "")
        from clawchat_gateway.tool_grants import GRANT_TOOL_NAMES, GRANT_BLOCK_MESSAGE

        if tool_name == "clawchat_tool_request":
            snapshot = request_snapshot(kwargs.get("session_id", ""), kwargs.get("turn_id", ""))
            return None if snapshot and snapshot.request_origin else _grant_block("not-granted")
        if tool_name in GRANT_TOOL_NAMES:
            if management_snapshot(kwargs.get("session_id", ""), kwargs.get("turn_id", "")):
                return None
            message = GRANT_BLOCK_MESSAGE
            # Upgrade hints are only disclosed on a verified owner turn.
            from clawchat_gateway.turn_grants import get_snapshot, session_context_value
            snapshot = get_snapshot(session_context_value("HERMES_SESSION_MESSAGE_ID"))
            if (_tool_grants_enabled() and not kwargs.get("turn_id") and snapshot and snapshot.owner_side and snapshot.owner_verified
                    and snapshot.chat_id == session_context_value("HERMES_SESSION_CHAT_ID")
                    and session_context_value("HERMES_SESSION_PLATFORM") == "clawchat"):
                message = MANAGEMENT_UPGRADE_MESSAGE
            return {"action": "block", "message": message}
        from clawchat_gateway.turn_grants import get_snapshot, lookup_snapshot_for_call, session_context_value
        from clawchat_gateway.tool_grants import (
            evaluate_tool_grant, grant_failure_code,
        )
        nonce = session_context_value("HERMES_SESSION_MESSAGE_ID")
        chat_id = session_context_value("HERMES_SESSION_CHAT_ID")
        can_bind_call = "session_id" in kwargs and "turn_id" in kwargs
        snapshot = (lookup_snapshot_for_call(nonce, chat_id, kwargs.get("session_id"), kwargs.get("turn_id"))
                    if can_bind_call else None)
        if snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools):
            return None
        if not _host_supports_pre_llm_call():
            legacy = get_snapshot(nonce)
            if (legacy and legacy.legacy_owner_tools and legacy.chat_id == chat_id
                    and session_context_value("HERMES_SESSION_PLATFORM") == "clawchat"):
                # Some old hosts pass empty execution IDs despite lacking the
                # binding hook. This exception is ordinary legacy access only.
                return None
        # Bound snapshots, including mixed overlaps and unattributed events in
        # the owner's chat, override the host's session-level owner inference.
        # An old host with no execution IDs keeps its legacy approval policy.
        session_turn = _session_turn()
        pending_snapshot = get_snapshot(nonce)
        if session_context_value("HERMES_SESSION_PLATFORM") == "clawchat" and (
                snapshot or can_bind_call or pending_snapshot and pending_snapshot.chat_id == chat_id):
            session_turn = "other"
        # The plugin's own tools: any turn that is not the owner's. The host's
        # send_message: only ClawChat turns the owner did not start, so another
        # platform's own sessions keep the host's behaviour.
        turn = session_turn if tool_name.startswith("clawchat_") or tool_name == "send_message" else ""
        attachment_permitted = False
        if (tool_name.startswith("clawchat_") and turn != "owner") or (
            tool_name == "send_message" and turn == "other"
        ):
            from clawchat_gateway.local_paths import ATTACHMENT_REFUSAL, LOCAL_PATH_REFUSAL
            from clawchat_gateway.memory_scope import is_local_surface

            if tool_name == "send_message" and isinstance(args, dict):
                target = str(args.get("target", "") or "").strip().lower()
                if not target.startswith("clawchat:") and (
                    re.search(r"\bMEDIA:", str(args.get("message", "")), re.IGNORECASE)
                    or args.get("media_files") or args.get("attachments")
                ):
                    return {"action": "block", "message": ATTACHMENT_REFUSAL}
            if not is_local_surface():
                message = args.get("message", "") if isinstance(args, dict) else ""
                local_marker = tool_name == "send_message" and any(
                    not match.group(1).lower().startswith(("http://", "https://"))
                    for match in re.finditer(r"\bMEDIA:\s*[\"']?([^\s\"']+)", str(message), re.IGNORECASE)
                )
                from clawchat_gateway.local_paths import local_attachment_allowed
                from clawchat_gateway.media_runtime import normalize_outbound_media_reference
                target_chat = chat_id
                if tool_name == "clawchat_send_file":
                    target_chat = args.get("chat_id", "") if isinstance(args, dict) else ""
                elif tool_name == "send_message" and isinstance(args, dict):
                    target_chat = str(args.get("target", "")).removeprefix("clawchat:")
                paths = []
                if tool_name == "clawchat_send_file" and isinstance(args, dict):
                    paths = [args.get("path")]
                elif tool_name == "send_message" and isinstance(args, dict):
                    paths = [m.group(1) for m in re.finditer(
                        r"\bMEDIA:\s*[\"']?([^\s\"']+)", str(message), re.IGNORECASE)]
                    for key in ("media_files", "attachments"):
                        values = args.get(key, [])
                        paths.extend(values if isinstance(values, list) else [values])
                attachment_permitted = bool(paths) and all(
                    isinstance(path, str) and local_attachment_allowed(
                        target_chat, normalize_outbound_media_reference(path))
                    for path in paths)
                if (has_local_path(args) or local_marker) and not attachment_permitted:
                    return {"action": "block", "message": LOCAL_PATH_REFUSAL}
        guarded = _guarded_call(tool_name, args)
        strict = not guarded and _strict_unregistered(tool_name)
        if session_turn != "other":
            return None
        grants_enabled = _tool_grants_enabled()
        if strict:
            logger.warning(
                "clawchat host tools: %s is not in the host registry and the memory provider's "
                "tools are unknown; treating it as restricted", tool_name)
        # Hosts without execution IDs cannot apply per-turn grants. Preserve
        # their pre-existing approval opt-in, including the ordinary messages.
        if grants_enabled and not can_bind_call:
            if not guarded and not strict:
                return _cross_session_decision(tool_name, args, kwargs.get("session_id", ""))
            if non_owner_host_tools() == SETTING_APPROVE:
                route_block = _cross_session_decision(tool_name, args, kwargs.get("session_id", ""))
                if route_block:
                    return route_block
            return _ordinary_non_owner_decision(tool_name, args, grants_enabled=True,
                approve=non_owner_host_tools() == SETTING_APPROVE)
        if grants_enabled:
            # A capable host with a missing/bad snapshot may conceal deny.
            grants = snapshot.grants if snapshot is not None else {}
            context = {"chat_id": chat_id, "default_allowed": not guarded and not strict}
            mode = evaluate_tool_grant(grants, tool_name, args, context, time.time() * 1000)
            if snapshot is not None:
                _, available = host_tool_catalog()
                if tool_name not in available:
                    if can_bind_call and _grant_search_bridge_allowed(tool_name, snapshot):
                        if tool_name == "tool_describe":
                            return _grant_tool_describe_block(args, snapshot)
                        return None
                    return _grant_block("not-available")
            if mode == "deny":
                reason = grant_failure_code(grants, tool_name, args=args, context=context,
                                            speakers=snapshot.non_owner_speakers if snapshot else ())
                return _grant_block(reason)
            if mode in ("ask", "direct") or not guarded and not strict:
                route_block = _cross_session_decision(tool_name, args, kwargs.get("session_id", ""))
                if route_block:
                    return route_block
            if mode == "ask":
                try:
                    return _ask_directive(snapshot, tool_name, args)
                except Exception:  # noqa: BLE001 - routing failure is never direct permission
                    return _grant_block("ask-unavailable")
            if mode == "direct":
                from clawchat_gateway.tool_capabilities import TOOL_CAPABILITIES
                from clawchat_gateway.local_paths import LOCAL_PATH_REFUSAL
                local_tools = set().union(*(TOOL_CAPABILITIES[id_]["tools"] for id_ in ("terminal", "files", "code")))
                if (guarded and tool_name not in local_tools and has_local_path(args, any_string=True)
                        and not (tool_name in {"clawchat_send_file", "send_message"} and attachment_permitted)):
                    return {"action": "block", "message": LOCAL_PATH_REFUSAL}
                return None
            if not guarded and not strict:
                return None
            reason = grant_failure_code(grants, tool_name, args=args, context=context,
                                        speakers=snapshot.non_owner_speakers if snapshot else ())
            if snapshot is not None and reason == "not-granted":
                from clawchat_gateway.tool_grants import has_grant_rows
                if not has_grant_rows(grants, now=time.time() * 1000):
                    return {**_ordinary_non_owner_decision(tool_name, args, grants_enabled=True),
                            "reasonCode": reason}
            return _grant_block(reason)
        if not guarded and not strict:
            return _cross_session_decision(tool_name, args, kwargs.get("session_id", ""))
        if non_owner_host_tools() == SETTING_APPROVE:
            route_block = _cross_session_decision(tool_name, args, kwargs.get("session_id", ""))
            if route_block:
                return route_block
        return _ordinary_non_owner_decision(tool_name, args, grants_enabled=False,
                                           approve=non_owner_host_tools() == SETTING_APPROVE)
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


def limited_tools_hint() -> Optional[str]:
    """:data:`LIMITED_TOOLS_HINT` for a turn the owner did not start, unless every
    limited call goes to the owner instead (``approve`` on a host that escalates)."""
    try:
        if non_owner_host_tools() == SETTING_APPROVE and _host_supports_approve():
            return None
        if _skills_inline_shell():
            return f"{LIMITED_TOOLS_HINT} {LIMITED_SKILL_VIEW_HINT}"
        return LIMITED_TOOLS_HINT
    except Exception:  # noqa: BLE001 - a hint only
        return LIMITED_TOOLS_HINT


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
    from clawchat_gateway import turn_attachments
    turn_attachments.completion_hook_registered = False
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
    try:
        from hermes_cli.plugins import VALID_HOOKS
        if "post_tool_call" in VALID_HOOKS:
            register_hook("post_tool_call", turn_attachments.post_tool_call)
            turn_attachments.completion_hook_registered = True
    except Exception:  # noqa: BLE001 - Files direct still works without artifacts
        logger.warning("clawchat attachments: tool completion hook unavailable; artifacts disabled")
    _install_grant_catalog_filter()
    _install_guest_search_guard()
    if _host_supports_pre_llm_call():
        try:
            register_hook("pre_llm_call", clawchat_pre_llm_call)
        except Exception:  # noqa: BLE001 - unbound grants remain unusable
            logger.warning("clawchat turn grants: pre_llm_call hook was not registered")


__all__ = [
    "BLOCK_MESSAGE",
    "BLOCKED_TOOLS_HINT",
    "FALLBACK_TOOLSETS",
    "LIMITED_BLOCK_MESSAGE",
    "LIMITED_TOOLS_HINT",
    "NO_MCP",
    "RESTRICTED_TOOLSETS",
    "SETTING_KEY",
    "blocked_tools_hint",
    "clawchat_pre_tool_call",
    "is_mcp_tool",
    "is_owner_turn",
    "limited_tools_hint",
    "memory_provider_tools",
    "non_owner_host_tools",
    "refuses_non_owner_turns",
    "register_host_tools_guard",
    "is_restricted_tool",
    "restricted_tool_names",
    "toolsets_for_source",
]
