"""Offline contract check against real Hermes dispatch and its async worker bridge."""
import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

try:
    import model_tools
    from tools.approval_context import _approval_tool_call_id
    from tools.registry import registry
    from gateway.session_context import set_session_vars, clear_session_vars
except ImportError as exc:  # pragma: no cover - environment gate
    pytest.skip(f"needs a real Hermes Agent checkout on PYTHONPATH ({exc})", allow_module_level=True)

from clawchat_gateway import host_tools_guard as guard, tools, turn_grants as turns


@pytest.mark.parametrize("owner_side,guest_overlap", [(True, False), (False, False), (True, True)],
                         ids=["owner-worker", "guest-worker", "owner-revoked-worker"])
@pytest.mark.parametrize("tool_name", ["clawchat_orchestrate_list_agents", "clawchat_widget_send"])
def test_real_dispatch_propagates_call_and_session_context_to_async_worker(monkeypatch, owner_side, guest_overlap, tool_name):
    main_thread = threading.get_ident()
    overlap = None
    snapshot = turns.register_turn(account_id="usr_agent", chat_id="cnv_host_context",
        speakers=("usr_owner" if owner_side else "usr_friend",), grants={},
        session_key="real-host-context", owner_side=owner_side)
    turns.bind_turn(snapshot.nonce, "host-session", "host-turn")
    ids = model_tools._CallIds(task_id="host-task", session_id="host-session",
                             tool_call_id="owner-call" if owner_side else "guest-call", turn_id="host-turn")
    client = SimpleNamespace(orch_list_agents=AsyncMock(return_value={"code": 0, "data": {}}))
    build = Mock(return_value=(client, None))
    monkeypatch.setattr(tools, "_build_client", build)
    sender = SimpleNamespace(send_widget_message=AsyncMock(return_value={"sent": True}))
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: sender)
    name = "clawchat_test_orchestrate_context_probe"

    async def handler(args, **kwargs):
        observed = dict(thread=threading.get_ident(),
                        chat=turns.session_context_value("HERMES_SESSION_CHAT_ID"),
                        call_id=_approval_tool_call_id.get(),
                        task_id=kwargs.get("task_id"), session_id=kwargs.get("session_id"))
        observed["cached_nonce"] = guard._ORCHESTRATE_ORIGINS.get(observed["call_id"], (None, None))[1]
        observed["result"] = (await tools.widget_send('{"v":1,"id":"todo"}')
                              if tool_name == "clawchat_widget_send" else
                              await tools.orchestrate_list_agents(purpose=args["purpose"]))
        observed["consumed"] = guard.take_orchestrate_origin(observed["call_id"]) is None
        return json.dumps(observed)

    registry.register(name, "clawchat-test-context", {"name": name, "parameters": {"type": "object"}},
                      handler, is_async=True)

    async def gateway_turn():
        nonlocal overlap
        tokens = set_session_vars(platform="clawchat", chat_id=snapshot.chat_id, chat_type="group",
                                  user_id=snapshot.speakers[0], message_id=snapshot.nonce)
        try:
            assert asyncio.get_running_loop().is_running()
            decision = guard._clawchat_pre_tool_decision(tool_name=tool_name,
                         args={"purpose": "verify context"}, **ids.hook_kwargs())
            assert decision is None if owner_side else decision["action"] == "block"
            if guest_overlap:
                overlap = turns.register_turn(account_id=snapshot.account_id, chat_id=snapshot.chat_id,
                    speakers=("usr_friend",), grants={}, session_key="real-host-context")
                assert not turns.get_snapshot(snapshot.nonce).orchestrate_retry
            if owner_side:
                # Disable the MESSAGE_ID fallback while retaining the live cached snapshot.
                set_session_vars(platform="clawchat", chat_id=snapshot.chat_id, chat_type="group",
                                 user_id=snapshot.speakers[0], message_id="unavailable-fallback")
                assert turns.get_snapshot(snapshot.nonce) is not None
            with model_tools._approval_observability(ids):
                result = registry.dispatch(name, {"purpose": "verify context"},
                    task_id=ids.task_id, session_id=ids.session_id, user_task="check orchestration")
            observed = json.loads(result)
            assert observed["thread"] != main_thread
            assert observed["chat"] == snapshot.chat_id
            assert observed["call_id"] == ids.tool_call_id
            assert observed["task_id"] == ids.task_id and observed["session_id"] == ids.session_id
            assert observed["consumed"]
            assert observed["cached_nonce"] == (snapshot.nonce if owner_side else None)
            if owner_side and not guest_overlap:
                if tool_name == "clawchat_widget_send":
                    assert observed["result"]["sent"] is True
                    sender.send_widget_message.assert_awaited_once_with('{"v":1,"id":"todo"}', "todo")
                    build.assert_not_called()
                else:
                    assert observed["result"] == {"code": 0, "data": {}}
                    build.assert_called_once_with()
                    client.orch_list_agents.assert_awaited_once_with(origin=dict(
                        conversation_id=snapshot.chat_id, initiator_id="usr_owner", purpose="verify context"))
            else:
                assert observed["result"]["status"] == "owner_turn_only"
                build.assert_not_called()
                client.orch_list_agents.assert_not_awaited()
                sender.send_widget_message.assert_not_awaited()
        finally:
            clear_session_vars(tokens)

    try:
        asyncio.run(gateway_turn())
    finally:
        registry.deregister(name)
        turns.release_turn(snapshot.nonce)
        if overlap is not None:
            turns.release_turn(overlap.nonce)
        guard.take_orchestrate_origin(ids.tool_call_id)


@pytest.mark.asyncio
async def test_scheduled_heartbeat_without_owner_snapshot_keeps_both_tools_blocked(monkeypatch):
    from dataclasses import replace
    from gateway.run_goals import GatewayGoalsMixin
    from clawchat_gateway.adapter import ClawChatAdapter, BasePlatformAdapter
    from clawchat_gateway.terminal_send import clear_clawchat_mention_sender

    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id='usr_agent', owner_user_id='usr_owner')
    a._owner_direct_chat_id = lambda: 'cnv_owner'
    monkeypatch.setattr(guard, '_owner_user_id', lambda: 'usr_owner')
    monkeypatch.setattr(BasePlatformAdapter, 'handle_message', AsyncMock())
    source = a.build_source(chat_id='cnv_owner', chat_type='dm', user_id='usr_owner', message_id='old-message')
    event = GatewayGoalsMixin._synthetic_prompt_event(source, 'check in')
    event._heartbeat_session_id = 'heartbeat-session'
    build = Mock()
    monkeypatch.setattr(tools, '_build_client', build)
    tokens = set_session_vars(platform='clawchat', chat_id='cnv_owner', chat_type='dm',
                              user_id='usr_owner', message_id=event.source.message_id or '')
    try:
        assert not event.internal and not event.source.message_id
        await a.handle_message(event)
        assert not a._grant_sessions and turns.get_snapshot(event.source.message_id) is None
        for name in ('clawchat_widget_send', 'clawchat_orchestrate_list_agents'):
            assert guard._clawchat_pre_tool_decision(tool_name=name,
                session_id='heartbeat-session', turn_id='heartbeat-turn')['action'] == 'block'
        assert (await tools.widget_send())['status'] == 'owner_turn_only'
        assert (await tools.orchestrate_list_agents(purpose='check in'))['status'] == 'owner_turn_only'
        build.assert_not_called()
    finally:
        clear_session_vars(tokens)
        clear_clawchat_mention_sender(a)
