"""Strict widgets reuse the owner guard without approved-retry authority."""
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clawchat_gateway import host_tools_guard as guard, tools, plugin_tools, turn_grants as turns
from clawchat_gateway import tool_grants as grants
from clawchat_gateway.tool_capabilities import (
    OWNER_ONLY_TOOLS, OWNER_TURN_STRICT_TOOLS, TOOL_CAPABILITIES,
    get_public_tools_override, parse_grant_selector,
)
from clawchat_gateway.widget_send import WIDGET_OWNER_ONLY_MESSAGE, WIDGET_V1_GUIDE, widget_owner_only
from test_orchestrate_owner_only import host, OWNER, FRIEND, CHAT

TOOL = 'clawchat_widget_send'
DEFS = [{'function': {'name': name}} for name in OWNER_ONLY_TOOLS]


@pytest.mark.parametrize('options,allowed', [
    ({'chat': 'cnv_owner', 'speakers': (OWNER,), 'owner_side': True}, True),
    ({'speakers': (OWNER,), 'owner_side': True}, True),
    ({'chat': 'cnv_owner', 'legacy_owner_tools': True}, True),
    ({'chat': 'cnv_friend', 'speakers': (FRIEND,)}, False),
    ({'speakers': (FRIEND,)}, False),
    ({'speakers': (OWNER, FRIEND)}, False),
    ({'orchestrate_retry': True}, False),
    ({'speakers': (), 'approve_eligible': False}, False),
])
async def test_strict_owner_matrix_all_three_layers(host, monkeypatch, options, allowed):
    snapshot = host.start(**options)
    names = {d['function']['name'] for d in guard._filter_grant_definitions(DEFS, snapshot)}
    assert (TOOL in names) is allowed
    result = guard._clawchat_pre_tool_decision(tool_name=TOOL, args={},
        session_id='session', turn_id='turn', tool_call_id='widget-call')
    sender = SimpleNamespace(send_widget_message=AsyncMock(return_value={'sent': True}))
    monkeypatch.setattr(tools, 'get_clawchat_sender', lambda: sender)
    token = host.approval._approval_tool_call_id.set('widget-call')
    try:
        if allowed:
            assert result is None
            # Force the handler to use the remembered origin across the host bridge.
            host.session['HERMES_SESSION_MESSAGE_ID'] = 'missing'
            assert json.loads(await plugin_tools.handle_clawchat_widget_send({'widget': '{"v":1,"id":"x"}'}))['sent']
            sender.send_widget_message.assert_awaited_once()
        else:
            assert result == {'action': 'block', 'message': WIDGET_OWNER_ONLY_MESSAGE}
            assert await tools.widget_send() == widget_owner_only()
            assert await tools.widget_send('{"v":1,"id":"x"}') == widget_owner_only()
            sender.send_widget_message.assert_not_awaited()
        assert guard.take_orchestrate_origin('widget-call') is None
    finally:
        host.approval._approval_tool_call_id.reset(token)
    if options.get('orchestrate_retry'):
        assert guard.orchestrate_owner_turn(snapshot) is not None
        assert 'clawchat_orchestrate_list_agents' in names
        assert guard._clawchat_pre_tool_decision(tool_name='clawchat_orchestrate_list_agents',
            args={'purpose': 'check'}, session_id='session', turn_id='turn') is None


async def test_owner_guide_requires_no_purpose_or_connection(host, monkeypatch):
    host.start(chat='cnv_owner', speakers=(OWNER,), owner_side=True)
    monkeypatch.setattr(tools, 'get_clawchat_sender', lambda: None)
    assert await tools.widget_send() == {'message': WIDGET_V1_GUIDE}


@pytest.mark.parametrize('change', ['guest-overlap', 'released', 'owner', 'chat', 'platform', 'retry-overlap'])
async def test_remembered_origin_is_consumed_and_revalidated(host, monkeypatch, change):
    s = host.start(speakers=(OWNER,), owner_side=True)
    assert guard._clawchat_pre_tool_decision(tool_name=TOOL, session_id='session', turn_id='turn',
                                           tool_call_id='call') is None
    if change == 'guest-overlap':
        host.start(speakers=(FRIEND,))
    elif change == 'retry-overlap':
        host.start(orchestrate_retry=True)
    elif change == 'released':
        turns.release_turn(s.nonce)
        host.start(speakers=(OWNER,), owner_side=True)
    elif change == 'owner':
        monkeypatch.setattr(guard, '_owner_user_id', lambda: FRIEND)
    else:
        host.session['HERMES_SESSION_CHAT_ID' if change == 'chat' else 'HERMES_SESSION_PLATFORM'] = 'other'
    token = host.approval._approval_tool_call_id.set('call')
    try:
        assert await tools.widget_send() == widget_owner_only()
        assert guard.take_orchestrate_origin('call') is None
    finally:
        host.approval._approval_tool_call_id.reset(token)


@pytest.mark.parametrize('order', ['owner-first', 'retry-first'])
def test_retry_overlap_does_not_unlock_strict_tool(host, order):
    owner = lambda: host.start(speakers=(OWNER,), owner_side=True)
    retry = lambda: host.start(orchestrate_retry=True)
    first, second = (owner(), retry()) if order == 'owner-first' else (retry(), owner())
    for s in (first, second):
        s = turns.get_snapshot(s.nonce)
        assert s.orchestrate_retry and not s.owner_side
        assert guard.orchestrate_owner_turn(s)
        assert guard.orchestrate_owner_turn(s, strict=True) is None
        names = {d['function']['name'] for d in guard._filter_grant_definitions(DEFS, s)}
        assert TOOL not in names and 'clawchat_orchestrate_list_agents' in names


def test_public_override_all_and_exact_grants_never_expose_widget(host, caplog):
    assert TOOL in OWNER_ONLY_TOOLS and TOOL in OWNER_TURN_STRICT_TOOLS
    assert get_public_tools_override([TOOL], __import__('logging').getLogger('widget-test')) is None
    assert 'owner-only tool' in caplog.text
    assert TOOL not in guard.public_tool_names([TOOL])
    assert not grants.valid_grant_tools([TOOL])
    assert all(TOOL not in capability['tools'] for capability in TOOL_CAPABILITIES.values())
    with pytest.raises(ValueError, match='invalid-what'):
        parse_grant_selector('tool:' + TOOL)
    for kind, what in [('all', 'all'), ('tool', TOOL)]:
        compiled = grants.resolve_turn_grants([FRIEND], OWNER,
            [{'grantee_user_id': FRIEND, 'what_kind': kind, 'what': what, 'mode': 'direct'},
             {'grantee_user_id': FRIEND, 'what_kind': 'tool', 'what': 'terminal', 'mode': 'direct'}], 0,
            public_tools=[TOOL], available_tools=[TOOL, 'terminal'])
        assert grants.evaluate_tool_grant(compiled, 'terminal') == 'direct'
        assert grants.evaluate_tool_grant(compiled, TOOL) is None
        snapshot = host.start(chat='cnv_' + kind, speakers=(FRIEND,), grants=compiled)
        assert guard._filter_grant_definitions([{'function': {'name': TOOL}}], snapshot) == []


def test_owner_fork_does_not_offer_widget(host):
    s = host.start(speakers=(OWNER,), owner_side=True)
    assert guard._filter_grant_definitions([{'function': {'name': TOOL}}], s, owner_fork=True) == []
    assert guard._clawchat_pre_tool_decision(tool_name=TOOL, session_id='fork', turn_id='fork')['message'] == WIDGET_OWNER_ONLY_MESSAGE


@pytest.mark.parametrize('field,value', [('chat_id', 'other'), ('chat_id', ''), ('owner_side', False)])
def test_strict_origin_requires_same_context(host, field, value):
    s = host.start(speakers=(OWNER,), owner_side=True)
    assert guard.orchestrate_owner_turn(replace(s, **{field: value}), strict=True) is None


@pytest.mark.parametrize('chat,kind,allowed', [('cnv_owner', 'direct', True),
    ('cnv_friend', 'direct', False), ('cnv_owner', 'group', False)])
async def test_host_internal_turn_uses_existing_owner_dm_attribution(host, monkeypatch, chat, kind, allowed):
    from dataclasses import dataclass
    from clawchat_gateway.adapter import ClawChatAdapter, BasePlatformAdapter
    from clawchat_gateway.terminal_send import clear_clawchat_mention_sender

    @dataclass
    class Source:
        platform: str = 'clawchat'
        chat_id: str = chat
        chat_type: str = kind
        message_id: str = ''

    # Overlapping turns left registered by other tests would merge into this one.
    for name in ('_snapshots', '_bindings', '_active', '_session_nonces', '_released_turns'):
        monkeypatch.setattr(turns, name, {})
    monkeypatch.setattr(turns, '_unverified_inputs', set())
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id='usr_agent', owner_user_id=OWNER)
    a._owner_direct_chat_id = lambda: 'cnv_owner'
    monkeypatch.setattr(BasePlatformAdapter, 'handle_message', AsyncMock(), raising=False)
    event = SimpleNamespace(internal=True, source=Source())
    try:
        await a.handle_message(event)
        snapshot = turns.get_snapshot(event.source.message_id)
        assert bool(snapshot and snapshot.owner_side) is allowed
        host.session.update(HERMES_SESSION_PLATFORM='clawchat', HERMES_SESSION_CHAT_ID=chat,
                            HERMES_SESSION_MESSAGE_ID=event.source.message_id)
        if allowed:
            assert snapshot.speakers == (OWNER,) and not snapshot.owner_verified
            assert guard.orchestrate_owner_turn(snapshot, strict=True)
            turns.bind_turn(snapshot.nonce, 'session', 'turn')
            assert guard._clawchat_pre_tool_decision(tool_name=TOOL, session_id='session', turn_id='turn') is None
            assert await tools.widget_send() == {'message': WIDGET_V1_GUIDE}
            # Native cron scopes deliberately clear platform/chat rather than attributing their origin.
            host.session.update(HERMES_SESSION_PLATFORM='', HERMES_SESSION_CHAT_ID='')
        assert await tools.widget_send() == widget_owner_only()
    finally:
        for key in a._grant_sessions:
            turns.release_session(a._account_id(), key)
        clear_clawchat_mention_sender(a)
