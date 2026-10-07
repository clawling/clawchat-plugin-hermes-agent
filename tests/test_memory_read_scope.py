"""Which ClawChat notes a conversation may read back through the memory tools.

A tool result becomes part of the calling session's history. A group is one
Hermes session shared by everyone in it and kept across turns, so whatever a
memory read or search returns there can be surfaced to anyone in the group
later, even when the reply that triggered it held it back. So the tools judge
every read by the conversation the call comes from (the host's session
context: platform, chat id, chat type, user id):

* the owner's direct chat, or a local surface (CLI, no gateway session in
  this process): every note;
* any other direct chat: every note except ``owner.md``;
* a group: that group's note, and the notes of people who are members of it
  (its cached participant list; recent speakers only when no list is known) —
  never ``owner.md``, never another group's note, never a note about someone
  outside the group;
* anything the plugin cannot place (another platform, no chat id, an unknown
  chat type, a gateway process with no session bound): like a group with no
  known members.

Search drops what the conversation may not read before it ranks or counts.
Writes keep the routing rule (``owner.md`` takes facts about the owner from
anywhere): ``mode=append`` is allowed for any target, but ``mode=replace`` and
``clawchat_memory_edit`` — which read or wipe what is there — only for notes
the conversation may read, and an append to an unreadable note does not say
whether its text was already there.
"""

from __future__ import annotations

import json
import sys
import types
from contextvars import ContextVar

import pytest

from clawchat_gateway import memory_scope, plugin_tools, tools
from clawchat_gateway.clawchat_memory import (
    read_clawchat_memory_file,
    write_clawchat_memory_body,
    write_clawchat_metadata,
)

AGENT = "usr_agent"
OWNER = "usr_owner"
ADA = "usr_ada"
BEN = "usr_ben"
OUTSIDER = "usr_outsider"
DM = "cnv_dm"
GROUP = "cnv_group"
OTHER_GROUP = "cnv_other"

OWNER_PRIVATE_FACT = "Owner private: diagnosed with condition Z"
ADA_FACT = "Ada lives in Lisbon"
OUTSIDER_FACT = "Outsider owes the bank money"
GROUP_FACT = "Group rule: meet Fridays"
OTHER_GROUP_FACT = "Other group plans a surprise for Ada"

SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_USER_ID",
    "HERMES_PLATFORM",
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    memories = tmp_path / "memories"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CLAWCHAT_OWNER_USER_ID", OWNER)
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(tools, "_resolve_memory_root", lambda: (memories, None))
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: False)
    write_clawchat_memory_body(memories, "owner", "owner", "append", OWNER_PRIVATE_FACT)
    write_clawchat_memory_body(memories, "user", ADA, "append", ADA_FACT)
    write_clawchat_memory_body(memories, "user", OUTSIDER, "append", OUTSIDER_FACT)
    write_clawchat_memory_body(memories, "group", GROUP, "append", GROUP_FACT)
    write_clawchat_memory_body(memories, "group", OTHER_GROUP, "append", OTHER_GROUP_FACT)
    write_clawchat_metadata(
        memories, "group", GROUP, {"group_title": "Hikers", "participant_ids": f"{OWNER},{ADA},{BEN},{AGENT}"}
    )
    write_clawchat_metadata(
        memories, "group", OTHER_GROUP, {"group_title": "Party", "participant_ids": f"{OWNER},{OUTSIDER}"}
    )
    return memories


def _session(monkeypatch, *, chat_id, chat_type, user_id="", platform="clawchat"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)


def in_group(monkeypatch, chat_id=GROUP):
    # A shared group session is keyed without a speaker.
    _session(monkeypatch, chat_id=chat_id, chat_type="group", user_id="__group_shared__")


def in_owner_dm(monkeypatch):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=OWNER)


def in_friend_dm(monkeypatch):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=ADA)


async def _call(handler, args):
    return json.loads(await handler(args))


async def read(target_type, target_id):
    return await _call(
        plugin_tools.handle_clawchat_memory_read, {"targetType": target_type, "targetId": target_id}
    )


async def search(query, **extra):
    return await _call(plugin_tools.handle_clawchat_memory_search, {"query": query, **extra})


def _found(result):
    return {(m["targetType"], m["targetId"]) for m in result["matches"]}


# --- group: owner.md never comes back -------------------------------------


@pytest.mark.asyncio
async def test_group_read_of_owner_note_is_refused_without_content(root, monkeypatch):
    in_group(monkeypatch)
    result = await read("owner", "owner")
    assert result.get("error") == "not_readable_here"
    assert "condition Z" not in json.dumps(result)
    assert "owner" in result["message"].lower()


@pytest.mark.asyncio
async def test_group_search_leaves_out_owner_note(root, monkeypatch):
    in_group(monkeypatch)
    result = await search("condition Z")
    assert result["matches"] == []
    assert OWNER_PRIVATE_FACT not in json.dumps(result)
    # The model is told why, so it does not go looking another way.
    assert "owner" in result["scopeNote"].lower()


@pytest.mark.asyncio
async def test_group_search_limited_to_owner_finds_nothing(root, monkeypatch):
    in_group(monkeypatch)
    result = await search("private", targetTypes=["owner"])
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_legacy_per_speaker_group_session_is_still_a_group(root, monkeypatch):
    # Per-speaker group sessions carry the real speaker as the session user —
    # even the owner speaking in a group must not open owner.md there.
    _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id=OWNER)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"


# --- group: members yes, outsiders and other groups no ---------------------


@pytest.mark.asyncio
async def test_group_reads_its_own_note_and_members_notes(root, monkeypatch):
    in_group(monkeypatch)
    assert GROUP_FACT in (await read("group", GROUP))["content"]
    assert ADA_FACT in (await read("user", ADA))["content"]


@pytest.mark.asyncio
async def test_group_cannot_read_a_non_member_or_another_group(root, monkeypatch):
    in_group(monkeypatch)
    outsider = await read("user", OUTSIDER)
    other = await read("group", OTHER_GROUP)
    assert outsider.get("error") == "not_readable_here"
    assert other.get("error") == "not_readable_here"
    assert OUTSIDER_FACT not in json.dumps(outsider)
    assert OTHER_GROUP_FACT not in json.dumps(other)


@pytest.mark.asyncio
async def test_group_search_keeps_only_this_group_and_its_members(root, monkeypatch):
    in_group(monkeypatch)
    # A word every note shares.
    write_clawchat_memory_body(root, "owner", "owner", "append", "keyword-shared")
    for target_type, target_id in (("user", ADA), ("user", OUTSIDER), ("group", GROUP), ("group", OTHER_GROUP)):
        write_clawchat_memory_body(root, target_type, target_id, "append", "keyword-shared")
    result = await search("keyword-shared")
    assert _found(result) == {("user", ADA), ("group", GROUP)}


@pytest.mark.asyncio
async def test_group_without_a_member_list_falls_back_to_recent_speakers(root, monkeypatch):
    write_clawchat_metadata(root, "group", GROUP, {"group_title": "Hikers", "participant_ids": ""})

    class Store:
        def list_recent_group_transcript(self, account_id, chat_id, limit):
            assert chat_id == GROUP
            return [{"direction": "inbound", "sender_id": ADA}, {"direction": "outbound", "sender_id": ""}]

    monkeypatch.setattr(memory_scope, "get_clawchat_store", lambda: Store())
    in_group(monkeypatch)
    assert ADA_FACT in (await read("user", ADA))["content"]
    assert (await read("user", OUTSIDER)).get("error") == "not_readable_here"


# --- direct chats -----------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_dm_reads_and_searches_owner_note(root, monkeypatch):
    in_owner_dm(monkeypatch)
    assert OWNER_PRIVATE_FACT in (await read("owner", "owner"))["content"]
    assert ("owner", "owner") in _found(await search("condition Z"))
    assert "scopeNote" not in await search("condition Z")


@pytest.mark.asyncio
async def test_owner_dm_keeps_reading_any_note(root, monkeypatch):
    in_owner_dm(monkeypatch)
    assert OUTSIDER_FACT in (await read("user", OUTSIDER))["content"]
    assert OTHER_GROUP_FACT in (await read("group", OTHER_GROUP))["content"]


@pytest.mark.asyncio
async def test_someone_elses_dm_cannot_read_owner_note(root, monkeypatch):
    in_friend_dm(monkeypatch)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"
    assert ("owner", "owner") not in _found(await search("condition Z"))
    assert ADA_FACT in (await read("user", ADA))["content"]


@pytest.mark.asyncio
async def test_a_dm_id_that_is_a_known_group_is_treated_as_a_group(root, monkeypatch):
    # The host says "dm", but the plugin knows this chat as a group (its
    # cached participant list): the more restrictive reading wins.
    _session(monkeypatch, chat_id=GROUP, chat_type="dm", user_id=OWNER)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"


@pytest.mark.asyncio
async def test_owner_dm_needs_a_known_owner(root, monkeypatch):
    monkeypatch.delenv("CLAWCHAT_OWNER_USER_ID")
    monkeypatch.setattr(memory_scope, "_owner_user_id", lambda: "")
    in_owner_dm(monkeypatch)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"


# --- unknown context is restrictive -----------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,chat_id,chat_type",
    [
        ("clawchat", GROUP, ""),  # chat type missing
        ("clawchat", GROUP, "channel"),  # chat type the plugin never sets
        ("clawchat", "", "dm"),  # no chat id
        ("telegram", "12345", "dm"),  # another platform's chat
    ],
)
async def test_unplaceable_session_is_treated_as_a_group(root, monkeypatch, platform, chat_id, chat_type):
    _session(monkeypatch, chat_id=chat_id, chat_type=chat_type, user_id=OWNER, platform=platform)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"
    assert ("owner", "owner") not in _found(await search("condition Z"))


@pytest.mark.asyncio
async def test_gateway_process_with_no_session_bound_is_restrictive(root, monkeypatch):
    # In the gateway, a tool call that lost its session context (empty vars)
    # must not be mistaken for the local CLI.
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"


@pytest.mark.asyncio
async def test_local_surface_reads_everything(root):
    # `hermes chat` on the agent's own machine: the operator owns these files.
    assert OWNER_PRIVATE_FACT in (await read("owner", "owner"))["content"]
    assert "scopeNote" not in await search("condition Z")


@pytest.mark.asyncio
async def test_host_context_vars_win_over_process_env(root, monkeypatch):
    # Hermes binds the session per task (ContextVars); os.environ may hold a
    # stale or other chat's values. A group in the context var must win over
    # an owner DM left in the environment.
    in_owner_dm(monkeypatch)
    vars_ = {
        "HERMES_SESSION_PLATFORM": "clawchat",
        "HERMES_SESSION_CHAT_ID": GROUP,
        "HERMES_SESSION_CHAT_TYPE": "group",
        "HERMES_SESSION_USER_ID": "__group_shared__",
    }
    module = types.ModuleType("gateway.session_context")
    module.get_session_env = lambda name, default="": vars_.get(name, default)
    module.session_context_engaged = lambda: True
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)
    monkeypatch.setattr(memory_scope, "_session_context_engaged", memory_scope._host_session_context_engaged)
    assert (await read("owner", "owner")).get("error") == "not_readable_here"


# --- writes -----------------------------------------------------------------


async def write(target_type, target_id, mode, content):
    return await _call(
        plugin_tools.handle_clawchat_memory_write,
        {"targetType": target_type, "targetId": target_id, "mode": mode, "content": content},
    )


async def edit(target_type, target_id, old, new):
    return await _call(
        plugin_tools.handle_clawchat_memory_edit,
        {"targetType": target_type, "targetId": target_id, "oldText": old, "newText": new},
    )


def _body(root, target_type, target_id):
    return read_clawchat_memory_file(root, target_type, target_id)["body"]


@pytest.mark.asyncio
async def test_group_may_append_a_fact_about_the_owner(root, monkeypatch):
    in_group(monkeypatch)
    result = await write("owner", "owner", "append", "Owner said in Hikers: moving to Berlin")
    assert result.get("ok") is True
    assert "moving to Berlin" in _body(root, "owner", "owner")
    assert OWNER_PRIVATE_FACT in _body(root, "owner", "owner")


@pytest.mark.asyncio
async def test_group_cannot_replace_or_edit_the_owner_note(root, monkeypatch):
    in_group(monkeypatch)
    replaced = await write("owner", "owner", "replace", "wiped")
    edited = await edit("owner", "owner", "condition Z", "nothing")
    assert replaced.get("error") == "not_readable_here"
    assert edited.get("error") == "not_readable_here"
    assert OWNER_PRIVATE_FACT in _body(root, "owner", "owner")


@pytest.mark.asyncio
async def test_group_append_to_owner_note_does_not_confirm_existing_text(root, monkeypatch):
    # "already in this note" would let anyone in the group test guesses.
    in_group(monkeypatch)
    result = await write("owner", "owner", "append", OWNER_PRIVATE_FACT)
    assert result.get("ok") is True
    assert "skippedDuplicateParagraphs" not in result and "note" not in result
    assert _body(root, "owner", "owner").count(OWNER_PRIVATE_FACT) == 1


@pytest.mark.asyncio
async def test_group_can_still_edit_its_own_and_members_notes(root, monkeypatch):
    in_group(monkeypatch)
    assert (await edit("group", GROUP, "Fridays", "Saturdays")).get("ok") is True
    assert (await write("user", ADA, "replace", "Ada lives in Porto")).get("ok") is True
    assert (await write("user", ADA, "append", "Ada lives in Porto")).get("skippedDuplicateParagraphs") == 1


@pytest.mark.asyncio
async def test_owner_dm_may_replace_the_owner_note(root, monkeypatch):
    in_owner_dm(monkeypatch)
    assert (await write("owner", "owner", "replace", "fresh")).get("ok") is True
    assert _body(root, "owner", "owner") == "fresh"


# --- the tool descriptions say so -------------------------------------------


def _description(name):
    registered = {}

    class Ctx:
        def register_tool(self, tool_name, toolset, schema, handler, **_kw):
            registered[tool_name] = schema

        def __getattr__(self, _attr):
            return lambda *a, **k: None

    plugin_tools.register_tools(Ctx())
    return registered[name]["description"]


@pytest.mark.parametrize("name", ["clawchat_memory_read", "clawchat_memory_search"])
def test_read_tool_descriptions_state_the_group_rule(name):
    text = _description(name).lower()
    assert "owner.md" in text and "group" in text
    assert "member" in text
