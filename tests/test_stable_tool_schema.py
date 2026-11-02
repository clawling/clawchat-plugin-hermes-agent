"""Session schemas stay fixed while each execution keeps its own authority."""

import json
import sys
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from clawchat_gateway import adapter as adapter_mod, host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage
from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS, OWNER_ONLY_TOOLS
from test_tool_grants_hook import host, context, call, TOOL
from test_tool_grants_ask import approval
from test_channel_prompt_split import split_env, prompt_env, harness, incoming, dispatch


@pytest.fixture(autouse=True)
def isolated_turn_registry(monkeypatch):
    """Turns left open by earlier test modules must not decide this module's turns."""
    for name in ("_snapshots", "_bindings", "_active", "_session_nonces", "_unverified_inputs",
                 "_execution_nonces", "_context_execution", "_approvals", "_released_turns", "_consumed_batches"):
        monkeypatch.setattr(turns, name, type(getattr(turns, name))())


@pytest.fixture
def stable(host, monkeypatch):
    monkeypatch.setattr(guard, "_host_supports_stable_schema", lambda: True)
    return host


def source(s, kind="group"):
    return SimpleNamespace(chat_id=s.chat_id, chat_type=kind, message_id=s.nonce, user_id="usr_a")


def turn(*, owner=False, verified=False, grants=None, speakers=None, chat="cnv_group"):
    return turns.register_turn(account_id="usr_agent", chat_id=chat, session_key=chat,
                               speakers=speakers if speakers is not None else ("usr_owner" if owner else "usr_a",),
                               grants=grants or {}, owner_side=owner, owner_verified=verified)


@pytest.mark.parametrize("enabled", [False, True])
def test_alternating_group_turns_have_identical_sorted_toolsets(stable, monkeypatch, enabled):
    monkeypatch.setattr(guard, "_tool_grants_enabled", lambda: enabled)
    selections = []
    for owner in (True, False, True):
        s = turn(owner=owner)
        try:
            selections.append(json.dumps(guard.toolsets_for_source(source(s), owner_user_id="usr_owner")))
        finally:
            turns.release_turn(s.nonce)
    assert selections[0] == selections[1] == selections[2]
    expected = guard._platform_toolsets() | ({guard.GRANT_REQUEST_TOOLSET} if enabled else set())
    assert json.loads(selections[0]) == sorted(expected)


@pytest.mark.parametrize("tool", [
    *AUTHORIZATION_TOOLS, *OWNER_ONLY_TOOLS,
    "terminal", "process_manage", "read_file", "write_file", "patch", "search_files",
    "execute_code", "delegate_task", "cronjob_manage", "kanban_create", "computer_use",
    "browser_navigate", "memory", "session_search", "manage_connections",
    "connectors__example__read", TOOL, "skill_manage",
])
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_wide_group_schema_still_blocks_ungranted_calls(stable, monkeypatch, tool, bound, enabled):
    monkeypatch.setattr(guard, "_tool_grants_enabled", lambda: enabled)
    s = turn()
    try:
        assert "memory" in guard.toolsets_for_source(source(s), owner_user_id="usr_owner")
        with stable(**context(s, group=True)):
            if bound:
                guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(tool, turn_id="t")["action"] == "block"
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("tool,args", [
    ("skill_view", {"name": "external-example"}),
    ("vision_analyze", {"image_url": "/example/private-image.png"}),
    ("video_analyze", {"video_url": "/example/private-video.mp4"}),
    ("image_generate", {"reference_image_urls": ["/example/private-image.png"]}),
    ("text_to_speech", {"output_path": "/example/generated.mp3"}),
])
def test_wide_schema_keeps_argument_limits(stable, monkeypatch, tool, args):
    monkeypatch.setattr(guard, "_tool_grants_enabled", lambda: False)
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    s = turn()
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(tool, turn_id="t", args=args)["action"] == "block"
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("grants,tool,expected", [
    ({TOOL: "direct"}, TOOL, None),
    ({TOOL: "direct"}, "terminal", "block"),
    ({"web_search": "deny"}, "web_search", "block"),
    ({TOOL: "ask"}, TOOL, "block"),
])
def test_grants_and_explicit_deny_are_enforced_at_dispatch(stable, grants, tool, expected):
    s = turn(grants=grants)
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            result = call(tool, turn_id="t")
            assert result is None if expected is None else result["action"] == expected
    finally:
        turns.release_turn(s.nonce)


def test_schema_exclusion_uses_the_bound_follow_up_snapshot(stable):
    token = turns.mark_unverified_input("usr_agent", "cnv_owner")
    first = turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", session_key="cnv_owner",
                                speakers=("usr_a",), grants={}, unverified_token=token)
    queued = None
    try:
        # The follow-up executes under the first input's context nonce.
        with stable(**context(first)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t1")
            assert call(TOOL, turn_id="t1")["action"] == "block"
            # Register only after the first input is consumed: these are
            # separate batches, so their grants must not be intersected.
            queued = turn(grants={TOOL: "direct"}, chat=first.chat_id)
            assert queued.grants.get(TOOL) == "direct"
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t2")
            assert turns.lookup_snapshot_for_call(first.nonce, first.chat_id, "s", "t2").nonce == queued.nonce
            assert guard._legacy_schema_hides(TOOL, first.nonce, first.chat_id, "dm")
            assert not guard._legacy_schema_hides(TOOL, queued.nonce, queued.chat_id, "dm")
            assert call(TOOL, turn_id="t2") is None
    finally:
        turns.release_turn(first.nonce)
        if queued is not None:
            turns.release_turn(queued.nonce)


def test_schema_exclusion_does_not_bypass_overlapping_batch_grants(stable, monkeypatch):
    first = turn()
    queued = turn(grants={TOOL: "direct"})
    try:
        with stable(**context(first, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t1")
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t2")
            bound = turns.lookup_snapshot_for_call(first.nonce, first.chat_id, "s", "t2")
            assert bound.nonce == queued.nonce
            assert TOOL in bound.grants.candidates and bound.grants.get(TOOL) is None
            assert guard._legacy_schema_hides(TOOL, bound.nonce, bound.chat_id, "group")
            assert call(TOOL, turn_id="t2")["reasonCode"] == "not-granted"
            # The grant path must also refuse independently of schema filtering.
            monkeypatch.setattr(guard, "_legacy_schema_hides", lambda *args: False)
            assert call(TOOL, turn_id="t2")["reasonCode"] == "not-granted"
    finally:
        turns.release_turn(first.nonce)
        turns.release_turn(queued.nonce)


@pytest.mark.parametrize("tool,toolset", [("terminal", "terminal"), (TOOL, "docs")])
def test_schema_exclusion_preserves_explicit_denial_reason(stable, monkeypatch, tool, toolset):
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"clawchat", toolset})
    original_resolve = guard._resolve_tools
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: [tool] if name == toolset else original_resolve(name))
    s = turn(grants={tool: "deny"})
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(tool, turn_id="t")["reasonCode"] == "denied"
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("bypass", [False, True])
def test_wide_schema_ask_requires_reliable_owner_gate(stable, approval, bypass):
    approval._yolo_active = lambda: bypass
    s = turn(grants={TOOL: "ask"})
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            result = call(turn_id="t")
            assert result["action"] == ("block" if bypass else "approve")
    finally:
        turns.release_turn(s.nonce)


def test_prefixed_mcp_toolset_keeps_existing_direct_grant(stable, monkeypatch):
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"clawchat", "mcp-docs"})
    s = turn(grants={TOOL: "direct"})
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t") is None
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("supports_approve", [False, True])
def test_legacy_approve_never_runs_without_a_human_gate(stable, monkeypatch, supports_approve):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off", "non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: supports_approve)
    s = turn()
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call("terminal", turn_id="t")["action"] == ("approve" if supports_approve else "block")
        # Synthetic/unattributed input does not inherit group approve.
        with stable(**context(s, nonce="", group=True)):
            assert call("terminal", turn_id="t")["action"] == "block"
    finally:
        turns.release_turn(s.nonce)


def test_unknown_memory_provider_and_mixed_toolset_members_stay_blocked(stable, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off"})
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: True)
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: None)
    monkeypatch.setattr(guard, "_registry_knows", lambda _: False)
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"clawchat", "mixed"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: {
        "clawchat": ["clawchat_memory_read"], "mixed": ["terminal", "mixed_helper"],
    }.get(name, []))
    s = turn()
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call("unknown_provider_tool", turn_id="t")["action"] == "block"
            monkeypatch.setattr(guard, "memory_provider_tools", lambda: frozenset())
            assert call("mixed_helper", turn_id="t")["action"] == "block"
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("failure", ["catalog", "record", "policy"])
def test_guard_errors_fail_closed(stable, monkeypatch, failure):
    def fail(*args, **kwargs):
        raise RuntimeError("fixture failure")
    s = turn()
    try:
        with stable(**context(s, group=True)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            if failure == "record":
                from clawchat_gateway import turn_attachments
                monkeypatch.setattr(turn_attachments, "record_call", fail)
            else:
                monkeypatch.setattr(guard, "host_tool_catalog" if failure == "catalog" else "_config_extra", fail)
            assert call("web_search", turn_id="t")["action"] == "block"
    finally:
        turns.release_turn(s.nonce)


def test_model_schema_is_stable_but_search_catalog_remains_authorized(stable, monkeypatch):
    model = ModuleType("model_tools")
    raw = [{"function": {"name": tool}} for tool in ("web_search", "terminal", TOOL, *AUTHORIZATION_TOOLS)]
    def definitions(enabled_toolsets=None, quiet_mode=False, skip_tool_search_assembly=False):
        assert skip_tool_search_assembly or not guard._guest_catalog_turn()
        return list(raw)
    model.get_tool_definitions = definitions
    model._last_resolved_tool_names = []
    monkeypatch.setitem(sys.modules, "model_tools", model)
    assert guard._install_grant_catalog_filter()
    schemas = []
    for owner in (True, False, True):
        s = turn(owner=owner, verified=owner)
        try:
            with stable(**context(s, group=True)):
                schemas.append(json.dumps(model.get_tool_definitions(quiet_mode=True)))
                names = {d["function"]["name"] for d in model.get_tool_definitions(
                    quiet_mode=True, skip_tool_search_assembly=True)}
                assert ("terminal" in names) is owner
                assert ("clawchat_tool_grant" in names) is owner
        finally:
            turns.release_turn(s.nonce)
    assert schemas[0] == schemas[1] == schemas[2]


@pytest.mark.asyncio
async def test_owner_dm_notification_uses_same_schema_and_peer_names(split_env, monkeypatch):
    h = split_env
    a = h.adapter
    monkeypatch.setattr(guard, "_host_supports_stable_schema", lambda: True)
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"clawchat", "terminal"})
    monkeypatch.setattr(guard, "_tool_grants_enabled", lambda: False)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", a._owner_direct_chat_id)
    monkeypatch.setattr(a, "_session_names", ClawChatAdapter._session_names.__get__(a))
    real = replace(incoming("usr_owner"), sender_name="Owner")
    first = await dispatch(h, real)
    first_tools = a.toolsets_for_source(first.source)
    note = InboundMessage.internal_synthetic(chat_id=real.chat_id, chat_type="direct",
        sender_id="clawchat-awareness", sender_name="ClawChat", text="ClawChat: contacts updated.",
        raw_message={"awareness": True}, plugin_notification=True, emitted_at=real.emitted_at)
    second = await dispatch(h, note)
    assert a.toolsets_for_source(second.source) == first_tools
    assert (first.source.chat_name, first.source.user_name) == (second.source.chat_name, second.source.user_name) == ("Owner", "Owner")
    assert second.text == note.text and note.sender_name == "ClawChat"
    for event in (first, second):
        assert "Tools this turn:" in event.channel_context
        assert "Tools this turn:" not in event.channel_prompt
    assert "ordinary owner tools" in first.channel_context
    assert "public defaults and current grants only" in second.channel_context


@pytest.mark.asyncio
async def test_group_permission_line_is_per_turn_only(split_env, monkeypatch):
    monkeypatch.setattr(guard, "_host_supports_stable_schema", lambda: True)
    owner = await dispatch(split_env, incoming("usr_owner", kind="group"))
    guest = await dispatch(split_env, incoming(kind="group", mid="msg-guest"))
    assert owner.channel_prompt == guest.channel_prompt
    assert "Tools this turn:" not in guest.channel_prompt
    assert "ordinary owner tools" in owner.channel_context
    assert "public defaults and current grants only" in guest.channel_context


@pytest.mark.parametrize("reason", ["guard-missing", "binding-missing", "override-missing", "turn-block-missing", "ids-missing", "context-missing", "hook-unsupported", "ready"])
def test_capability_detection_requires_registered_hooks_and_host_contract(host, monkeypatch, reason):
    plugins = ModuleType("hermes_cli.plugins")
    def directive(tool_name="", args=None, session_id="", turn_id=""):
        pass
    plugins._get_pre_tool_call_directive_details = directive if reason != "ids-missing" else lambda: None
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(guard, "_hook_missing", reason == "guard-missing")
    monkeypatch.setattr(guard, "_binding_hook_registered", reason != "binding-missing")
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: reason != "override-missing")
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: reason != "hook-unsupported")
    monkeypatch.setattr(adapter_mod, "_host_supports_channel_context", lambda: reason != "turn-block-missing")
    if reason == "context-missing":
        monkeypatch.delitem(sys.modules["gateway.session_context"]._VAR_MAP, "HERMES_SESSION_MESSAGE_ID")
    assert guard._host_supports_stable_schema() is (reason == "ready")
    s = turn()
    try:
        actual = guard.toolsets_for_source(source(s), owner_user_id="usr_owner")
        if reason == "ready":
            assert "memory" in actual
        else:
            legacy = guard._toolsets_for_source(source(s), owner_user_id="usr_owner")
            assert json.dumps(actual) == json.dumps(list(dict.fromkeys([guard.GRANT_REQUEST_TOOLSET, *legacy])))
    finally:
        turns.release_turn(s.nonce)


def test_friend_dm_keeps_legacy_selection(stable):
    s = turn(chat="cnv_friend")
    try:
        legacy = guard._toolsets_for_source(source(s, "dm"), owner_user_id="usr_owner")
        assert guard.toolsets_for_source(source(s, "dm"), owner_user_id="usr_owner") == list(
            dict.fromkeys([guard.GRANT_REQUEST_TOOLSET, *legacy]))
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("failure", [None, "pre_tool_call", "pre_llm_call", "missing"])
def test_registration_tracks_actual_guard_and_binding_installation(host, monkeypatch, failure):
    plugins = ModuleType("hermes_cli.plugins")
    plugins.VALID_HOOKS = {"pre_tool_call", "pre_llm_call"}
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", plugins)
    monkeypatch.setattr(guard, "_hook_missing", None)
    monkeypatch.setattr(guard, "_binding_hook_registered", False)
    monkeypatch.setattr(guard, "_install_grant_catalog_filter", lambda: False)
    monkeypatch.setattr(guard, "_install_guest_search_guard", lambda: False)
    hooks = {}
    def register(name, callback):
        if name == failure:
            raise RuntimeError("fixture registration failure")
        hooks[name] = callback
    ctx = SimpleNamespace() if failure == "missing" else SimpleNamespace(register_hook=register)
    if failure == "pre_tool_call":
        with pytest.raises(RuntimeError):
            guard.register_host_tools_guard(ctx)
    else:
        guard.register_host_tools_guard(ctx)
    assert (guard._hook_missing is False) is (failure not in {"missing", "pre_tool_call"})
    assert guard._binding_hook_registered is (failure is None)
    if failure is None:
        assert hooks["pre_tool_call"] is guard.clawchat_pre_tool_call
        assert hooks["pre_llm_call"] is guard.clawchat_pre_llm_call
