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

The hook also limits calls of tools such turns keep, by their arguments:

* ``skill_manage`` — a skill it writes is in the owner's direct chat from then
  on (scripts included). ``skill_view`` / ``skills_list`` stay, except that with
  the owner's ``skills.inline_shell`` on (the host runs a skill's ``!`cmd```)
  ``skill_view`` opens only ClawChat's own skills;
* ``vision_analyze`` / ``video_analyze`` / ``image_generate`` read any local
  image: only web / data: sources and files in the host's media caches or the
  plugin's inbound attachment directory pass;
* ``text_to_speech`` with an ``output_path`` writes anywhere.

And an external memory provider's tools (honcho, mem0, ...): the host gates
them with the ``memory`` toolset only from 0.18.1, and only the override
(0.20.1+) drops that toolset, so the hook blocks them by the names the provider
lists; when it cannot be loaded, a turn the override does not narrow gets no
tool the host registry does not know.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from clawchat_gateway import host_tools_guard as guard
from clawchat_gateway.tool_grants import GRANT_BLOCK_MESSAGE

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

#: Tools the host registry holds (an external memory provider's are not in it).
REGISTERED = {"web_search", "todo_list", "clarify", "clawchat_memory_read", "skill_view", "vision_analyze"}

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
    monkeypatch.setattr(guard, "_registry_knows", lambda name: name in REGISTERED)
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: frozenset())
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: False)
    monkeypatch.setattr(guard, "_plugin_skill_ids", lambda: {"clawchat-core", "clawchat-liveware"})
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
    assert kept == [guard.GRANT_REQUEST_TOOLSET, "clarify", "clawchat", "todo", "web", guard.NO_MCP]
    for name in ("session_search", "connections", "core"):
        assert name not in kept


def test_owner_direct_chat_keeps_them():
    assert guard.toolsets_for_source(Source(OWNER_DM, "dm", OWNER), owner_user_id=OWNER) is None


def test_approve_keeps_them_visible_for_the_gate(monkeypatch, legacy_plugin_turn):
    _approve(monkeypatch)
    for turn in TURNS.values():
        nonce = legacy_plugin_turn(**turn)
        source = Source(turn["chat_id"], turn["chat_type"], turn["user_id"])
        source.message_id = nonce
        assert guard.toolsets_for_source(source, owner_user_id=OWNER) is None


def test_approve_without_plugin_snapshot_blocks_and_narrows(monkeypatch, legacy_plugin_turn):
    _approve(monkeypatch)
    turn = TURNS["group_friend"]
    legacy_plugin_turn(**turn, with_snapshot=False)
    assert guard.clawchat_pre_tool_call(tool_name="session_search", args={})["action"] == "block"
    kept = guard.toolsets_for_source(Source(**turn), owner_user_id=OWNER)
    assert kept and "session_search" not in kept and "connections" not in kept


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
    assert result == {"action": "block", "message": GRANT_BLOCK_MESSAGE}


@pytest.mark.parametrize("tool,args", RECALL_AND_ACCOUNT_CALLS)
@pytest.mark.parametrize("where", list(TURNS))
def test_hook_blocks_without_a_chat_type(monkeypatch, tool, args, where):
    """Hermes before 0.19.1: no chat type, the chat id decides."""
    _session(monkeypatch, **{**TURNS[where], "chat_type": ""})
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result == {"action": "block", "message": GRANT_BLOCK_MESSAGE}


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
def test_approve_escalates_them(monkeypatch, legacy_plugin_turn, tool, args, where):
    _approve(monkeypatch)
    _session(monkeypatch, **TURNS[where])
    legacy_plugin_turn(**TURNS[where])
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result["action"] == "approve"
    assert result["rule_key"] == f"clawchat-non-owner:{tool}"


def test_approve_blocks_when_the_host_cannot_escalate(monkeypatch, legacy_plugin_turn):
    _approve(monkeypatch)
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
    _session(monkeypatch, **TURNS["friend_dm"])
    legacy_plugin_turn(**TURNS["friend_dm"])
    result = guard.clawchat_pre_tool_call(tool_name="session_search", args={"query": "x"})
    assert result == {"action": "block", "message": GRANT_BLOCK_MESSAGE}


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


# --- calls limited by their arguments ----------------------------------------


@pytest.fixture
def media(tmp_path, monkeypatch):
    """A Hermes home with an image cache, an inbound directory, and a private photo."""
    home = tmp_path / "hermes"
    inbound = tmp_path / "clawchat-media"
    for path in (home / "cache" / "images", inbound, tmp_path / "Pictures"):
        path.mkdir(parents=True)
    monkeypatch.setattr(guard, "_hermes_home", lambda: home)
    monkeypatch.setattr(guard, "_config_extra", lambda: {"media_download_dir": str(inbound)})
    files = {
        "cached": home / "cache" / "images" / "img_1.png",
        "inbound": inbound / "abc-photo.jpg",
        "private": tmp_path / "Pictures" / "passport.jpg",
    }
    for path in files.values():
        path.write_bytes(b"\x89PNG")
    return files


def _limited(monkeypatch, where, tool, args):
    _session(monkeypatch, **TURNS[where])
    return guard.clawchat_pre_tool_call(tool_name=tool, args=args)


@pytest.mark.parametrize("where", list(TURNS))
def test_skill_manage_is_blocked(monkeypatch, where):
    result = _limited(monkeypatch, where, "skill_manage", {"action": "create", "name": "x", "content": "y"})
    assert result == {"action": "block", "message": guard.LIMITED_BLOCK_MESSAGE}


@pytest.mark.parametrize("where", list(TURNS))
@pytest.mark.parametrize("name", ["owner-notes", "clawchat:clawchat-core", "clawchat-liveware"])
def test_skill_view_stays_without_inline_shell(monkeypatch, where, name):
    assert _limited(monkeypatch, where, "skill_view", {"name": name}) is None
    assert _limited(monkeypatch, where, "skills_list", {}) is None


@pytest.mark.parametrize("where", list(TURNS))
@pytest.mark.parametrize("name", ["owner-notes", "research/arxiv", "clawchat:owner-notes", "", None])
def test_inline_shell_limits_skill_view_to_clawchat_skills(monkeypatch, where, name):
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    result = _limited(monkeypatch, where, "skill_view", {"name": name})
    assert result == {"action": "block", "message": guard.LIMITED_BLOCK_MESSAGE}


@pytest.mark.parametrize("where", list(TURNS))
@pytest.mark.parametrize("name", ["clawchat:clawchat-core", "clawchat-core", "clawchat-liveware"])
def test_inline_shell_still_opens_clawchat_skills(monkeypatch, where, name):
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    assert _limited(monkeypatch, where, "skill_view", {"name": name}) is None
    assert _limited(monkeypatch, where, "skills_list", {}) is None


def test_inline_shell_unreadable_counts_as_on(monkeypatch):
    monkeypatch.undo()  # the real reader
    monkeypatch.setattr(guard, "_host_config", lambda: (_ for _ in ()).throw(OSError("no config")))
    assert guard._skills_inline_shell() is True
    monkeypatch.setattr(guard, "_host_config", lambda: {"skills": {"inline_shell": True}})
    assert guard._skills_inline_shell() is True
    monkeypatch.setattr(guard, "_host_config", lambda: {"skills": {}})
    assert guard._skills_inline_shell() is False


def test_plugin_skill_ids_include_bundled_and_updated(monkeypatch):
    monkeypatch.undo()
    from clawchat_gateway import skill_update

    monkeypatch.setattr(skill_update, "read_local_manifest", lambda: {"clawchat-new": "1.0.0"})
    ids = guard._plugin_skill_ids()
    assert "clawchat-core" in ids and "clawchat-new" in ids


@pytest.mark.parametrize("where", list(TURNS))
@pytest.mark.parametrize(
    "source",
    ["https://cdn.example.com/a.png", "http://example.com/b.jpg", "data:image/png;base64,iVBORw0KGgo=", "cached", "inbound"],
)
def test_vision_on_web_and_conversation_media_passes(monkeypatch, media, where, source):
    source = str(media.get(source, source))
    assert _limited(monkeypatch, where, "vision_analyze", {"image_url": source, "question": "what?"}) is None
    assert _limited(monkeypatch, where, "vision_analyze", {"image_url": f"file://{source}"} if source.startswith("/") else {"image_url": source}) is None


@pytest.mark.parametrize("where", list(TURNS))
def test_vision_on_other_local_files_is_blocked(monkeypatch, media, where):
    private = media["private"]
    escape = media["cached"].parent / ".." / ".." / ".." / "Pictures" / "passport.jpg"
    for source in (str(private), f"file://{private}", str(escape), "~/.hermes/.env", "s3://bucket/key.png"):
        result = _limited(monkeypatch, where, "vision_analyze", {"image_url": source})
        assert result == {"action": "block", "message": guard.LIMITED_BLOCK_MESSAGE}, source


def test_vision_through_a_symlink_out_of_the_cache_is_blocked(monkeypatch, media):
    link = media["cached"].parent / "link.jpg"
    link.symlink_to(media["private"])
    assert _limited(monkeypatch, "friend_dm", "vision_analyze", {"image_url": str(link)})["action"] == "block"


@pytest.mark.parametrize("where", list(TURNS))
def test_image_generate_and_video_analyze_follow_the_same_rule(monkeypatch, media, where):
    private, cached = str(media["private"]), str(media["cached"])
    assert _limited(monkeypatch, where, "image_generate", {"prompt": "a cat"}) is None
    assert _limited(monkeypatch, where, "image_generate", {"prompt": "x", "image_url": cached}) is None
    assert _limited(monkeypatch, where, "image_generate", {"prompt": "x", "image_url": private})["action"] == "block"
    blocked = _limited(
        monkeypatch, where, "image_generate", {"prompt": "x", "reference_image_urls": ["https://e.com/a.png", private]}
    )
    assert blocked["action"] == "block"
    assert _limited(monkeypatch, where, "video_analyze", {"video_url": private})["action"] == "block"
    assert _limited(monkeypatch, where, "video_analyze", {"video_url": "https://e.com/v.mp4"}) is None


def test_media_args_as_a_json_string(monkeypatch, media):
    args = '{"image_url": "%s"}' % media["private"]
    assert _limited(monkeypatch, "friend_dm", "vision_analyze", args)["action"] == "block"


@pytest.mark.parametrize("where", list(TURNS))
def test_tts_with_an_output_path_is_blocked(monkeypatch, where):
    assert _limited(monkeypatch, where, "text_to_speech", {"text": "hi"}) is None
    assert _limited(monkeypatch, where, "text_to_speech", {"text": "hi", "output_path": " "}) is None
    result = _limited(monkeypatch, where, "text_to_speech", {"text": "hi", "output_path": "/home/me/notes.md"})
    assert result == {"action": "block", "message": guard.LIMITED_BLOCK_MESSAGE}


@pytest.mark.parametrize("chat_type", ["dm", ""])
def test_owner_direct_chat_keeps_every_limited_call(monkeypatch, media, chat_type):
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    _session(monkeypatch, chat_id=OWNER_DM, chat_type=chat_type, user_id=OWNER)
    for tool, args in (
        ("skill_manage", {"action": "create"}),
        ("skill_view", {"name": "owner-notes"}),
        ("vision_analyze", {"image_url": str(media["private"])}),
        ("text_to_speech", {"text": "hi", "output_path": "/tmp/x.mp3"}),
    ):
        assert guard.clawchat_pre_tool_call(tool_name=tool, args=args) is None, tool


@pytest.mark.parametrize("where", list(TURNS))
def test_approve_escalates_limited_calls(monkeypatch, legacy_plugin_turn, media, where):
    _approve(monkeypatch)
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    legacy_plugin_turn(**TURNS[where])
    result = _limited(monkeypatch, where, "skill_manage", {"action": "create"})
    assert result["action"] == "approve"


def test_limited_tools_hint(monkeypatch):
    assert guard.limited_tools_hint() == guard.LIMITED_TOOLS_HINT
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    assert guard.LIMITED_SKILL_VIEW_HINT in guard.limited_tools_hint()
    _approve(monkeypatch)
    assert guard.limited_tools_hint() is None  # every limited call asks the owner
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
    assert guard.limited_tools_hint().startswith(guard.LIMITED_TOOLS_HINT)


def test_channel_prompt_carries_the_limits_only_outside_the_owner_chat(monkeypatch):
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway.inbound import InboundMessage

    adapter = ClawChatAdapter({})
    monkeypatch.setattr(ClawChatAdapter, "_is_owner_turn", lambda self, inbound: inbound.sender_id == OWNER)
    def inbound(sender):
        return InboundMessage(chat_id=DM, chat_type="direct", sender_id=sender, sender_name="", text="hi", raw_message={})
    assert guard.LIMITED_TOOLS_HINT in adapter._non_owner_turn_section(inbound(FRIEND))
    assert adapter._non_owner_turn_section(inbound(OWNER)) is None


# --- an external memory provider ---------------------------------------------

PROVIDER_TOOLS = frozenset({"honcho_search", "honcho_profile", "honcho_conclude"})


@pytest.mark.parametrize("override", [True, False])
@pytest.mark.parametrize("tool", sorted(PROVIDER_TOOLS))
@pytest.mark.parametrize("where", list(TURNS))
def test_memory_provider_tools_are_blocked(monkeypatch, override, tool, where):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: PROVIDER_TOOLS)
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: override)
    result = _limited(monkeypatch, where, tool, {"query": "owner"})
    assert result == {"action": "block", "message": GRANT_BLOCK_MESSAGE}


@pytest.mark.parametrize("chat_type", ["dm", ""])
def test_owner_direct_chat_keeps_the_provider(monkeypatch, chat_type):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: PROVIDER_TOOLS)
    _session(monkeypatch, chat_id=OWNER_DM, chat_type=chat_type, user_id=OWNER)
    assert guard.clawchat_pre_tool_call(tool_name="honcho_search", args={}) is None


def test_approve_escalates_provider_tools(monkeypatch, legacy_plugin_turn):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: PROVIDER_TOOLS)
    _approve(monkeypatch)
    legacy_plugin_turn(**TURNS["group_owner"])
    assert _limited(monkeypatch, "group_owner", "honcho_conclude", {})["action"] == "approve"


@pytest.mark.parametrize("where", list(TURNS))
def test_unknown_provider_blocks_unregistered_tools_without_the_override(monkeypatch, caplog, where):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: None)
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: False)
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.host_tools_guard"):
        result = _limited(monkeypatch, where, "honcho_search", {})
    assert result == {"action": "block", "message": GRANT_BLOCK_MESSAGE}
    assert "not in the host registry" in caplog.text
    for tool, args in KEPT_CALLS:
        assert _limited(monkeypatch, where, tool, args) is None, tool


def test_unknown_provider_under_approve_escalates_unregistered_tools(monkeypatch, legacy_plugin_turn):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: None)
    _approve(monkeypatch)
    legacy_plugin_turn(**TURNS["friend_dm"])
    assert _limited(monkeypatch, "friend_dm", "honcho_search", {})["action"] == "approve"


def test_unknown_provider_with_the_override_leaves_the_rest(monkeypatch):
    """0.20.1+, off: the override drops the memory toolset and the provider's tools with it."""
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: None)
    assert _limited(monkeypatch, "friend_dm", "honcho_search", {}) is None


def test_unknown_provider_owner_chat_is_untouched(monkeypatch):
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: None)
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: False)
    _session(monkeypatch, chat_id=OWNER_DM, chat_type="dm", user_id=OWNER)
    assert guard.clawchat_pre_tool_call(tool_name="honcho_search", args={}) is None


class _Provider:
    def __init__(self, schemas):
        self._schemas = schemas

    def get_tool_schemas(self):
        return self._schemas


@pytest.fixture
def provider_lookup(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(guard, "_provider_tools", {})
    monkeypatch.setattr(guard, "_provider_tools_result", guard._UNSET)
    return monkeypatch


def test_provider_tool_names_come_from_the_provider(provider_lookup):
    mp = provider_lookup
    calls = []

    def load(name):
        calls.append(name)
        return _Provider([{"name": "mem0_search"}, {"type": "function", "function": {"name": "mem0_profile"}}])

    mp.setattr(guard, "_host_config", lambda: {"memory": {"provider": "mem0"}})
    import types, sys

    mp.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    mod = types.ModuleType("plugins.memory")
    mod.load_memory_provider = load
    mp.setitem(sys.modules, "plugins.memory", mod)
    assert guard.memory_provider_tools() == {"mem0_search", "mem0_profile"}
    assert guard.memory_provider_tools() == {"mem0_search", "mem0_profile"}
    assert calls == ["mem0"]  # once per process


@pytest.mark.parametrize("provider", ["", "builtin", "none", None])
def test_builtin_memory_has_no_provider_tools(provider_lookup, provider):
    provider_lookup.setattr(guard, "_host_config", lambda: {"memory": {"provider": provider}})
    assert guard.memory_provider_tools() == frozenset()


@pytest.mark.parametrize("schemas", [None, [], "boom"])
def test_provider_that_cannot_list_its_tools_is_unknown(provider_lookup, caplog, schemas):
    mp = provider_lookup
    import types, sys

    def load(name):
        if schemas == "boom":
            raise RuntimeError("import error")
        return None if schemas is None else _Provider(schemas)

    mp.setattr(guard, "_host_config", lambda: {"memory": {"provider": "honcho"}})
    mp.setitem(sys.modules, "plugins", types.ModuleType("plugins"))
    mod = types.ModuleType("plugins.memory")
    mod.load_memory_provider = load
    mp.setitem(sys.modules, "plugins.memory", mod)
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.host_tools_guard"):
        assert guard.memory_provider_tools() is None
    assert "could not list the tools of memory provider" in caplog.text


def test_unreadable_config_is_unknown_and_retried(provider_lookup):
    mp = provider_lookup
    state = {"fail": True}

    def config():
        if state["fail"]:
            raise OSError("locked")
        return {}

    mp.setattr(guard, "_host_config", config)
    assert guard.memory_provider_tools() is None
    state["fail"] = False
    assert guard.memory_provider_tools() == frozenset()
