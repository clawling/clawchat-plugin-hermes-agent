"""Both host tool layers and pre-LLM binding fail closed."""
from types import SimpleNamespace
import pytest
from clawchat_gateway import host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.tool_grants import (
    GRANT_BLOCK_MESSAGE, GRANT_INTERSECTION_BLOCK_MESSAGE, GRANT_ASK_DENIED_MESSAGE,
    GRANT_TOOL_NAMES, GRANT_ASK_UNAVAILABLE_MESSAGE,
)
from clawchat_gateway.local_paths import LOCAL_PATH_REFUSAL
from test_turn_grants import install_host_context, snapshot

TOOL = "mcp__docs__read"


@pytest.fixture
def host(monkeypatch):
    bind = install_host_context(monkeypatch)
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: True)
    from clawchat_gateway.tool_capabilities import CAPABILITY_TOOL_NAMES, AUTHORIZATION_TOOLS
    catalog = frozenset(CAPABILITY_TOOL_NAMES) | {TOOL, "skill_manage", "clawchat_memory_read", *AUTHORIZATION_TOOLS}
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (catalog, catalog))
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"web", "clawchat", "memory", "docs"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: {
        "web": ["web_search"], "clawchat": ["clawchat_memory_read"], "memory": ["memory"],
        "docs": [TOOL], "mcp-docs": [TOOL],
    }.get(name, []))

    monkeypatch.setattr(guard, "_owner_user_id", lambda: "usr_owner")
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: "cnv_owner")
    monkeypatch.setattr(guard, "_is_known_group", lambda _: False)
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_registry_toolset", lambda name: "mcp-docs" if name == TOOL else "memory")
    monkeypatch.setattr(guard, "_mcp_server_names", lambda: {"docs"})
    monkeypatch.setattr(guard, "_narrowed_toolsets", lambda: ["web", "clawchat", guard.NO_MCP])
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: frozenset())
    monkeypatch.setattr(guard, "_session_value", turns.session_context_value)
    return bind


def context(s, *, nonce=None, group=False):
    return dict(HERMES_SESSION_MESSAGE_ID=s.nonce if nonce is None else nonce,
                HERMES_SESSION_CHAT_ID=s.chat_id, HERMES_SESSION_CHAT_TYPE="group" if group else "dm",
                HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_USER_ID="usr_a")


def call(tool=TOOL, **kwargs):
    return guard.clawchat_pre_tool_call(tool_name=tool, args=kwargs.pop("args", {}),
                                       session_id="s", **kwargs)


def test_direct_grant_allows_only_after_pre_llm_binding(host):
    s = snapshot()
    try:
        with host(**context(s)):
            assert call(turn_id="t")["message"] == GRANT_BLOCK_MESSAGE
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t", args={"url": "https://example.org/docs/1"}) is None
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("path", ["/tmp/key", "~/key", "file:///tmp/key", "../key", "x/../../key",
                                    "C:\\Users\\key", "read /tmp/key", "https://example.org and /tmp/key", "`/tmp/key`", "run;/tmp/key"])
def test_direct_grant_blocks_nested_local_paths(host, path):
    s = snapshot()
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t", args={"nested": [{"arbitrary": path}]})["message"] == LOCAL_PATH_REFUSAL
    finally:
        turns.release_turn(s.nonce)


def test_ask_is_blocked_even_with_legacy_approve(host, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    s = snapshot(grants={TOOL: "ask"})
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t") == {"action": "block", "reasonCode": "ask-unavailable", "message": GRANT_ASK_UNAVAILABLE_MESSAGE, "blockReason": GRANT_ASK_UNAVAILABLE_MESSAGE}
    finally:
        turns.release_turn(s.nonce)


def test_ungranted_and_group_intersection_messages(host):
    s = snapshot(grants={}, speakers=("usr_alice", "usr_bob"), any_tools=frozenset({TOOL}))
    turns.bind_turn(s.nonce, "s", "t")
    try:
        with host(**context(s)):
            assert call(turn_id="t")["message"] == GRANT_INTERSECTION_BLOCK_MESSAGE
        with host(**context(s, group=True)):
            assert call(turn_id="t")["message"] == GRANT_INTERSECTION_BLOCK_MESSAGE
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("failure", ["missing_turn", "missing_nonce", "followup", "different_session", "released"])
def test_missing_identity_and_followup_never_borrow_grants(host, failure, monkeypatch):
    s = snapshot()
    try:
        turns.bind_turn(s.nonce, "s", "t")
        if failure == "released":
            turns.release_turn(s.nonce)
        values = context(s)
        if failure == "missing_nonce":
            values.pop("HERMES_SESSION_MESSAGE_ID")
            monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", s.nonce)
        with host(**values):
            if failure == "followup":
                guard.clawchat_pre_llm_call(session_id="s", turn_id="new-turn")
            result = guard.clawchat_pre_tool_call(
                tool_name=TOOL, args={}, session_id="other" if failure == "different_session" else "s",
                **({} if failure == "missing_turn" else {"turn_id": "new-turn" if failure == "followup" else "t"}),
            )
            expected = {"action": "block", "message": GRANT_BLOCK_MESSAGE}
            if failure != "missing_turn":
                expected["reasonCode"] = "not-granted"
                expected["blockReason"] = GRANT_BLOCK_MESSAGE
            assert result == expected
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("tool", GRANT_TOOL_NAMES)
@pytest.mark.parametrize("platform", ["telegram", "clawchat", ""])
def test_management_tools_block_on_every_platform(host, tool, platform):
    with host(HERMES_SESSION_PLATFORM=platform):
        assert call(tool, turn_id="t")["action"] == "block"


def test_off_preserves_original_block_and_approve(host, monkeypatch):
    s = snapshot()
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off"})
            assert call(turn_id="t") == {"action": "block", "message": guard.BLOCK_MESSAGE}
            monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off", "non-owner-host-tools": "approve"})
            monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
            assert call(turn_id="t")["action"] == "approve"
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("tool,toolset,expected", [(TOOL, "mcp-docs", {"web", "clawchat", "docs"}),
                                                  ("memory", "memory", {"web", "clawchat", "memory", guard.NO_MCP})])
def test_first_layer_adds_granted_toolset_and_explicit_mcp_server(host, monkeypatch, tool, toolset, expected):
    monkeypatch.setattr(guard, "_registry_toolset", lambda name: toolset if name == tool else "clawchat" if name.startswith("clawchat_") else "web")
    s = snapshot(grants={tool: "direct"})
    try:
        source = SimpleNamespace(chat_id=s.chat_id, chat_type="dm", user_id="usr_a", message_id=s.nonce)
        assert set(guard.toolsets_for_source(source, owner_user_id="usr_owner")) == expected
    finally:
        turns.release_turn(s.nonce)


def test_first_layer_resolution_error_keeps_original_nonempty_list(host, monkeypatch):
    s = snapshot()
    try:
        source = SimpleNamespace(chat_id=s.chat_id, chat_type="dm", user_id="usr_a", message_id=s.nonce)
        monkeypatch.setattr(guard, "_registry_toolset", lambda _: (_ for _ in ()).throw(RuntimeError()))
        assert guard.toolsets_for_source(source, owner_user_id="usr_owner") == ["web", "clawchat", guard.NO_MCP]
    finally:
        turns.release_turn(s.nonce)


def test_hook_registration_checks_pre_llm_capability(host, monkeypatch):
    recorded = []
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: True)
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    guard.register_host_tools_guard(SimpleNamespace(register_hook=lambda *args: recorded.append(args)))
    assert [item[0] for item in recorded] == ["pre_tool_call"]
    recorded.clear()
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: True)
    guard.register_host_tools_guard(SimpleNamespace(register_hook=lambda *args: recorded.append(args)))
    assert [item[0] for item in recorded] == ["pre_tool_call", "pre_llm_call"]


def test_grant_lookup_exception_blocks(host, monkeypatch):
    monkeypatch.setattr(turns, "lookup_for_tool_call", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    with host(HERMES_SESSION_PLATFORM="clawchat"):
        assert call(turn_id="t")["action"] == "block"


def test_direct_grant_keeps_relative_path_field_check(host):
    s = snapshot()
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t", args={"nested": {"path": "relative.db"}})["message"] == LOCAL_PATH_REFUSAL
            assert call(turn_id="t", args={"path": "https://example.org/document"}) is None
    finally:
        turns.release_turn(s.nonce)


def test_empty_private_snapshot_does_not_enable_group_approve(host, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    s = snapshot(grants={})
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call(turn_id="t") == {
                "action": "block", "reasonCode": "not-granted", "message": GRANT_BLOCK_MESSAGE,
            }
    finally:
        turns.release_turn(s.nonce)


def test_default_on_empty_grants_preserves_limited_block_message(host, monkeypatch):
    monkeypatch.setattr(guard, "is_restricted_tool", lambda _: False)
    s = snapshot(grants={})
    try:
        with host(**context(s)):
            guard.clawchat_pre_llm_call(session_id="s", turn_id="t")
            assert call("skill_manage", turn_id="t", args={"action": "create"}) == {
                "action": "block", "reasonCode": "not-granted", "message": guard.LIMITED_BLOCK_MESSAGE,
            }
    finally:
        turns.release_turn(s.nonce)
