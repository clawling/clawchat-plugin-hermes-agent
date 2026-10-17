"""Persistent access cards, trusted guest resumption and failure notices."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from dataclasses import replace
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from clawchat_gateway import host_tools_guard as guard, plugin_tools, tools, turn_grants as turns
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage, parse_inbound_message
from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.tool_capabilities import AUTHORIZATION_TOOLS, TOOL_CAPABILITIES, ORIGIN_INHERITANCE_POLICY, resolve_origin_grant_context
from clawchat_gateway.tool_grants import GRANT_WRITE_FAILED_MESSAGE, NON_OWNER_GRANTS_HINT, GRANT_BLOCK_MESSAGE
from clawchat_gateway.tool_requests import (
    REQUEST_CARD_TTL_MS, REQUEST_COOLDOWN_MS, REQUEST_WAKE_STALE_MS, REQUEST_DAILY_WINDOW_POLICY,
    REQUEST_COPY, build_request_card, build_request_notice, sanitize_request_text, resolve_request_daily_window,
)
from test_tool_grants_admission import harness, message, coalesced, CHAT, OWNER_CHAT, wait_for_events
from test_tool_grants_hook import host, context
from test_tool_grants_ask import approval

NOW = 1_800_000_000_000


@pytest.fixture
def store(tmp_path):
    value = ClawChatStore(tmp_path / 'requests.sqlite')
    value.initialize()
    return value


def origin(user='usr_guest', chat=CHAT, kind='direct'):
    return turns.RequestOrigin(user, chat, kind, chat + (':__direct__' if kind == 'direct' else ':' + user), 'msg-original', 'Please check the files')


def reserve(store, *, user='usr_guest', now=NOW, capability='terminal', account='agent', route=None):
    return store.reserve_request(account_id=account, owner_user_id='usr_owner', origin=route or origin(user),
        requester_name='Friend', capability_id=capability, reason='Check the result', now=now)


def decide(store, request, mode='direct', now=NOW + 1):
    return store.confirm_request(account_id=request['account_id'], code=request['code'], mode=mode,
        decided_by_user_id='usr_owner', now=now)


def fetch(store, request):
    return store.get_request(account_id=request['account_id'], request_id=request['request_id'])


def test_reserve_cooldown_daily_rolling_limit_and_denied(store):
    first = reserve(store)['request']
    assert reserve(store)['status'] == 'waiting'
    store.mark_sent(account_id='agent', request_id=first['request_id'], now=NOW)
    assert reserve(store, now=NOW + REQUEST_COOLDOWN_MS)['status'] == 'waiting'  # pending still waits
    decide(store, first, 'deny')
    assert reserve(store, now=NOW + REQUEST_COOLDOWN_MS + 1) == {'status': 'blocked', 'reason': 'denied'}
    for index in range(19):
        assert reserve(store, user=f'usr_other{index}')['status'] == 'sending'
    assert reserve(store, user='usr_twentyfirst') == {'status': 'blocked', 'reason': 'rate-limited'}
    assert reserve(store, user='usr_twentyfirst', now=NOW + REQUEST_CARD_TTL_MS)['status'] == 'sending'
    assert REQUEST_DAILY_WINDOW_POLICY == 'rolling-24h'
    assert resolve_request_daily_window(NOW, 'UTC') == {'start': NOW - REQUEST_CARD_TTL_MS, 'end': NOW}


def test_failed_send_counts_daily_quota_without_person_cooldown(store):
    for _ in range(20):
        request = reserve(store)['request']
        store.mark_failed(account_id='agent', request_id=request['request_id'], now=NOW)
    assert reserve(store) == {'status': 'blocked', 'reason': 'rate-limited'}
    assert reserve(store, now=NOW + REQUEST_CARD_TTL_MS)['status'] == 'sending'


def test_shared_code_collision_retries_and_exhaustion_rolls_back(store, monkeypatch):
    from clawchat_gateway import tool_grants
    store.create_pending_grant(account_id='agent', code='000001', now=NOW,
        payload=dict(grantees=[dict(id='usr_other', name='Friend')], tools=['terminal'], mode='direct'))
    codes = iter(['000001'] * 5 + ['000002'])
    monkeypatch.setattr(tool_grants, 'generate_grant_code', lambda: next(codes))
    assert reserve(store)['request']['code'] == '000002'
    calls = []
    monkeypatch.setattr(tool_grants, 'generate_grant_code', lambda: calls.append(1) or '000001')
    with pytest.raises(sqlite3.IntegrityError):
        reserve(store, user='usr_other')
    assert len(calls) == 6
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute('SELECT count(*) FROM tool_grant_requests').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM tool_grant_pending').fetchone()[0] == 2


@pytest.mark.parametrize('mode,status', [('direct', 'allowed'), ('ask', 'asked'), ('deny', 'denied')])
def test_sending_card_confirms_atomically_once(store, mode, status):
    request = reserve(store)['request']
    result = decide(store, request, mode)
    assert result['rows'][0]['what_kind'] == 'capability'
    assert result['rows'][0]['what'] == 'terminal' and result['rows'][0]['mode'] == mode
    assert result['request']['status'] == status
    assert result['request']['wake_state'] == 'pending'
    assert result['request']['decided_by_user_id'] == 'usr_owner'
    store.mark_sent(account_id='agent', request_id=request['request_id'], now=NOW + 2)
    assert fetch(store, request)['status'] == status
    assert decide(store, request, 'deny') is None


def test_confirmation_write_failure_rolls_back_everything(store, monkeypatch):
    request = reserve(store)['request']
    original = store._upsert_tool_grant
    def fail_after_write(*args):
        original(*args)
        raise RuntimeError('write failed')
    monkeypatch.setattr(store, '_upsert_tool_grant', fail_after_write)
    with pytest.raises(RuntimeError):
        decide(store, request)
    assert store.list_grants(account_id='agent', now=NOW) == []
    assert fetch(store, request)['status'] == 'sending'
    assert fetch(store, request)['wake_state'] == 'none'
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute('SELECT consumed_at FROM tool_grant_pending').fetchone() == (None,)


def test_concurrent_confirmation_and_wake_claim_only_once(store):
    request = reserve(store)['request']
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: decide(ClawChatStore(store.db_path), request), range(6)))
    assert sum(value is not None for value in results) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda _: ClawChatStore(store.db_path).claim_wake(account_id='agent', wake_key=request['wake_key'], now=NOW + 2), range(6)))
    assert sum(value is not None for value in claims) == 1


def test_concurrent_reservation_serializes_quota(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(ClawChatStore(store.db_path)), range(8)))
    assert [r['status'] for r in results].count('sending') == 1
    assert [r['status'] for r in results].count('waiting') == 7


def notices(store, *, now=NOW + 5, chat=CHAT, session=CHAT + ':__direct__', users=('usr_guest',)):
    return store.take_pending_notice(account_id='agent', requester_ids=users, session_key=session, chat_id=chat, chat_type='direct', now=now)


def test_restart_recovery_stale_dispatch_and_notice_once_original_route(store):
    first = reserve(store)['request']
    decide(store, first)
    restarted = ClawChatStore(store.db_path)
    assert restarted.recover_stale(account_id='agent', now=NOW + 2) == [first['wake_key']]
    restarted.claim_wake(account_id='agent', wake_key=first['wake_key'], now=NOW + 2)
    assert restarted.recover_stale(account_id='agent', now=NOW + 3) == []
    assert notices(restarted, now=NOW + 3) == []
    assert notices(restarted, now=NOW + REQUEST_WAKE_STALE_MS + 2, chat='cnv_other') == []
    assert notices(restarted, now=NOW + REQUEST_WAKE_STALE_MS + 2, session='other') == []
    assert notices(restarted, now=NOW + REQUEST_WAKE_STALE_MS + 2, users=('usr_other',)) == []
    notice = notices(restarted, now=NOW + REQUEST_WAKE_STALE_MS + 2)
    assert len(notice) == 1 and notice[0]['last_error_code'] == 'dispatch-timeout'
    assert fetch(restarted, first)['wake_notified_at'] is None
    restarted.finish_pending_notice(account_id='agent', claim_key=notice[0]['notice_claim_key'], accepted=True,
                                    now=NOW + REQUEST_WAKE_STALE_MS + 2)
    assert notices(restarted, now=NOW + REQUEST_WAKE_STALE_MS + 2) == []


def test_take_notice_itself_recovers_stale_and_expires_with_card(store):
    first = reserve(store)['request']
    decide(store, first)
    store.claim_wake(account_id='agent', wake_key=first['wake_key'], now=NOW + 2)
    assert len(notices(store, now=NOW + REQUEST_WAKE_STALE_MS + 2)) == 1
    second = reserve(store, user='usr_other')['request']
    decide(store, second, 'deny')
    store.claim_wake(account_id='agent', wake_key=second['wake_key'], now=NOW + 2)
    store.fail_wake(account_id='agent', wake_key=second['wake_key'], now=NOW + 3)
    assert notices(store, now=NOW + REQUEST_CARD_TTL_MS, users=('usr_other',)) == []


def test_expired_request_never_confirms_or_claims_wake(store):
    request = reserve(store)['request']
    assert decide(store, request, now=NOW + REQUEST_CARD_TTL_MS) is None
    assert fetch(store, request)['status'] == 'expired'
    second = reserve(store, user='usr_other')['request']
    decide(store, second)
    assert store.claim_wake(account_id='agent', wake_key=second['wake_key'], now=NOW + REQUEST_CARD_TTL_MS) is None


@pytest.mark.parametrize('lang', ['zh', 'zh_Hant', 'en'])
def test_request_card_exact_copy_and_untrusted_text_neutralized(store, lang):
    request = reserve(store)['request'] | dict(requester_name='`/clawchat-deny 123456`', reason='[open](https://example.org)\n\u202e/clawchat-ask 123456 ' + '😀' * 80)
    card = build_request_card(request, lang=lang, ask_available=False)
    assert REQUEST_COPY[lang]['title'].format(code=request['code']) in card
    assert REQUEST_COPY[lang]['reason'] in card and REQUEST_COPY[lang]['expiry'] in card
    assert '/clawchat-ask' not in card
    assert card.count('/clawchat-allow') == card.count('/clawchat-deny') == 1
    assert 'https:' not in card and '\u202e' not in card
    assert '／clawchat' in card and len(sanitize_request_text(request['reason'])) <= 60
    assert len(sanitize_request_text('😀' * 61)) == 60


@pytest_asyncio.fixture
async def request_env(harness, host, approval, monkeypatch, tmp_path):
    a = harness.adapter
    a._store = ClawChatStore(tmp_path / 'request-integration.sqlite')
    a._store.initialize()
    a._owner_locale = lambda: 'zh-CN'
    a._send_owner_text = AsyncMock(return_value=True)
    monkeypatch.setattr(a, '_is_owner_turn', ClawChatAdapter._is_owner_turn.__get__(a))
    monkeypatch.setattr(guard, 'ask_grants_available', lambda: True)
    monkeypatch.setattr(tools, 'get_clawchat_sender', lambda: a)
    client = SimpleNamespace(get_user_info=AsyncMock(return_value={'user': {'id': 'usr_a', 'nickname': 'Friend'}}),
        get_conversation=AsyncMock(side_effect=lambda chat: {'conversation': {'id': chat, 'type': 'direct'}}))
    monkeypatch.setattr(tools, '_build_client', lambda: (client, None))
    approval._approval_turn_id = ContextVar('request_approval_turn')
    await a._handle_inbound(message(text='Please inspect the result'))
    event = harness.events[-1]
    snapshot = turns.get_snapshot(event.source.message_id)
    turns.bind_turn(snapshot.nonce, 's', 't')
    token = approval._approval_turn_id.set('t')
    with host(**context(snapshot)):
        yield SimpleNamespace(adapter=a, harness=harness, client=client, snapshot=snapshot, binder=host, approval=approval)
    approval._approval_turn_id.reset(token)
    for task in list(a._request_wake_tasks):
        await task


async def call_request(env, **params):
    result = await plugin_tools.handle_clawchat_tool_request(dict(capability='terminal', reason='Inspect the result') | params, session_id='s')
    return json.loads(result)


def latest_request(env):
    with sqlite3.connect(env.adapter._store.db_path) as conn:
        id_ = conn.execute('SELECT request_id FROM tool_grant_requests ORDER BY created_at DESC LIMIT 1').fetchone()[0]
    return env.adapter._store.get_request(account_id='usr_agent', request_id=id_)


@pytest.mark.asyncio
async def test_guest_requests_owner_dm_card_with_pending_guidance(request_env):
    h = request_env
    assert guard.clawchat_pre_tool_call(tool_name='clawchat_tool_request', session_id='s', turn_id='t') is None
    result = await call_request(h)
    assert result['status'] == 'pending' and result['sent']
    assert result['note'].startswith('I have asked the owner. I will continue once they approve.')
    h.adapter._send_owner_text.assert_awaited_once()
    assert h.adapter._send_owner_text.call_args.args[0] == OWNER_CHAT
    card = h.adapter._send_owner_text.call_args.args[1]
    request = latest_request(h)
    assert request['code'] not in json.dumps(result) and '/clawchat-ask ' + request['code'] in card
    assert request['source_chat_id'] == CHAT
    assert (await call_request(h))['status'] == 'waiting'
    assert h.adapter._send_owner_text.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,command', [('direct', 'allow'), ('ask', 'ask'), ('deny', 'deny')])
async def test_owner_selection_wakes_original_guest_with_fresh_grants_once(request_env, mode, command):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    # Finish the previous guest turn so wake can enter its own turn.
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    # Owner decision writes access, then a separate internal task wakes the guest.
    incoming = message('usr_owner', f'/clawchat-{command} {request["code"]}', chat=OWNER_CHAT)
    assert await h.adapter._consume_grant_confirmation(incoming)
    await asyncio.gather(*h.adapter._request_wake_tasks)
    assert len(h.harness.events) == 2
    event = h.harness.events[-1]
    s = turns.get_snapshot(event.source.message_id)
    assert s.speakers == ('usr_a',) and s.non_owner_speakers == {'usr_a'}
    assert not s.owner_side and not s.owner_verified and not s.legacy_owner_tools
    assert event.source.user_id == 'usr_a' and event.source.chat_id == CHAT
    assert s.grants.get('terminal') == mode
    assert ('Continue the request' in event.text) is (mode != 'deny')
    assert latest_request(h)['wake_state'] == 'delivered'
    await h.adapter._dispatch_request_wake(request['wake_key'])
    await h.adapter._consume_grant_confirmation(incoming)
    assert len(h.harness.events) == 2
    if mode == 'deny':
        assert '未向 Friend 开放 终端' in h.adapter._send_owner_text.call_args_list[-2].args[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['guest', 'other-dm', 'group', 'internal', 'wire-synthetic', 'off'])
@pytest.mark.parametrize('command', ['allow', 'ask', 'deny'])
async def test_three_commands_rejected_and_consumed_before_model(request_env, case, command):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    incoming = message('usr_owner', f'/clawchat-{command} {request["code"]}', chat=OWNER_CHAT)
    if case == 'guest':
        incoming = replace(incoming, sender_id='usr_other')
    elif case == 'other-dm':
        incoming = replace(incoming, chat_id='cnv_elsewhere')
    elif case == 'group':
        incoming = replace(incoming, chat_type='group')
    elif case == 'internal':
        incoming = InboundMessage.internal_synthetic(chat_id=OWNER_CHAT, chat_type='direct', sender_id='usr_owner', sender_name='Owner', text=incoming.text, raw_message={})
    elif case == 'wire-synthetic':
        incoming = replace(incoming, raw_message={'synthetic': True})
    else:
        h.adapter._clawchat_config = replace(h.adapter._clawchat_config, tool_grants='off')
    await h.adapter._handle_inbound(incoming)
    assert len(h.harness.events) == 1
    assert latest_request(h)['status'] == 'pending'
    assert h.adapter._send_owner_text.await_count == 1


@pytest.mark.asyncio
async def test_ask_unavailable_card_and_confirmation_refuse(request_env, monkeypatch):
    h = request_env
    monkeypatch.setattr(guard, 'ask_grants_available', lambda: False)
    await call_request(h)
    assert '/clawchat-ask' not in h.adapter._send_owner_text.call_args.args[1]
    request = latest_request(h)
    await h.adapter._consume_grant_confirmation(message('usr_owner', f'/clawchat-ask {request["code"]}', chat=OWNER_CHAT))
    assert latest_request(h)['status'] == 'pending'
    assert '做不到每次问你' in h.adapter._send_owner_text.call_args.args[1]
    assert h.adapter._store.list_grants(account_id='usr_agent') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['false', 'raise', 'storage'])
async def test_send_or_storage_failure_never_claims_owner_was_asked(request_env, monkeypatch, failure):
    h = request_env
    if failure == 'storage':
        monkeypatch.setattr(h.adapter._store, 'reserve_request', lambda **_: (_ for _ in ()).throw(RuntimeError()))
    else:
        h.adapter._send_owner_text = AsyncMock(return_value=False, side_effect=RuntimeError() if failure == 'raise' else None)
    result = await call_request(h)
    assert result['status'] == 'failed' and not result['sent']
    assert result['error'] == ('storage-unavailable' if failure == 'storage' else 'send-failed')
    assert 'Do not say the owner was asked.' in result['note']
    if failure != 'storage':
        assert latest_request(h)['status'] == 'failed'


@pytest.mark.asyncio
async def test_adapter_failed_confirmation_rolls_back_and_receipts_write_failure(request_env, monkeypatch):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    monkeypatch.setattr(h.adapter._store, '_upsert_tool_grant', lambda *_: (_ for _ in ()).throw(RuntimeError()))
    await h.adapter._consume_grant_confirmation(message('usr_owner', f'/clawchat-allow {request["code"]}', chat=OWNER_CHAT))
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_WRITE_FAILED_MESSAGE['zh']
    assert latest_request(h)['status'] == 'pending' and latest_request(h)['wake_state'] == 'none'


@pytest.mark.asyncio
@pytest.mark.parametrize('params', [dict(userId='usr_other'), dict(chatId='cnv_other'), dict(mode='direct'), dict(capability='all'), dict(capability='read_file'), dict(reason='')])
async def test_request_rejects_extra_fields_all_and_tool_names(request_env, params):
    result = await call_request(request_env, **params)
    assert result.get('error') or result['status'] == 'blocked'
    request_env.adapter._send_owner_text.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case,reason', [('deny', 'denied'), ('mixed', 'batch-intersection'), ('unavailable', 'not-available'), ('released', 'context-unavailable')])
async def test_request_blocks_deny_intersection_unavailable_and_released_origin(request_env, monkeypatch, case, reason):
    h = request_env
    if case in ('deny', 'mixed'):
        current = turns.get_snapshot(h.snapshot.nonce)
        rows = [dict(grantee_user_id='usr_a', grantee_name='Friend', what_kind='capability', what='terminal', mode='deny', expires_at=None)] if case == 'deny' else []
        from clawchat_gateway.tool_grants import resolve_turn_grants
        speakers = ('usr_a', 'usr_other') if case == 'mixed' else ('usr_a',)
        turns._snapshots[current.nonce] = replace(current, grants=resolve_turn_grants(list(speakers), 'usr_owner', rows, int(time.time()*1000)), non_owner_speakers=frozenset(speakers))
    elif case == 'unavailable':
        monkeypatch.setattr(guard, 'host_tool_catalog', lambda: (frozenset(), frozenset()))
    else:
        turns.release_turn(h.snapshot.nonce)
    assert (await call_request(h))['reason'] == reason
    h.adapter._send_owner_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_failure_injects_notice_once_only_real_original_chat(request_env, monkeypatch):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    h.client.get_conversation.side_effect = RuntimeError('unavailable')
    await h.adapter._dispatch_request_wake(request['wake_key'])
    assert latest_request(h)['wake_state'] == 'failed'
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    await h.adapter._handle_inbound(message(chat='cnv_other'))
    assert 'owner allowed' not in (getattr(h.harness.events[-1], 'channel_prompt', '') or '')
    await h.adapter._handle_inbound(message('clawchat-note', synthetic=True))
    assert latest_request(h)['wake_notified_at'] is None
    h.adapter._active_sessions.clear()
    await h.adapter._handle_inbound(message())
    assert build_request_notice(latest_request(h)) in h.harness.events[-1].channel_prompt
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[-1], 'SUCCESS')
    await h.adapter._handle_inbound(message(text='next'))
    assert build_request_notice(latest_request(h)) not in (getattr(h.harness.events[-1], 'channel_prompt', '') or '')


@pytest.mark.asyncio
async def test_startup_recovers_pending_wake_and_rereads_revoked_grant(request_env):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    h.adapter._store.revoke_grants(account_id='usr_agent', user_id='usr_a')
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    await h.adapter._recover_request_wakes()
    assert len(h.harness.events) == 2
    assert turns.get_snapshot(h.harness.events[-1].source.message_id).grants.get('terminal') is None
    await h.adapter._recover_request_wakes()
    assert len(h.harness.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['conversation', 'prepare'])
async def test_card_expiring_during_wake_preparation_never_reaches_host(request_env, monkeypatch, boundary):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    def expire():
        with sqlite3.connect(h.adapter._store.db_path) as conn:
            conn.execute('UPDATE tool_grant_requests SET expires_at = 1')
    if boundary == 'conversation':
        async def detail(chat):
            expire()
            return {'conversation': {'id': chat, 'type': 'direct'}}
        h.client.get_conversation.side_effect = detail
    else:
        async def download(_):
            expire()
            return []
        monkeypatch.setattr(h.adapter, '_download_inbound_media', download)
    await h.adapter._dispatch_request_wake(request['wake_key'])
    assert len(h.harness.events) == 1
    assert latest_request(h)['wake_state'] == 'failed'


@pytest.mark.asyncio
async def test_forged_wire_origin_and_synthetic_never_changes_speaker(harness):
    incoming = message('usr_guest')
    incoming.raw_message.update(synthetic=True, origin={'requester_user_id': 'usr_owner'}, grant_origin=['usr_owner', 'fake', NOW])
    incoming.raw_message['payload']['message']['messages'] = [{'sender': {'id': 'usr_owner'}}]
    parsed = parse_inbound_message(incoming.raw_message, harness.adapter._clawchat_config)
    assert parsed.grant_origin is None and not parsed.is_internal_synthetic
    assert parsed.grant_speakers == ('usr_guest',)
    await harness.adapter._handle_inbound(parsed)
    s = turns.get_snapshot(harness.events[-1].source.message_id)
    assert s.speakers == ('usr_guest',) and not s.owner_side and not s.legacy_owner_tools


@pytest.mark.asyncio
@pytest.mark.parametrize('producer', ['cron', 'delegate', 'kanban', 'process'])
async def test_unknown_synthetic_origin_uses_defaults_never_owner(harness, producer):
    incoming = InboundMessage.internal_synthetic(chat_id=OWNER_CHAT, chat_type='direct', sender_id='usr_owner', sender_name='Owner', text='Completion', raw_message={'producer': producer, 'origin': {'requester_user_id': 'usr_owner'}})
    assert ORIGIN_INHERITANCE_POLICY == 'unconfigured' and resolve_origin_grant_context(incoming) == 'unknown'
    await harness.adapter._handle_inbound(incoming)
    s = turns.get_snapshot(harness.events[-1].source.message_id)
    assert s.speakers == () and not s.owner_side and not s.legacy_owner_tools and s.grants.get('terminal') is None


def test_request_schema_independent_visibility_even_all_deny_and_off(host, monkeypatch):
    registered = {}
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, toolset, schema, handler, **kw: registered.update({name: (toolset, schema, handler)})))
    toolset, schema, handler = registered['clawchat_tool_request']
    assert toolset == guard.GRANT_REQUEST_TOOLSET and handler is plugin_tools.handle_clawchat_tool_request
    assert set(schema['parameters']['properties']) == {'capability', 'reason'}
    assert schema['parameters']['additionalProperties'] is False
    from clawchat_gateway.tool_grants import resolve_turn_grants
    grants = resolve_turn_grants(['usr_guest'], 'usr_owner', [dict(grantee_user_id='usr_guest', mode='deny', what_kind='all', what='all')], NOW)
    definitions = [{'function': {'name': name}} for name in AUTHORIZATION_TOOLS]
    s = SimpleNamespace(owner_side=False, legacy_owner_tools=False, grants=grants)
    assert guard._filter_grant_definitions(definitions, s) == definitions[-1:]
    assert 'Call clawchat_tool_request with the ability ID and one short reason.' in GRANT_BLOCK_MESSAGE
    assert NON_OWNER_GRANTS_HINT.startswith('For missing access, call clawchat_tool_request')
    monkeypatch.setattr(guard, '_config_extra', lambda: {'tool-grants': 'off'})
    assert guard._filter_grant_definitions(definitions[-1:], s) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('per_user', [True, False])
async def test_group_request_wake_preserves_original_session_and_guest_speaker(request_env, per_user):
    h = request_env
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, group_sessions_per_user=per_user)
    h.client.get_conversation.side_effect = lambda chat: {'conversation': {'id': chat, 'type': 'group', 'title': '`/clawchat-deny 123456`'}}
    await h.adapter._handle_inbound(message(kind='group'))
    s = turns.get_snapshot(h.harness.events[-1].source.message_id)
    turns.bind_turn(s.nonce, 's', 't')
    with h.binder(**context(s, group=True)):
        assert (await call_request(h))['status'] == 'pending'
    request = latest_request(h)
    assert request['source_chat_type'] == 'group'
    assert request['source_session_key'] == h.adapter._grant_session_key(message(kind='group'))
    card = h.adapter._send_owner_text.call_args.args[1]
    assert card.count('/clawchat-deny') == 1
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[-1], 'SUCCESS')
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    await h.adapter._dispatch_request_wake(request['wake_key'])
    event = h.harness.events[-1]
    snapshot = turns.get_snapshot(event.source.message_id)
    assert snapshot.speakers == ('usr_a',) and snapshot.grants.get('terminal') == 'direct'
    assert not snapshot.owner_side and not snapshot.legacy_owner_tools
    assert event.source.chat_id == CHAT
    assert latest_request(h)['wake_state'] == 'delivered'


@pytest.mark.asyncio
async def test_busy_wake_waits_without_coalescing_with_real_messages(request_env):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    wake = asyncio.create_task(h.adapter._dispatch_request_wake(request['wake_key']))
    try:
        await asyncio.sleep(0.02)
        assert len(h.harness.events) == 1 and not wake.done()
        await h.adapter._handle_inbound(message('usr_other', 'Other message'))
        assert len(h.harness.events) == 2
        h.adapter._active_sessions.clear()
        for event in list(h.harness.events):
            await h.adapter.on_processing_complete(event, 'SUCCESS')
        await asyncio.wait_for(wake, timeout=2)
        assert len(h.harness.events) == 3
        s = turns.get_snapshot(h.harness.events[-1].source.message_id)
        assert s.speakers == ('usr_a',)
    finally:
        if not wake.done():
            wake.cancel()
            await asyncio.gather(wake, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('command', ['ask', 'deny'])
async def test_proactive_grant_cards_reject_other_selection_commands(request_env, command):
    h = request_env
    h.adapter._store.create_pending_grant(account_id='usr_agent', code='000999',
        payload=dict(grantees=[dict(id='usr_a', name='Friend')], tools=['terminal'], mode='ask'))
    incoming = message('usr_owner', f'/clawchat-{command} 000999', chat=OWNER_CHAT)
    assert await h.adapter._consume_grant_confirmation(incoming)
    assert h.adapter._store.list_grants(account_id='usr_agent') == []
    await h.adapter._consume_grant_confirmation(replace(incoming, text='/clawchat-allow 000999'))
    assert h.adapter._store.list_grants(account_id='usr_agent')[0]['mode'] == 'ask'


@pytest.mark.asyncio
async def test_connect_ready_schedules_wake_recovery(request_env, monkeypatch):
    h = request_env
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, websocket_url='wss://example.invalid', token='example')
    monkeypatch.setattr(h.adapter, '_spawn_plugin_report', lambda **_: None)
    monkeypatch.setattr(h.adapter, '_connection', SimpleNamespace(start=AsyncMock(), wait_until_ready=AsyncMock(return_value=True)))
    recovery = AsyncMock()
    monkeypatch.setattr(h.adapter, '_recover_request_wakes', recovery)
    assert await h.adapter.connect()
    await asyncio.gather(*h.adapter._request_wake_tasks)
    recovery.assert_awaited_once()


def test_concurrent_account_requests_cannot_exceed_daily_quota(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda index: reserve(ClawChatStore(store.db_path), user=f'usr_guest{index}'), range(25)))
    assert sum(result['status'] == 'sending' for result in results) == 20
    assert sum(result == {'status': 'blocked', 'reason': 'rate-limited'} for result in results) == 5


@pytest.mark.asyncio
async def test_host_silently_rejecting_wake_keeps_pending_notice(request_env, monkeypatch):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    async def reject(event):
        event._gateway_accepted = False
    monkeypatch.setattr(h.adapter, 'handle_message', reject)
    await h.adapter._dispatch_request_wake(request['wake_key'])
    assert latest_request(h)['wake_state'] == 'failed'
    assert latest_request(h)['wake_notified_at'] is None
    assert h.adapter._store.list_grants(account_id='usr_agent')[0]['mode'] == 'direct'


@pytest.mark.asyncio
async def test_startup_busy_chat_does_not_hold_other_pending_wakes(request_env):
    h = request_env
    await call_request(h)
    first = latest_request(h)
    second = h.adapter._store.reserve_request(account_id='usr_agent', owner_user_id='usr_owner',
        origin=origin('usr_other', 'cnv_elsewhere'), requester_name='Another friend', capability_id='terminal', reason='Check the result')['request']
    for request in (first, second):
        h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    recovery = asyncio.create_task(h.adapter._recover_request_wakes())
    try:
        await wait_for_events(h.harness, 2)
        assert h.harness.events[-1].source.chat_id == 'cnv_elsewhere'
        assert not recovery.done()
        h.adapter._active_sessions.clear()
        for event in list(h.harness.events):
            await h.adapter.on_processing_complete(event, 'SUCCESS')
        await asyncio.wait_for(recovery, timeout=2)
        assert len(h.harness.events) == 3
        assert h.harness.events[-1].source.chat_id == CHAT
    finally:
        if not recovery.done():
            recovery.cancel()
            await asyncio.gather(recovery, return_exceptions=True)


@pytest.mark.asyncio
async def test_daily_quota_after_failed_sends_reports_rate_limited_not_waiting(request_env):
    from clawchat_gateway.tool_requests import REQUEST_RATE_LIMITED_MESSAGE
    h = request_env
    for index in range(20):
        request = reserve(h.adapter._store, account='usr_agent', user=f'usr_failed{index}', now=int(time.time() * 1000))['request']
        h.adapter._store.mark_failed(account_id='usr_agent', request_id=request['request_id'])
    result = await call_request(h)
    assert {key: result[key] for key in ('status', 'reason', 'sent')} == dict(status='blocked', reason='rate-limited', sent=False)
    assert result['note'] == REQUEST_RATE_LIMITED_MESSAGE + " Explain only the requested ability in the conversation's language. Do not reveal others' access."
    assert 'was not sent' in result['note'] and 'ask the owner directly' in result['note']
    assert 'I have already asked' not in result['note'] and 'I have asked' not in result['note']
    h.adapter._send_owner_text.assert_not_awaited()


def test_cooldown_without_pending_card_does_not_claim_waiting(store):
    request = reserve(store)['request']
    decide(store, request)
    store.revoke_grants(account_id='agent', user_id='usr_guest')
    assert reserve(store, now=NOW + 2) == dict(status='blocked', reason='rate-limited')


@pytest.mark.parametrize('lang', ['zh', 'zh_Hant', 'en'])
@pytest.mark.parametrize('capability', list(TOOL_CAPABILITIES))
def test_request_card_host_limit_copy_only_for_limited_capabilities(store, lang, capability):
    from clawchat_gateway.tool_grants import HERMES_LIMIT_COPY
    request = reserve(store, capability=capability)['request']
    card = build_request_card(request, lang=lang, ask_available=True)
    assert (HERMES_LIMIT_COPY[lang] in card) is (capability in ('subagent', 'schedule'))
    assert card.count(HERMES_LIMIT_COPY[lang]) == (1 if capability in ('subagent', 'schedule') else 0)


def failed_wake(store):
    request = reserve(store)['request']
    decide(store, request)
    store.claim_wake(account_id='agent', wake_key=request['wake_key'], now=NOW + 2)
    store.fail_wake(account_id='agent', wake_key=request['wake_key'], now=NOW + 3)
    return request


def test_notice_claim_release_timeout_and_stale_acknowledgement(store):
    from clawchat_gateway.tool_requests import REQUEST_NOTICE_CLAIM_TTL_MS
    request = failed_wake(store)
    first = notices(store)[0]
    assert fetch(store, request)['wake_notified_at'] is None
    assert notices(store) == []
    store.finish_pending_notice(account_id='agent', claim_key=first['notice_claim_key'], accepted=False, now=NOW + 6)
    second = notices(store, now=NOW + 7)[0]
    assert second['notice_claim_key'] != first['notice_claim_key']
    assert notices(store, now=NOW + 7 + REQUEST_NOTICE_CLAIM_TTL_MS - 1) == []
    restarted = ClawChatStore(store.db_path)
    third = notices(restarted, now=NOW + 7 + REQUEST_NOTICE_CLAIM_TTL_MS)[0]
    assert third['notice_claim_key'] != second['notice_claim_key']
    assert restarted.finish_pending_notice(account_id='agent', claim_key=second['notice_claim_key'], accepted=True,
        now=NOW + 8 + REQUEST_NOTICE_CLAIM_TTL_MS) == 0
    assert fetch(restarted, request)['wake_notified_at'] is None
    restarted.finish_pending_notice(account_id='agent', claim_key=third['notice_claim_key'], accepted=True,
        now=NOW + 8 + REQUEST_NOTICE_CLAIM_TTL_MS)
    assert notices(restarted, now=NOW + 9 + REQUEST_NOTICE_CLAIM_TTL_MS) == []
    assert fetch(restarted, request)['wake_notified_at'] is not None


def test_concurrent_notice_claims_only_one_turn_can_inject(store):
    failed_wake(store)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: notices(ClawChatStore(store.db_path)), range(6)))
    assert sum(len(result) for result in results) == 1


def test_notice_claim_expiring_with_card_is_not_consumed(store):
    request = failed_wake(store)
    claim = notices(store)[0]
    store.finish_pending_notice(account_id='agent', claim_key=claim['notice_claim_key'], accepted=True, now=NOW + REQUEST_CARD_TTL_MS)
    assert fetch(store, request)['wake_notified_at'] is None
    assert notices(store, now=NOW + REQUEST_CARD_TTL_MS) == []


def test_notice_claim_migration_upgrades_existing_requests_without_loss(tmp_path, monkeypatch):
    from clawchat_gateway import storage
    migrations = storage.MIGRATIONS
    monkeypatch.setattr(storage, 'MIGRATIONS', migrations[:15])
    old = ClawChatStore(tmp_path / 'upgrade.sqlite')
    request = failed_wake(old)
    monkeypatch.setattr(storage, 'MIGRATIONS', migrations)
    upgraded = ClawChatStore(old.db_path)
    claim = notices(upgraded)[0]
    assert claim['request_id'] == request['request_id']
    assert fetch(upgraded, request)['decision_mode'] == 'direct'
    upgraded.finish_pending_notice(account_id='agent', claim_key=claim['notice_claim_key'], accepted=True, now=NOW + 6)
    assert notices(upgraded, now=NOW + 7) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['reject', 'exception', 'cancel', 'render'])
async def test_notice_remains_pending_until_host_accepts(request_env, monkeypatch, failure):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='direct', decided_by_user_id='usr_owner')
    h.client.get_conversation.side_effect = RuntimeError('unavailable')
    await h.adapter._dispatch_request_wake(request['wake_key'])
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    original_handle, original_render = h.adapter.handle_message, h.adapter._render_channel_prompt_parts
    observed = []
    async def fail(event):
        observed.append(event)
        assert latest_request(h)['wake_notified_at'] is None
        assert latest_request(h)['notice_claim_key']
        if failure == 'exception':
            raise RuntimeError('host failed')
        if failure == 'cancel':
            raise asyncio.CancelledError()
        event._gateway_accepted = False
    monkeypatch.setattr(h.adapter, 'handle_message', fail)
    if failure == 'render':
        monkeypatch.setattr(h.adapter, '_render_channel_prompt_parts', lambda _: (_ for _ in ()).throw(RuntimeError('render failed')))
    if failure == 'reject':
        await h.adapter._handle_inbound(message())
    else:
        with pytest.raises(asyncio.CancelledError if failure == 'cancel' else RuntimeError):
            await h.adapter._handle_inbound(message())
    assert latest_request(h)['wake_notified_at'] is None
    assert latest_request(h)['notice_claim_key'] is None
    if observed:
        assert build_request_notice(request | dict(decision_mode='direct')) in observed[0].channel_prompt
    monkeypatch.setattr(h.adapter, 'handle_message', original_handle)
    monkeypatch.setattr(h.adapter, '_render_channel_prompt_parts', original_render)
    await h.adapter._handle_inbound(message(text='try again'))
    assert build_request_notice(latest_request(h)) in h.harness.events[-1].channel_prompt
    assert latest_request(h)['wake_notified_at'] is not None
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[-1], 'SUCCESS')
    await h.adapter._handle_inbound(message(text='next'))
    assert build_request_notice(latest_request(h)) not in (getattr(h.harness.events[-1], 'channel_prompt', '') or '')


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [('usr_owner', 'usr_a'), ('usr_a', 'usr_owner')])
async def test_owner_and_unique_guest_batch_always_requests_for_guest(request_env, order):
    h = request_env
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, group_sessions_per_user=False)
    h.client.get_conversation.side_effect = lambda chat: {'conversation': {'id': chat, 'type': 'group', 'title': 'Group'}}
    incoming = await coalesced([message(sender, text='Please inspect the result', kind='group') for sender in order])
    await h.adapter._handle_inbound(incoming)
    snapshot = turns.get_snapshot(h.harness.events[-1].source.message_id)
    assert snapshot.request_origin.requester_user_id == 'usr_a'
    turns.bind_turn(snapshot.nonce, 's', 't')
    with h.binder(**context(snapshot, group=True)):
        assert (await call_request(h))['status'] == 'pending'
    assert latest_request(h)['requester_user_id'] == 'usr_a'
    h.client.get_user_info.assert_awaited_once_with('usr_a')
    assert h.adapter._send_owner_text.call_args.args[0] == OWNER_CHAT


@pytest.mark.asyncio
@pytest.mark.parametrize('order', [('usr_owner', 'usr_a', 'usr_b'), ('usr_a', 'usr_b', 'usr_owner')])
async def test_multiple_guest_batch_has_no_request_context_or_card(request_env, order):
    h = request_env
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, group_sessions_per_user=False)
    await h.adapter._handle_inbound(await coalesced([message(sender, kind='group') for sender in order]))
    snapshot = turns.get_snapshot(h.harness.events[-1].source.message_id)
    assert snapshot.request_origin is None
    turns.bind_turn(snapshot.nonce, 's', 't')
    with h.binder(**context(snapshot, group=True)):
        assert guard.clawchat_pre_tool_call(tool_name='clawchat_tool_request', session_id='s', turn_id='t')['action'] == 'block'
        assert (await call_request(h))['status'] == 'blocked'
    h.adapter._send_owner_text.assert_not_awaited()
