"""Widget validation, coalescing and live-adapter transport contracts."""
import asyncio
from dataclasses import replace
import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clawchat_gateway import host_tools_guard as guard, tools, plugin_tools, terminal_send
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.api_client import ClawChatApiError
from clawchat_gateway.connection import ConnectionState
from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.widget_send import (
    WIDGET_V1_GUIDE, WIDGET_SENT, WIDGET_DESCRIPTION, NOT_CONNECTED,
    WidgetSendThrottle, check_widget_for_send, run_widget_tool, widget_queued,
)

DOC = '{ "v":1, "id":"todo", "title":"秘密" }'


class Clock:
    def __init__(self):
        self.time = 0.0
        self.handles = []

    def now(self):
        return self.time

    def schedule(self, delay, callback):
        handle = SimpleNamespace(due=self.time + delay, callback=callback, cancelled=False)
        handle.cancel = lambda: setattr(handle, 'cancelled', True)
        self.handles.append(handle)
        return handle

    async def advance(self, seconds):
        self.time += seconds
        for handle in list(self.handles):
            if not handle.cancelled and handle.due <= self.time:
                handle.cancelled = True
                handle.callback()
        await settle()


async def settle():
    for _ in range(12):
        await asyncio.sleep(0)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def throttle(clock):
    t = WidgetSendThrottle(now=clock.now, call_later=clock.schedule)
    yield t
    t.cancel_all()


def test_guide_text():
    assert WIDGET_V1_GUIDE.startswith('## Home-screen widgets (widget/v1)')
    assert WIDGET_V1_GUIDE.count('clawchat_widget_send') == 1
    assert not re.search(r'\bsend_widget\b', WIDGET_V1_GUIDE)


def test_guide_matches_bundled_core_skill_section():
    from pathlib import Path
    skill = (Path(__file__).resolve().parents[1] / 'skills' / 'clawchat-core' / 'SKILL.md').read_text(encoding='utf-8')
    start = skill.index('## Home-screen widgets (widget/v1)')
    assert start > 0
    assert skill[start:].rstrip('\n') == WIDGET_V1_GUIDE.rstrip('\n')


def test_guide_keeps_behaviour_sections_ahead_of_format():
    headings = re.findall(r'^### (.+)$', WIDGET_V1_GUIDE, re.M)
    assert headings[:6] == ['When to send', 'When to update', 'Design', 'Layouts', 'Removing one', 'Document']


def test_guide_removal_snapshot_passes_validation():
    removal = '{"v": 1, "id": "todo", "removed": true}'
    assert removal in WIDGET_V1_GUIDE
    assert check_widget_for_send(removal) == {'id': 'todo'}


@pytest.mark.parametrize('doc,code', [
    ('broken', 'not_json'), ('[]', 'not_json'), ('"hi"', 'not_json'),
    ('1', 'not_json'), ('null', 'not_json'), ('{"v":NaN}', 'not_json'),
    ('{"v":Infinity}', 'not_json'), ('{"v":-Infinity}', 'not_json'),
    ('{"id":"x"}', 'bad_version'), ('{"v":null}', 'bad_version'),
    ('{"v":1}', 'bad_id'), ('{"v":1,"id":12}', 'bad_id'),
    ('{"v":1,"id":""}', 'bad_id'),
])
async def test_validation_never_enters_throttle(doc, code, throttle):
    send = AsyncMock(side_effect=lambda *_: pytest.fail('must not send'))
    checked = check_widget_for_send(doc)
    assert checked['code'] == code
    assert checked['error'].endswith(f'({code}). Check it against the widget/v1 format before you retry.')
    assert (await run_widget_tool(doc, send))['code'] == code
    send.assert_not_awaited()
    assert not throttle.keys


def test_utf8_boundary_and_validation_order():
    base = '{"v":1,"id":"x","text":""}'
    doc = base[:-2] + 'é' * ((16384 - len(base)) // 2) + 'x' * ((16384 - len(base)) % 2) + '"}'
    assert len(doc.encode()) == 16384
    assert check_widget_for_send(doc) == {'id': 'x'}
    assert check_widget_for_send(doc[:-2] + 'x"}')['code'] == 'too_large'
    assert check_widget_for_send('é' * 8193)['code'] == 'too_large'
    # Only presence/non-null v and a nonempty string id are checked.
    assert check_widget_for_send('{"v":false,"id":" Any ID "}') == {'id': ' Any ID '}


@pytest.mark.parametrize('widget', [None, '', ' \n\t ', 42, {}, []])
async def test_guide_does_not_send(widget):
    send = AsyncMock()
    assert await run_widget_tool(widget, send) == {'message': WIDGET_V1_GUIDE}
    send.assert_not_awaited()


async def test_original_document_is_sent_verbatim():
    send = AsyncMock(return_value={'sent': True})
    padded = '\n' + DOC + '  '
    assert (await run_widget_tool(padded, send))['message'] == WIDGET_SENT
    send.assert_awaited_once_with(padded, 'todo')


async def test_outer_whitespace_counts_toward_the_size_limit():
    send = AsyncMock()
    body = '{"v":1,"id":"todo","t":"' + 'x' * (16384 - 26) + '"}'
    assert len(body.encode('utf-8')) == 16384
    assert 'error' not in check_widget_for_send(body)
    result = await run_widget_tool(body + ' ', send)
    assert result['code'] == 'too_large'
    send.assert_not_awaited()


async def test_first_concurrent_ack_and_coalescing(throttle, clock):
    started, ack = asyncio.Event(), asyncio.Event()
    sent = []

    async def first():
        sent.append('first')
        started.set()
        await ack.wait()

    immediate = asyncio.create_task(throttle.submit('account', 'todo', DOC, first))
    await started.wait()
    second = AsyncMock()
    assert await throttle.submit('account', 'todo', 'second', second) == {'queued': True, 'due': 60}
    second.assert_not_awaited()
    await clock.advance(10)
    third = AsyncMock(side_effect=lambda: sent.append('third'))
    queued = await run_widget_tool(DOC, lambda doc, id_: throttle.submit('account', id_, doc, third))
    assert queued['message'] == widget_queued('todo', 60)
    assert '00:01:00 UTC' in queued['message']
    await clock.advance(50)
    assert sent == ['first', 'third']  # The timer does not wait for the first ACK.
    third.assert_awaited_once()
    assert (await throttle.submit('account', 'todo', 'fourth', AsyncMock()))['due'] == 120
    ack.set()
    assert await immediate == {'sent': True}


async def test_ids_and_accounts_are_independent(throttle):
    send = AsyncMock()
    for account, id_ in [('a', 'x'), ('a', 'y'), ('b', 'x')]:
        assert await throttle.submit(account, id_, DOC, send) == {'sent': True}
    assert send.await_count == 3


@pytest.mark.parametrize('flush_first', [False, True])
async def test_flush_submit_race(throttle, clock, flush_first):
    send = AsyncMock()
    await throttle.submit('a', 'x', DOC, send)
    pending = AsyncMock()
    await throttle.submit('a', 'x', 'old', pending)
    clock.time = 60
    handle = clock.handles[0]
    handle.callback()  # Fired, but its lock acquisition has not run yet.
    if flush_first:
        await settle()
    newest = AsyncMock()
    result = await throttle.submit('a', 'x', 'new', newest)
    await settle()
    assert pending.await_count + newest.await_count == 1
    if flush_first:
        assert result == {'queued': True, 'due': 120}
    else:
        assert result == {'sent': True}
        pending.assert_not_awaited()


async def test_immediate_failure_restores_window(throttle, clock):
    failing = AsyncMock(side_effect=RuntimeError('ack failed'))
    with pytest.raises(RuntimeError, match='ack failed'):
        await throttle.submit('a', 'x', DOC, failing)
    assert throttle.keys['a', 'x'].last_start is None
    assert not clock.handles
    assert await throttle.submit('a', 'x', DOC, AsyncMock()) == {'sent': True}
    await clock.advance(60)
    with pytest.raises(RuntimeError):
        await throttle.submit('a', 'x', DOC, failing)
    assert throttle.keys['a', 'x'].last_start == 0


async def test_timed_failure_logs_no_content_and_consumes_window(throttle, clock, caplog):
    await throttle.submit('a', 'x', DOC, AsyncMock())
    await throttle.submit('a', 'x', DOC, AsyncMock(side_effect=RuntimeError(DOC)))
    await clock.advance(60)
    assert 'widget send failed' in caplog.text and 'RuntimeError' in caplog.text
    assert DOC not in caplog.text and '秘密' not in caplog.text
    assert (await throttle.submit('a', 'x', DOC, AsyncMock()))['due'] == 120


async def test_cancel_all_cancels_timers(throttle, clock):
    await throttle.submit('a', 'x', DOC, AsyncMock())
    pending = AsyncMock()
    await throttle.submit('a', 'x', DOC, pending)
    throttle.cancel_all()
    assert clock.handles[0].cancelled and not throttle.keys
    await clock.advance(60)
    pending.assert_not_awaited()


@pytest.fixture
def adapter(monkeypatch, tmp_path, clock, clawchat_test_platform):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id='usr_agent', owner_user_id='usr_owner', token='test-token')
    a._store = SimpleNamespace(get_activation_widget_target=lambda **_: ('usr_owner', 'cnv_owner', 'generation'))
    a._widget_throttle = WidgetSendThrottle(now=clock.now, call_later=clock.schedule)
    a._connection._state = ConnectionState.READY
    a._connection.send_frame = AsyncMock(return_value=True)
    a.client = SimpleNamespace(get_direct_conversation=AsyncMock(return_value={'conversation': {'id': 'cnv_resolved'}}))
    monkeypatch.setattr(a, '_new_api_client', lambda: a.client)
    monkeypatch.setattr(tools, 'get_clawchat_sender', lambda: a)
    monkeypatch.setattr(guard, 'orchestrate_handler_origin', lambda **_: {'initiator_id': 'usr_owner'})
    yield a
    a._widget_throttle.cancel_all()
    terminal_send.clear_clawchat_mention_sender(a)


async def live(adapter):
    adapter._connection._owner_loop = asyncio.get_running_loop()


async def test_transport_exact_owner_frame_and_no_terminal(adapter, monkeypatch):
    await live(adapter)
    monkeypatch.setenv('HERMES_SESSION_CHAT_ID', 'cnv_group')
    monkeypatch.setattr(adapter, '_host_process_sender', lambda: True)
    result = await tools.widget_send('\n' + DOC + '  ')
    assert result == {'sent': True, 'message': WIDGET_SENT}
    call = adapter._connection.send_frame.call_args
    assert call.kwargs == {'wait_for_ack': True, 'queue_when_unready': False}
    frame = call.args[0]
    assert frame['event'] == 'message.send' and frame['chat_id'] == 'cnv_owner'
    assert frame['payload']['message_mode'] == 'widget'
    assert frame['payload']['message']['body']['fragments'] == [{'kind': 'widget', 'text': '\n' + DOC + '  '}]
    assert frame['payload']['message']['context'] == {'mentions': [], 'reply': None}
    assert frame['payload']['message_id']
    adapter.client.get_direct_conversation.assert_not_awaited()
    assert 'terminal' not in result and 'noFollowupReply' not in result
    assert not terminal_send._terminal_sends


@pytest.mark.parametrize('row', [('usr_other', 'cnv_wrong', 'g'), ('usr_owner', '', 'g'), None])
async def test_target_mismatch_or_missing_cnv_resolves_once_and_caches(adapter, row):
    await live(adapter)
    adapter._store.get_activation_widget_target = lambda **_: row
    for id_ in ['x', 'y']:
        assert (await adapter.send_widget_message(DOC, id_))['sent']
    adapter.client.get_direct_conversation.assert_awaited_once_with('usr_owner')
    assert all(c.args[0]['chat_id'] == 'cnv_resolved' for c in adapter._connection.send_frame.call_args_list)


@pytest.mark.parametrize('failure', ['no-owner', 'no-cnv', 'api'])
async def test_target_failures_send_nothing(adapter, failure):
    await live(adapter)
    adapter._store.get_activation_widget_target = lambda **_: None
    if failure == 'no-owner':
        adapter._owner_user_id = lambda: ''
    elif failure == 'no-cnv':
        adapter.client.get_direct_conversation.return_value = {}
    else:
        adapter.client.get_direct_conversation.side_effect = ClawChatApiError('api', 'not friends', code=19012)
    result = await tools.widget_send(DOC)
    assert result['message'] == ('not friends' if failure == 'api' else 'no owner chat')
    if failure == 'api':
        assert result['meta']['code'] == 19012
    adapter._connection.send_frame.assert_not_awaited()
    assert not adapter._widget_throttle.keys


@pytest.mark.parametrize('change', ['owner', 'generation', 'resolution'])
async def test_pending_dropped_if_target_changes_or_resolution_fails(adapter, clock, caplog, change):
    await live(adapter)
    assert (await tools.widget_send(DOC))['sent']
    assert (await tools.widget_send(DOC))['queued']
    if change == 'owner':
        adapter._owner_user_id = lambda: 'usr_new'
    elif change == 'generation':
        adapter._store.get_activation_widget_target = lambda **_: ('usr_owner', 'cnv_other', 'new')
    else:
        adapter._store.get_activation_widget_target = Mock(side_effect=RuntimeError('lookup failed'))
    await clock.advance(60)
    assert adapter._connection.send_frame.await_count == 1
    assert 'widget send failed' in caplog.text
    assert DOC not in caplog.text


@pytest.mark.parametrize('failure', ['offline', 'false', 'stopped-loop', 'ack'])
async def test_transport_failure_is_error_and_not_throttled(adapter, failure):
    await live(adapter)
    if failure == 'offline':
        adapter._connection._state = ConnectionState.DISCONNECTED
    elif failure == 'false':
        adapter._connection.send_frame.return_value = False
    elif failure == 'stopped-loop':
        adapter._connection._owner_loop = None
    else:
        adapter._connection.send_frame.side_effect = TimeoutError('ack timeout')
    result = await tools.widget_send(DOC)
    assert result['error'] == 'transport'
    assert result['message'] == ('ack timeout' if failure == 'ack' else NOT_CONNECTED)
    assert all(s.last_start is None for s in adapter._widget_throttle.keys.values())


@pytest.mark.parametrize('sender', [None, SimpleNamespace()])
async def test_no_live_adapter_error(adapter, monkeypatch, sender):
    monkeypatch.setattr(tools, 'get_clawchat_sender', lambda: sender)
    assert (await tools.widget_send(DOC))['message'] == 'no live ClawChat connection in this process'


async def test_worker_loop_hops_to_adapter_loop(adapter):
    await live(adapter)
    owner_loop = asyncio.get_running_loop()
    observed = []

    async def send_frame(*_, **__):
        observed.append(asyncio.get_running_loop())
        return True

    adapter._connection.send_frame = send_frame
    outcome = []
    def worker():
        async def submit():
            task = asyncio.create_task(adapter.send_widget_message(DOC, 'todo'))
            for _ in range(200):
                if task.done():
                    return await task
                await asyncio.sleep(0.01)
            raise AssertionError('adapter loop did not finish submission')
        try:
            outcome.append(asyncio.run(submit()))
        except BaseException as exc:
            outcome.append(exc)
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    for _ in range(300):
        if outcome:
            break
        await asyncio.sleep(0.01)
    assert outcome == [{'sent': True}]
    assert observed == [owner_loop]


async def test_disconnect_clears_timers_and_target_cache(adapter, clock, monkeypatch):
    await live(adapter)
    await tools.widget_send(DOC)
    await tools.widget_send(DOC)
    monkeypatch.setattr(adapter._connection, 'stop', AsyncMock())
    await adapter.disconnect()
    assert not adapter._widget_throttle.keys and not adapter._widget_target_cache
    assert adapter._widget_identity is None and clock.handles[0].cancelled
    await clock.advance(60)
    assert adapter._connection.send_frame.await_count == 1


def test_atomic_activation_target_read(tmp_path):
    store = ClawChatStore(tmp_path / 'test.sqlite')
    store.upsert_activation(platform='hermes', account_id='default', user_id='usr_agent',
                            owner_user_id='usr_owner', conversation_id='cnv_owner', access_token='first')
    owner, cnv, generation = store.get_activation_widget_target(platform='hermes', account_id='default')
    assert (owner, cnv) == ('usr_owner', 'cnv_owner')
    assert len(generation) == 64 and 'first' not in generation
    store.upsert_activation(platform='hermes', account_id='default', user_id='usr_agent',
                            owner_user_id='usr_other', conversation_id='cnv_other', access_token='second')
    assert store.get_activation_widget_target(platform='hermes', account_id='default')[2] != generation


def test_registration_and_manifest_match():
    import yaml
    from pathlib import Path
    registered = {}
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, toolset, schema, handler, **kw:
        registered.update({name: (toolset, schema, handler, kw)})))
    manifest = yaml.safe_load(Path('plugin.yaml').read_text())
    assert set(manifest['provides_tools']) == set(registered)
    toolset, schema, handler, kw = registered['clawchat_widget_send']
    assert toolset == 'clawchat' and handler is plugin_tools.handle_clawchat_widget_send and kw['is_async']
    assert schema['description'] == plugin_tools._direct_tool_description(WIDGET_DESCRIPTION)
    assert schema['parameters'] == {'type': 'object', 'properties': {'widget': {
        'type': 'string', 'description': 'the widget/v1 document as a JSON string; omit to get the format'}}}


def test_description_asks_for_a_same_turn_resend_when_data_changes():
    assert ('Only after they asked for one. When what one shows changes (a tap on it, '
            'or the same data changed in chat), send it again in that turn. Each call is '
            'the whole widget') in WIDGET_DESCRIPTION


async def test_handler_logs_only_id_and_byte_length(adapter, caplog, monkeypatch):
    await live(adapter)
    monkeypatch.setattr(plugin_tools, '_record_tool_call', Mock(side_effect=AssertionError('must not persist content')))
    with caplog.at_level('INFO', logger='clawchat_gateway.plugin_tools'):
        result = json.loads(await plugin_tools.handle_clawchat_widget_send({'widget': DOC}))
    assert result['sent'] and 'id=' in caplog.text and 'bytes=' in caplog.text
    assert DOC not in caplog.text and '秘密' not in caplog.text


async def test_old_ack_failure_cannot_undo_a_newer_flush(throttle, clock):
    started, fail = asyncio.Event(), asyncio.Event()

    async def first():
        started.set()
        await fail.wait()
        raise RuntimeError('late ack failure')

    task = asyncio.create_task(throttle.submit('a', 'x', DOC, first))
    await started.wait()
    newest = AsyncMock()
    await throttle.submit('a', 'x', DOC, newest)
    await clock.advance(60)
    newest.assert_awaited_once()
    fail.set()
    with pytest.raises(RuntimeError, match='late ack failure'):
        await task
    assert (await throttle.submit('a', 'x', DOC, AsyncMock()))['due'] == 120


async def test_concurrent_resolution_calls_api_once(adapter):
    await live(adapter)
    adapter._store.get_activation_widget_target = lambda **_: ('usr_other', 'cnv_wrong', 'generation')
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(owner):
        started.set()
        await release.wait()
        return {'conversation': {'id': 'cnv_resolved'}}

    adapter.client.get_direct_conversation.side_effect = lookup
    first = asyncio.create_task(adapter.send_widget_message(DOC, 'one'))
    await started.wait()
    second = asyncio.create_task(adapter.send_widget_message(DOC, 'two'))
    release.set()
    assert await asyncio.gather(first, second) == [{'sent': True}, {'sent': True}]
    adapter.client.get_direct_conversation.assert_awaited_once_with('usr_owner')
    ids = [c.args[0]['payload']['message_id'] for c in adapter._connection.send_frame.call_args_list]
    assert len(set(ids)) == 2


async def test_disconnect_during_resolution_cannot_send_or_refill_cache(adapter, monkeypatch):
    await live(adapter)
    adapter._store.get_activation_widget_target = lambda **_: None
    started, release = asyncio.Event(), asyncio.Event()

    async def lookup(owner):
        started.set()
        await release.wait()
        return {'conversation': {'id': 'cnv_resolved'}}

    adapter.client.get_direct_conversation.side_effect = lookup
    task = asyncio.create_task(tools.widget_send(DOC))
    await started.wait()
    monkeypatch.setattr(adapter._connection, 'stop', AsyncMock())
    await adapter.disconnect()
    release.set()
    assert 'error' in await task
    adapter._connection.send_frame.assert_not_awaited()
    assert not adapter._widget_target_cache and not adapter._widget_throttle.keys


async def test_immediate_failure_rearms_pending_against_restored_start(throttle, clock):
    await throttle.submit('a', 'x', DOC, AsyncMock())  # t0
    await clock.advance(61)
    started, fail = asyncio.Event(), asyncio.Event()

    async def failing():
        started.set()
        await fail.wait()
        raise RuntimeError('ack failed')

    immediate = asyncio.create_task(throttle.submit('a', 'x', DOC, failing))
    await started.wait()
    await clock.advance(1)  # t62
    pending = AsyncMock()
    assert await throttle.submit('a', 'x', DOC, pending) == {'queued': True, 'due': 121}
    original_timer = clock.handles[-1]
    await clock.advance(1)  # t63
    fail.set()
    with pytest.raises(RuntimeError, match='ack failed'):
        await immediate
    assert throttle.keys['a', 'x'].last_start == 0
    assert original_timer.cancelled and clock.handles[-1].due == 63
    pending.assert_not_awaited()
    await clock.advance(0)
    pending.assert_awaited_once()
    assert throttle.keys['a', 'x'].last_start == 63
    assert (await throttle.submit('a', 'x', DOC, AsyncMock()))['due'] == 123
    await clock.advance(58)  # t121: the cancelled timer must not send again.
    pending.assert_awaited_once()


@pytest.mark.parametrize('number', ['9' * 5000, '9' * 5000 + '.5e9999'])
async def test_large_json_numbers_preserve_shape_without_conversion(number):
    doc = '{"v":1,"id":"todo","value":' + number + '}'
    assert len(doc.encode('utf-8')) < 16384
    assert check_widget_for_send(doc) == {'id': 'todo'}
    send = AsyncMock(return_value={'sent': True})
    assert (await run_widget_tool(doc, send))['sent']
    send.assert_awaited_once_with(doc, 'todo')
    assert check_widget_for_send('{"v":1,"id":' + number + '}')['code'] == 'bad_id'
    assert check_widget_for_send(number)['code'] == 'not_json'


async def test_socket_close_after_write_before_ack_fails_without_replay(adapter, monkeypatch):
    from clawchat_gateway import connection as connection_mod

    class Socket:
        def __init__(self):
            self.sent = []
            self.written = asyncio.Event()
            self.closed = asyncio.Event()

        async def send(self, text):
            self.sent.append(json.loads(text))
            self.written.set()

        async def close(self):
            self.closed.set()

    first, reconnected = Socket(), Socket()
    conn = connection_mod.ClawChatConnection(adapter._clawchat_config, on_message=AsyncMock())
    conn._store = None
    conn._owner_loop = asyncio.get_running_loop()
    adapter._connection = conn
    adapter._store.get_activation_widget_target = lambda **_: ('usr_owner', 'cnv_' + 'A' * 26, 'g')
    monkeypatch.setattr(connection_mod, '_ws_connect', AsyncMock(side_effect=[first, reconnected]))
    monkeypatch.setattr(conn, '_connection_credentials_current', lambda: True)
    monkeypatch.setattr(conn, '_watch_activation_credentials', AsyncMock())

    async def read_loop(socket):
        conn._hello_wait.set_result(True)
        await socket.closed.wait()

    monkeypatch.setattr(conn, '_read_loop', read_loop)
    connections, send = [], None
    try:
        connections.append(asyncio.create_task(conn._run_one_connection()))
        await asyncio.wait_for(conn._ready_event.wait(), 2)
        send = asyncio.create_task(tools.widget_send(DOC))
        await asyncio.wait_for(first.written.wait(), 2)
        assert len(first.sent) == 1 and first.sent[0]['payload']['message_mode'] == 'widget'
        assert conn._pending_acks and not send.done() and not conn._send_queue
        await first.close()
        assert await asyncio.wait_for(connections[0], 2) is False
        result = await asyncio.wait_for(send, 2)
        assert result['error'] == 'transport' and result['message'] == 'connection disconnected'
        assert not conn._pending_acks and not conn._send_queue
        connections.append(asyncio.create_task(conn._run_one_connection()))
        await asyncio.wait_for(conn._ready_event.wait(), 2)
        await settle()
        assert conn.is_ready and reconnected.sent == [] and not conn._send_queue
    finally:
        await first.close()
        await reconnected.close()
        await asyncio.gather(*connections, return_exceptions=True)
        if send is not None:
            await asyncio.gather(send, return_exceptions=True)
