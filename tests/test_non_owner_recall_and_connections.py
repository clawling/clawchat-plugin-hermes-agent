"""session_search and connections in turns the owner did not start.

``test_friend_turn_tools`` and ``test_non_owner_residual`` pin the terminal /
file / code / browser / sub-agent / cron / MCP / memory narrowing. Two host
toolsets stayed in a friend's direct chat and in every group turn and read
what belongs to the owner:

* ``session_search`` reads every conversation in Hermes' session store — the
  owner's direct chat, other friends, other groups, other platforms, and with
  its ``profile`` argument other profiles' stores. Nothing limits it to the
  current chat, and a hook cannot rewrite its arguments, so the whole tool goes;
* ``connections`` (Hermes 0.21.3+): ``manage_connections`` installs, enables or
  authorizes MCP servers in the owner's config without a confirmation card on a
  messaging platform, and while it is in the turn the owner's connected hosted
  accounts (``connectors__<app>__<tool>``, reached through ``tool_call``) are
  callable there.

Both layers now cover them like the other host tools (``non-owner-host-tools``:
off drops / blocks them, approve escalates every call). Tools with nothing of
the owner's to reach — the per-session ``todo_list``, web search, clarify —
stay.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from clawchat_gateway import host_tools_guard as guard

OWNER = "usr_owner"
FRIEND = "usr_friend"
DM = "cnv_dm"  # a friend's direct chat
GROUP = "cnv_group"
OWNER_DM = "cnv_owner_dm"  # the owner's own direct chat (activation conversation)

HOST_TOOLSETS = {
    "terminal": ["terminal", "process_manage"],
    "web": ["web_search", "web_extract"],
    "todo": ["todo_list"],
    "clarify": ["clarify"],
    "session_search": ["session_search"],
    "connections": ["manage_connections"],
    "clawchat": ["clawchat_memory_read", "clawchat_memory_write"],
    # A composite that carries session_search (the host's core set does).
    "core": ["web_search", "todo_list", "session_search"],
}
PLATFORM = set(HOST_TOOLSETS)

RECALL_AND_ACCOUNT_CALLS = [
    ("session_search", {"query": "owner home address"}),
    ("session_search", {"session_id": "20261010_owner_dm"}),
    ("session_search", {"session_id": "work/20261010_abc", "profile": "work"}),
    ("manage_connections", {"action": "status"}),
    ("manage_connections", {"action": "install", "connectors": [{"name": "filesystem", "mcp": True}]}),
    ("connectors__gmail__search_messages", {"query": "invoice"}),
]
KEPT_CALLS = [
    ("todo_list", {"todos": [{"id": "1", "content": "reply", "status": "pending"}]}),
    ("web_search", {"query": "weather"}),
    ("clarify", {"question": "which one?"}),
    ("clawchat_memory_read", {"target": "user", "user_id": FRIEND}),
]

SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_USER_ID",
    "HERMES_PLATFORM",
)

TURNS = {
    "friend_dm": dict(chat_id=DM, chat_type="dm", user_id=FRIEND),
    "group_friend": dict(chat_id=GROUP, chat_type="group", user_id=FRIEND),
    "group_owner": dict(chat_id=GROUP, chat_type="group", user_id=OWNER),
}


@dataclass
class Source:
    chat_id: str
    chat_type: str
    user_id: str | None = None
    platform: str = "clawchat"


@pytest.fixture(autouse=True)
def _host(monkeypatch):
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(guard, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: OWNER_DM)
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: chat_id == GROUP)
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: set(PLATFORM))
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: list(HOST_TOOLSETS.get(name, [])))
    monkeypatch.setattr(guard, "_mcp_server_names", lambda: set())
    monkeypatch.setattr(guard, "_registry_toolset", lambda name: "")
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: True)
    monkeypatch.setattr(guard, "_hook_missing", None)


def _session(monkeypatch, *, chat_id, chat_type, user_id, platform="clawchat"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)


def _approve(monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})


# --- layer 1: the per-source override (Hermes 0.20.1+) -----------------------


@pytest.mark.parametrize("where", list(TURNS))
def test_override_drops_session_search_and_connections(where):
    turn = TURNS[where]
    kept = guard.toolsets_for_source(Source(turn["chat_id"], turn["chat_type"], turn["user_id"]), owner_user_id=OWNER)
    assert kept == ["clarify", "clawchat", "todo", "web", guard.NO_MCP]
    for name in ("session_search", "connections", "core"):
        assert name not in kept


def test_owner_direct_chat_keeps_them():
    assert guard.toolsets_for_source(Source(OWNER_DM, "dm", OWNER), owner_user_id=OWNER) is None


def test_approve_keeps_them_visible_for_the_gate(monkeypatch):
    _approve(monkeypatch)
    for turn in TURNS.values():
        source = Source(turn["chat_id"], turn["chat_type"], turn["user_id"])
        assert guard.toolsets_for_source(source, owner_user_id=OWNER) is None


def test_static_list_covers_them_without_the_host_resolver(monkeypatch):
    def broken(name):
        raise ImportError("no toolsets module")

    monkeypatch.setattr(guard, "_resolve_tools", broken)
    names = guard.restricted_tool_names()
    assert {"session_search", "manage_connections"} <= names


# --- layer 2: the pre_tool_call hook -----------------------------------------


@pytest.mark.parametrize("tool,args", RECALL_AND_ACCOUNT_CALLS)
@pytest.mark.parametrize("where", list(TURNS))
def test_hook_blocks_them_in_non_owner_turns(monkeypatch, tool, args, where):
    _session(monkeypatch, **TURNS[where])
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result == {"action": "block", "message": guard.BLOCK_MESSAGE}


@pytest.mark.parametrize("tool,args", RECALL_AND_ACCOUNT_CALLS)
@pytest.mark.parametrize("where", list(TURNS))
def test_hook_blocks_without_a_chat_type(monkeypatch, tool, args, where):
    """Hermes before 0.19.1: no chat type, the chat id decides."""
    _session(monkeypatch, **{**TURNS[where], "chat_type": ""})
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result == {"action": "block", "message": guard.BLOCK_MESSAGE}


@pytest.mark.parametrize("tool,args", RECALL_AND_ACCOUNT_CALLS)
@pytest.mark.parametrize("chat_type", ["dm", ""])
def test_owner_direct_chat_is_untouched(monkeypatch, tool, args, chat_type):
    _session(monkeypatch, chat_id=OWNER_DM, chat_type=chat_type, user_id=OWNER)
    assert guard.clawchat_pre_tool_call(tool_name=tool, args=args) is None


def test_plugin_turn_in_the_owner_chat_may_search(monkeypatch):
    _session(monkeypatch, chat_id=OWNER_DM, chat_type="dm", user_id="clawchat")
    assert guard.clawchat_pre_tool_call(tool_name="session_search", args={"query": "x"}) is None


def test_other_platforms_are_untouched(monkeypatch):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=FRIEND, platform="telegram")
    assert guard.clawchat_pre_tool_call(tool_name="session_search", args={"query": "x"}) is None


@pytest.mark.parametrize("tool,args", KEPT_CALLS)
@pytest.mark.parametrize("where", list(TURNS))
def test_tools_with_nothing_of_the_owners_stay(monkeypatch, tool, args, where):
    _session(monkeypatch, **TURNS[where])
    assert guard.clawchat_pre_tool_call(tool_name=tool, args=args) is None


@pytest.mark.parametrize("tool,args", RECALL_AND_ACCOUNT_CALLS)
@pytest.mark.parametrize("where", list(TURNS))
def test_approve_escalates_them(monkeypatch, tool, args, where):
    _approve(monkeypatch)
    _session(monkeypatch, **TURNS[where])
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result["action"] == "approve"
    assert result["rule_key"] == f"clawchat-non-owner:{tool}"


def test_approve_blocks_when_the_host_cannot_escalate(monkeypatch):
    _approve(monkeypatch)
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
    _session(monkeypatch, **TURNS["friend_dm"])
    result = guard.clawchat_pre_tool_call(tool_name="session_search", args={"query": "x"})
    assert result == {"action": "block", "message": guard.BLOCK_MESSAGE}


@pytest.mark.parametrize("tool", ["session_search", "manage_connections", "connectors__gmail__send_message"])
def test_hook_failure_fails_closed(monkeypatch, caplog, tool):
    def broken():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(guard, "_owner_user_id", broken)
    _session(monkeypatch, **TURNS["friend_dm"])
    with caplog.at_level("WARNING", logger="clawchat_gateway.host_tools_guard"):
        result = guard.clawchat_pre_tool_call(tool_name=tool, args={})
    assert result == {"action": "block", "message": guard.BLOCK_MESSAGE}
    assert "blocking" in caplog.text


def test_block_message_and_hint_name_them():
    assert "past conversations" in guard.BLOCK_MESSAGE
    assert "connected accounts" in guard.BLOCK_MESSAGE
    assert "session_search" in guard.BLOCKED_TOOLS_HINT
    assert "manage_connections" in guard.BLOCKED_TOOLS_HINT
