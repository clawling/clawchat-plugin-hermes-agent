"""Strict owner execution checks, server identities and confirmation cards."""
import asyncio
import json
import time
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from clawchat_gateway import tools, plugin_tools, host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.tool_grants import GRANT_BLOCK_MESSAGE, GRANT_TOOL_NAMES
from test_tool_grants_admission import harness, message, OWNER_CHAT, CHAT
from test_tool_grants_hook import host, context
from test_tool_grants_ask import approval

PARAMS = {"userIds": ["usr_a"], "tools": ["mcp__docs__read"], "mode": "direct"}
GROUP = "cnv_01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest_asyncio.fixture
async def management(harness, host, approval, monkeypatch, tmp_path):
    a = harness.adapter
    a._store = ClawChatStore(tmp_path / "grants.sqlite")
    a._store.initialize()
    a._owner_locale = lambda: "zh-CN"
    a._send_owner_text = AsyncMock(return_value=True)
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: a)
    client = SimpleNamespace(get_user_info=AsyncMock(return_value={"user": {"nickname": "张三"}}),
                             get_conversation=AsyncMock(return_value={"conversation": {"type": "group", "participants": [
                                 {"user_id": "usr_a"}, {"user_id": "usr_agent"}, {"user_id": "usr_owner"}]}}))
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    approval._approval_turn_id = ContextVar("approval_turn_id")
    await a._handle_inbound(message("usr_owner", chat=OWNER_CHAT))
    event = harness.events[-1]
    s = turns.get_snapshot(event.source.message_id)
    turns.bind_turn(s.nonce, "s", "t")
    token = approval._approval_turn_id.set("t")
    with host(**dict(context(s), HERMES_SESSION_USER_ID="usr_owner")):
        yield SimpleNamespace(adapter=a, snapshot=s, client=client, binder=host, approval=approval, harness=harness)
    approval._approval_turn_id.reset(token)


@pytest.mark.asyncio
async def test_owner_grant_sends_localized_card_without_code_in_model_result(management):
    h = management
    result = await plugin_tools.handle_clawchat_tool_grant(PARAMS, session_id="s")
    data = json.loads(result)
    assert data["pending"] is True and "do not say it is active" in data["note"]
    text = h.adapter._send_owner_text.call_args.args[1]
    code = text.split("码 ")[1][:6]
    assert code.isdigit() and code not in result and "code" not in data
    assert "允许：张三（usr_a）" in text
    assert h.adapter._store.list_grants(account_id="usr_agent") == []
    assert guard.clawchat_pre_tool_call(tool_name=GRANT_TOOL_NAMES[0], session_id="s", turn_id="t") is None
    incoming = message("usr_owner", f"/clawchat-allow {code}", chat=OWNER_CHAT)
    assert await h.adapter._consume_grant_confirmation(incoming)
    assert len(h.adapter._store.list_grants(account_id="usr_agent")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["synthetic", "other_sender", "missing_sender", "rejected_confirmation"])
@pytest.mark.parametrize("entry", ["message", "internal"])
async def test_unverified_input_revokes_running_owner_management_without_queueing(management, case, entry):
    h = management
    # A real owner merge must not hide the earlier nonce retained by the host.
    await h.adapter._handle_inbound(message("usr_owner", "second", chat=OWNER_CHAT))
    owner_nonces = [event.source.message_id for event in h.harness.events]
    assert all(turns.get_snapshot(nonce).owner_verified for nonce in owner_nonces)
    async def check_revoked_before_preparation(_):
        assert all(not turns.get_snapshot(nonce).owner_verified for nonce in owner_nonces)
        assert guard.management_snapshot("s", "t") is None
        return False
    h.harness.monkeypatch.setattr(h.adapter, "_maybe_consume_skill_update_consent", check_revoked_before_preparation)
    sender = "usr_a" if case == "other_sender" else "" if case == "missing_sender" else "usr_owner"
    incoming = message(sender, "/clawchat-allow 123456" if case == "rejected_confirmation" else "wake",
                       chat=OWNER_CHAT, synthetic=case in {"synthetic", "rejected_confirmation"})
    if entry == "message":
        await h.adapter._on_message(incoming.raw_message)
    else:
        await h.adapter._handle_inbound(incoming)
    assert not h.adapter._grant_admission_queue._pending
    assert all(not turns.get_snapshot(nonce).owner_verified for nonce in owner_nonces)
    if case == "rejected_confirmation":
        assert len(h.harness.events) == 2
    else:
        assert len(h.harness.events) == 3
    fresh = turns.active_snapshot("usr_agent", h.adapter._grant_session_key(incoming))
    assert not fresh.owner_verified and fresh.grants == {}
    for tool in GRANT_TOOL_NAMES:
        assert guard.clawchat_pre_tool_call(tool_name=tool, session_id="s", turn_id="t")["action"] == "block"
    assert (await tools.manage_tool_grants("grant", PARAMS, session_id="s"))["message"] == GRANT_BLOCK_MESSAGE
    h.adapter._send_owner_text.assert_not_awaited()
    h.harness.monkeypatch.setattr(h.adapter, "_maybe_consume_skill_update_consent", AsyncMock(return_value=False))
    await h.adapter._handle_inbound(message("usr_owner", "third", chat=OWNER_CHAT))
    assert all(not turns.get_snapshot(nonce).owner_verified for nonce in owner_nonces)
    assert guard.management_snapshot("s", "t") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["synthetic", "other_sender", "missing_sender"])
@pytest.mark.parametrize("entry", ["message", "internal"])
async def test_unverified_dispatch_race_denies_new_owner_nonce_until_idle(management, case, entry):
    h = management
    a = h.adapter
    entered, accept = asyncio.Event(), asyncio.Event()
    original_handle = a.handle_message
    unverified_events = []

    async def delayed_host(event):
        if event.text == "wake":
            unverified_events.append(event)
            entered.set()
            # Mirror the host's async profile read before busy admission.
            await accept.wait()
            # Queue debounce keeps the owner's source nonce. This input has
            # no processing_complete callback of its own after being merged.
            h.harness.events[-1].text += "\n" + event.text
        else:
            await original_handle(event)

    h.harness.monkeypatch.setattr(a, "handle_message", delayed_host)
    sender = "usr_a" if case == "other_sender" else "" if case == "missing_sender" else "usr_owner"
    incoming = message(sender, "wake", chat=OWNER_CHAT, synthetic=case == "synthetic")
    # There is no host guard during the delayed profile read. The plugin's
    # in-flight dispatch count must still prevent clearing the marker.
    a._active_sessions.clear()
    dispatch = asyncio.create_task(a._on_message(incoming.raw_message) if entry == "message"
                                   else a._handle_inbound(incoming))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert not turns.get_snapshot(h.snapshot.nonce).owner_verified
        await a._on_message(message("usr_owner", "during read", chat=OWNER_CHAT).raw_message)
        current = turns.get_snapshot(h.harness.events[-1].source.message_id)
        assert not current.owner_verified
        assert not a._grant_admission_queue._pending
        turns.bind_turn(current.nonce, "s", "t")
        with h.binder(**dict(context(current), HERMES_SESSION_USER_ID="usr_owner")):
            for tool in GRANT_TOOL_NAMES:
                assert guard.clawchat_pre_tool_call(tool_name=tool, session_id="s", turn_id="t")["action"] == "block"
            for operation in ("grant", "revoke", "list"):
                assert (await tools.manage_tool_grants(operation, PARAMS, session_id="s"))["message"] == GRANT_BLOCK_MESSAGE
        h.adapter._send_owner_text.assert_not_awaited()

        accept.set()
        await dispatch
        assert "wake" in h.harness.events[-1].text
        await a._on_message(message("usr_owner", "after merge", chat=OWNER_CHAT).raw_message)
        assert not turns.get_snapshot(h.harness.events[-1].source.message_id).owner_verified
        assert not a._grant_admission_queue._pending
        # No complete for the synthetic input: busy alone retains the marker.
        assert unverified_events[0] not in h.harness.events
        a._active_sessions.clear()
        await a._on_message(message("usr_owner", "after idle", chat=OWNER_CHAT).raw_message)
        recovered = turns.get_snapshot(h.harness.events[-1].source.message_id)
        assert recovered.owner_verified
        turns.bind_turn(recovered.nonce, "s", "t")
        with h.binder(**dict(context(recovered), HERMES_SESSION_USER_ID="usr_owner")):
            for tool in GRANT_TOOL_NAMES:
                assert guard.clawchat_pre_tool_call(tool_name=tool, session_id="s", turn_id="t") is None
            assert await tools.manage_tool_grants("list", {}, session_id="s") == {"grants": []}
    finally:
        accept.set()
        await dispatch
        for event in unverified_events:
            turns.release_turn(event.source.message_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("store", ["_pending_messages", "_text_debounce"])
async def test_host_held_input_keeps_unverified_marker(management, store):
    a = management.adapter
    await a._on_message(message("usr_owner", "wake", chat=OWNER_CHAT, synthetic=True).raw_message)
    a._active_sessions.clear()
    # The host still buffers that input; it can merge into the owner's next event.
    management.harness.monkeypatch.setattr(a, store, {f"agent:main:clawchat:dm:{OWNER_CHAT}": object()}, raising=False)
    await a._on_message(message("usr_owner", "while held", chat=OWNER_CHAT).raw_message)
    assert not turns.get_snapshot(management.harness.events[-1].source.message_id).owner_verified
    getattr(a, store).clear()
    a._active_sessions.clear()
    await a._on_message(message("usr_owner", "after flush", chat=OWNER_CHAT).raw_message)
    assert turns.get_snapshot(management.harness.events[-1].source.message_id).owner_verified


@pytest.mark.asyncio
async def test_unverified_complete_during_owner_dispatch_keeps_marker(management):
    h = management
    a = h.adapter
    await a._handle_inbound(message("usr_owner", "wake", chat=OWNER_CHAT, synthetic=True))
    unverified = h.harness.events[-1]
    entered, proceed = asyncio.Event(), asyncio.Event()

    async def download(_):
        entered.set()
        await proceed.wait()
        return []

    h.harness.monkeypatch.setattr(a, "_download_inbound_media", download)
    dispatch = asyncio.create_task(a._handle_inbound(message("usr_owner", "preparing", chat=OWNER_CHAT)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        a._active_sessions.clear()
        await a.on_processing_complete(unverified, "SUCCESS")
        proceed.set()
        await dispatch
        assert not turns.get_snapshot(h.harness.events[-1].source.message_id).owner_verified
        # Only a complete with host idle and no plugin dispatch can clear it.
        a._active_sessions.clear()
        await a.on_processing_complete(h.harness.events[-1], "SUCCESS")
        await a._handle_inbound(message("usr_owner", "recovered", chat=OWNER_CHAT))
        assert turns.get_snapshot(h.harness.events[-1].source.message_id).owner_verified
    finally:
        proceed.set()
        await dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_platform", "group", "friend_dm", "synthetic", "other_sender", "empty_sender", "off", "missing_turn", "unbound", "session_mismatch", "released"])
async def test_management_hook_and_handler_refuse_unreliable_owner_rounds(management, case):
    h = management
    s = h.snapshot
    values = dict(context(s), HERMES_SESSION_USER_ID="usr_owner")
    turn_id, session_id = "t", "s"
    if case in {"group", "friend_dm", "synthetic", "other_sender", "empty_sender"}:
        incoming = message("" if case == "empty_sender" else "usr_a" if case == "other_sender" else "usr_owner",
                           chat=CHAT if case == "friend_dm" else OWNER_CHAT,
                           kind="group" if case == "group" else "direct", synthetic=case == "synthetic")
        # Use adapter's actual snapshot generation, with no active-session merge.
        h.adapter._active_sessions.clear()
        await h.adapter._handle_inbound(incoming)
        nonce = next(reversed(h.adapter._grant_reply_anchors))
        s = turns.get_snapshot(nonce)
        assert not s.owner_verified
        turns.bind_turn(s.nonce, "s", "t")
        values = context(s, group=case == "group")
    elif case == "other_platform":
        values["HERMES_SESSION_PLATFORM"] = "telegram"
    elif case == "off":
        h.adapter._clawchat_config = replace(h.adapter._clawchat_config, tool_grants="off")
    elif case == "missing_turn":
        turn_id = ""
    elif case == "unbound":
        with turns._lock:
            turns._bindings.pop(s.nonce)
    elif case == "session_mismatch":
        session_id = "different"
    else:
        turns.release_turn(s.nonce)
    token = h.approval._approval_turn_id.set(turn_id)
    try:
        with h.binder(**values):
            if case == "off":
                from unittest.mock import patch
                with patch.object(guard, "_tool_grants_enabled", return_value=False):
                    blocked = guard.clawchat_pre_tool_call(tool_name=GRANT_TOOL_NAMES[0], session_id=session_id, turn_id=turn_id)
            else:
                blocked = guard.clawchat_pre_tool_call(tool_name=GRANT_TOOL_NAMES[0], session_id=session_id, turn_id=turn_id)
            assert blocked["action"] == "block"
            result = await tools.manage_tool_grants("grant", PARAMS, session_id=session_id)
            assert result["message"] == GRANT_BLOCK_MESSAGE
            h.adapter._send_owner_text.assert_not_awaited()
    finally:
        h.approval._approval_turn_id.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_name", "lookup_error", "wrong_profile_id", "missing_members", "invalid_member", "partial_count", "has_more"])
async def test_incomplete_names_or_group_membership_never_sends_card(management, case):
    h = management
    params = dict(PARAMS)
    if case == "missing_name":
        h.client.get_user_info.return_value = {"user": {"nickname": ""}}
    elif case == "lookup_error":
        h.client.get_user_info.side_effect = RuntimeError("private path")
    elif case == "wrong_profile_id":
        h.client.get_user_info.return_value = {"user": {"nickname": "Wrong", "id": "usr_wrong"}}
    else:
        params.pop("userIds")
        params["groupId"] = GROUP
        detail = h.client.get_conversation.return_value["conversation"]
        if case == "missing_members":
            detail.pop("participants")
        elif case == "invalid_member":
            detail["participants"].append({})
        elif case == "partial_count":
            detail["member_count"] = 9
        else:
            detail["has_more"] = True
    result = await tools.manage_tool_grants("grant", params, session_id="s")
    assert result["error"] == "validation"
    assert "private path" not in result["message"]
    h.adapter._send_owner_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_group_card_excludes_agent_and_owner_and_baseline_forces_ask(management):
    h = management
    params = {"groupId": GROUP, "tools": ["terminal"], "mode": "direct"}
    result = await tools.manage_tool_grants("grant", params, session_id="s")
    assert result["pending"]
    assert h.client.get_user_info.call_args.args == ("usr_a",)
    text = h.adapter._send_owner_text.call_args.args[1]
    assert "每次先问你" in text and "这类操作每次都会先问你。" in text
    assert "usr_agent" not in text and "usr_owner" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["send_false", "send_error", "store_error"])
async def test_card_and_storage_failures_never_report_success(management, failure):
    h = management
    if failure == "send_false":
        h.adapter._send_owner_text.return_value = False
    elif failure == "send_error":
        h.adapter._send_owner_text.side_effect = RuntimeError("private detail")
    else:
        h.adapter._store.create_pending_grant = lambda **_: (_ for _ in ()).throw(RuntimeError("private detail"))
    result = await tools.manage_tool_grants("grant", PARAMS, session_id="s")
    assert "error" in result and "pending" not in result and "code" not in result
    assert "private detail" not in result["message"]
    assert h.adapter._store.list_grants(account_id="usr_agent") == []


@pytest.mark.asyncio
async def test_revoke_counts_and_list_excludes_expired_rows(management):
    h = management
    store = h.adapter._store
    now = int(time.time() * 1000)
    for code, expiry in [("000001", None), ("000002", now + 1)]:
        payload = {"grantees": [{"id": "usr_a" if expiry is None else "usr_b", "name": "Person"}],
                   "tools": ["mcp__docs__read", "memory"], "mode": "direct", "expires_at": expiry}
        store.create_pending_grant(account_id="usr_agent", code=code, payload=payload, now=now)
        store.confirm_pending_grant(account_id="usr_agent", code=code, now=now)
    # Deterministically advance list time without waiting.
    original = store.list_grants
    store.list_grants = lambda **kw: original(**kw, now=now + 10)
    result = await tools.manage_tool_grants("list", {}, session_id="s")
    assert len(result["grants"]) == 2
    assert all(row["grantee_user_id"] == "usr_a" and row["grantee_name"] == "Person" for row in result["grants"])
    assert await tools.manage_tool_grants("revoke", {"userId": "usr_a", "tools": ["memory"]}, session_id="s") == {"revoked": 1}
    assert await tools.manage_tool_grants("revoke", {"userId": "usr_a"}, session_id="s") == {"revoked": 1}
    assert await tools.manage_tool_grants("revoke", {"userId": "usr_a"}, session_id="s") == {"revoked": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, dict(PARAMS, groupId=GROUP), dict(PARAMS, tools=["*"]),
                                      dict(PARAMS, tools=[GRANT_TOOL_NAMES[0]]), dict(PARAMS, expiresAt="yesterday")])
async def test_grant_validation_precedes_card_delivery(management, params):
    result = await tools.manage_tool_grants("grant", params, session_id="s")
    assert result["error"] == "validation"
    management.adapter._send_owner_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["lookup", "send", "store", "list", "validation"])
async def test_management_error_messages_do_not_disclose_private_details(management, failure):
    h = management
    private = "Ada approvals.mode tool-grants /tmp/private 0.21.0 terminal mcp__docs__read"
    params, operation = dict(PARAMS), "grant"
    if failure == "lookup":
        h.client.get_user_info.side_effect = RuntimeError(private)
    elif failure == "send":
        h.adapter._send_owner_text.side_effect = RuntimeError(private)
    elif failure == "store":
        h.adapter._store.create_pending_grant = lambda **_: (_ for _ in ()).throw(RuntimeError(private))
    elif failure == "list":
        operation, params = "list", {}
        h.adapter._store.list_grants = lambda **_: (_ for _ in ()).throw(RuntimeError(private))
    else:
        params["mode"] = private
    result = await tools.manage_tool_grants(operation, params, session_id="s")
    assert "error" in result
    for forbidden in private.split():
        assert forbidden not in result["message"]


def test_management_schema_manifest_and_nonowner_whitelist(monkeypatch):
    from pathlib import Path
    import yaml
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    calls = {}
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, group, schema, handler, **kw: calls.update({name: schema})))
    manifest = yaml.safe_load(Path("plugin.yaml").read_text())
    assert set(manifest["provides_tools"]) == set(calls)
    for name in GRANT_TOOL_NAMES:
        assert name in calls and calls[name]["parameters"]["additionalProperties"] is False
        assert guard.clawchat_pre_tool_call(tool_name=name)["action"] == "block"


@pytest.mark.parametrize("policy", ["off", "approve"])
@pytest.mark.parametrize("chat_type,user_id", [("dm", "usr_a"), ("group", "usr_a"), ("group", "usr_owner")])
@pytest.mark.parametrize("with_alias", [False, True])
def test_management_toolset_hidden_from_nonowner_schema_and_catalog(monkeypatch, policy, chat_type, user_id, with_alias):
    registered = {}
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": policy})
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, group, schema, handler, **kw:
                                registered.update({name: (group, schema)})))
    for name in GRANT_TOOL_NAMES:
        assert registered[name][0] == guard.GRANT_MANAGEMENT_TOOLSET
    groups = {group for group, _ in registered.values()} | {"web", "terminal"}
    if with_alias:
        groups.add("custom-clawchat")

    def resolve(group):
        if group == "custom-clawchat":
            return list(registered)
        return [name for name, (toolset, _) in registered.items() if toolset == group] + {
            "web": ["web_search"], "terminal": ["terminal"],
        }.get(group, [])

    monkeypatch.setattr(guard, "_platform_toolsets", lambda: groups)
    monkeypatch.setattr(guard, "_resolve_tools", resolve)
    monkeypatch.setattr(guard, "_mcp_server_names", lambda: set())
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: frozenset())
    source = SimpleNamespace(chat_id=CHAT, chat_type=chat_type, user_id=user_id)
    kept = guard.toolsets_for_source(source, owner_user_id="usr_owner")
    assert kept and guard.GRANT_MANAGEMENT_TOOLSET not in kept
    assert "custom-clawchat" not in kept
    catalog = {name for group in kept for name in resolve(group)}
    schemas = {name: schema for name, (_, schema) in registered.items() if name in catalog}
    assert not set(GRANT_TOOL_NAMES) & (catalog | schemas.keys())
    assert "clawchat_memory_read" in schemas and "web_search" in catalog
    assert ("terminal" in catalog) == (policy == "approve")
    owner = SimpleNamespace(chat_id=OWNER_CHAT, chat_type="dm", user_id="usr_owner")
    assert guard.toolsets_for_source(owner, owner_user_id="usr_owner") is None
    assert set(GRANT_TOOL_NAMES) <= {name for group in groups for name in resolve(group)}


@pytest.mark.parametrize("setting", ["off", "on", "read_error"])
def test_management_tools_registration_respects_setting(monkeypatch, setting):
    def config():
        if setting == "read_error":
            raise OSError("unreadable config")
        return {"tool-grants": setting}

    monkeypatch.setattr(guard, "_config_extra", config)
    registered = {}
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, group, schema, handler, **kw:
                                registered.update({name: group})))
    assert "clawchat_memory_read" in registered
    assert set(registered) & set(GRANT_TOOL_NAMES) == (set() if setting == "off" else set(GRANT_TOOL_NAMES))
    assert (guard.GRANT_MANAGEMENT_TOOLSET in registered.values()) == (setting != "off")
    if setting != "on":
        for name in GRANT_TOOL_NAMES:
            assert guard.clawchat_pre_tool_call(tool_name=name)["action"] == "block"


def test_configuration_documents_grant_precedence_and_visibility_limits():
    from pathlib import Path

    text = " ".join(Path("docs/configuration.md").read_text().split())
    assert "Effective per-person grants take precedence over `non-owner-host-tools`" in text
    assert "direct grants execute without this gate" in text
    assert "ask grants route each call to the owner's direct chat" in text
    assert "including normally open tools such as `web_search`" in text
    assert "Hermes before 0.20.1 has no layer 1" in text
    assert "management tools can remain visible to non-owner turns" in text
    assert "Per-person grants and the `non-owner-host-tools: approve` opt-in apply here too" in text
    assert "every call goes to the human gate" not in text
    assert "visible and every call in such a turn" not in text
