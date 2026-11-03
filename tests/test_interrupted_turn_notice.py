"""Restart notices use durable handoffs and outbound claims, never host replay."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import sqlite3
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from clawchat_gateway import history_maintenance as hm, storage
from clawchat_gateway.interrupted_turns import InterruptedTurnNotices, NOTICE_TEXT
from clawchat_gateway.owner_language import OWNER_LANGUAGES
from clawchat_gateway.storage import ClawChatStore
from test_history_maintenance import rig, inbound, claim, event


@pytest.fixture
async def notices(rig, monkeypatch):
    rig.a._connection = NS(send_frame=AsyncMock(return_value=True))
    monkeypatch.setattr(rig.a, '_owner_locale', lambda: None)
    rig.m.interrupted_notices = InterruptedTurnNotices(rig.m, delay=0, started_at=time.time() - 30)
    return rig.m.interrupted_notices


def handed(rig, mid='interrupted', *, group=False, handoff=None, changes=None, chat='cnv_test'):
    msg = inbound(mid, group=group, chat=chat)
    assert claim(rig, msg)
    key = rig.m.key(msg)
    rig.m.store.finish_held_batch(rig.m.account, key, {mid},
                                  handoff_at=time.time() - 300 if handoff is None else handoff)
    if changes:
        rig.m.store.update_journal_payload(rig.m.account, key, mid, changes)
    return msg, key


def rows(rig, key):
    return rig.m.store.list_held(rig.m.account, key, states=('held', 'dispatching', 'dispatched'))


def outbound(rig, *, chat='cnv_test', at=None, account='default', kind='message', event_type='message.reply'):
    assert rig.m.store.claim_message_once(platform='hermes', account_id=account, kind=kind,
        direction='outbound', event_type=event_type, chat_id=chat, message_id='previous-output',
        text='delivered or claimed answer', raw={}, created_at=int((time.time() if at is None else at) * 1000))


@pytest.mark.parametrize('group', [False, True])
async def test_restart_notice_once_with_durable_marker_and_ack(rig, notices, group):
    msg, key = handed(rig, group=group)
    await rig.m.close()
    fresh = hm.HistoryMaintenance(rig.a, rig.llm)
    rig.a._history = fresh
    fresh.connect()
    checker = fresh.interrupted_notices
    checker.started_at = time.time() - 30
    checker.delay = 0
    sent = []
    async def send(frame, *, wait_for_ack):
        assert wait_for_ack
        assert rows(rig, key)[0]['payload']['notified'] is True
        sent.append(frame)
        return True
    rig.a._connection.send_frame = send
    try:
        fresh.begin_recovery()
        await checker.task
        assert len(sent) == 1
        frame = sent[0]
        assert frame['event'] == 'message.reply' and frame['chat_id'] == msg.chat_id
        assert frame['payload']['message']['context']['reply']['reply_to_msg_id'] == 'interrupted'
        assert frame['payload']['message']['body']['fragments'][0]['text'] == NOTICE_TEXT['en']
        assert 'clawchat_interrupted_turn_notice' not in frame
        await checker.check()
        assert len(sent) == 1
        # The claimed outbound persists but cannot enter history tools, injected
        # context, summaries, activity or the legacy group-history path.
        raw = rig.m.store.recent_outbound_messages(account_id='default', chat_id=msg.chat_id, since_ms=0)[0]['raw']
        assert raw['clawchat_interrupted_turn_notice'] is True
        assert raw['clawchat_history_activity'] == {'kind': 'notice'}
        assert [row['message_id'] for row in rig.m.store.list_chat_history_messages('default', msg.chat_id)] == ['interrupted']
        assert [row['message_id'] for row in rig.m.store.list_recent_group_messages('default', msg.chat_id, 20)] == ['interrupted']
        assert [row['message_id'] for row in rig.m.store.list_recent_group_transcript('default', msg.chat_id, 20)] == ['interrupted']
        assert [e.id for e in fresh.entries(msg)] == ['interrupted']
        assert all(e.id == 'interrupted' for e in fresh.activity_entries(msg))
    finally:
        await fresh.close()


@pytest.mark.parametrize('changes', [
    {'terminal': True}, {'terminal': 'success'}, {'terminal': 'failure'}, {'terminal': 'cancelled'},
    {'notified': True}, {'control': True}, {'local_consumed': True}, {'internal_synthetic': True},
])
async def test_terminal_or_excluded_rows_never_notify(rig, notices, changes):
    _, key = handed(rig, changes=changes)
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()
    assert rows(rig, key)[0]['payload'].get('notified') == changes.get('notified')


@pytest.mark.parametrize('case', ['legacy', 'expired', 'current-process', 'synthetic', 'held', 'dispatching'])
async def test_legacy_expired_and_unhanded_rows_do_not_notify(rig, notices, case):
    msg = inbound('excluded')
    claim(rig, msg)
    key = rig.m.key(msg)
    if case in {'legacy', 'held', 'dispatching'}:
        if case != 'held':
            rig.m.store.mark_held_state(rig.m.account, key, 'excluded', 'held',
                                        'dispatching' if case == 'dispatching' else 'dispatched')
    else:
        at = time.time() - (90000 if case == 'expired' else 5 if case == 'current-process' else 300)
        rig.m.store.finish_held_batch(rig.m.account, key, {'excluded'}, handoff_at=at)
        if case == 'synthetic':
            rig.m.store.update_journal_payload(rig.m.account, key, 'excluded',
                                                {'frame': dict(msg.raw_message, synthetic=True)})
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()
    assert not rows(rig, key)[0]['payload'].get('notified')


async def test_notice_switch_disables_even_database_checks(rig, notices, monkeypatch):
    handed(rig)
    rig.a._clawchat_config = replace(rig.a._clawchat_config, interrupted_turn_notice=False)
    def unexpected(*args, **kwargs):
        pytest.fail('disabled notices must not scan')
    monkeypatch.setattr(rig.m.store, 'list_held', unexpected)
    notices.schedule()
    assert notices.task is None
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()


@pytest.mark.parametrize('event_type', ['message.reply', 'message.send', 'message.error'])
async def test_any_outbound_claim_after_handoff_suppresses_notice(rig, notices, event_type):
    _, key = handed(rig)
    outbound(rig, event_type=event_type)
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()
    assert not rows(rig, key)[0]['payload'].get('notified')


@pytest.mark.parametrize('scope', ['before', 'other-chat', 'other-account'])
async def test_outbound_query_uses_chat_time_and_ledger_namespace(rig, notices, scope):
    handed(rig)
    outbound(rig, at=time.time() - 600 if scope == 'before' else None,
             chat='cnv_other' if scope == 'other-chat' else 'cnv_test',
             account='another-account' if scope == 'other-account' else 'default')
    await notices.check()
    rig.a._connection.send_frame.assert_awaited_once()


async def test_multiple_members_reply_to_latest_and_mark_every_candidate(rig, notices):
    _, key = handed(rig, 'first', handoff=time.time() - 600)
    handed(rig, 'latest', handoff=time.time() - 300)
    await notices.check()
    frame = rig.a._connection.send_frame.call_args.args[0]
    assert frame['payload']['message']['context']['reply']['reply_to_msg_id'] == 'latest'
    assert all(row['payload']['notified'] for row in rows(rig, key))
    await notices.check()
    rig.a._connection.send_frame.assert_awaited_once()


async def test_reply_after_older_handoff_does_not_hide_newer_interruption(rig, notices):
    _, key = handed(rig, 'answered', handoff=time.time() - 600)
    outbound(rig, at=time.time() - 450)
    handed(rig, 'interrupted', handoff=time.time() - 300)
    await notices.check()
    frame = rig.a._connection.send_frame.call_args.args[0]
    assert frame['payload']['message']['context']['reply']['reply_to_msg_id'] == 'interrupted'
    assert not rows(rig, key)[0]['payload'].get('notified')


async def test_notified_commit_failure_rolls_back_batch_and_prevents_send(rig, notices, monkeypatch):
    _, key = handed(rig, 'first')
    handed(rig, 'second')
    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError('placeholder commit failure')
    monkeypatch.setattr(rig.m.store, '_tool_grants_connection',
                        lambda: sqlite3.connect(rig.m.store.db_path, factory=FailingCommit))
    await notices.run(rig.m.epoch)
    rig.a._connection.send_frame.assert_not_awaited()
    assert all(not row['payload'].get('notified') for row in rows(rig, key))


async def test_unavailable_outbound_ledger_never_sends(rig, notices, monkeypatch):
    _, key = handed(rig)
    original = rig.m.store._tool_grants_connection
    class BrokenLedger(sqlite3.Connection):
        def execute(self, sql, *args):
            if 'FROM clawchat_messages' in sql:
                raise sqlite3.OperationalError('placeholder read failure')
            return super().execute(sql, *args)
    monkeypatch.setattr(rig.m.store, '_tool_grants_connection',
                        lambda: sqlite3.connect(rig.m.store.db_path, factory=BrokenLedger))
    await notices.run(rig.m.epoch)
    monkeypatch.setattr(rig.m.store, '_tool_grants_connection', original)
    rig.a._connection.send_frame.assert_not_awaited()
    assert not rows(rig, key)[0]['payload'].get('notified')


@pytest.mark.parametrize('locale,language', [('zh-CN', 'zh'), (None, 'en'), ('unknown', 'en')])
async def test_owner_language_and_english_fallback(rig, notices, monkeypatch, locale, language):
    handed(rig)
    monkeypatch.setattr(rig.a, '_owner_locale', lambda: locale)
    await notices.check()
    frame = rig.a._connection.send_frame.call_args.args[0]
    assert frame['payload']['message']['body']['fragments'] == [{'kind': 'text', 'text': NOTICE_TEXT[language]}]
    assert set(NOTICE_TEXT) == set(OWNER_LANGUAGES)


async def test_shutdown_cancels_delay_and_replacement_epoch_is_fenced(rig, notices):
    import asyncio
    handed(rig)
    notices.delay = 120
    rig.m.begin_recovery()
    task = notices.task
    assert task in rig.m.drains
    await asyncio.sleep(0)
    await rig.m.close()
    assert task.cancelled()
    rig.a._connection.send_frame.assert_not_awaited()
    rig.m.connect()
    rig.a._history = NS()
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()


async def test_failed_send_is_never_repeated(rig, notices):
    _, key = handed(rig)
    rig.a._connection.send_frame.return_value = False
    await notices.check()
    assert rows(rig, key)[0]['payload']['notified']
    await notices.check()
    rig.a._connection.send_frame.assert_awaited_once()


async def test_concurrent_notice_claims_have_one_winner(rig, notices):
    _, key = handed(rig, 'first')
    handed(rig, 'second')
    other = ClawChatStore(rig.m.store.db_path)
    other.initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        winners = list(pool.map(lambda store: store.claim_interrupted_turn_notice(
            rig.m.account, key, started_at=notices.started_at, now=time.time()), [rig.m.store, other]))
    assert sum(row is not None for row in winners) == 1
    assert all(row['payload']['notified'] for row in rows(rig, key))


async def test_cleanup_preserves_recent_handoff_even_for_old_held_input(rig, notices, monkeypatch):
    real_now = time.time()
    monkeypatch.setattr(storage, '_now_ms', lambda: int((real_now - 9 * 86400) * 1000))
    _, key = handed(rig)
    monkeypatch.setattr(storage, '_now_ms', lambda: int(real_now * 1000))
    assert rig.m.store.purge_dispatched(8 * 86400) == 0
    await notices.check()
    assert rows(rig, key)[0]['payload']['notified']


async def test_handoff_stamps_whole_batch_and_completion_merges_members(rig, notices):
    first, second = inbound('first'), inbound('second')
    claim(rig, first)
    claim(rig, second)
    key = rig.m.key(first)
    ev = event(rig, second)
    ev.raw_message = {'clawchat_raw': {'clawchat_group_batch': True,
                                     'messages': [first.raw_message, second.raw_message]}}
    rig.m.dispatched(ev)
    batch = rows(rig, key)
    assert len({row['payload']['handoff_at'] for row in batch}) == 1
    assert all(row['state'] == 'dispatched' for row in batch)
    rig.m.dispatched(ev)
    assert rows(rig, key) == batch  # A duplicate handoff cannot refresh its age.
    rig.m.completed(ev, NS(name='CANCELLED'))
    assert all(row['payload']['terminal'] == 'cancelled' for row in rows(rig, key))


async def test_local_consumption_has_no_handoff(rig, notices):
    msg = inbound('consumed')
    claim(rig, msg)
    rig.m.no_reply(msg)
    payload = rows(rig, rig.m.key(msg))[0]['payload']
    assert payload['local_consumed'] is True and 'handoff_at' not in payload
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()


@pytest.mark.parametrize('decision', ['terminal', 'outbound'])
async def test_transaction_rechecks_decisions_after_candidate_scan(rig, notices, monkeypatch, decision):
    _, key = handed(rig)
    original = rig.m.store.claim_interrupted_turn_notice
    def intervene(*args, **kwargs):
        if decision == 'terminal':
            rig.m.store.update_journal_payload(rig.m.account, key, 'interrupted', {'terminal': True})
        else:
            outbound(rig)
        return original(*args, **kwargs)
    monkeypatch.setattr(rig.m.store, 'claim_interrupted_turn_notice', intervene)
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()
    assert not rows(rig, key)[0]['payload'].get('notified')


async def test_outbound_claim_failure_does_not_send_or_repeat(rig, notices, monkeypatch):
    _, key = handed(rig)
    monkeypatch.setattr(rig.a, '_claim_outbound_message', lambda **kwargs: None)
    await notices.check()
    assert rows(rig, key)[0]['payload']['notified'] is True
    rig.a._connection.send_frame.assert_not_awaited()
    await notices.check()
    rig.a._connection.send_frame.assert_not_awaited()
