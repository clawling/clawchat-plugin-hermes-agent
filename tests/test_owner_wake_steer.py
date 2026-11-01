"""Owner confirmation, isolated owner input and bounded idle restart retries."""
import asyncio
import logging
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway import adapter as adapter_mod, host_tools_guard as guard, restart, turn_grants as turns
from clawchat_gateway.tool_grants import GRANT_INVALID_CODE_MESSAGE, GRANT_WRITE_FAILED_MESSAGE, PENDING_TTL_MS, OWNER_DOWNGRADED_MESSAGE
from test_tool_grants_admission import harness, message, OWNER_CHAT, CHAT, wait_for_events
from test_tool_grants_hook import host, context
from test_tool_grants_ask import approval
from test_owner_turn_downgrade import notice
from test_tool_requests import request_env, call_request, latest_request, store, reserve, fetch, NOW


def proactive(store, *, account='usr_agent', user='usr_a', tools=('cap:terminal',), mode='direct', now=None):
    store.create_pending_grant(account_id=account, code='482193', now=now or int(time.time() * 1000),
        payload=dict(grantees=[dict(id=user, name='Friend')], tools=list(tools), mode=mode))


def confirmation():
    return message('usr_owner', '/clawchat-allow 482193', chat=OWNER_CHAT)


@pytest.mark.parametrize('mode,status', [('direct', 'allowed'), ('ask', 'asked')])
@pytest.mark.parametrize('selectors', [('cap:terminal',), ('all',), ('tool:terminal', 'tool:process')])
async def test_proactive_confirmation_resumes_original_guest(request_env, mode, status, selectors):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(h.harness.events[0], 'SUCCESS')
    proactive(h.adapter._store, mode=mode, tools=selectors)
    await h.adapter._consume_grant_confirmation(confirmation())
    await asyncio.gather(*h.adapter._request_wake_tasks)
    assert len(h.harness.events) == 2
    event = h.harness.events[-1]
    snapshot = turns.get_snapshot(event.source.message_id)
    assert event.source.chat_id == CHAT and event.source.user_id == 'usr_a'
    assert snapshot.speakers == ('usr_a',) and snapshot.non_owner_speakers == {'usr_a'}
    assert not snapshot.owner_side and not snapshot.owner_verified and not snapshot.legacy_owner_tools
    assert snapshot.grants.get('terminal') == mode
    assert latest_request(h)['status'] == status
    assert latest_request(h)['decided_by_user_id'] == 'usr_owner'
    assert latest_request(h)['wake_state'] == 'delivered'
    await h.adapter._consume_grant_confirmation(message('usr_owner', '/clawchat-allow ' + request['code'], chat=OWNER_CHAT))
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_INVALID_CODE_MESSAGE['zh']
    await h.adapter._consume_grant_confirmation(confirmation())
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_INVALID_CODE_MESSAGE['zh']
    assert len(h.harness.events) == 2


@pytest.mark.parametrize('case', ['person', 'capability', 'partial', 'expired-request', 'decided',
                                  'unconfirmed', 'expired-card', 'deny', 'write-failed', 'ask-unavailable'])
async def test_proactive_nonmatches_and_failures_never_wake(request_env, monkeypatch, case):
    h = request_env
    await call_request(h)
    request = latest_request(h)
    kwargs = {}
    if case == 'person':
        kwargs['user'] = 'usr_other'
    if case in ('capability', 'partial'):
        kwargs['tools'] = ['cap:files'] if case == 'capability' else ['tool:terminal']
    if case == 'expired-card':
        kwargs['now'] = int(time.time() * 1000) - PENDING_TTL_MS - 1
    if case == 'ask-unavailable':
        kwargs['mode'] = 'ask'
        monkeypatch.setattr(guard, 'ask_grants_available', lambda: False)
    if case == 'expired-request':
        with sqlite3.connect(h.adapter._store.db_path) as conn:
            conn.execute('UPDATE tool_grant_requests SET expires_at = 1')
    if case == 'decided':
        h.adapter._store.confirm_request(account_id='usr_agent', code=request['code'], mode='deny',
            decided_by_user_id='usr_owner')
    if case == 'deny':
        h.adapter._store.apply_grant(account_id='usr_agent', payload=dict(
            grantees=[dict(id='usr_a', name='Friend')], tools=['all'], mode='deny'))
    else:
        proactive(h.adapter._store, **kwargs)
    before = latest_request(h)
    if case == 'write-failed':
        original = h.adapter._store._upsert_tool_grant
        def broken(*args):
            original(*args)
            raise RuntimeError('write failed')
        monkeypatch.setattr(h.adapter._store, '_upsert_tool_grant', broken)
    if case != 'unconfirmed':
        await h.adapter._consume_grant_confirmation(confirmation())
    assert not h.adapter._request_wake_tasks
    after = latest_request(h)
    assert after['decision_mode'] == before['decision_mode']
    assert after['wake_state'] == before['wake_state']
    assert after['status'] == ('expired' if case == 'expired-request' else before['status'])
    assert len(h.harness.events) == 1
    if case == 'write-failed':
        assert h.adapter._store.list_grants(account_id='usr_agent') == []
        assert h.adapter._send_owner_text.call_args.args[1] == GRANT_WRITE_FAILED_MESSAGE['zh']
        with sqlite3.connect(h.adapter._store.db_path) as conn:
            assert conn.execute('SELECT consumed_at FROM tool_grant_pending WHERE code = ?', ('482193',)).fetchone() == (None,)


def test_all_requests_and_the_grant_roll_back_together(store, monkeypatch):
    requests = [reserve(store, capability=cap)['request'] for cap in ('terminal', 'files')]
    proactive(store, account='agent', user='usr_guest', tools=['all'], now=NOW)
    original = store._decide_request
    calls = []
    def fail_second(*args):
        original(*args)
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('decision write failed')
    monkeypatch.setattr(store, '_decide_request', fail_second)
    with pytest.raises(RuntimeError):
        store.confirm_pending_grant_decision(account_id='agent', code='482193',
            decided_by_user_id='usr_owner', now=NOW + 1)
    assert store.list_grants(account_id='agent', now=NOW) == []
    assert all(fetch(store, req)['status'] == 'sending' and fetch(store, req)['wake_state'] == 'none'
               for req in requests)
    with sqlite3.connect(store.db_path) as conn:
        assert all(row == (None,) for row in conn.execute('SELECT consumed_at FROM tool_grant_pending'))


async def finish(h, event):
    h.adapter._active_sessions.clear()
    await h.adapter.on_processing_complete(event, 'SUCCESS')


@pytest.mark.parametrize('kind', ['awareness', 'moment_comment', 'foreign_sender'])
@pytest.mark.parametrize('entry', ['wire', 'internal'])
async def test_owner_steer_waits_for_release_then_has_owner_tools(harness, host, monkeypatch, caplog, kind, entry):
    h, a = harness, harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_poll_seconds = 0.005
    caplog.set_level(logging.INFO, logger=adapter_mod.__name__)
    if kind == 'foreign_sender':
        incoming = message('usr_other', chat=OWNER_CHAT)
    else:
        incoming = notice(a, kind)
        if kind == 'moment_comment':
            incoming = replace(incoming, _plugin_notification_origin=None)
    await a._handle_inbound(incoming)
    first = h.events[0]
    original = turns.get_snapshot(first.source.message_id)
    turns.bind_turn(original.nonce, 'notice-session', 'notice-turn')
    owner_input = message('usr_owner', 'private owner text', chat=OWNER_CHAT)
    if entry == 'wire':
        await a._on_message(owner_input.raw_message)
    else:
        await a._handle_inbound(owner_input)
    await asyncio.sleep(0.015)
    assert len(h.events) == 1
    assert turns.get_snapshot(original.nonce) is not None
    assert 'owner_input_held reason=active_notice_turn' in caplog.text
    assert 'private owner text' not in caplog.text
    await finish(h, first)
    await wait_for_events(h, 2)
    second = turns.get_snapshot(h.events[1].source.message_id)
    assert second.nonce != original.nonce
    assert second.owner_side and second.owner_verified and not second.non_owner_speakers
    with host(**context(second)):
        guard.clawchat_pre_llm_call(session_id='owner-session', turn_id='owner-turn')
        for tool in ('terminal', 'clawchat_orchestrate_list_agents', 'clawchat_widget_send'):
            assert guard._clawchat_pre_tool_decision(tool_name=tool, args={},
                session_id='owner-session', turn_id='owner-turn') is None
    await asyncio.gather(*a._owner_input_tasks.values())


@pytest.mark.parametrize('kind', ['awareness', 'foreign_sender'])
async def test_owner_hold_rechecked_after_media_preparation(harness, monkeypatch, kind):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_poll_seconds = 0.005
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def download(inbound):
        if inbound.text == 'first owner input' and not proceed.is_set():
            entered.set()
            await proceed.wait()
        return []
    monkeypatch.setattr(a, '_download_inbound_media', download)
    preparing = asyncio.create_task(a._handle_inbound(
        message('usr_owner', 'first owner input', chat=OWNER_CHAT)))
    await entered.wait()
    assert not harness.events and not a._owner_input_pending
    incoming = notice(a, 'awareness') if kind == 'awareness' else message('usr_other', chat=OWNER_CHAT)
    await a._handle_inbound(incoming)
    original = harness.events[0]
    nonce = original.source.message_id
    proceed.set()
    await preparing
    await a._handle_inbound(message('usr_owner', 'second owner input', chat=OWNER_CHAT))
    assert len(harness.events) == 1
    key = a._grant_session_key(incoming)
    assert [inbound.text for inbound, _ in a._owner_input_pending[key]] == [
        'first owner input', 'second owner input']
    assert turns.get_snapshot(nonce) is not None
    await finish(harness, original)
    await wait_for_events(harness, 3)
    assert [event.text for event in harness.events[1:]] == ['first owner input', 'second owner input']
    for event in harness.events[1:]:
        snapshot = turns.get_snapshot(event.source.message_id)
        assert snapshot.owner_side and snapshot.owner_verified and not snapshot.non_owner_speakers
        assert snapshot.nonce != nonce
    await asyncio.gather(*a._owner_input_tasks.values())


@pytest.mark.parametrize('mode', ['queue', 'interrupt'])
async def test_explicit_steer_waits_in_other_busy_modes(harness, monkeypatch, mode):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', mode)
    a._owner_input_poll_seconds = 0.005
    await a._handle_inbound(notice(a, 'awareness'))
    await a._on_message(message('usr_owner', '/steer inspect this', chat=OWNER_CHAT).raw_message)
    assert len(harness.events) == 1
    await finish(harness, harness.events[0])
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(harness.events[1].source.message_id).owner_side
    await asyncio.gather(*a._owner_input_tasks.values())


@pytest.mark.parametrize('case', ['queue', 'interrupt', 'group', 'approval', 'allow-code', 'control'])
async def test_owner_controls_and_other_modes_are_not_held(harness, monkeypatch, case):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', case if case in ('queue', 'interrupt') else 'steer')
    await a._handle_inbound(notice(a, 'awareness'))
    incoming = message('usr_owner', chat=OWNER_CHAT)
    if case == 'group':
        incoming = message('usr_owner', chat=OWNER_CHAT, kind='group')
    if case == 'approval':
        monkeypatch.setattr(a, '_direct_chat_approval_blocking', lambda _: True)
    if case in ('approval', 'control', 'allow-code'):
        incoming = message('usr_owner', {'approval': 'yes', 'control': '/restart',
            'allow-code': '/clawchat-allow 482193'}[case], chat=OWNER_CHAT)
    assert not a._hold_owner_inbound(incoming)
    if case != 'allow-code':
        await a._handle_inbound(incoming)
        assert len(harness.events) == 2
    assert not a._owner_input_tasks


@pytest.mark.parametrize('kind', ['moment_comment', 'foreign_sender', 'awareness'])
async def test_owner_hold_timeout_keeps_old_downgrade(harness, host, monkeypatch, kind):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_hold_seconds = 0.01
    a._owner_input_poll_seconds = 0.005
    incoming = message('usr_other', chat=OWNER_CHAT) if kind == 'foreign_sender' else notice(a, kind)
    if kind == 'moment_comment':
        incoming = replace(incoming, _plugin_notification_origin=None)
    await a._handle_inbound(incoming)
    original_nonce = harness.events[0].source.message_id
    await a._handle_inbound(message('usr_owner', chat=OWNER_CHAT))
    await wait_for_events(harness, 2)
    snapshot = turns.get_snapshot(harness.events[1].source.message_id)
    assert turns.get_snapshot(original_nonce) is not None
    assert snapshot.owner_side is (kind == 'awareness')
    assert not snapshot.owner_verified
    if kind != 'awareness':
        with host(**context(snapshot)):
            guard.clawchat_pre_llm_call(session_id='timeout-session', turn_id='owner-turn')
            for tool in ('terminal', 'clawchat_orchestrate_list_agents'):
                result = guard._clawchat_pre_tool_decision(tool_name=tool, args={},
                    session_id='timeout-session', turn_id='owner-turn')
                assert result['message'] == OWNER_DOWNGRADED_MESSAGE
    await asyncio.gather(*a._owner_input_tasks.values())


def test_busy_mode_env_config_and_host_default(monkeypatch):
    monkeypatch.setitem(sys.modules, 'gateway.run', SimpleNamespace(
        _load_gateway_config=lambda: {'display': {'busy_input_mode': 'steer'}}))
    monkeypatch.delenv('HERMES_GATEWAY_BUSY_INPUT_MODE', raising=False)
    assert adapter_mod._read_host_busy_input_mode() == 'steer'
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', ' Queue ')
    assert adapter_mod._read_host_busy_input_mode() == 'queue'
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'bad-mode')
    assert adapter_mod._read_host_busy_input_mode() == 'interrupt'
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', ' ')
    def unreadable():
        raise OSError('unreadable')
    monkeypatch.setitem(sys.modules, 'gateway.run', SimpleNamespace(_load_gateway_config=unreadable))
    assert adapter_mod._read_host_busy_input_mode() == 'interrupt'


@pytest.mark.parametrize('success_on', [1, 3, None])
def test_restart_retries_log_attempts_and_stop_at_deadline(monkeypatch, capsys, success_on):
    spawned = []
    monkeypatch.setattr(restart.subprocess, 'Popen', lambda argv, **kw: spawned.append((argv, kw)))
    command = restart.schedule_gateway_restart(delay_seconds=0, retry_timeout_seconds=200)
    argv, kwargs = spawned[0]
    assert '--yes' not in command and '--interrupt' not in command
    monkeypatch.setattr(sys, 'argv', argv)
    for key, value in kwargs['env'].items():
        monkeypatch.setenv(key, value)
    elapsed, sleeps, calls = [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
    def call(argv, **kw):
        assert 'timeout' not in kw  # a foreground replacement gateway never returns
        calls.append(argv)
        return 0 if len(calls) == success_on else 1
    monkeypatch.setattr(time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(time, 'sleep', sleep)
    monkeypatch.setattr(subprocess, 'call', call)
    exec(argv[2], {})
    output = capsys.readouterr().out
    assert all(call[-2:] == ['gateway', 'restart'] and '--yes' not in call and '--interrupt' not in call for call in calls)
    for attempt in range(1, len(calls) + 1):
        assert f'attempt={attempt} exit_code=' in output
    if success_on:
        assert len(calls) == success_on and sleeps == ([0] if success_on == 1 else [0, 5, 10])
        assert output.rstrip().endswith(f'attempt={success_on} exit_code=0')
    else:
        assert elapsed[0] == 200
        assert sleeps == [0, 5, 10, 20, 40, 60, 60, 5]
        assert 'owner must restart manually' in output


async def test_proactive_multiple_requests_schedule_each_wake_after_receipt(request_env, monkeypatch):
    h = request_env
    await call_request(h)
    first = latest_request(h)
    second = h.adapter._store.reserve_request(account_id='usr_agent', owner_user_id='usr_owner',
        origin=replace(h.snapshot.request_origin, source_chat_id='cnv_another', source_session_key='cnv_another:__direct__'),
        requester_name='Friend', capability_id='files', reason='Read the report')['request']
    proactive(h.adapter._store, tools=['all'])
    wakes = []
    async def dispatch(key):
        h.adapter._send_owner_text.assert_awaited()
        wakes.append(key)
    monkeypatch.setattr(h.adapter, '_dispatch_request_wake', dispatch)
    await h.adapter._on_message(confirmation().raw_message)
    await asyncio.gather(*h.adapter._request_wake_tasks)
    assert set(wakes) == {first['wake_key'], second['wake_key']}
    for req in (first, second):
        assert h.adapter._store.get_request(account_id='usr_agent', request_id=req['request_id'])['status'] == 'allowed'


async def test_confirmation_bypasses_owner_hold_during_notice(request_env, monkeypatch):
    h = request_env
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    await call_request(h)
    await finish(h.harness, h.harness.events[0])
    await h.adapter._handle_inbound(notice(h.adapter, 'awareness'))
    proactive(h.adapter._store)
    await h.adapter._on_message(confirmation().raw_message)
    assert not h.adapter._owner_input_tasks
    await asyncio.gather(*h.adapter._request_wake_tasks)
    assert latest_request(h)['status'] == 'allowed'
    assert latest_request(h)['wake_state'] == 'delivered'


async def test_held_owner_inputs_keep_order_and_do_not_follow_active_pointer(harness, monkeypatch):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_poll_seconds = 0.005
    await a._handle_inbound(notice(a, 'awareness'))
    original = harness.events[0]
    # A newer owner nonce must not hide the older live notification.
    turns.register_turn(account_id=a._account_id(), chat_id=OWNER_CHAT,
        session_key=a._grant_session_key(message('usr_owner', chat=OWNER_CHAT)),
        speakers=('usr_owner',), grants={}, owner_side=True)
    for text in ('first held', 'second held'):
        await a._on_message(message('usr_owner', text, chat=OWNER_CHAT).raw_message)
    await asyncio.sleep(0.01)
    assert len(harness.events) == 1
    await finish(harness, original)
    await wait_for_events(harness, 3)
    assert [event.text for event in harness.events[1:]] == ['first held', 'second held']
    assert all(turns.get_snapshot(event.source.message_id).owner_side for event in harness.events[1:])
    assert len({event.source.message_id for event in harness.events}) == 3
    await asyncio.gather(*a._owner_input_tasks.values())


@pytest.mark.parametrize('readable', [True, False])
async def test_conversational_approval_word_waits_when_no_approval_is_pending(harness, monkeypatch, readable):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    def get_pending(_):
        if not readable:
            raise RuntimeError('confirmation state unavailable')
        return None
    monkeypatch.setitem(sys.modules, 'tools.slash_confirm', SimpleNamespace(get_pending=get_pending))
    a._owner_input_poll_seconds = 0.005
    await a._handle_inbound(notice(a, 'awareness'))
    await a._on_message(message('usr_owner', 'yes', chat=OWNER_CHAT).raw_message)
    assert len(harness.events) == 1
    await finish(harness, harness.events[0])
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(harness.events[1].source.message_id).owner_side
    await asyncio.gather(*a._owner_input_tasks.values())


async def test_proactive_request_wakes_even_if_owner_receipt_fails(request_env, monkeypatch):
    h = request_env
    await call_request(h)
    await finish(h.harness, h.harness.events[0])
    proactive(h.adapter._store)
    async def failed_receipt(*args):
        raise OSError('receipt unavailable')
    monkeypatch.setattr(h.adapter, '_send_owner_text', failed_receipt)
    with pytest.raises(OSError, match='receipt unavailable'):
        await h.adapter._consume_grant_confirmation(confirmation())
    await asyncio.gather(*h.adapter._request_wake_tasks)
    assert latest_request(h)['wake_state'] == 'delivered'
    assert not turns.get_snapshot(h.harness.events[-1].source.message_id).owner_side


async def test_idle_host_releases_a_lingering_notice_snapshot_before_the_deadline(harness, monkeypatch):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_poll_seconds = 0.005
    await a._handle_inbound(notice(a, 'awareness'))
    await a._on_message(message('usr_owner', 'after idle', chat=OWNER_CHAT).raw_message)
    await asyncio.sleep(0.01)
    assert len(harness.events) == 1
    # The host went idle without a release callback; the snapshot lingers.
    a._active_sessions.clear()
    await wait_for_events(harness, 2)
    assert harness.events[1].text == 'after idle'
    await asyncio.gather(*a._owner_input_tasks.values())


async def test_a_failed_held_dispatch_keeps_later_held_inputs(harness, monkeypatch):
    a = harness.adapter
    monkeypatch.setenv('HERMES_GATEWAY_BUSY_INPUT_MODE', 'steer')
    a._owner_input_poll_seconds = 0.005
    await a._handle_inbound(notice(a, 'awareness'))
    original = harness.events[0]
    route = a._route_inbound
    async def flaky(inbound):
        if inbound.text == 'first held':
            raise RuntimeError('dispatch failed')
        await route(inbound)
    monkeypatch.setattr(a, '_route_inbound', flaky)
    for text in ('first held', 'second held'):
        await a._on_message(message('usr_owner', text, chat=OWNER_CHAT).raw_message)
    await finish(harness, original)
    await wait_for_events(harness, 2)
    assert harness.events[1].text == 'second held'
    await asyncio.gather(*a._owner_input_tasks.values())
