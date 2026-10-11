"""The owner's direct chat on hosts that bind no chat type (Hermes < 0.19.1).

Hermes only binds ``HERMES_SESSION_CHAT_TYPE`` into the session context from
0.19.1; 0.12 - 0.19.0 bind platform, chat id and user id only. Turn ownership
(``host_tools_guard``) and the memory read scope (``memory_scope``) used to
require a "dm"/"direct" chat type, so on those hosts every ClawChat turn was
"not the owner's": the owner lost terminal/file tools in their own direct chat
and the memory tools read nothing there.

With no chat type, the chat id decides: the turn is the owner's iff the chat is
the owner's activation conversation (compared case-insensitively) and not a
chat the plugin knows as a group. Anything else - another chat, a group, no
chat id, a failed lookup - is not the owner's. With a chat type present nothing
changes.
"""

from __future__ import annotations

import json

import pytest

from clawchat_gateway import host_tools_guard as guard
from clawchat_gateway import memory_scope, plugin_tools, tools
from clawchat_gateway.clawchat_memory import write_clawchat_memory_body, write_clawchat_metadata

OWNER = "usr_owner"
FRIEND = "usr_friend"
OWNER_DM = "cnv_Owner_DM"  # the activation conversation, as stored
FRIEND_DM = "cnv_friend_dm"
GROUP = "cnv_group"

OWNER_FACT = "Owner private: diagnosed with condition Z"
FRIEND_FACT = "Friend lives in Lisbon"
GROUP_FACT = "Group rule: meet Fridays"

SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_SOURCE",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_USER_ID",
    "HERMES_PLATFORM",
)


def _boom():
    raise RuntimeError("store unavailable")


def _session(monkeypatch, *, chat_id, user_id, chat_type=None):
    """Bind the session the way the host does; ``chat_type=None`` = host < 0.19.1."""
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "clawchat")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)
    if chat_type is not None:
        monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)


# --- host tools (pre_tool_call hook) ------------------------------------------


@pytest.fixture
def host(monkeypatch):
    for key in SESSION_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(guard, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: OWNER_DM)
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: chat_id == GROUP)
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: [])
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)


def _action(tool="read_file"):
    return (guard.clawchat_pre_tool_call(tool_name=tool, args={}) or {}).get("action", "allow")


@pytest.mark.parametrize("chat_id", [OWNER_DM, OWNER_DM.lower(), OWNER_DM.upper()])
@pytest.mark.parametrize("user_id", [OWNER, "clawchat-awareness"])
def test_owner_dm_without_chat_type_keeps_host_tools(host, monkeypatch, chat_id, user_id):
    _session(monkeypatch, chat_id=chat_id, user_id=user_id)
    assert _action("read_file") == "allow"
    assert _action("terminal") == "allow"


def test_empty_chat_type_counts_as_missing(host, monkeypatch):
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER, chat_type="")
    assert _action("terminal") == "allow"


@pytest.mark.parametrize(
    "chat_id,user_id",
    [
        (FRIEND_DM, FRIEND),
        (FRIEND_DM, OWNER),  # the owner's user id alone does not make a chat theirs
        (GROUP, FRIEND),
        (GROUP, OWNER),
    ],
)
def test_other_chats_without_chat_type_are_blocked(host, monkeypatch, chat_id, user_id):
    _session(monkeypatch, chat_id=chat_id, user_id=user_id)
    assert _action("read_file") == "block"
    assert _action("terminal") == "block"


def test_owner_chat_that_is_a_known_group_is_blocked(host, monkeypatch):
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: chat_id.lower() == OWNER_DM.lower())
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert _action("terminal") == "block"


def test_group_lookup_failure_without_chat_type_blocks(host, monkeypatch):
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: _boom())
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert _action("terminal") == "block"


@pytest.mark.parametrize("lookup", [_boom, lambda: "", lambda: None])
def test_owner_chat_unknown_without_chat_type_blocks(host, monkeypatch, lookup):
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lookup)
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert _action("terminal") == "block"


def test_missing_chat_id_without_chat_type_blocks(host, monkeypatch):
    _session(monkeypatch, chat_id="", user_id=OWNER)
    assert _action("terminal") == "block"


def test_toolset_override_with_empty_chat_type_uses_the_chat_id(host, monkeypatch):
    class Source:
        def __init__(self, chat_id, user_id):
            self.chat_id, self.chat_type, self.user_id = chat_id, "", user_id

    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"terminal", "clawchat"})
    lookup = lambda: OWNER_DM  # noqa: E731
    assert guard.toolsets_for_source(Source(OWNER_DM.lower(), OWNER), owner_user_id=OWNER, owner_direct_chat_id=lookup) is None
    assert guard.toolsets_for_source(Source(FRIEND_DM, FRIEND), owner_user_id=OWNER, owner_direct_chat_id=lookup) == [
        "clawchat",
        "no_mcp",
    ]


# chat type present: unchanged (exact chat id match, user id still counts)


def test_with_chat_type_owner_dm_is_unchanged(host, monkeypatch):
    _session(monkeypatch, chat_id=OWNER_DM, user_id="clawchat-awareness", chat_type="dm")
    assert _action("terminal") == "allow"
    _session(monkeypatch, chat_id=FRIEND_DM, user_id=OWNER, chat_type="dm")
    assert _action("terminal") == "allow"


def test_with_chat_type_case_variant_is_unchanged(host, monkeypatch):
    # The exact match the host-typed path always used.
    _session(monkeypatch, chat_id=OWNER_DM.lower(), user_id="clawchat-awareness", chat_type="dm")
    assert _action("terminal") == "block"


@pytest.mark.parametrize("chat_type", ["group", "channel"])
def test_with_chat_type_non_direct_is_unchanged(host, monkeypatch, chat_type):
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER, chat_type=chat_type)
    assert _action("terminal") == "block"


def test_with_chat_type_friend_dm_is_unchanged(host, monkeypatch):
    _session(monkeypatch, chat_id=FRIEND_DM, user_id=FRIEND, chat_type="dm")
    assert _action("terminal") == "block"


# --- memory read scope --------------------------------------------------------


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
    write_clawchat_memory_body(memories, "user", FRIEND, "append", FRIEND_FACT)
    write_clawchat_memory_body(memories, "group", GROUP, "append", GROUP_FACT)
    write_clawchat_metadata(memories, "group", GROUP, {"group_title": "Hikers", "participant_ids": f"{OWNER},{FRIEND}"})
    return memories


async def _read(target_type, target_id):
    return json.loads(
        await plugin_tools.handle_clawchat_memory_read({"targetType": target_type, "targetId": target_id})
    )


async def _search(query):
    result = json.loads(await plugin_tools.handle_clawchat_memory_search({"query": query}))
    return {(m["targetType"], m["targetId"]) for m in result["matches"]}


@pytest.mark.parametrize("chat_id", [OWNER_DM, OWNER_DM.lower()])
@pytest.mark.parametrize("user_id", [OWNER, "clawchat-memory-migration"])
async def test_memory_owner_dm_without_chat_type_reads_everything(root, monkeypatch, chat_id, user_id):
    _session(monkeypatch, chat_id=chat_id, user_id=user_id)
    assert memory_scope.resolve_memory_scope(root).kind == "owner_direct"
    assert OWNER_FACT in (await _read("owner", "owner"))["content"]
    assert GROUP_FACT in (await _read("group", GROUP))["content"]
    assert ("owner", "owner") in await _search("condition Z")


@pytest.mark.parametrize(
    "chat_id,user_id",
    [(FRIEND_DM, FRIEND), (FRIEND_DM, OWNER), (GROUP, OWNER), (GROUP, FRIEND), ("", OWNER)],
)
async def test_memory_other_chats_without_chat_type_never_read_owner_note(root, monkeypatch, chat_id, user_id):
    _session(monkeypatch, chat_id=chat_id, user_id=user_id)
    assert memory_scope.resolve_memory_scope(root).kind != "owner_direct"
    assert (await _read("owner", "owner")).get("error") == "not_readable_here"
    assert ("owner", "owner") not in await _search("condition Z")


async def test_memory_group_without_chat_type_is_still_scoped_to_the_group(root, monkeypatch):
    _session(monkeypatch, chat_id=GROUP, user_id=FRIEND)
    scope = memory_scope.resolve_memory_scope(root)
    assert scope.kind == "group" and scope.chat_id == GROUP
    assert GROUP_FACT in (await _read("group", GROUP))["content"]


async def test_memory_owner_chat_that_is_a_known_group_stays_a_group(root, monkeypatch):
    monkeypatch.setattr(memory_scope, "_owner_direct_chat_id", lambda: GROUP)
    _session(monkeypatch, chat_id=GROUP, user_id=OWNER)
    assert memory_scope.resolve_memory_scope(root).kind == "group"
    assert (await _read("owner", "owner")).get("error") == "not_readable_here"


@pytest.mark.parametrize("lookup", [_boom, lambda: "", lambda: None])
async def test_memory_owner_chat_unknown_without_chat_type_reads_nothing(root, monkeypatch, lookup):
    monkeypatch.setattr(memory_scope, "_owner_direct_chat_id", lookup)
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert memory_scope.resolve_memory_scope(root).kind == "group"
    assert (await _read("owner", "owner")).get("error") == "not_readable_here"


async def test_memory_with_chat_type_is_unchanged(root, monkeypatch):
    _session(monkeypatch, chat_id=OWNER_DM, user_id="clawchat-awareness", chat_type="dm")
    assert memory_scope.resolve_memory_scope(root).kind == "direct"
    _session(monkeypatch, chat_id=FRIEND_DM, user_id=OWNER, chat_type="dm")
    assert memory_scope.resolve_memory_scope(root).kind == "owner_direct"
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER, chat_type="channel")
    assert memory_scope.resolve_memory_scope(root).kind == "group"
    assert memory_scope.resolve_memory_scope(root).members == frozenset()


def test_memory_scope_owner_chat_lookup_reads_the_activation_store(monkeypatch):
    class Store:
        def get_activation_conversation(self, *, platform, account_id):
            assert (platform, account_id) == ("hermes", memory_scope.ACCOUNT_ID)
            return OWNER_DM

    monkeypatch.setattr(memory_scope, "get_clawchat_store", lambda: Store())
    assert memory_scope._owner_direct_chat_id() == OWNER_DM


def test_owner_unknown_without_chat_type_blocks(host, monkeypatch):
    monkeypatch.setattr(guard, "_owner_user_id", lambda: "")
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert _action("terminal") == "block"


async def test_memory_owner_unknown_without_chat_type_reads_nothing(root, monkeypatch):
    monkeypatch.delenv("CLAWCHAT_OWNER_USER_ID")
    monkeypatch.setattr(memory_scope, "_owner_user_id", lambda: "")
    _session(monkeypatch, chat_id=OWNER_DM, user_id=OWNER)
    assert (await _read("owner", "owner")).get("error") == "not_readable_here"
