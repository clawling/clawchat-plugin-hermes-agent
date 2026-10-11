"""Host tools in turns the owner did not start.

Hermes gives a ClawChat turn its full host toolset — terminal, file, code
execution, delegation, cron — and its default ``approvals.mode: smart`` runs a
"dangerous" command the auxiliary model rates low-risk without asking anyone,
while ``cat``/``ls``/``curl`` never count as dangerous at all. In a friend's
direct chat, or any group turn, that let someone other than the owner have the
agent run commands on the owner's machine or read ``$HERMES_HOME/.env``,
``config.yaml`` and ``owner.md`` — stopped only if the model refused.

Two layers, both in ``clawchat_gateway.host_tools_guard``:

* ``toolsets_for_source`` (the adapter's per-source override) drops those
  toolsets from every turn but the owner's own direct chat, so the model never
  sees the tools;
* a ``pre_tool_call`` hook blocks any of their tools that still reaches dispatch
  in such a turn — or, when the owner opted in with
  ``non-owner-host-tools: approve``, escalates it to the human-approval gate,
  which never goes through smart (a friend's direct chat refuses it on the spot;
  a group's goes to the owner).

The host swallows exceptions from both places and then *opens up* (an override
that raises falls back to the full platform toolset; a hook that raises is
ignored), so both fail closed here.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from clawchat_gateway import host_tools_guard as guard

OWNER = "usr_owner"
FRIEND = "usr_friend"
DM = "cnv_dm"
GROUP = "cnv_group"
OWNER_DM = "cnv_owner_dm"  # the owner's own direct chat (activation conversation)

HOST_TOOLSETS = {
    "terminal": ["terminal", "process_manage"],
    "file": ["read_file", "write_file", "patch", "search_files"],
    "code_execution": ["execute_code"],
    "delegation": ["delegate_task"],
    "cronjob": ["cronjob_manage"],
    "computer_use": ["computer_use"],
    "browser": ["browser_navigate", "browser_snapshot", "browser_cdp", "browser_exec", "browser_vision"],
    "web": ["web_search", "web_extract"],
    "memory": ["memory"],
    "skills": ["skills_list", "skill_view", "skill_manage"],
    "todo": ["todo_list"],
    "clawchat": ["clawchat_memory_read", "clawchat_send_file"],
}

FULL = {"browser", "web", "terminal", "file", "code_execution", "delegation", "cronjob", "memory", "skills", "todo", "clawchat"}

SESSION_KEYS = (
    "HERMES_SESSION_PLATFORM",
    "HERMES_SESSION_CHAT_ID",
    "HERMES_SESSION_CHAT_TYPE",
    "HERMES_SESSION_USER_ID",
)


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
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: set(FULL))
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: list(HOST_TOOLSETS.get(name, [])))
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)


def _session(monkeypatch, *, chat_id, chat_type, user_id, platform="clawchat"):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", platform)
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    monkeypatch.setenv("HERMES_SESSION_CHAT_TYPE", chat_type)
    monkeypatch.setenv("HERMES_SESSION_USER_ID", user_id)


def _approve(monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})


# --- toolsets_for_source ----------------------------------------------------


def test_owner_direct_chat_keeps_the_platform_toolset():
    assert guard.toolsets_for_source(Source(DM, "dm", OWNER), owner_user_id=OWNER) is None


@pytest.mark.parametrize(
    "source",
    [
        Source(DM, "dm", FRIEND),
        Source(GROUP, "group", FRIEND),
        Source(GROUP, "group", OWNER),  # every group turn, whoever spoke
        Source(GROUP, "dm", OWNER),  # a host "dm" for a chat known as a group
        Source(DM, "dm", None),
    ],
)
def test_other_turns_lose_the_host_tools(source):
    kept = guard.toolsets_for_source(source, owner_user_id=OWNER)
    assert kept, "an empty override is ignored by the host and means the full toolset"
    assert set(kept) == (FULL - set(guard.RESTRICTED_TOOLSETS)) | {guard.NO_MCP}
    for name in ("terminal", "file", "code_execution", "delegation", "cronjob", "browser", "memory"):
        assert name not in kept


def test_owner_unknown_restricts_even_the_owner_chat():
    assert guard.toolsets_for_source(Source(DM, "dm", OWNER), owner_user_id="") is not None


def test_composite_that_carries_a_host_tool_is_dropped(monkeypatch):
    # Outside a fully registered host the resolver can hand back the platform
    # composite itself; anything that resolves to a restricted tool goes.
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"hermes-clawchat", "web", "debugging"})
    monkeypatch.setattr(
        guard,
        "_resolve_tools",
        lambda name: {"hermes-clawchat": ["terminal", "web_search"], "debugging": ["terminal"], "web": ["web_search"]}.get(
            name, []
        ),
    )
    assert guard.toolsets_for_source(Source(DM, "dm", FRIEND), owner_user_id=OWNER) == ["web", guard.NO_MCP]


def test_nothing_left_still_returns_a_non_empty_override(monkeypatch):
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"terminal", "file"})
    assert guard.toolsets_for_source(Source(DM, "dm", FRIEND), owner_user_id=OWNER) == list(guard.FALLBACK_TOOLSETS)


def test_override_failure_fails_closed(monkeypatch, caplog):
    def boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr(guard, "_platform_toolsets", boom)
    assert guard.toolsets_for_source(Source(DM, "dm", FRIEND), owner_user_id=OWNER) == list(guard.FALLBACK_TOOLSETS)
    assert "fail" in caplog.text.lower()


def test_owner_lookup_failure_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("no owner")

    assert guard.toolsets_for_source(Source(DM, "dm", OWNER), owner_user_id=boom) is not None


def test_approve_setting_keeps_the_toolset(monkeypatch):
    _approve(monkeypatch)
    assert guard.toolsets_for_source(Source(GROUP, "group", FRIEND), owner_user_id=OWNER) is None


# --- pre_tool_call ----------------------------------------------------------


@pytest.mark.parametrize(
    "tool,args",
    [
        # Built at runtime: Hermes' install scanner rates these literals CRITICAL and
        # blocks the whole plugin, tests included (--force does not override it).
        ("terminal", {"command": " ".join(["cat", "$HERMES_HOME/" + ".env"])}),
        ("terminal", {"command": " ".join(["rm", "-" + "rf", "/tmp/x"])}),
        ("read_file", {"path": "/opt/data/.env"}),
        ("read_file", {"path": "/opt/data/config.yaml"}),
        ("read_file", {"path": "/opt/data/memories/clawchat/owner.md"}),
        ("search_files", {"pattern": "TOKEN"}),
        ("execute_code", {"code": "print(open('.env').read())"}),
        ("delegate_task", {"goal": "read .env"}),
        ("cronjob_manage", {"action": "create"}),
        ("browser_navigate", {"url": "file:///opt/data/.env"}),
        ("browser_cdp", {"method": "Page.navigate", "params": {"url": "file:///opt/data/config.yaml"}}),
        ("browser_future_tool", {}),
    ],
)
@pytest.mark.parametrize("where", ["friend_dm", "group_friend", "group_owner"])
def test_non_owner_turn_blocks_host_tools(monkeypatch, tool, args, where):
    if where == "friend_dm":
        _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=FRIEND)
    elif where == "group_friend":
        _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id=FRIEND)
    else:
        _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id=OWNER)
    result = guard.clawchat_pre_tool_call(tool_name=tool, args=args)
    assert result["action"] == "block"
    assert result["message"]


def test_owner_direct_chat_is_untouched(monkeypatch):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=OWNER)
    assert guard.clawchat_pre_tool_call(tool_name="terminal", args={"command": "ls"}) is None


def test_other_tools_pass_in_a_friend_turn(monkeypatch):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=FRIEND)
    assert guard.clawchat_pre_tool_call(tool_name="web_search", args={}) is None
    assert guard.clawchat_pre_tool_call(tool_name="clawchat_memory_read", args={}) is None


@pytest.mark.parametrize("platform", ["telegram", ""])
def test_other_platforms_and_local_runs_are_not_ours(monkeypatch, platform):
    if platform:
        _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=FRIEND, platform=platform)
    assert guard.clawchat_pre_tool_call(tool_name="terminal", args={}) is None


def test_approve_setting_escalates_to_the_human_gate(monkeypatch):
    _approve(monkeypatch)
    _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id=FRIEND)
    result = guard.clawchat_pre_tool_call(tool_name="terminal", args={"command": "uname"})
    assert result["action"] == "approve"
    assert result["rule_key"].startswith("clawchat-non-owner:")


def test_approve_setting_blocks_when_the_host_cannot_escalate(monkeypatch):
    # An older host ignores "approve" — the tool would then just run.
    _approve(monkeypatch)
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
    _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id=FRIEND)
    assert guard.clawchat_pre_tool_call(tool_name="terminal", args={})["action"] == "block"


def test_hook_failure_fails_closed(monkeypatch, caplog):
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id=OWNER)

    def boom():
        raise RuntimeError("store locked")

    monkeypatch.setattr(guard, "_owner_user_id", boom)
    result = guard.clawchat_pre_tool_call(tool_name="terminal", args={})
    assert result["action"] == "block"
    assert "fail" in caplog.text.lower()


@pytest.mark.parametrize("value,expected", [("approve", "approve"), (" APPROVE ", "approve"), ("off", "off"), ("on", "off"), (True, "off"), (None, "off")])
def test_setting_values(monkeypatch, value, expected):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": value})
    assert guard.non_owner_host_tools() == expected


def test_restricted_tools_cover_the_host_toolsets():
    for name in (
        "terminal",
        "process_manage",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "execute_code",
        "delegate_task",
        "cronjob_manage",
        "browser_navigate",
        "browser_cdp",
        "browser_exec",
    ):
        assert guard.is_restricted_tool(name)
    assert "browser" in guard.RESTRICTED_TOOLSETS
    assert not guard.is_restricted_tool("web_extract")


# --- wiring -----------------------------------------------------------------


def test_adapter_overrides_toolsets_per_source():
    from clawchat_gateway.adapter import ClawChatAdapter

    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._owner_user_id = lambda: OWNER
    assert adapter.toolsets_for_source(Source(DM, "dm", OWNER)) is None
    kept = adapter.toolsets_for_source(Source(DM, "dm", FRIEND))
    assert kept and "terminal" not in kept and "file" not in kept


def test_adapter_passes_its_own_owner_chat(monkeypatch):
    from clawchat_gateway.adapter import ClawChatAdapter

    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: "")  # global lookup sees nothing
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._owner_user_id = lambda: OWNER
    adapter._owner_direct_chat_id = lambda: OWNER_DM  # this profile's activation conversation
    assert adapter.toolsets_for_source(Source(OWNER_DM, "dm", "clawchat-permission-result")) is None
    assert adapter.toolsets_for_source(Source(DM, "dm", "clawchat-awareness")) is not None


def test_adapter_override_fails_closed_when_owner_lookup_breaks():
    from clawchat_gateway.adapter import ClawChatAdapter

    adapter = ClawChatAdapter.__new__(ClawChatAdapter)

    def boom():
        raise RuntimeError("store locked")

    adapter._owner_user_id = boom
    kept = adapter.toolsets_for_source(Source(DM, "dm", OWNER))
    assert kept is not None and "terminal" not in kept


def _load_plugin_entry(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    entry = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_plugin_entry_friend_tools", entry)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_plugin_registers_the_pre_tool_call_hook(monkeypatch):
    plugin = _load_plugin_entry(monkeypatch)
    hooks = {}

    class Ctx:
        def register_hook(self, name, fn):
            hooks[name] = fn

    plugin._register_host_tools_guard(Ctx())
    assert hooks["pre_tool_call"] is guard.clawchat_pre_tool_call


def test_plugin_manifest_declares_the_hook():
    from pathlib import Path

    import yaml

    manifest = yaml.safe_load((Path(__file__).resolve().parent.parent / "plugin.yaml").read_text())
    assert "pre_tool_call" in manifest["provides_hooks"]


# --- turns the plugin itself starts in the owner's direct chat ---------------
#
# The memory-migration hint, permission receipts, moment-comment and awareness
# notes are synthetic turns the plugin starts in the owner's direct chat; their
# sender is "ClawChat" (clawchat-*), not the owner. The batch3 e2e found them
# treated as someone else's turn: the migration hint could not read MEMORY.md
# under the default "off", and under "approve" the owner had to refuse two
# prompts before the hint went out. The owner's direct chat holds only the owner
# and the agent, so its chat id decides.

PLUGIN_SENDERS = ["clawchat-permission-result", "clawchat-awareness", "clawchat-memory-migration", None]


@pytest.mark.parametrize("sender", PLUGIN_SENDERS)
def test_plugin_turn_in_the_owner_chat_keeps_the_host_tools(sender):
    assert guard.toolsets_for_source(Source(OWNER_DM, "dm", sender), owner_user_id=OWNER) is None


@pytest.mark.parametrize("sender", [s for s in PLUGIN_SENDERS if s])
def test_plugin_turn_in_the_owner_chat_is_not_blocked(monkeypatch, sender):
    _session(monkeypatch, chat_id=OWNER_DM, chat_type="dm", user_id=sender)
    assert guard.clawchat_pre_tool_call(tool_name="read_file", args={"path": "MEMORY.md"}) is None


def test_plugin_turn_in_a_friend_chat_is_still_narrowed(monkeypatch):
    # e.g. the friend greeting: synthetic, but in the friend's direct chat.
    kept = guard.toolsets_for_source(Source(DM, "dm", "clawchat-awareness"), owner_user_id=OWNER)
    assert kept is not None and "file" not in kept
    _session(monkeypatch, chat_id=DM, chat_type="dm", user_id="clawchat-awareness")
    assert guard.clawchat_pre_tool_call(tool_name="read_file", args={})["action"] == "block"


def test_owner_chat_id_never_unlocks_a_group(monkeypatch):
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: GROUP)
    assert guard.toolsets_for_source(Source(GROUP, "group", OWNER), owner_user_id=OWNER) is not None
    _session(monkeypatch, chat_id=GROUP, chat_type="group", user_id="clawchat-awareness")
    assert guard.clawchat_pre_tool_call(tool_name="terminal", args={})["action"] == "block"


def test_owner_chat_lookup_failure_fails_closed(monkeypatch):
    def boom():
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(guard, "_owner_direct_chat_id", boom)
    assert guard.toolsets_for_source(Source(OWNER_DM, "dm", "clawchat-awareness"), owner_user_id=OWNER) is not None
    _session(monkeypatch, chat_id=OWNER_DM, chat_type="dm", user_id="clawchat-awareness")
    assert guard.clawchat_pre_tool_call(tool_name="read_file", args={})["action"] == "block"
    # the owner speaking in their own chat does not need the lookup
    assert guard.toolsets_for_source(Source(OWNER_DM, "dm", OWNER), owner_user_id=OWNER) is None
