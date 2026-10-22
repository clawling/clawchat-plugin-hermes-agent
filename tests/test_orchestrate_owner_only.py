"""Owner-only orchestration cannot be exposed, granted or dispatched by guests."""
from contextvars import ContextVar
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote, unquote

import pytest

from clawchat_gateway import host_tools_guard as guard, tool_grants as grants, turn_grants as turns
from clawchat_gateway import api_client, plugin_tools, tools, permission_result
from clawchat_gateway.gate_outcome import map_gate_outcome, owner_turn_only
from clawchat_gateway.inbound import InboundMessage, parse_inbound_message
from clawchat_gateway.tool_capabilities import OWNER_ONLY_TOOLS, AUTHORIZATION_TOOLS, get_public_tools_override

OWNER, FRIEND, CHAT = "usr_owner", "usr_friend", "cnv_group"
ORIGIN = dict(conversation_id=CHAT, initiator_id=OWNER)
DEFINITIONS = [{"function": {"name": name}} for name in (*OWNER_ONLY_TOOLS, "terminal", "clawchat_get_conversation")]


@pytest.fixture
def host(monkeypatch):
    session, nonces = {}, []
    monkeypatch.setattr(turns, "session_context_value", lambda key: session.get(key, ""))
    monkeypatch.setattr(guard, "_session_value", lambda key: session.get(key, ""))
    monkeypatch.setattr(guard, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: "cnv_owner")
    monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "on"})
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: True)
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (set(OWNER_ONLY_TOOLS) | {"terminal"}, set(OWNER_ONLY_TOOLS) | {"terminal"}))
    monkeypatch.setattr(guard, "_strict_unregistered", lambda _: False)
    approval = ModuleType("tools.approval_context")
    approval._approval_tool_call_id = ContextVar("test_tool_call_id", default="")
    monkeypatch.setitem(sys.modules, "tools.approval_context", approval)
    model = ModuleType("model_tools")
    model._last_resolved_tool_names = []
    monkeypatch.setitem(sys.modules, "model_tools", model)
    guard._ORCHESTRATE_ORIGINS.clear()

    def start(*, chat=CHAT, speakers=(), **kw):
        snapshot = turns.register_turn(account_id="usr_agent", chat_id=chat, speakers=speakers,
                                       grants=kw.pop("grants", {}), session_key="owner-only:" + chat, **kw)
        nonces.append(snapshot.nonce)
        turns.bind_turn(snapshot.nonce, "session", "turn")
        session.update(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID=chat,
                       HERMES_SESSION_MESSAGE_ID=snapshot.nonce, HERMES_SESSION_USER_ID=OWNER,
                       HERMES_SESSION_CHAT_TYPE="group" if chat == CHAT else "direct")
        return snapshot

    yield SimpleNamespace(start=start, session=session, approval=approval, model=model)
    for nonce in nonces:
        turns.release_turn(nonce)
    guard._ORCHESTRATE_ORIGINS.clear()


SCENARIOS = [
    ("friend-dm", dict(chat="cnv_friend", speakers=(FRIEND,)), False),
    ("mixed-group", dict(speakers=(OWNER, FRIEND)), False),
    ("owner-group", dict(speakers=(OWNER,), owner_side=True), True),
    ("owner-dm", dict(chat="cnv_owner", speakers=(OWNER,), owner_side=True), True),
    ("host-internal", dict(chat="cnv_owner", speakers=(OWNER,), owner_side=True, owner_verified=False), True),
    ("owner-action", dict(chat="cnv_owner", legacy_owner_tools=True), True),
    ("grant-receipt", dict(speakers=(FRIEND,), request_origin=turns.RequestOrigin(FRIEND, CHAT, "group", "key", "msg", "request")), False),
    ("process-completion", dict(speakers=(FRIEND,), owner_side=False, approve_eligible=False), False),
    ("approved-retry", dict(orchestrate_retry=True), True),
]


@pytest.mark.parametrize("name,options,allowed", SCENARIOS, ids=[row[0] for row in SCENARIOS])
def test_catalog_and_pre_tool_layers(host, name, options, allowed):
    snapshot = host.start(**options)
    names = {item["function"]["name"] for item in guard._filter_grant_definitions(DEFINITIONS, snapshot)}
    assert set(OWNER_ONLY_TOOLS) <= names if allowed else names.isdisjoint(OWNER_ONLY_TOOLS)
    for index, tool in enumerate(OWNER_ONLY_TOOLS):
        call_id = f"call-{index}"
        result = guard._clawchat_pre_tool_decision(tool_name=tool, args={"purpose": "check"},
                    session_id="session", turn_id="turn", tool_call_id=call_id)
        if allowed:
            assert result is None
            assert guard.take_orchestrate_origin(call_id) == (snapshot.nonce, dict(conversation_id=snapshot.chat_id, initiator_id=OWNER))
            assert guard.take_orchestrate_origin(call_id) is None
        else:
            assert result == dict(action="block", message=owner_turn_only(tool)["instruction"])
            assert guard.take_orchestrate_origin(call_id) is None
    if name == "approved-retry":
        assert not snapshot.owner_side and "terminal" not in names
        assert guard._clawchat_pre_tool_decision(tool_name="terminal", args={}, session_id="session", turn_id="turn")["action"] == "block"


def test_owner_fork_never_exposes_or_dispatches_orchestration(host):
    snapshot = host.start(speakers=(OWNER,), owner_side=True)
    names = {item["function"]["name"] for item in guard._filter_grant_definitions(DEFINITIONS, snapshot, owner_fork=True)}
    assert names.isdisjoint(OWNER_ONLY_TOOLS)
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="fork", turn_id="fork")["action"] == "block"


@pytest.mark.parametrize("change", ["missing-snapshot", "platform", "chat", "owner-mismatch", "no-owner", "missing-chat"])
def test_origin_requires_matching_trusted_context(host, monkeypatch, change):
    snapshot = host.start(speakers=(OWNER,), owner_side=True)
    if change == "missing-snapshot":
        snapshot = None
    elif change == "platform":
        host.session["HERMES_SESSION_PLATFORM"] = "telegram"
    elif change == "chat":
        host.session["HERMES_SESSION_CHAT_ID"] = "cnv_other"
    elif change == "missing-chat":
        snapshot = replace(snapshot, chat_id="")
        host.session["HERMES_SESSION_CHAT_ID"] = ""
    elif change == "owner-mismatch":
        monkeypatch.setattr(guard, "_owner_user_id", lambda: FRIEND)
    else:
        snapshot = replace(snapshot, owner_side=False, legacy_owner_tools=True, speakers=())
        monkeypatch.setattr(guard, "_owner_user_id", lambda: "")
    assert guard.orchestrate_owner_turn(snapshot) is None


def test_verified_owner_speaker_can_supply_identity_without_sender(host, monkeypatch):
    snapshot = host.start(speakers=(OWNER,), owner_side=True)
    monkeypatch.setattr(guard, "_owner_user_id", lambda: "")
    assert guard.orchestrate_owner_turn(snapshot) == ORIGIN


@pytest.mark.parametrize("order", ["retry-first", "guest-first"])
def test_guest_overlap_revokes_retry_in_both_directions(host, order):
    retry = lambda: host.start(orchestrate_retry=True)
    guest = lambda: host.start(speakers=(FRIEND,))
    first, second = (retry(), guest()) if order == "retry-first" else (guest(), retry())
    assert not turns.get_snapshot(first.nonce).orchestrate_retry
    assert not turns.get_snapshot(second.nonce).orchestrate_retry
    assert guard.orchestrate_owner_turn(turns.get_snapshot(first.nonce)) is None


def test_owner_dm_retry_registered_after_synthetic_input_mark_keeps_only_orchestration(host):
    snapshot = host.start(chat="cnv_owner", orchestrate_retry=True)
    turns.mark_unverified_input(snapshot.account_id, "owner-only:cnv_owner")
    try:
        # The mark revokes every live nonce; the receipt's own turn, registered after it, keeps the retry.
        assert not turns.get_snapshot(snapshot.nonce).orchestrate_retry
        retry = host.start(chat="cnv_owner", orchestrate_retry=True)
        assert retry.orchestrate_retry and not retry.owner_side
        assert guard.orchestrate_owner_turn(retry) == dict(conversation_id="cnv_owner", initiator_id=OWNER)
    finally:
        turns.clear_unverified_input(snapshot.account_id, "owner-only:cnv_owner")


@pytest.mark.asyncio
async def test_handler_without_origin_does_not_construct_client(host, monkeypatch):
    host.start(speakers=(FRIEND,))
    client = Mock()
    monkeypatch.setattr(tools, "_build_client", client)
    result = await tools.orchestrate_list_agents(purpose="check")
    assert result == owner_turn_only(OWNER_ONLY_TOOLS[0])
    client.assert_not_called()


@pytest.mark.asyncio
async def test_handler_prefers_and_consumes_cached_origin(host, monkeypatch):
    snapshot = host.start(speakers=(OWNER,), owner_side=True)
    cached = dict(ORIGIN)
    guard._remember_orchestrate_origin("call", cached, snapshot.nonce)
    host.session["HERMES_SESSION_MESSAGE_ID"] = "unavailable-fallback"
    token = host.approval._approval_tool_call_id.set("call")
    client = SimpleNamespace(orch_list_agents=AsyncMock(return_value={"code": 0}))
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    try:
        assert await tools.orchestrate_list_agents(purpose="check") == {"code": 0}
        client.orch_list_agents.assert_awaited_once_with(origin=dict(cached, purpose="check"))
        assert guard.take_orchestrate_origin("call") is None
        # The consumed record cannot restore authority without a fallback snapshot.
        assert guard.orchestrate_handler_origin() is None
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
        assert guard.orchestrate_handler_origin() == ORIGIN
    finally:
        host.approval._approval_tool_call_id.reset(token)


def test_origin_cache_ttl_and_capacity(host, monkeypatch):
    monkeypatch.setattr(guard, "_MAX_ORCHESTRATE_ORIGINS", 2)
    now = [1.0]
    monkeypatch.setattr(guard.time, "monotonic", lambda: now[0])
    for key in ("a", "b", "c"):
        guard._remember_orchestrate_origin(key, ORIGIN, "nonce")
    assert guard.take_orchestrate_origin("a") is None
    now[0] += 601
    assert guard.take_orchestrate_origin("b") is None
    guard._remember_orchestrate_origin("d", ORIGIN, "nonce")
    assert set(guard._ORCHESTRATE_ORIGINS) == {"d"}


def test_missing_call_ids_use_bound_snapshot_and_handler_fallback(host):
    host.start(speakers=(OWNER,), owner_side=True)
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0]) is None
    assert guard.orchestrate_handler_origin() == ORIGIN
    assert not guard._ORCHESTRATE_ORIGINS


@pytest.mark.parametrize("available", [False, True])
def test_owner_only_hint_mentions_only_available_conversation_tool(host, available):
    snapshot = host.start(speakers=(FRIEND,))
    host.model._last_resolved_tool_names = ["clawchat_get_conversation"] if available else []
    result = guard.orchestrate_owner_only("op", snapshot)
    assert ("; for group info use clawchat_get_conversation" in result["instruction"]) is available
    assert result["instruction"].endswith(".")
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session", turn_id="turn")["message"] == result["instruction"]


@pytest.mark.parametrize("tool", OWNER_ONLY_TOOLS)
def test_all_and_exact_person_grants_cannot_authorize_orchestration(host, caplog, tool):
    rows = [dict(grantee_user_id=FRIEND, what_kind="all", what="all", mode="direct"),
            dict(grantee_user_id=FRIEND, what_kind="tool", what=tool, mode="direct")]
    resolved = grants.resolve_turn_grants([FRIEND], OWNER, rows, 1, available_tools=[tool, "terminal"])
    assert resolved.get(tool) is None and tool not in resolved
    assert resolved.get("terminal") == "direct"
    assert grants.freeze_turn_grants({tool: "direct"}).get(tool) is None
    assert tool not in guard.public_tool_names([tool, "clawchat_get_conversation"])
    from clawchat_gateway import tool_capabilities as caps
    caps._PUBLIC_TOOLS_WARNED.clear()
    assert get_public_tools_override(["clawchat_get_conversation", tool], logging.getLogger(__name__)) is None
    assert "owner-only tool" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", OWNER_ONLY_TOOLS)
async def test_grant_and_request_handlers_reject_orchestration(host, monkeypatch, tool):
    snapshot = host.start(speakers=(OWNER,), owner_side=True)
    sender = SimpleNamespace(_store=Mock())
    monkeypatch.setattr(tools, "_grant_management_context", lambda _: (snapshot, sender, None))
    client = Mock()
    monkeypatch.setattr(tools, "_build_client", client)
    result = await tools.manage_tool_grants("grant", dict(userId=FRIEND, tools=[tool], mode="direct"))
    assert result["error"] == "validation"
    result = await tools.request_tool_access(dict(capability=tool, reason="check"))
    assert result["reason"] == "not-available"
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", [None, "", " \n\t", 1, [], False])
async def test_purpose_is_required_before_network(host, monkeypatch, purpose):
    host.start(speakers=(OWNER,), owner_side=True)
    client = Mock()
    monkeypatch.setattr(tools, "_build_client", client)
    for invoke in plugin_tools._ORCHESTRATION_HANDLERS.values():
        result = await invoke(tools, dict(agentId="agt_x", conversationId=CHAT, behavior="ok", description="ok",
                                        title="Crew", agentIds=["agt_x"], muted=False, code="abc", purpose=purpose))
        assert result == tools._validation_error("purpose is required")
    client.assert_not_called()


@pytest.mark.asyncio
async def test_settings_require_a_setting_beyond_purpose(host, monkeypatch):
    host.start(speakers=(OWNER,), owner_side=True)
    client = Mock()
    monkeypatch.setattr(tools, "_build_client", client)
    result = await tools.orchestrate_set_group_agent_settings(CHAT, "agt_x", purpose="check")
    assert result == tools._validation_error("set at least one of muted, replyMode, batchDelaySeconds")
    client.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["中文", "🦀😀", "+", "%", "%25", "two words", " " * 60 + "x", " " + "中😀+%%25 " * 20])
async def test_purpose_truncation_and_headers_for_all_routes(host, monkeypatch, purpose):
    host.start(speakers=(OWNER,), owner_side=True)
    client = api_client.ClawChatApiClient(base_url="https://example.invalid", token="t", user_id="usr_agent", device_id="dev_x")
    request = AsyncMock(return_value={"code": 0})
    monkeypatch.setattr(client, "_call_envelope", request)
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    for invoke in plugin_tools._ORCHESTRATION_HANDLERS.values():
        result = await invoke(tools, dict(agentId="agt_x", conversationId=CHAT, behavior="ok", description="ok",
                                        title="Crew", agentIds=["agt_x"], muted=False, code="abc", purpose=purpose))
        assert result == {"code": 0}
        headers = dict(request.await_args.kwargs["extra_headers"])
        assert headers.pop("content-type", "application/json") == "application/json"
        sent = purpose.strip()[:60].strip()
        assert sent and sent == unquote(headers["X-Clawchat-Purpose"])
        assert headers == {"X-Clawchat-Origin-Conversation": CHAT, "X-Clawchat-Origin-Initiator": OWNER,
                           "X-Clawchat-Purpose": quote(sent, safe="")}
        if request.await_args.kwargs.get("body"):
            assert "purpose" not in json.loads(request.await_args.kwargs["body"])


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [{}, {"conversation_id": CHAT}, {"initiator_id": OWNER}, dict(ORIGIN, purpose="")])
async def test_api_refuses_incomplete_origin_without_request(monkeypatch, origin):
    client = api_client.ClawChatApiClient(base_url="https://example.invalid", token="t", user_id="usr_agent", device_id="dev_x")
    request = AsyncMock()
    monkeypatch.setattr(client, "_call_envelope", request)
    with pytest.raises(ValueError):
        await client.orch_list_agents(origin=origin)
    request.assert_not_awaited()


@pytest.mark.parametrize("code", [21001, 21003, 21007, None, 29002])
def test_gate_fixtures_and_unknown_codes(code):
    suffix = str(code) if code in (21001, 21003, 21007) else "other"
    fixture = json.loads((Path(__file__).resolve().parents[1] / "clawchat_gateway/fixtures/permission_events" / f"gate-outcome-{suffix}.json").read_text())
    result = map_gate_outcome(fixture["wire"]["code"], fixture["wire"]["data"])
    expected = fixture["expected"]["result"]
    if code == 21001:
        from clawchat_gateway.gate_outcome import PERMISSION_RECEIPT_RULE_TEXT
        expected = dict(expected, instruction=PERMISSION_RECEIPT_RULE_TEXT)
    assert result == expected
    if code == 21007:
        assert map_gate_outcome(code, {"operation": 1}) == owner_turn_only("")
    if code in (None, 29002):
        assert map_gate_outcome(code, {}) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [21007, 21001, 21003, 29002])
async def test_only_21007_replaces_orchestration_envelope(host, monkeypatch, code):
    host.start(speakers=(OWNER,), owner_side=True)
    envelope = dict(code=code, data={"operation": "orchestrate.read"})
    client = SimpleNamespace(orch_list_agents=AsyncMock(return_value=envelope))
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    result = await tools.orchestrate_list_agents(purpose="check")
    assert result == (owner_turn_only("orchestrate.read") if code == 21007 else envelope)


@pytest.mark.parametrize("operation,outcome,retry", [("orchestrate.read", "approved_retry", True),
    ("orchestrate.read", "approved", False), ("group.manage", "approved_retry", False)])
def test_only_deduped_orchestration_retry_receipt_sets_private_marker(operation, outcome, retry):
    permission_result._seen_request_ids.clear()
    frame = dict(chat_id=CHAT, chat_type="group", payload={"metadata": dict(kind="permission_result",
                 request_id="prq_owner_only", operation=operation, outcome=outcome)})
    try:
        inbound = permission_result.handle_permission_result(frame)
        assert inbound.orchestrate_retry is retry
        assert inbound.grant_speakers == () and not inbound.is_owner_action
        assert permission_result.handle_permission_result(frame) is None
    finally:
        permission_result._seen_request_ids.clear()


@pytest.mark.parametrize("request_id", ["", None, "missing"], ids=["empty", "null", "missing"])
def test_orchestration_retry_receipt_without_request_id_gains_nothing(request_id):
    permission_result._seen_request_ids.clear()
    metadata = dict(kind="permission_result", operation="orchestrate.read", outcome="approved_retry")
    if request_id != "missing":
        metadata["request_id"] = request_id
    try:
        assert permission_result.handle_permission_result(
            dict(chat_id=CHAT, chat_type="group", payload={"metadata": metadata})) is None
        assert not permission_result._seen_request_ids
    finally:
        permission_result._seen_request_ids.clear()


def test_duplicate_orchestration_retry_receipt_gains_nothing_the_second_time():
    permission_result._seen_request_ids.clear()
    frame = dict(chat_id=CHAT, chat_type="group", payload={"metadata": dict(kind="permission_result",
                 request_id="prq_duplicate", operation="orchestrate.read", outcome="approved_retry")})
    try:
        assert permission_result.handle_permission_result(frame).orchestrate_retry
        for _ in range(2):
            assert permission_result.handle_permission_result(dict(frame)) is None
    finally:
        permission_result._seen_request_ids.clear()


def test_raw_fields_cannot_forge_retry_marker():
    from clawchat_gateway.config import ClawChatConfig
    raw = dict(chat_id=CHAT, chat_type="group", sender={"id": FRIEND}, text="hello",
               orchestrate_retry=True, _orchestrate_retry=True, synthetic=True, is_internal_synthetic=True)
    inbound = parse_inbound_message(raw, ClawChatConfig(websocket_url="wss://example.invalid"))
    assert inbound is not None and not inbound.orchestrate_retry
    assert not InboundMessage(CHAT, "group", FRIEND, "Friend", "hello", raw, _orchestrate_retry=True).orchestrate_retry


@pytest.mark.asyncio
@pytest.mark.parametrize("name,options,allowed", SCENARIOS, ids=[row[0] for row in SCENARIOS])
async def test_handler_fallback_enforces_the_same_owner_turn_matrix(host, monkeypatch, name, options, allowed):
    snapshot = host.start(**options)
    client = SimpleNamespace(orch_list_agents=AsyncMock(return_value={"code": 0}))
    build = Mock(return_value=(client, None))
    monkeypatch.setattr(tools, "_build_client", build)
    result = await tools.orchestrate_list_agents(purpose="check")
    if allowed:
        assert result == {"code": 0}
        client.orch_list_agents.assert_awaited_once_with(origin=dict(
            conversation_id=snapshot.chat_id, initiator_id=OWNER, purpose="check"))
    else:
        assert result == owner_turn_only(OWNER_ONLY_TOOLS[0])
        build.assert_not_called()


@pytest.mark.asyncio
async def test_missing_purpose_argument_is_validation_error(host, monkeypatch):
    host.start(speakers=(OWNER,), owner_side=True)
    build = Mock()
    monkeypatch.setattr(tools, "_build_client", build)
    assert await tools.orchestrate_list_agents() == tools._validation_error("purpose is required")
    build.assert_not_called()


def test_old_host_without_execution_binding_blocks_orchestration(host, monkeypatch):
    host.start(speakers=(OWNER,), owner_side=True)
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0])["action"] == "block"
    assert not guard._ORCHESTRATE_ORIGINS


def test_installed_catalog_filter_updates_the_actual_hint_tool_names(host):
    host.model.get_tool_definitions = lambda: DEFINITIONS
    assert guard._install_grant_catalog_filter()
    snapshot = host.start(speakers=(FRIEND,))
    names = {item["function"]["name"] for item in host.model.get_tool_definitions()}
    assert names.isdisjoint(OWNER_ONLY_TOOLS)
    assert "clawchat_get_conversation" in host.model._last_resolved_tool_names
    assert "; for group info use clawchat_get_conversation" in guard.orchestrate_owner_only("op", snapshot)["instruction"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["guest-overlap", "released", "initiator", "platform", "chat"])
async def test_cached_origin_rechecks_live_snapshot_before_building_client(host, monkeypatch, change):
    snapshot = host.start(orchestrate_retry=True)
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                           turn_id="turn", tool_call_id="call") is None
    if change == "guest-overlap":
        host.start(speakers=(FRIEND,))
        # A stale callback still carrying the original nonce must see its revoked authority.
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
    elif change == "released":
        turns.release_turn(snapshot.nonce)
        # Even a valid new owner snapshot cannot replace the released cached snapshot.
        host.start(speakers=(OWNER,), owner_side=True)
    elif change == "initiator":
        monkeypatch.setattr(guard, "_owner_user_id", lambda: FRIEND)
        assert guard.orchestrate_owner_turn(turns.get_snapshot(snapshot.nonce))["initiator_id"] == FRIEND
    elif change == "platform":
        host.session["HERMES_SESSION_PLATFORM"] = "telegram"
    else:
        host.session["HERMES_SESSION_CHAT_ID"] = "cnv_other"
    build = Mock()
    monkeypatch.setattr(tools, "_build_client", build)
    token = host.approval._approval_tool_call_id.set("call")
    try:
        assert (await tools.orchestrate_list_agents(purpose="check"))["status"] == "owner_turn_only"
        assert "call" not in guard._ORCHESTRATE_ORIGINS
        build.assert_not_called()
    finally:
        host.approval._approval_tool_call_id.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["retry-first", "owner-first"])
async def test_retry_and_owner_overlap_preserve_only_orchestration_in_both_orders(host, monkeypatch, order):
    retry = lambda: host.start(orchestrate_retry=True)
    owner = lambda: host.start(speakers=(OWNER,), owner_side=True)
    first, second = (retry(), owner()) if order == "retry-first" else (owner(), retry())
    client = SimpleNamespace(orch_list_agents=AsyncMock(return_value={"code": 0}))
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    for item in (first, second):
        snapshot = turns.get_snapshot(item.nonce)
        assert snapshot.orchestrate_retry and not snapshot.owner_side and not snapshot.legacy_owner_tools
        assert grants.evaluate_tool_grant(snapshot.grants, "terminal") is None
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
        names = {item["function"]["name"] for item in guard._filter_grant_definitions(DEFINITIONS, snapshot)}
        assert set(OWNER_ONLY_TOOLS) <= names and "terminal" not in names
        call_id = "overlap:" + snapshot.nonce
        assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                               turn_id="turn", tool_call_id=call_id) is None
        token = host.approval._approval_tool_call_id.set(call_id)
        try:
            assert await tools.orchestrate_list_agents(purpose="check") == {"code": 0}
        finally:
            host.approval._approval_tool_call_id.reset(token)
    assert client.orch_list_agents.await_count == 2
    host.start(speakers=(FRIEND,))
    for item in (first, second):
        snapshot = turns.get_snapshot(item.nonce)
        assert not snapshot.orchestrate_retry
        assert guard.orchestrate_owner_turn(snapshot) is None
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
        assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                               turn_id="turn")["action"] == "block"
        assert (await tools.orchestrate_list_agents(purpose="check"))["status"] == "owner_turn_only"
    assert client.orch_list_agents.await_count == 2


@pytest.mark.asyncio
async def test_owner_batch_after_ordinary_receipt_overlap_gains_no_orchestration(host, monkeypatch):
    receipt = host.start()
    owner = host.start(speakers=(OWNER,), owner_side=True)
    build = Mock()
    monkeypatch.setattr(tools, "_build_client", build)
    for item in (receipt, owner):
        snapshot = turns.get_snapshot(item.nonce)
        assert not snapshot.owner_side and not snapshot.orchestrate_retry
        assert guard.orchestrate_owner_turn(snapshot) is None
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
        names = {item["function"]["name"] for item in guard._filter_grant_definitions(DEFINITIONS, snapshot)}
        assert names.isdisjoint(OWNER_ONLY_TOOLS)
        assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                               turn_id="turn", tool_call_id="call:" + item.nonce)["action"] == "block"
        assert (await tools.orchestrate_list_agents(purpose="check"))["status"] == "owner_turn_only"
    build.assert_not_called()


def test_fresh_retry_receipt_after_ordinary_receipt_keeps_only_its_own_authority(host):
    receipt = host.start()
    retry = host.start(orchestrate_retry=True)
    assert guard.orchestrate_owner_turn(turns.get_snapshot(retry.nonce)) == ORIGIN
    assert guard.orchestrate_owner_turn(turns.get_snapshot(receipt.nonce)) is None


def test_ordinary_permission_result_overlap_revokes_orchestration_with_owner_side_as_before(host):
    owner = host.start(speakers=(OWNER,), owner_side=True)
    permission_result._seen_request_ids.clear()
    try:
        inbound = permission_result.handle_permission_result(dict(chat_id=CHAT, chat_type="group",
            payload={"metadata": dict(kind="permission_result", request_id="prq_ordinary_overlap",
                                      operation="orchestrate.read", outcome="approved")}))
        synthetic = host.start(speakers=inbound.grant_speakers, orchestrate_retry=inbound.orchestrate_retry)
        for item in (owner, synthetic):
            snapshot = turns.get_snapshot(item.nonce)
            assert not snapshot.owner_side and not snapshot.orchestrate_retry
            assert guard.orchestrate_owner_turn(snapshot) is None
            host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
            names = {item["function"]["name"] for item in guard._filter_grant_definitions(DEFINITIONS, snapshot)}
            assert names.isdisjoint(OWNER_ONLY_TOOLS)
            assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                                   turn_id="turn")["action"] == "block"
    finally:
        permission_result._seen_request_ids.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("visible", [False, True])
async def test_owner_only_hint_requires_conversation_tool_visible_in_this_snapshot(host, monkeypatch, visible):
    snapshot = host.start(speakers=(FRIEND,), grants={} if visible else {"clawchat_get_conversation": "deny"})
    host.model._last_resolved_tool_names = ["clawchat_get_conversation"]
    expected = owner_turn_only(OWNER_ONLY_TOOLS[0], conversation_available=visible)
    assert guard.orchestrate_owner_only(OWNER_ONLY_TOOLS[0], snapshot) == expected
    assert guard._clawchat_pre_tool_decision(tool_name=OWNER_ONLY_TOOLS[0], session_id="session",
                                           turn_id="turn")["message"] == expected["instruction"]
    build = Mock()
    monkeypatch.setattr(tools, "_build_client", build)
    assert await tools.orchestrate_list_agents(purpose="check") == expected
    build.assert_not_called()


def test_owner_only_hint_omits_conversation_tool_without_snapshot_or_when_filter_fails(host, monkeypatch):
    snapshot = host.start(speakers=(FRIEND,))
    host.model._last_resolved_tool_names = ["clawchat_get_conversation"]
    assert guard.orchestrate_owner_only("op") == owner_turn_only("op")
    monkeypatch.setattr(guard, "_filter_grant_definitions", Mock(side_effect=RuntimeError("filter failed")))
    assert guard.orchestrate_owner_only("op", snapshot) == owner_turn_only("op")


def test_unverified_input_revokes_derived_orchestration_of_live_owner_turns(host):
    owner = host.start(speakers=(OWNER,), owner_side=True)
    assert turns.get_snapshot(owner.nonce).orchestrate_retry
    turns.mark_unverified_input("usr_agent", "owner-only:" + CHAT)
    try:
        assert guard.orchestrate_owner_turn(turns.get_snapshot(owner.nonce)) is None
        # A later owner batch is downgraded by the mark too.
        assert guard.orchestrate_owner_turn(host.start(speakers=(OWNER,), owner_side=True)) is None
    finally:
        turns.clear_unverified_input("usr_agent", "owner-only:" + CHAT)


@pytest.mark.parametrize("order", ["owner-first", "action-first"])
def test_owner_and_owner_action_overlap_keep_orchestration_in_both_orders(host, order):
    owner = lambda: host.start(chat="cnv_owner", speakers=(OWNER,), owner_side=True)
    action = lambda: host.start(chat="cnv_owner", legacy_owner_tools=True)
    first, second = (owner(), action()) if order == "owner-first" else (action(), owner())
    for item in (first, second):
        snapshot = turns.get_snapshot(item.nonce)
        host.session["HERMES_SESSION_MESSAGE_ID"] = snapshot.nonce
        assert guard.orchestrate_owner_turn(snapshot) == dict(conversation_id="cnv_owner", initiator_id=OWNER)
    host.start(chat="cnv_owner", speakers=(FRIEND,))
    for item in (first, second):
        assert guard.orchestrate_owner_turn(turns.get_snapshot(item.nonce)) is None
