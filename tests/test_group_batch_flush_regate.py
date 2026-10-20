"""A queued group batch must re-check mute / reply-mode when it flushes.

The mute and reply-mode gates used to run only at enqueue. A non-mention batch
waits ``batch_delay_seconds`` (default 10 s) before it runs; if the owner muted
the agent, or switched the group to mention-only, during that window the stale
enqueue-time decision still let the batch reach the model. The OpenClaw plugin
re-gates in its coalescer ``dispatch`` callback; these tests pin the same
semantics for Hermes:

- now muted -> drop the whole batch;
- now ``reply_mode == "mention"`` -> keep the batch only if at least one message
  in it addressed the agent (own id or ``"all"``);
- the flush waits for an in-flight settings refresh (bounded) first.
"""

from __future__ import annotations

import asyncio

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.api_client import ClawChatApiClient
from clawchat_gateway.group_settings import GroupSettings
from clawchat_gateway.inbound import InboundMessage

CHAT = "cnv_group_1"


@pytest.fixture
def adapter(monkeypatch) -> tuple[ClawChatAdapter, list]:
    dispatched: list = []

    async def fake_handle_inbound(self, inbound):
        dispatched.append(inbound)

    # Patch on the CLASS before construction: the coalescer's dispatch callable
    # is bound in __init__, so an instance-level patch would be too late.
    monkeypatch.setattr(ClawChatAdapter, "_handle_inbound", fake_handle_inbound)
    a = ClawChatAdapter({})
    return a, dispatched


def msg(text: str, *, mentioned: bool = False) -> InboundMessage:
    return InboundMessage(
        chat_id=CHAT,
        chat_type="group",
        sender_id="usr_human",
        sender_name="Ada",
        text=text,
        raw_message={"payload": {"message_id": f"msg_{text}"}},
        was_mentioned=mentioned,
        mentioned_user_ids=["all"] if mentioned else [],
    )


def set_settings(a: ClawChatAdapter, *, muted: bool, reply_mode: str, seq: int) -> None:
    a._group_settings_cache.apply_fetched(
        [
            GroupSettings(
                conversation_id=CHAT,
                muted=muted,
                reply_mode=reply_mode,
                batch_delay_seconds=10,
                version=seq,
            )
        ],
        seq,
    )


def enqueue(a: ClawChatAdapter, *messages: InboundMessage) -> None:
    for m in messages:
        a._group_message_coalescer.enqueue(m, idle_seconds_override=3600.0)


@pytest.mark.asyncio
async def test_batch_dispatches_when_settings_unchanged(adapter):
    a, dispatched = adapter
    set_settings(a, muted=False, reply_mode="all", seq=1)
    enqueue(a, msg("one"), msg("two"))

    await a._group_message_coalescer.flush_now(CHAT)

    assert len(dispatched) == 1
    assert "one" in dispatched[0].text and "two" in dispatched[0].text


@pytest.mark.asyncio
async def test_batch_dropped_when_muted_while_queued(adapter):
    a, dispatched = adapter
    set_settings(a, muted=False, reply_mode="all", seq=1)
    enqueue(a, msg("one"), msg("two", mentioned=True))
    set_settings(a, muted=True, reply_mode="all", seq=2)

    await a._group_message_coalescer.flush_now(CHAT)

    assert dispatched == []


@pytest.mark.asyncio
async def test_unmentioned_batch_dropped_when_switched_to_mention_mode(adapter):
    a, dispatched = adapter
    set_settings(a, muted=False, reply_mode="all", seq=1)
    enqueue(a, msg("one"), msg("two"))
    set_settings(a, muted=False, reply_mode="mention", seq=2)

    await a._group_message_coalescer.flush_now(CHAT)

    assert dispatched == []


@pytest.mark.asyncio
async def test_batch_with_a_mention_survives_switch_to_mention_mode(adapter):
    a, dispatched = adapter
    set_settings(a, muted=False, reply_mode="all", seq=1)
    enqueue(a, msg("one"), msg("two", mentioned=True))
    set_settings(a, muted=False, reply_mode="mention", seq=2)

    await a._group_message_coalescer.flush_now(CHAT)

    assert len(dispatched) == 1
    assert dispatched[0].was_mentioned is True


@pytest.mark.asyncio
async def test_flush_waits_for_in_flight_settings_refresh(adapter):
    a, dispatched = adapter
    set_settings(a, muted=False, reply_mode="all", seq=1)
    enqueue(a, msg("one"))
    # An agent.config.changed refresh is in flight: the gate is closed.
    a._group_settings_ready.clear()

    flush = asyncio.create_task(a._group_message_coalescer.flush_now(CHAT))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not flush.done()

    # The refresh lands with the agent now muted, then opens the gate.
    set_settings(a, muted=True, reply_mode="all", seq=2)
    a._group_settings_ready.set()
    await flush

    assert dispatched == []


@pytest.mark.asyncio
async def test_row_missing_batch_delay_uses_server_default():
    client = ClawChatApiClient.__new__(ClawChatApiClient)

    async def fake_call_json(method, path, **_kwargs):
        return {
            "settings": [
                {"conversation_id": CHAT, "muted": False, "reply_mode": "all", "version": 1}
            ]
        }

    client._call_json = fake_call_json
    result = await client.get_my_group_settings()

    assert result.authoritative is True
    assert result.rows[0].batch_delay_seconds == 10
