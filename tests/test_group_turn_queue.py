"""Group messages that arrive while the group's turn is running wait for it.

With one shared session per group, a batch that flushes while the previous
group turn is still running would reach Hermes' busy-session path, whose
behaviour depends on the host version and on ``display.busy_input_mode``
(interrupt the running turn; queue but merge texts under the first queued
event's metadata; or, on old hosts, overwrite the single queued slot). The
plugin therefore holds the batch itself: while the group's session is busy the
coalescer keeps collecting, and the moment the session is free everything that
arrived is flushed as one next batch — in order, with its own metadata.

Commands (``/stop``, ``/new``) are not batched and still reach Hermes at once.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
from clawchat_gateway.inbound import InboundMessage

CHAT = "cnv_g"


def msg(text: str, chat: str = CHAT) -> InboundMessage:
    return InboundMessage(
        chat_id=chat,
        chat_type="group",
        sender_id="usr_ada",
        sender_name="Ada",
        text=text,
        raw_message={"payload": {"message_id": f"id_{text}"}},
    )


@pytest.mark.asyncio
async def test_busy_chat_is_held_then_flushed_as_one_batch():
    busy = {CHAT: True}
    dispatched: list[InboundMessage] = []

    async def dispatch(message):
        dispatched.append(message)

    coalescer = GroupMessageCoalescer(
        idle_seconds=3600,
        max_wait_seconds=3600,
        dispatch=dispatch,
        is_busy=lambda chat_id: busy.get(chat_id, False),
        busy_poll_seconds=0.01,
    )
    coalescer.enqueue(msg("one"))
    await coalescer.flush_now(CHAT)  # e.g. a mention while the turn runs
    assert dispatched == []
    coalescer.enqueue(msg("two"))
    await asyncio.sleep(0.05)
    assert dispatched == []
    busy[CHAT] = False
    for _ in range(50):
        if dispatched:
            break
        await asyncio.sleep(0.01)
    assert len(dispatched) == 1
    text = dispatched[0].text
    assert text.index("one") < text.index("two")
    await coalescer.cancel()


@pytest.mark.asyncio
async def test_other_chats_are_not_held():
    dispatched: list[str] = []

    async def dispatch(message):
        dispatched.append(message.chat_id)

    coalescer = GroupMessageCoalescer(
        idle_seconds=3600,
        max_wait_seconds=3600,
        dispatch=dispatch,
        is_busy=lambda chat_id: chat_id == CHAT,
        busy_poll_seconds=0.01,
    )
    coalescer.enqueue(msg("x", chat="cnv_other"))
    await coalescer.flush_now("cnv_other")
    assert dispatched == ["cnv_other"]
    await coalescer.cancel()


@pytest.mark.asyncio
async def test_cancel_drops_held_batches_and_stops_waiting():
    async def dispatch(_message):
        raise AssertionError("must not dispatch after cancel")

    coalescer = GroupMessageCoalescer(
        idle_seconds=3600,
        max_wait_seconds=3600,
        dispatch=dispatch,
        is_busy=lambda _chat: True,
        busy_poll_seconds=0.01,
    )
    coalescer.enqueue(msg("one"))
    await coalescer.flush_now(CHAT)
    await coalescer.cancel()
    await asyncio.sleep(0.03)


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return ClawChatAdapter({})


def test_group_is_busy_while_its_host_session_runs(adapter):
    adapter._active_sessions = {f"agent:main:clawchat:group:{CHAT}:__group_shared__": object()}
    assert adapter._group_turn_in_flight(CHAT) is True
    assert adapter._group_turn_in_flight("cnv_other") is False
    adapter._active_sessions = {}
    assert adapter._group_turn_in_flight(CHAT) is False


def test_group_is_busy_while_the_plugin_is_dispatching(adapter):
    adapter._group_dispatching.add(CHAT)
    assert adapter._group_turn_in_flight(CHAT) is True


def test_legacy_per_speaker_groups_are_not_held(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, group_sessions_per_user=True)
    adapter._active_sessions = {f"agent:main:clawchat:group:{CHAT}:usr_ada": object()}
    assert adapter._group_turn_in_flight(CHAT) is False


@pytest.mark.asyncio
async def test_dispatch_marks_the_group_busy_until_handed_to_the_host(adapter, monkeypatch):
    seen: list[bool] = []

    async def fake_handle_inbound(inbound):
        seen.append(adapter._group_turn_in_flight(inbound.chat_id))

    monkeypatch.setattr(adapter, "_handle_inbound", fake_handle_inbound)
    adapter._group_settings_ready.set()
    await adapter._dispatch_group_batch(msg("hello"))
    assert seen == [True]
    assert adapter._group_turn_in_flight(CHAT) is False
