"""Pin current widget action input: only accompanying text reaches Hermes."""
from dataclasses import replace
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway import turn_grants
from clawchat_gateway.terminal_send import clear_clawchat_mention_sender


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id='usr_agent', owner_user_id='usr_owner')
    a._owner_direct_chat_id = lambda: 'cnv_owner'
    a._compose_channel_prompt_parts = lambda *_, **__: []
    a._maybe_consume_skill_update_consent = AsyncMock(return_value=False)
    a._sync_inbound_metadata = AsyncMock()
    a._schedule_profile_sync = lambda coro: coro.close()
    a._resolve_inbound_sender_context = lambda inbound: inbound
    a.handle_message = AsyncMock()
    if not hasattr(a, 'build_source'):
        a.build_source = lambda **kw: SimpleNamespace(**kw)
    yield a
    for key in a._grant_sessions:
        turn_grants.release_session(a._account_id(), key)
    clear_clawchat_mention_sender(a)


def frame(text=None, mode='normal'):
    fragments = [{'kind': 'widget_action', 'widget_id': 'todo', 'text': 'hidden action data'}]
    if text is not None:
        fragments.append({'kind': 'text', 'text': text})
    return {'event': 'message.send', 'chat_id': 'cnv_owner', 'chat_type': 'direct',
            'sender': {'id': 'usr_owner'}, 'payload': {'message_id': 'msg_widget', 'message_mode': mode,
            'message': {'body': {'fragments': fragments}}}}


@pytest.mark.parametrize('text,expected', [('[widget todo] checked "Pay rent" (t1)', '[widget todo] checked "Pay rent" (t1)'), (None, '')])
async def test_widget_action_host_event_text(adapter, text, expected):
    await adapter._on_message(frame(text))
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.call_args.args[0].text == expected


@pytest.mark.parametrize('mode', ['widget', 'thinking'])
async def test_non_normal_widget_input_is_dropped_whole(adapter, mode):
    await adapter._on_message(frame('text must also be dropped', mode))
    adapter.handle_message.assert_not_awaited()
