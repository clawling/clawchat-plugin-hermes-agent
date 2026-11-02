"""Runtime history contract with an offline host, ledger and independent LLM."""
from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from clawchat_gateway import history_maintenance as hm
from clawchat_gateway.adapter import ClawChatAdapter, MessageEvent
from clawchat_gateway.atomic_file import atomic_write_text
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import InboundMessage
from clawchat_gateway.rolling_summary import read_summary
from clawchat_gateway.storage import ClawChatStore

SUMMARY = "## Now\nwork\n## Decided\nmeet at 10\n## Open\n(none)\n## Index\nclue · id m1"


class LLM:
    def __init__(self):
        self.calls = []
        self.error = None
        self.text = SUMMARY
        self.wait = None

    async def acomplete(self, messages, *, timeout, max_tokens):
        self.calls.append((messages, timeout, max_tokens))
        if self.wait:
            await self.wait.wait()
        if self.error:
            raise self.error
        return NS(text=self.text)


class Host:
    def __init__(self, directory):
        self.path = directory / 'sessions.json'
        self.routes = {}
        self.session_store = NS(sessions_dir=directory, peek_session_id=lambda key: self.routes.get(key))
        self._queued_events = {}
        self._startup_restore_queue = []
        self.running = False
        self.reset_calls = []
        self.reset_gate = None
        self.reset_error = None
        self.save_reset = True
        self.confirm_gate = True
        self.notices = []

    def _session_key_for_source(self, source):
        suffix = ':' + source.user_id if source.chat_type == 'group' else ''
        return 'agent:main:clawchat:' + source.chat_type + ':' + source.chat_id + suffix

    def _is_session_running(self, key):
        return self.running

    def persist(self, key, sid):
        self.routes[key] = sid
        atomic_write_text(self.path, json.dumps({k: {'session_id': v} for k, v in self.routes.items()}))

    async def _handle_reset_command(self, event):
        self.reset_calls.append(event)
        self.notices.append(hm.suppress_reset_output(event.source.chat_id))
        if self.reset_gate:
            await self.reset_gate.wait()
        if self.save_reset:
            self.persist(self._session_key_for_source(event.source), 'new-session')
        if self.reset_error:
            raise self.reset_error
        return 'unused banner'


@pytest.fixture
async def rig(monkeypatch, tmp_path):
    monkeypatch.setattr('clawchat_gateway.adapter._HOST_AGENT_VERSION', '0.22.5')
    monkeypatch.setattr('clawchat_gateway.adapter._host_supports_channel_context', lambda: True)
    a = ClawChatAdapter({})
    a._store = ClawChatStore(tmp_path / 'clawchat.sqlite')
    a._clawchat_config = replace(a._clawchat_config, user_id='usr_agent', owner_user_id='usr_owner',
        history_budget_dm=5000, history_budget_group=5000, sediment_on_compact=True,
        keep_recent_turns=1, recent_window=timedelta(hours=1))
    a._active_sessions = {}
    a._pending_messages = {}
    a._text_debounce = {}
    a.build_source = lambda **kw: NS(**kw)
    a._group_settings_ready.set()
    a.gateway_runner = Host(tmp_path)
    llm = LLM()
    m = hm.HistoryMaintenance(a, llm)
    a._history = m
    m.connect()
    calls = []

    async def notes(**kw):
        calls.append('notes')
        return True
    a._run_sediment_turn = notes

    async def continuation(inbound):
        calls.append(a._inbound_trigger_id(inbound.raw_message))
        m.dispatched(event(NS(m=m), inbound))
    a._continue_claimed_inbound = continuation
    yield NS(a=a, m=m, host=a.gateway_runner, llm=llm, calls=calls, path=tmp_path)
    await m.close()


def inbound(mid='trigger', text='question', chat='cnv_test', sender='usr_owner', group=False):
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    frame = {'type': 'event', 'event': 'message.send', 'chat_id': chat, 'chat_type': 'group' if group else 'direct',
        'emitted_at': ts, 'sender': {'id': sender, 'nick_name': 'Owner'},
        'payload': {'message_id': mid, 'conversation_id': chat, 'sender': {'id': sender},
                    'message': {'body': {'text': text, 'fragments': [{'kind': 'text', 'text': text}]}}}}
    return InboundMessage(chat_id=chat, chat_type='group' if group else 'direct', sender_id=sender,
                          sender_name='Owner', text=text, raw_message=frame, emitted_at=ts)


def claim(rig, message):
    m = rig.m
    return m.store.claim_message_once(platform='hermes', account_id='default', kind='message', direction='inbound',
        event_type='message.send', chat_id=message.chat_id, message_id=message.raw_message['payload']['message_id'],
        text=message.text, raw=message.raw_message, **m.claim_options(message))


def ledger(rig, count=5, text='中' * 5000, sender='usr_owner'):
    messages = []
    for i in range(count):
        msg = inbound(f'm{i}', text, sender=sender)
        stamp = int((datetime.now(timezone.utc) - timedelta(days=4, minutes=count-i)).timestamp() * 1000)
        msg.raw_message['emitted_at'] = stamp
        claim(rig, msg)
        messages.append(msg)
    return messages


def initialize(rig, msg):
    key = rig.m.key(msg)
    rig.host.persist(key, 'old-session')
    state = rig.m.state(key)
    rig.m.cas(key, state, host_session='old-session', injected_for='old-session')
    return key


async def compact(rig, msg, why='budget'):
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), why)
    await rig.m.start(msg)
    return key, rig.m.state(key)


def event(rig, msg, nonce='nonce-test'):
    source = rig.m.source(msg)
    source.message_id = nonce
    return NS(source=source, text=msg.text, message_id=rig.m.adapter._inbound_trigger_id(msg.raw_message),
              raw_message={'clawchat_raw': msg.raw_message})


def test_bound_private_signature():
    async def good(event): pass
    async def extra(event, option=None): pass
    async def keyword(*, event): pass
    def sync(event): pass
    async def wrong(message): pass
    assert hm.reset_signature_supported(good)
    assert all(not hm.reset_signature_supported(fn) for fn in (None, extra, keyword, sync, wrong))


def test_unreadable_signature(monkeypatch):
    async def fn(event): pass
    monkeypatch.setattr(inspect, 'signature', lambda _: (_ for _ in ()).throw(ValueError()))
    assert not hm.reset_signature_supported(fn)


def test_local_host_signature_contract():
    # Supported budget host range: >= v0.14.0, through v0.22.5 canary.
    # Capability checks are mandatory above that range too; AST pins the local host.
    from hermes_source import require_hermes_source
    root = require_hermes_source('gateway/slash_commands_session.py')
    tree = ast.parse((root / 'gateway/slash_commands_session.py').read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_handle_reset_command')
    assert [p.arg for p in fn.args.args] == ['self', 'event']
    assert not fn.args.posonlyargs and not fn.args.kwonlyargs and fn.args.vararg is None and fn.args.kwarg is None


@pytest.mark.parametrize('missing', ['llm', 'channel', 'runner', 'reset', 'route', 'old-version', 'queue'])
async def test_capability_failure_retains_legacy(rig, monkeypatch, missing, caplog):
    if missing == 'llm': rig.m.llm = None
    if missing == 'channel': monkeypatch.setattr('clawchat_gateway.adapter._host_supports_channel_context', lambda: False)
    if missing == 'runner': rig.a.gateway_runner = None
    if missing == 'reset': rig.host._handle_reset_command = lambda event: None
    if missing == 'route': rig.host.session_store.sessions_dir = None
    if missing == 'old-version': monkeypatch.setattr('clawchat_gateway.adapter._HOST_AGENT_VERSION', '0.13.9')
    if missing == 'queue': rig.host._queued_events = None
    rig.m.connect()
    assert not rig.m.enabled(inbound())
    assert 'host compaction and legacy notes retained' in caplog.text


async def test_switch_and_conversation_override(rig):
    msg = inbound()
    rig.a._clawchat_config = replace(rig.a._clawchat_config, budget_compaction=False)
    assert not rig.m.enabled(msg)
    rig.a._clawchat_config = replace(rig.a._clawchat_config,
        chats={msg.chat_id: {'budget-compaction': True}})
    assert rig.m.enabled(msg)


async def test_budget_summary_notes_reset_then_first_injection(rig):
    ledger(rig)
    msg = inbound()
    claim(rig, msg)
    key, state = await compact(rig, msg)
    assert state['summary_through'] == 'm4'
    assert state['inject_next'] and state['host_session'] == 'new-session'
    assert not state['pending']
    assert rig.calls[0] == 'notes'
    assert len(rig.llm.calls) == 2  # each whole block <= 24k
    assert all(timeout == 120 and maximum == 4000 for _, timeout, maximum in rig.llm.calls)
    assert all('meet at 10' not in messages[0]['content'] for messages, _, _ in rig.llm.calls)
    assert rig.host.notices == [True] and rig.host.confirm_gate
    next_msg = inbound('next')
    claim(rig, next_msg)
    text = await rig.m.before(next_msg)
    assert '## ClawChat Conversation Summary' in text and 'meet at 10' in text
    assert 'id trigger' in text and 'id m0,' not in text
    ev = event(rig, next_msg)
    rig.m.bind_injection(next_msg, ev, text)
    rig.m.observe_request(ev.source.message_id, key, 'new-session', None)
    state = rig.m.state(key)
    assert state['injected_for'] == 'new-session' and not state['inject_next']
    assert state['open_context'] is None and state['open_history'] > 0
    assert await rig.m.before(inbound('later')) == ''


@pytest.mark.parametrize('failure', ['call', 'format', 'publish', 'permission'])
async def test_summary_failure_no_notes_no_reset_backoff(rig, monkeypatch, failure):
    ledger(rig)
    msg = inbound()
    if failure == 'call': rig.llm.error = RuntimeError('placeholder-summary-error')
    if failure == 'format': rig.llm.text = 'wrong headings'
    if failure == 'permission': rig.llm.error = PermissionError('placeholder-denied')
    if failure == 'publish': monkeypatch.setattr(hm, 'publish_summary', lambda *a, **k: (_ for _ in ()).throw(OSError()))
    key, state = await compact(rig, msg)
    assert not state['summary_ref'] and not state['summary_through']
    assert not rig.host.reset_calls and 'notes' not in rig.calls
    assert state['pending']['cut_id'] == 'm3'
    if failure != 'permission':
        assert state['failures'] == 1 and state['retry_after_msgs'] == 1
    else:
        assert not rig.m.available


async def test_pending_cut_fixed_backoff_persists_restart(rig):
    ledger(rig)
    msg = inbound()
    rig.llm.error = RuntimeError()
    key, state = await compact(rig, msg)
    old = state['pending']['cut_id']
    ledger(rig, count=1, text='extra')
    rig.m.queue_pending(msg, state, 'idle')
    assert rig.m.state(key)['pending']['cut_id'] == old
    fresh = hm.HistoryMaintenance(rig.a, rig.llm)
    fresh.connect()
    assert fresh.state(key)['retry_after_msgs'] == 1
    await fresh.close()
    for expected in (2, 4, 8, 8):
        await rig.m.start(msg)
        assert rig.m.state(key)['retry_after_msgs'] == expected
    rig.llm.error = None
    await rig.m.start(msg)
    assert not rig.m.state(key)['pending']


async def test_three_blocks_per_maintenance_and_renewed_unlimited(rig):
    ledger(rig, count=5, text='中' * 23500)
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    await rig.m.start(msg)
    assert len(rig.llm.calls) == 3
    assert rig.m.state(key)['summary_through'] == 'm2' and not rig.host.reset_calls
    await rig.m.start(msg)
    assert len(rig.llm.calls) == 4 and rig.host.reset_calls


async def test_idle_is_after_answer_and_uses_original_timestamp(rig):
    ledger(rig, count=2, text='agreement')
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    await rig.m.before(msg)
    assert not rig.llm.calls and not rig.host.reset_calls
    assert rig.m.after_turn(event(rig, msg))
    assert rig.m.state(key)['pending']['why'] == 'idle'
    await rig.m.tasks[key]
    assert rig.host.reset_calls


async def test_strict_budget_and_ledger_fallback(rig):
    ledger(rig, text='brief')
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    state = rig.m.state(key)
    rig.m.cas(key, state, open_history=5000, open_context=100)
    rig.m.usage[key] = ('old-session', 100)
    await rig.m.before(msg)
    rig.m.turns['trigger'] = (msg, None)
    rig.m.after_turn(event(rig, msg))
    assert not rig.m.state(key)['pending']
    rig.m.usage[key] = ('old-session', 101)
    rig.m.turns['trigger'] = (msg, None)
    rig.m.after_turn(event(rig, msg))
    assert rig.m.state(key)['pending']['why'] == 'budget'
    await rig.m.tasks[key]


@pytest.mark.parametrize('why', ['idle', 'external', 'renewed'])
async def test_sparse_foreign_fold_skipped(rig, why):
    ledger(rig, count=1, sender='usr_guest')
    msg = inbound()
    key = initialize(rig, msg)
    state = rig.m.queue_pending(msg, rig.m.state(key), why, cut='m0' if why == 'renewed' else None)
    assert not state.get('pending')
    assert not rig.llm.calls


async def test_reset_failed_persistence_retains_pending_then_retry_only_reset(rig, caplog):
    ledger(rig)
    msg = inbound()
    rig.host.save_reset = False
    key, state = await compact(rig, msg)
    assert state['host_session'] == 'old-session' and state['pending']['stage'] == 'reset'
    assert not state['inject_next'] and state['retry_after_msgs'] == 1
    calls = len(rig.llm.calls)
    rig.host.save_reset = True
    rig.host.reset_error = RuntimeError('after-save')
    await rig.m.start(msg)
    assert len(rig.llm.calls) == calls and rig.calls.count('notes') == 1
    assert not rig.m.state(key)['pending']
    assert 'did not commit' in caplog.text


async def test_database_route_authority_never_uses_legacy_mirror(rig):
    rig.host.persist('key', 'legacy')
    db = NS(load_gateway_routing_entries=lambda **kw: {'key': json.dumps({'session_id': 'durable'})})
    store = NS(_routing_db=db, _routing_scope=lambda: 'scope', sessions_dir=rig.path)
    read = hm.routing_reader(store)
    assert read('key') == 'durable'
    db.load_gateway_routing_entries = lambda **kw: (_ for _ in ()).throw(OSError())
    with pytest.raises(OSError):
        hm.routing_reader(store)('key')


async def test_cas_race_discards_summary_without_reset(rig, monkeypatch):
    ledger(rig)
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    real = hm.publish_summary
    def publish(*a, **kw):
        ref = real(*a, **kw)
        rig.m.cas(key, rig.m.state(key), inject_next=1)
        return ref
    monkeypatch.setattr(hm, 'publish_summary', publish)
    await rig.m.start(msg)
    state = rig.m.state(key)
    assert not state['summary_ref'] and not rig.host.reset_calls


async def test_external_rotation_without_summary_injects_recent_then_pending(rig):
    ledger(rig, text='meeting')
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.host.persist(key, 'external-session')
    text = await rig.m.before(msg)
    assert '## Recent messages' in text and 'meeting' in text
    assert '## ClawChat Conversation Summary' not in text
    assert rig.m.state(key)['pending']['why'] == 'external'


async def test_user_reset_confirmed_boundary_renewed_more_than_three_blocks(rig):
    ledger(rig, count=5, text='中' * 23500)
    command = inbound('cmd', '/new')
    claim(rig, command)
    key = initialize(rig, command)
    assert await rig.m.before(command) == ''
    assert not rig.m.state(key)['pending'] and not rig.m.state(key)['boundary']
    rig.host.persist(key, 'user-new')
    rig.m.observe_reset('old-session', 'user-new', None)
    state = rig.m.state(key)
    assert state['boundary'] == 'cmd' and state['pending']['why'] == 'renewed'
    msg = inbound('after-new')
    claim(rig, msg)
    text = await rig.m.before(msg)
    assert len(rig.llm.calls) == 5 and not rig.host.reset_calls
    assert 'meet at 10' in text and 'id m0,' not in text
    assert not rig.m.state(key)['pending'] and rig.m.state(key)['inject_next']


async def test_reset_candidate_cancel_or_internal_does_not_advance(rig):
    ledger(rig, text='brief')
    command = inbound('cmd', '/new')
    claim(rig, command)
    key = initialize(rig, command)
    await rig.m.before(command)
    rig.m.observe_reset('other-session', 'new-session', None)
    rig.m.observe_reset('old-session', 'new-session', (object(), command.chat_id))
    assert not rig.m.state(key)['boundary']
    await rig.m.before(inbound('cancelled-next'))
    assert key not in rig.m.candidates and not rig.m.state(key)['pending']


async def test_rejected_first_turn_and_missing_usage_confirmation(rig):
    ledger(rig, text='brief')
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    text = await rig.m.before(msg)
    rig.m.bind_injection(msg, event(rig, msg, 'first'), text)
    assert rig.m.state(key)['inject_next']
    # Host refusal produces no request hook, so the next real turn also injects.
    second = inbound('second')
    claim(rig, second)
    second_text = await rig.m.before(second)
    assert second_text
    rig.m.bind_injection(second, event(rig, second, 'second'), second_text)
    rig.m.observe_request('second', key, 'old-session', None)
    assert not rig.m.state(key)['inject_next']
    assert not await rig.m.before(inbound('third'))


async def test_nonce_and_actual_session_guard(rig):
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    rig.m.bind_injection(msg, event(rig, msg, 'bound'), 'history')
    rig.m.observe_request('unrelated', key, 'old-session', 10)
    assert rig.m.state(key)['inject_next']
    rig.m.observe_request('bound', key, 'replaced-session', 10)
    assert rig.m.state(key)['inject_next']


async def test_mid_dispatch_rotation_marks_next_injection(rig):
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.bind_injection(msg, event(rig, msg), '')
    rig.host.persist(key, 'late-session')
    rig.m.observe_request('nonce-test', key, 'late-session', None)
    assert rig.m.state(key)['inject_next']


async def test_claim_and_held_are_one_transaction(rig):
    msg = inbound()
    # JSON serialization fails after the ledger INSERT; rollback must undo both.
    assert rig.m.store.claim_message_once(platform='hermes', account_id='default', kind='message',
        direction='inbound', event_type='message.send', chat_id=msg.chat_id, message_id='atomic',
        text='text', raw={}, held_key='key', held_payload=object()) is None
    assert rig.m.store.list_chat_history_messages('default', msg.chat_id) == []
    assert claim(rig, msg)
    held = rig.m.store.list_held(rig.m.account)[0]
    assert held['message_id'] == 'trigger' and held['seq'] > 0
    assert claim(rig, msg) is False


async def test_held_restart_order_dispatching_and_terminal_rules(rig):
    msgs = [inbound(f'held{i}') for i in range(4)]
    key = initialize(rig, msgs[0])
    for msg in msgs: claim(rig, msg)
    for mid in ('held1', 'held2', 'held3'):
        rig.m.store.mark_held_state(rig.m.account, key, mid, 'held', 'dispatching')
    row = rig.m.held_row(key, 'held2')
    rig.m.store.update_held_payload(rig.m.account, key, 'held2', row['payload'] | {'terminal': True})
    # Process/pending/failed outbound rows do not imply completion.
    rig.m.store.claim_message_once(platform='hermes', account_id='default', kind='message', direction='outbound',
        event_type='message.send', chat_id=msgs[0].chat_id, message_id='process', text='thinking', raw={})
    await rig.m.recover()
    assert rig.calls == ['held0', 'held1', 'held3']
    assert not rig.m.store.list_held(rig.m.account)


async def test_independent_deadline_releases_during_late_reset(rig, monkeypatch):
    monkeypatch.setattr(hm, 'MAINTENANCE_SECONDS', .05)
    monkeypatch.setattr(hm, 'RESET_WARNING_SECONDS', .01)
    ledger(rig)
    msg = inbound()
    key = initialize(rig, msg)
    # Old ledger items are already dispatched, so only the arrival is held.
    for row in rig.m.store.list_held(rig.m.account, key):
        rig.m.store.mark_held_state(rig.m.account, key, row['message_id'], 'held', 'dispatched')
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    rig.host.reset_gate = asyncio.Event()
    task = asyncio.create_task(rig.m.start(msg))
    while not rig.host.reset_calls:
        await asyncio.sleep(.001)
    later = inbound('during-reset')
    claim(rig, later)
    assert rig.m.defer(later)
    await asyncio.sleep(.08)
    assert 'during-reset' in rig.calls and not rig.host.reset_gate.is_set()
    assert not hm.suppress_reset_output(later.chat_id)
    assert key in rig.m.reset_tasks
    rig.host.reset_gate.set()
    await task
    await asyncio.sleep(.01)
    assert rig.m.state(key)['host_session'] == 'new-session'
    assert key not in rig.m.reset_tasks


async def test_idle_processing_and_all_queues_must_be_empty(rig):
    msg = inbound()
    key = initialize(rig, msg)
    assert await rig.m.idle(msg)
    for container, marker in ((rig.a._active_sessions, key), (rig.a._pending_messages, key),
                              (rig.a._text_debounce, key), (rig.host._queued_events, key)):
        container[marker] = object()
        assert not await rig.m.idle(msg)
        container.clear()
    rig.host.session_authority = NS(_pending_rows=lambda ref: [{'status': 'started'}])
    assert not await rig.m.idle(msg)
    rig.host.session_authority = None
    rig.a._group_message_coalescer._pending[msg.chat_id] = [msg]
    assert not await rig.m.idle(msg)


async def test_two_canonical_keys_do_not_share_state(rig):
    one, two = inbound(group=True), inbound(sender='usr_guest', group=True)
    rig.a._clawchat_config = replace(rig.a._clawchat_config, group_sessions_per_user=True)
    assert rig.m.key(one) != rig.m.key(two)
    key = initialize(rig, one)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    assert rig.m.state(rig.m.key(two)) == {'gen': 0}


async def test_snapshot_persisted_before_dispatch_reused_on_replay(rig):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    ev = event(rig, msg)
    ev.channel_context = 'original context'
    ev.timestamp = datetime.now(timezone.utc)
    rig.m.save_snapshot(msg, ev)
    row = rig.m.held_row(key, 'trigger')
    assert row['payload']['snapshot']['nonce'] == 'nonce-test'
    rig.m.replays['trigger'] = row['payload']['snapshot']
    newer = event(rig, msg)
    newer.channel_context = 'different context'
    rig.m.save_snapshot(msg, newer)
    assert newer.channel_context == ev.channel_context and newer.timestamp == ev.timestamp
    assert rig.m.replay_nonce(msg) == 'nonce-test'


async def test_late_summary_cannot_publish_after_close(rig):
    ledger(rig)
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    rig.llm.wait = asyncio.Event()
    task = asyncio.create_task(rig.m.start(msg))
    while not rig.llm.calls:
        await asyncio.sleep(.001)
    rig.m.closed = True
    rig.m.epoch += 1
    rig.llm.wait.set()
    await task
    assert not rig.m.state(key)['summary_ref'] and not rig.host.reset_calls


async def test_real_adapter_wires_context_stable_id_and_suppresses_legacy(rig, monkeypatch):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    events = []
    async def handle(ev):
        events.append(ev)
    monkeypatch.setattr(rig.a, 'handle_message', handle)
    monkeypatch.setattr(rig.a, '_compose_channel_prompt_parts', lambda *a, **k: [])
    await rig.a._handle_inbound_now(msg)
    assert events[0].message_id == 'trigger'
    assert '## Recent messages' in events[0].channel_context
    assert not rig.m.store.list_held(rig.m.account, key)
    # The budget branch consumes the completed real turn and never calls old threshold notes.
    legacy = []
    monkeypatch.setattr(rig.a, '_maybe_schedule_compact_sediment', lambda *a: legacy.append(True))
    rig.a._after_host_turn(events[0])
    assert not legacy
    rig.m.available = False
    rig.a._after_host_turn(events[0])
    assert legacy == [True]


async def test_group_injection_skips_old_backfill_and_confirms_seen_ids(rig, monkeypatch):
    msg = inbound(group=True)
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    events = []
    async def handle(ev): events.append(ev)
    async def noop(*a): pass
    monkeypatch.setattr(rig.a, 'handle_message', handle)
    monkeypatch.setattr(rig.a, '_ensure_group_participants_metadata', noop)
    monkeypatch.setattr(rig.a, '_compose_channel_prompt_parts', lambda *a, **k: [])
    monkeypatch.setattr(rig.a, '_group_context_for_turn', lambda *a: pytest.fail('old backfill during injection'))
    await rig.a._handle_inbound_now(msg)
    assert events[0].text == msg.text
    nonce = events[0].source.message_id
    assert not rig.a._delivered_group_message_ids.get(msg.chat_id)
    rig.m.observe_request(nonce, key, 'old-session', None)
    assert 'trigger' in rig.a._delivered_group_message_ids[msg.chat_id]


async def test_claimed_group_waiting_in_coalescer_remains_recoverable(rig):
    msg = inbound(group=True)
    claim(rig, msg)
    key = initialize(rig, msg)
    async def queue_only(incoming):
        rig.a._group_message_coalescer._pending[incoming.chat_id] = [incoming]
    rig.a._continue_claimed_inbound = queue_only
    await rig.m.continue_claimed(msg)
    assert rig.m.held_row(key, 'trigger')['state'] == 'dispatching'
    rig.m.blocked[key] = object()
    assert rig.m.defer(msg)
    assert rig.m.held_row(key, 'trigger')['state'] == 'held'


async def test_malformed_reset_is_disabled_without_dropping_inbound(rig):
    rig.host._handle_reset_command = lambda event: None
    rig.m.connect()
    seen = []
    async def legacy(incoming): seen.append(incoming.text)
    rig.a._continue_claimed_inbound = legacy
    msg = inbound()
    assert not rig.m.claim_options(msg)
    await rig.a._continue_claimed_inbound(msg)
    assert seen == ['question'] and not rig.host.reset_calls


async def test_completed_hook_requires_confirmed_success(rig):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    ev = event(rig, msg)
    rig.m.completed(ev, NS(name='FAILURE'))
    assert not rig.m.held_row(key, 'trigger')['payload'].get('terminal')
    rig.m.completed(ev, NS(name='SUCCESS'))
    assert rig.m.held_row(key, 'trigger')['payload']['terminal']


async def test_history_hook_correlates_nonce_without_usage(rig, monkeypatch):
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    rig.m.bind_injection(msg, event(rig, msg, 'hook-nonce'), 'recent text')
    monkeypatch.setattr(hm, '_session_env', lambda name: 'hook-nonce' if name.endswith('MESSAGE_ID') else key)
    hm.post_api_request(platform='clawchat', session_id='old-session', usage=None)
    await asyncio.sleep(0)
    assert rig.m.state(key)['injected_for'] == 'old-session' and not rig.m.state(key)['inject_next']


async def test_failure_backoff_decrements_on_real_completed_messages(rig):
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), pending={'why': 'idle', 'cut_id': None}, retry_after_msgs=2)
    for remaining in (1, 0):
        rig.m.turns['trigger'] = (msg, None)
        assert rig.m.after_turn(event(rig, msg))
        assert rig.m.state(key)['retry_after_msgs'] == remaining
        assert bool(rig.m.tasks) == (remaining == 0)
    await rig.m.tasks[key]


async def test_renewed_merges_existing_pending_progress(rig):
    ledger(rig, count=5, text='中' * 23500)
    command = inbound('boundary', '/new')
    claim(rig, command)
    key = initialize(rig, command)
    rig.m.queue_pending(command, rig.m.state(key), 'budget')
    await rig.m.start(command)
    assert rig.m.state(key)['summary_through'] == 'm2'
    await rig.m.before(command)
    rig.host.persist(key, 'user-new')
    rig.m.observe_reset('old-session', 'user-new', None)
    assert rig.m.state(key)['summary_through'] == 'm2'
    msg = inbound('after-command')
    claim(rig, msg)
    text = await rig.m.before(msg)
    assert len(rig.llm.calls) == 5 and 'meet at 10' in text
    assert rig.m.state(key)['summary_through'] == 'm4'


async def test_internal_reset_context_drops_send_and_edit(rig):
    token = hm._internal_reset.set((object(), 'cnv_test'))
    try:
        assert (await rig.a.send('cnv_test', 'reset notice')).success
        assert (await rig.a.edit_message('cnv_test', 'message', 'reset edit')).success
        assert hm.suppress_reset_output('cnv_test')
        assert not hm.suppress_reset_output('cnv_other')
    finally:
        hm._internal_reset.reset(token)


async def test_diagnostic_logs_do_not_include_summary_errors_or_messages(rig, caplog):
    placeholder = 'placeholder-secret-for-history-output'
    ledger(rig, text='中' * 5000 + placeholder)
    rig.llm.error = RuntimeError('https://example.invalid/?token=' + placeholder)
    await compact(rig, inbound())
    assert placeholder not in caplog.text and 'token=' not in caplog.text


async def test_group_batches_are_isolated_by_canonical_participant(rig):
    rig.a._clawchat_config = replace(rig.a._clawchat_config, group_sessions_per_user=True)
    one = inbound('one', group=True)
    two = inbound('two', group=True, sender='usr_guest')
    assert rig.a._group_batch_key(one) != rig.a._group_batch_key(two)
    batch = rig.a._group_message_coalescer
    captured = []
    async def capture(msg): captured.append(msg)
    batch._dispatch = capture
    batch.enqueue(one)
    batch.enqueue(two)
    await batch.flush_now(rig.a._group_batch_key(one))
    await batch.flush_now(rig.a._group_batch_key(two))
    assert [msg.grant_speakers for msg in captured] == [('usr_owner',), ('usr_guest',)]
    await batch.cancel()


async def test_group_batch_restart_replays_exact_snapshot_once(rig):
    one, two = inbound('batch-one', group=True), inbound('batch-two', group=True)
    key = initialize(rig, one)
    claim(rig, one)
    claim(rig, two)
    merged = []
    async def capture(msg): merged.append(msg)
    batch = rig.a._group_message_coalescer
    batch._dispatch = capture
    batch.enqueue(one)
    batch.enqueue(two)
    await batch.flush_now(rig.a._group_batch_key(one))
    combined = merged[0]
    ev = event(rig, two)
    ev.raw_message = {'clawchat_raw': combined.raw_message}
    ev.text = combined.text
    ev.channel_context = 'immutable batch context'
    rig.m.save_snapshot(combined, ev)
    await rig.m.recover()
    assert rig.calls == ['batch-two']
    assert not rig.m.store.list_held(rig.m.account, key)
    await batch.cancel()


async def test_restart_resumes_pending_without_retrigger(rig):
    ledger(rig)
    msg = inbound()
    rig.llm.error = RuntimeError()
    key, state = await compact(rig, msg)
    cut = state['pending']['cut_id']
    await rig.m.close()
    fresh = hm.HistoryMaintenance(rig.a, rig.llm)
    rig.a._history = fresh
    fresh.connect()
    rig.llm.error = None
    await fresh.start(msg)
    assert fresh.state(key)['summary_through'] == cut
    assert fresh.state(key)['host_session'] == 'new-session'
    await fresh.close()


async def test_new_during_maintenance_is_held_and_released_in_order(rig):
    first, command, last = inbound('first'), inbound('cmd', '/new'), inbound('last')
    key = initialize(rig, first)
    token = object()
    rig.m.blocked[key] = token
    for msg in (first, command, last):
        assert claim(rig, msg)
        await rig.m.continue_claimed(msg)
    assert not rig.calls
    assert [row['message_id'] for row in rig.m.store.list_held(rig.m.account, key)] == ['first', 'cmd', 'last']
    rig.m.blocked.pop(key)
    await rig.m.release(key)
    assert rig.calls == ['first', 'cmd', 'last']


async def test_original_summary_unchanged_on_next_call_failure(rig):
    ledger(rig, count=5, text='中' * 23500)
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    await rig.m.start(msg)
    old = rig.m.state(key)
    ref = old['summary_ref']
    body = read_summary(rig.m.directory(key), ref)
    rig.llm.error = RuntimeError('placeholder-provider-failure')
    await rig.m.start(msg)
    assert rig.m.state(key)['summary_ref'] == ref
    assert read_summary(rig.m.directory(key), ref) == body
    assert not rig.host.reset_calls


async def test_note_failure_does_not_prevent_reset(rig):
    ledger(rig)
    msg = inbound()
    async def failed_notes(**kwargs): raise RuntimeError('placeholder-notes-failure')
    rig.a._run_sediment_turn = failed_notes
    _, state = await compact(rig, msg)
    assert state['host_session'] == 'new-session' and not state['pending']


async def test_empty_fold_budget_rotates_with_only_window(rig):
    msg = inbound(text='中' * 6000)
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    assert rig.m.state(key)['pending']['cut_id'] is None
    await rig.m.start(msg)
    assert not rig.llm.calls and rig.host.reset_calls
    text = await rig.m.before(inbound('next'))
    assert text.startswith('## Recent messages') and 'id trigger' in text


async def test_late_committed_reset_after_generation_race_is_reconciled(rig):
    msg = inbound()
    key = initialize(rig, msg)
    rig.m.cas(key, rig.m.state(key), pending={
        'why': 'budget', 'cut_id': None, 'stage': 'reset', 'host_session': 'old-session'})
    rig.host.persist(key, 'late-session')
    text = await rig.m.before(msg)
    state = rig.m.state(key)
    assert text and state['host_session'] == 'late-session'
    assert not state['pending'] and state['inject_next']
    assert not rig.host.reset_calls


async def test_old_host_time_reset_warning(rig, monkeypatch, caplog):
    monkeypatch.setattr('clawchat_gateway.adapter._HOST_AGENT_VERSION', '0.14.0')
    rig.host.config = NS(default_reset_policy=NS(mode='both'), reset_by_platform={})
    rig.m.connect()
    assert rig.m.available
    assert 'time resets may clear context before answering' in caplog.text


def test_grant_documentation_contract_unchanged():
    from test_tool_grants_tools import test_configuration_documents_grant_precedence_and_visibility_limits
    test_configuration_documents_grant_precedence_and_visibility_limits()


@pytest.mark.parametrize('tag', ['HEAD', 'v0.21.6', 'v0.22.5'])
def test_history_install_payload_scan(tmp_path, tag):
    # Include new files before git staging, as well as the existing tracked tree.
    import shutil
    from hermes_source import require_hermes_source
    from test_install_scan import _copy_tracked_tree, _pinned_scanner, _run_scan
    host = require_hermes_source('tools/plugin_guard.py')
    scanner = host if tag == 'HEAD' else _pinned_scanner(host, tag, tmp_path / 'scanners')
    if scanner is None:
        pytest.skip('pinned host scanner absent')
    tree = _copy_tracked_tree(tmp_path)
    for relative in ('clawchat_gateway/history_maintenance.py', 'tests/test_history_maintenance.py'):
        shutil.copy2(Path(relative), tree / relative)
    result = _run_scan(scanner, tree)
    assert result is not None
    assert result['critical'] == [] and result['high'] == []


async def test_plain_user_confirmation_keeps_original_reset_candidate(rig):
    ledger(rig, text='meeting')
    command = inbound('command', '/new')
    claim(rig, command)
    key = initialize(rig, command)
    await rig.m.before(command)
    confirmation = inbound('confirmation', 'approve once')
    claim(rig, confirmation)
    assert await rig.m.before(confirmation) == ''
    assert rig.m.candidates[key]['mid'] == 'command'
    assert not rig.m.state(key)['boundary']
    rig.host.persist(key, 'confirmed-new')
    rig.m.observe_reset('old-session', 'confirmed-new', None)
    assert rig.m.state(key)['boundary'] == 'command'


async def test_new_arrival_cannot_overtake_held_release(rig):
    first, second, third = inbound('first'), inbound('second'), inbound('third')
    key = initialize(rig, first)
    claim(rig, first)
    claim(rig, second)
    gate, started = asyncio.Event(), asyncio.Event()
    async def slow_dispatch(msg):
        mid = rig.a._inbound_trigger_id(msg.raw_message)
        if mid == 'first':
            started.set()
            await gate.wait()
        rig.calls.append(mid)
        rig.m.dispatched(event(rig, msg))
    rig.a._continue_claimed_inbound = slow_dispatch
    task = asyncio.create_task(rig.m.release(key))
    await started.wait()
    claim(rig, third)
    await rig.m.continue_claimed(third)
    assert not rig.calls
    gate.set()
    await task
    assert rig.calls == ['first', 'second', 'third']


async def test_connection_close_cancels_inline_renewed_maintenance(rig):
    ledger(rig)
    command = inbound('boundary', '/new')
    claim(rig, command)
    key = initialize(rig, command)
    rig.m.queue_pending(command, rig.m.state(key), 'renewed', cut='boundary', boundary='boundary')
    rig.llm.wait = asyncio.Event()
    task = asyncio.create_task(rig.m.start(command, renewed=True))
    while not rig.llm.calls:
        await asyncio.sleep(.001)
    await rig.m.close()
    assert task.cancelled() and not rig.m.state(key)['summary_ref']


def test_local_host_user_reset_still_uses_confirmation_gate():
    from hermes_source import require_hermes_source
    host = require_hermes_source('gateway/run_inbound.py')
    tree = ast.parse((host / 'gateway/run_inbound.py').read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == '_hm_cmd_new')
    assert any(isinstance(n, ast.Attribute) and n.attr == '_hm_confirm_destructive' for n in ast.walk(fn))
    assert any(isinstance(n, ast.Attribute) and n.attr == '_handle_reset_command' for n in ast.walk(fn))


async def test_startup_recovers_pending_from_dispatched_journal(rig):
    ledger(rig)
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    for row in rig.m.store.list_held(rig.m.account, key):
        rig.m.store.finish_held_batch(rig.m.account, key, {row['message_id']})
    await rig.m.recover()
    assert key in rig.m.tasks
    await rig.m.tasks[key]
    assert rig.m.state(key)['host_session'] == 'new-session'


async def test_explicit_plugin_no_reply_durably_finishes_journal(rig):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    rig.m.no_reply(msg)
    await rig.m.recover()
    assert not rig.calls and not rig.m.store.list_held(rig.m.account, key)


async def test_synthetic_turn_never_uses_legacy_compaction_when_budget_available(rig):
    msg = inbound()
    initialize(rig, msg)
    synthetic = event(rig, msg)
    synthetic.raw_message = {'clawchat_raw': {'synthetic': True}}
    assert rig.m.after_turn(synthetic)
    rig.a._clawchat_config = replace(rig.a._clawchat_config, budget_compaction=False)
    assert not rig.m.after_turn(synthetic)


async def test_maintenance_started_during_media_preparation_defers_before_lock(rig, monkeypatch):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    async def media(_):
        rig.m.blocked[key] = object()
        return []
    monkeypatch.setattr(rig.a, '_download_inbound_media', media)
    await rig.a._handle_inbound_now(msg)
    assert rig.m.held_row(key, 'trigger')['state'] == 'held'
    assert not rig.calls


async def test_replay_confirms_only_history_actually_in_saved_event(rig):
    msg = inbound()
    claim(rig, msg)
    key = initialize(rig, msg)
    ev = event(rig, msg)
    ev.source.chat_name = 'original label'
    rig.m.save_snapshot(msg, ev, '')
    row = rig.m.held_row(key, 'trigger')
    rig.m.replays['trigger'] = row['payload']['snapshot']
    newer = event(rig, msg)
    newer.source.chat_name = 'updated label'
    metadata = rig.m.save_snapshot(msg, newer, 'newly prepared history')
    assert newer.source.chat_name == 'original label'
    assert not metadata['injected']
    rig.m.cas(key, rig.m.state(key), inject_next=1)
    rig.m.bind_injection(msg, newer, 'newly prepared history', metadata)
    rig.m.observe_request('nonce-test', key, 'old-session', 100)
    assert rig.m.state(key)['inject_next']


async def test_confirmed_window_ids_excluded_from_later_per_speaker_backfill(rig, monkeypatch):
    msg = inbound(group=True)
    rig.a._clawchat_config = replace(rig.a._clawchat_config, group_sessions_per_user=True)
    key = initialize(rig, msg)
    rig.m.confirmed[key] = {'injected-message'}
    monkeypatch.setattr(rig.m.store, 'list_recent_group_messages', lambda *args: [
        {'message_id': 'injected-message', 'text': 'already seen'},
        {'message_id': 'unseen-message', 'text': 'new information'}])
    text = rig.a._build_mention_prior_context_text(msg)
    assert 'already seen' not in text and 'new information' in text


async def test_deadline_includes_idle_wait_and_releases_real_preparation_lock(rig, monkeypatch):
    monkeypatch.setattr(hm, 'MAINTENANCE_SECONDS', .12)
    monkeypatch.setattr(hm, 'RESET_WARNING_SECONDS', .01)
    monkeypatch.setattr(hm, 'POLL_SECONDS', .001)
    ledger(rig)
    msg = inbound()
    key = initialize(rig, msg)
    for row in rig.m.store.list_held(rig.m.account, key):
        rig.m.store.finish_held_batch(rig.m.account, key, {row['message_id']})
    rig.m.queue_pending(msg, rig.m.state(key), 'budget')
    loop = asyncio.get_running_loop()
    started = loop.time()
    async def delayed_idle(_): return loop.time() - started >= .07
    monkeypatch.setattr(rig.m, 'idle', delayed_idle)
    rig.host.reset_gate = asyncio.Event()
    delivered = asyncio.Event()
    async def real_preparation(incoming):
        await rig.m.prepare(incoming)
        rig.m.dispatched(event(rig, incoming))
        delivered.set()
    rig.a._continue_claimed_inbound = real_preparation
    task = asyncio.create_task(rig.m.start(msg))
    while not rig.host.reset_calls:
        await asyncio.sleep(.001)
    later = inbound('waiting-for-reset')
    claim(rig, later)
    assert rig.m.defer(later)
    await asyncio.wait_for(delivered.wait(), .09)
    assert loop.time() - started < .18
    assert not rig.host.reset_gate.is_set()
    rig.host.reset_gate.set()
    await task
    await asyncio.sleep(.005)


async def test_account_replacement_fences_old_generation(rig):
    msg = inbound()
    key = initialize(rig, msg)
    old = rig.m.state(key)
    rig.a._clawchat_config = replace(rig.a._clawchat_config, user_id='usr_replacement')
    assert not rig.m.enabled(msg) and not rig.m.claim_options(msg)
    with pytest.raises(hm.Superseded):
        rig.m.cas(key, old, inject_next=1)
    assert rig.m.state(key) == old


async def test_socket_close_and_ready_replace_history_instance(rig, monkeypatch):
    from clawchat_gateway.connection import ConnectionState
    old = rig.m
    await rig.a._on_state_change(ConnectionState.RECONNECTING)
    assert old.closed
    rig.a._connection._cfg = rig.a._clawchat_config
    for name in ('_schedule_owner_metadata_refresh', '_schedule_activation_bootstrap', '_schedule_liveware_sample',
                 '_schedule_memory_migration_hint', '_spawn_group_settings_refresh', '_spawn_permissions_refresh'):
        monkeypatch.setattr(rig.a, name, lambda *a: None)
    async def noop(): pass
    monkeypatch.setattr(rig.a, '_schedule_reconnect_conversation_refresh', noop)
    await rig.a._on_state_change(ConnectionState.READY)
    replacement = rig.a._history
    assert replacement is not old and not replacement.closed
    assert replacement.llm is rig.llm and replacement.available
    assert replacement.drains  # READY schedules recovery without holding the receive loop.
    await replacement.close()


async def test_mentioned_group_command_is_not_real_history_or_idle_timestamp(rig):
    msg = inbound('mentioned-command', '@agent /new', group=True)
    msg.raw_message['payload']['message']['body']['fragments'] = [
        {'kind': 'mention', 'user_id': 'usr_agent', 'display': 'agent'},
        {'kind': 'text', 'text': ' /new'}]
    claim(rig, msg)
    assert rig.m.entries(msg) == []


async def test_oversized_ascii_ledger_entry_keeps_original_before_block_truncation(rig):
    text = 'lorem-ipsum-' * 12000
    msg = inbound('oversized-ascii', text)
    claim(rig, msg)
    entries = rig.m.entries(msg)
    assert entries[0].text == text
    blocks = hm.split_blocks(entries)
    assert len(blocks) == 1 and blocks[0][0].truncated
    assert 'chat_history_read around=oversized-ascii' in blocks[0][0].text
