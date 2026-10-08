"""A Hermes notice the plugin does not know yet stays out of a turn that ends in silence.

Hermes sends its runtime notices ("⚠️ …", "ℹ️ …", "🔄 …") through the same
``send()`` as replies. On ClawChat nothing on the call marks one as status: the
``non_conversational`` metadata flag is set for Discord only
(``gateway/run.py _non_conversational_metadata``), ``_interim_send`` marks every
mid-turn send including the agent's own interim text, and ``notify`` marks only
the turn-final reply. So the plugin keeps its prefix list, and every new Hermes
wording leaked until the list caught up (#240 #241 #342 #509 #523 #542 #637).

Fallback: while a turn runs, a send that opens with a status glyph and is not on
the list is held. If the turn ends without a reply (no-reply), the held notices
are dropped; otherwise they go out when the turn ends. The agent's own interim
text (no glyph) is sent at once, and the ``full`` output preset still shows
everything as before.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway.adapter import NO_REPLY_TOKEN, ClawChatAdapter

GROUP = "cnv_group"
NEW_NOTICE = "⚠️ Brand-new Hermes notice nobody has listed yet — retrying."


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a._known_chat_types[GROUP] = "group"
    a._active_sessions = {}
    a.frames = []

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        a.frames.append(frame)
        return True

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_kw: True)
    monkeypatch.setattr(a, "_runtime_status_messages_enabled", lambda: False)
    return a


def _texts(adapter):
    out = []
    for frame in adapter.frames:
        payload = frame["payload"]
        body = payload.get("message", {}).get("body") or payload.get("message", {})
        fragments = body.get("fragments") or payload.get("fragments") or []
        out.append("".join(f.get("text", "") for f in fragments if f.get("kind") == "text"))
    return out


def _turn(adapter):
    adapter._active_sessions[f"agent:main:clawchat:group:{GROUP}"] = object()


def _end_turn(adapter):
    adapter._active_sessions.clear()
    event = SimpleNamespace(source=SimpleNamespace(chat_id=GROUP, chat_type="group", user_id="usr_ada"), raw_message={})
    asyncio.run(adapter.on_processing_complete(event, None))


def test_an_unknown_notice_in_a_silent_turn_is_dropped(adapter):
    _turn(adapter)
    asyncio.run(adapter.send(GROUP, NEW_NOTICE))
    asyncio.run(adapter.send(GROUP, NO_REPLY_TOKEN, metadata={"notify": True}))
    _end_turn(adapter)
    assert adapter.frames == []


def test_an_unknown_notice_in_a_turn_with_a_reply_goes_out_after_it(adapter):
    _turn(adapter)
    asyncio.run(adapter.send(GROUP, NEW_NOTICE))
    asyncio.run(adapter.send(GROUP, "Here is the plan.", metadata={"notify": True}))
    _end_turn(adapter)
    assert _texts(adapter) == ["Here is the plan.", NEW_NOTICE]


def test_the_agents_interim_text_is_not_held(adapter):
    _turn(adapter)
    asyncio.run(adapter.send(GROUP, "Let me check that first."))
    assert _texts(adapter) == ["Let me check that first."]


def test_outside_a_turn_nothing_changes(adapter):
    asyncio.run(adapter.send(GROUP, NEW_NOTICE))
    assert _texts(adapter) == [NEW_NOTICE]


def test_the_full_preset_still_shows_it_at_once(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_runtime_status_messages_enabled", lambda: True)
    _turn(adapter)
    asyncio.run(adapter.send(GROUP, NEW_NOTICE))
    assert _texts(adapter) == [NEW_NOTICE]
