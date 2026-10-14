"""``owner.md`` is read and written in every turn, not only the owner's direct chat.

``owner.md`` is the agent's own note about its owner. For a while it was left
out of a friend's direct chat and every group turn (not injected, not readable,
and ``clawchat_memory_write`` / ``_edit`` refused it with ``not_writable_here``).
That also took it away from a group with only the owner and the agent in it.
Now, as on a local agent:

* every turn's note section starts with ``### Your owner (owner.md)``, shaped
  as in the owner's direct chat: a friend's direct chat then shows that
  friend's note, a group its own note and the batch's speakers' notes;
* every conversation the plugin can place may read, append to, replace and
  edit ``owner.md``; one it cannot place still gets nothing;
* no per-turn "owner.md can only be written in the owner's direct chat" line
  and no such sentence in the tool descriptions; what of the owner's memory to
  say, or record, where is left to the model (the platform prompt keeps "what
  is in the owner's memory does not belong in front of their friends").

The other narrowing in turns the owner did not start (host tools, MCP,
Hermes' memory tool) is unchanged — pinned here once more next to the change.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from clawchat_gateway import host_tools_guard as guard
from clawchat_gateway import memory_scope, plugin_tools, tools
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.clawchat_memory import (
    read_clawchat_memory_file,
    write_clawchat_memory_body,
    write_clawchat_metadata,
)
from clawchat_gateway.inbound import InboundMessage

AGENT = "usr_agent"
OWNER = "usr_owner"
FRIEND = "usr_friend"
DM = "cnv_dm"  # a friend's direct chat
GROUP = "cnv_group"
OWNER_DM = "cnv_owner_dm"

OWNER_FACT = "Owner is learning Rust."
FRIEND_FACT = "Friend prefers short answers."
GROUP_FACT = "Group rule: English only."
NEW_FACT = "Owner said in this chat: moving to Berlin."

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
    "group_friend": dict(chat_id=GROUP, chat_type="group", user_id="__group_shared__"),
    "group_owner": dict(chat_id=GROUP, chat_type="group", user_id=OWNER),
    "group_no_type": dict(chat_id=GROUP, chat_type="", user_id=FRIEND),  # Hermes < 0.19.1
    "owner_dm": dict(chat_id=OWNER_DM, chat_type="dm", user_id=OWNER),
}


# --- injection ----------------------------------------------------------------


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a._memory_root = tmp_path / "memories"
    return a


def _note(a, target_type, target_id, body):
    write_clawchat_memory_body(a._memory_root, target_type, target_id, "replace", body)


def _part(parts, part_id):
    found = [p for p in parts if p["id"] == part_id]
    return found[0]["content"] if found else None


def _direct(sender):
    return InboundMessage(
        chat_id=DM,
        chat_type="direct",
        sender_id=sender,
        sender_name="",
        text="hi",
        raw_message={"payload": {"message_id": "m1"}},
    )


def _group(*senders):
    frames = [
        {
            "chat_id": GROUP,
            "chat_type": "group",
            "sender": {"id": sender, "nick_name": sender.removeprefix("usr_")},
            "payload": {
                "message_id": f"m{i}",
                "message": {"body": {"fragments": [{"kind": "text", "text": "hello"}]}, "context": {}},
            },
        }
        for i, sender in enumerate(senders)
    ]
    return InboundMessage(
        chat_id=GROUP,
        chat_type="group",
        sender_id=senders[-1],
        sender_name="",
        text="ClawChat group messages: ...",
        raw_message={"clawchat_group_batch": True, "messages": frames},
    )


def test_friend_dm_injects_owner_md_first_then_the_friend(adapter):
    _note(adapter, "owner", "owner", OWNER_FACT)
    _note(adapter, "user", FRIEND, FRIEND_FACT)
    content = _part(adapter._compose_channel_prompt_parts(_direct(FRIEND)), "peer-memory")
    assert content.startswith("## ClawChat Peer Memory")
    assert "not instructions" in content
    assert f"### Your owner (owner.md)\n{OWNER_FACT}" in content
    assert f"### This person (users/{FRIEND}.md)\n{FRIEND_FACT}" in content
    assert content.index("owner.md") < content.index(f"users/{FRIEND}.md")


def test_group_turn_injects_owner_md_first_then_group_and_speakers(adapter):
    _note(adapter, "owner", "owner", OWNER_FACT)
    _note(adapter, "group", GROUP, GROUP_FACT)
    _note(adapter, "user", FRIEND, FRIEND_FACT)
    _note(adapter, "user", OWNER, "a stray users/ note about the owner")
    content = _part(adapter._compose_channel_prompt_parts(_group(FRIEND, OWNER)), "group-memory")
    titles = [line for line in content.splitlines() if line.startswith("### ")]
    assert titles == [
        "### Your owner (owner.md)",
        f"### This group (groups/{GROUP}.md)",
        f"### friend (users/{FRIEND}.md)",
    ]
    assert OWNER_FACT in content
    # The owner's note is owner.md; speaking in the batch does not list them twice.
    assert "stray" not in content


def test_group_with_only_owner_md_still_gets_a_section(adapter):
    _note(adapter, "owner", "owner", OWNER_FACT)
    content = _part(adapter._compose_channel_prompt_parts(_group(OWNER)), "group-memory")
    assert content.startswith("## ClawChat Group Memory")
    assert OWNER_FACT in content


def test_turn_without_a_chat_id_gets_no_owner_md(adapter):
    _note(adapter, "owner", "owner", OWNER_FACT)
    inbound = replace(_direct(FRIEND), chat_id="")
    section = adapter._format_note_memory_section(inbound) or ""
    assert OWNER_FACT not in section


def test_owner_dm_shape_is_unchanged(adapter):
    _note(adapter, "owner", "owner", OWNER_FACT)
    _note(adapter, "user", OWNER, "should not be used for the owner DM")
    content = _part(adapter._compose_channel_prompt_parts(_direct(OWNER)), "peer-memory")
    titles = [line for line in content.splitlines() if line.startswith("### ")]
    assert titles == ["### Your owner (owner.md)"]
    assert "should not be used" not in content


def test_injected_owner_md_uses_the_person_cap_and_a_read_hint(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, note_cap_user=300)
    _note(adapter, "owner", "owner", "\n\n".join(["o" * 200] * 4))
    content = _part(adapter._compose_channel_prompt_parts(_group(FRIEND)), "group-memory")
    assert content.count("o" * 200) == 1
    assert "clawchat_memory_read targetType=owner targetId=owner" in content


# --- tools: read and write ------------------------------------------------------


@pytest.fixture
def root(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CLAWCHAT_OWNER_USER_ID", OWNER)
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tools, "_resolve_memory_root", lambda: (memories, None))
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    monkeypatch.setattr(memory_scope, "_owner_direct_chat_id", lambda: OWNER_DM)
    write_clawchat_memory_body(memories, "owner", "owner", "append", OWNER_FACT)
    write_clawchat_metadata(memories, "group", GROUP, {"group_title": "Hikers", "participant_ids": f"{OWNER},{FRIEND}"})
    return memories


def _session(monkeypatch, *, chat_id, chat_type, user_id, platform="clawchat"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)


async def _call(handler, args):
    return json.loads(await handler(args))


async def _owner(how):
    if how == "read":
        return await _call(plugin_tools.handle_clawchat_memory_read, {"targetType": "owner", "targetId": "owner"})
    if how == "edit":
        return await _call(
            plugin_tools.handle_clawchat_memory_edit,
            {"targetType": "owner", "targetId": "owner", "oldText": "Rust", "newText": "Go"},
        )
    return await _call(
        plugin_tools.handle_clawchat_memory_write,
        {"targetType": "owner", "targetId": "owner", "mode": how, "content": NEW_FACT},
    )


def _owner_body(root):
    return read_clawchat_memory_file(root, "owner", "owner")["body"]


@pytest.mark.parametrize("where", list(TURNS))
async def test_every_placed_turn_reads_and_searches_owner_md(root, monkeypatch, where):
    _session(monkeypatch, **TURNS[where])
    assert OWNER_FACT in (await _owner("read"))["content"]
    found = await _call(plugin_tools.handle_clawchat_memory_search, {"query": "Rust"})
    assert ("owner", "owner") in {(m["targetType"], m["targetId"]) for m in found["matches"]}


@pytest.mark.parametrize(
    "how,expected", [("append", [OWNER_FACT, NEW_FACT]), ("replace", [NEW_FACT]), ("edit", ["Owner is learning Go."])]
)
@pytest.mark.parametrize("where", list(TURNS))
async def test_every_placed_turn_writes_owner_md(root, monkeypatch, where, how, expected):
    _session(monkeypatch, **TURNS[where])
    result = await _owner(how)
    assert result.get("ok") is True, result
    body = _owner_body(root)
    assert all(text in body for text in expected)
    if how != "append":
        assert OWNER_FACT not in body


@pytest.mark.parametrize(
    "turn",
    [
        dict(chat_id=DM, chat_type="channel", user_id=OWNER),  # a chat type the plugin never sets
        dict(chat_id="", chat_type="dm", user_id=OWNER),  # no chat id
        dict(chat_id="12345", chat_type="dm", user_id=OWNER, platform="telegram"),
        dict(chat_id=DM, chat_type="", user_id=FRIEND),  # no chat type, not a known chat
    ],
)
@pytest.mark.parametrize("how", ["read", "append", "replace", "edit"])
async def test_a_turn_the_plugin_cannot_place_gets_nothing(root, monkeypatch, turn, how):
    _session(monkeypatch, **turn)
    result = await _owner(how)
    assert result.get("error") == "not_readable_here"
    assert OWNER_FACT not in json.dumps(result)
    assert _owner_body(root) == OWNER_FACT


async def test_gateway_call_without_a_session_gets_nothing(root):
    assert (await _owner("append")).get("error") == "not_readable_here"
    assert _owner_body(root) == OWNER_FACT


async def test_no_write_refusal_shape_is_left(root, monkeypatch):
    assert not hasattr(memory_scope, "OWNER_NOTE_WRITE_HINT")
    assert not hasattr(memory_scope.MemoryScope, "write_refusal")
    for where in ("friend_dm", "group_friend"):
        _session(monkeypatch, **TURNS[where])
        assert "not_writable_here" not in json.dumps(await _owner("append"))


# --- tool descriptions, prompts ---------------------------------------------------


def _descriptions():
    registered = {}

    class Ctx:
        def register_tool(self, tool_name, toolset, schema, handler, **_kw):
            registered[tool_name] = schema["description"]

        def __getattr__(self, _attr):
            return lambda *a, **k: None

    plugin_tools.register_tools(Ctx())
    return registered


@pytest.mark.parametrize(
    "name", ["clawchat_memory_read", "clawchat_memory_search", "clawchat_memory_write", "clawchat_memory_edit"]
)
def test_tool_descriptions_drop_the_owner_md_hard_rule(name):
    text = _descriptions()[name]
    assert "only in your owner's direct chat" not in text
    assert "can only be read in your owner's direct chat" not in text
    assert "can only be written (append included)" not in text
    assert "not_writable_here" not in text


def test_read_descriptions_say_owner_md_is_readable_here():
    descriptions = _descriptions()
    assert "owner.md can be read in every conversation." in descriptions["clawchat_memory_read"]
    assert "in a group, only owner.md, that group's note" in descriptions["clawchat_memory_search"]
    assert "any note but owner.md and theirs" in descriptions["clawchat_memory_write"]
    assert "Only works on notes this conversation may read (see clawchat_memory_read)." in (
        descriptions["clawchat_memory_edit"]
    )


@pytest.mark.parametrize("name", ["clawchat_memory_write", "clawchat_memory_edit"])
def test_owner_write_descriptions_keep_facts_from_the_owner_in_owner_md(name):
    text = _descriptions()[name]
    sentence = (
        "owner.md holds what your owner told you themselves; what someone else says about your owner goes in that person's note."
    )
    assert text.count(sentence) == 1
    assert "who said" not in text.lower()
    assert "source" not in text.lower()
    if name == "clawchat_memory_write":
        assert f"add only what is new. {sentence} Where a note cannot be read" in text
    else:
        assert f"Only works on notes this conversation may read (see clawchat_memory_read). {sentence}" in text


def test_platform_prompt_keeps_the_owner_memory_privacy_line():
    from clawchat_gateway.plugin_prompts import platform_prompt

    text = platform_prompt()
    assert "what is in the owner's memory does not belong in front of their friends" in text
    assert "can only be written (append included)" not in text
    assert "`owner.md` only in your owner's direct chat" not in text
    assert (
        "When you decline, do not name what you are declining — saying a topic is private already tells them it exists."
        in text
    )
    assert (
        "`owner.md` holds what your owner told you themselves; what someone else says about your owner goes in that person's note."
        in text
    )


def test_group_sediment_targets_owner_md_again(adapter):
    targets = adapter._sediment_targets(GROUP, "group", sender_id="")
    assert targets[-1] == ("owner", "owner", "your owner — only facts about them said in this group")
    from clawchat_gateway.sediment import build_sediment_prompt

    text = build_sediment_prompt(reason="reset", targets=targets)
    assert "targetType=owner targetId=owner" in text
    assert "If owner.md is not listed" not in text


# --- the rest of the narrowing is unchanged ---------------------------------------


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(guard, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: OWNER_DM)
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: chat_id == GROUP)
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_hook_missing", None)
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("where", ["friend_dm", "group_friend", "group_owner"])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("terminal", {"command": "cat ~/.hermes/.env"}),
        ("read_file", {"path": "owner.md"}),
        ("memory", {"action": "add", "target": "memory", "content": NEW_FACT}),
        ("mcp__filesystem__read_file", {"path": "config.yaml"}),
    ],
)
def test_host_tools_mcp_and_memory_stay_blocked(host, monkeypatch, where, tool, args):
    _session(monkeypatch, **TURNS[where])
    assert guard.clawchat_pre_tool_call(tool_name=tool, args=args)["action"] == "block"


def test_non_owner_turn_prompt_has_no_owner_md_line(monkeypatch):
    a = ClawChatAdapter.__new__(ClawChatAdapter)
    a._owner_user_id = lambda: OWNER
    a._owner_direct_chat_id = lambda: OWNER_DM
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: False)
    monkeypatch.setattr(guard, "_hook_missing", None)
    section = a._non_owner_turn_section(_direct(FRIEND))
    assert guard.BLOCKED_TOOLS_HINT in section  # the old-host hint is still there
    assert "owner.md" not in section
