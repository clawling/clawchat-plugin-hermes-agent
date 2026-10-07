"""The inbound rate-spike warning counts only messages that reach the agent.

The warning ("possible self-echo / interrupt loop") exists to catch the agent
answering its own output. It used to count every inbound frame before the
self-echo drop and the message-id dedupe, so replays and redeliveries in a
busy group kept tripping it, and the agent's own echoes (already dropped, so
harmless) did too. Now a frame is counted only once it has passed both.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from clawchat_gateway.adapter import INBOUND_RATE_WARN_THRESHOLD, ClawChatAdapter

AGENT = "usr_agent"


class Store:
    def __init__(self):
        self.seen: set[str] = set()

    def claim_message_once(self, **kw):
        mid = kw.get("message_id")
        if mid in self.seen:
            return False
        self.seen.add(mid)
        return True


def frame(message_id: str, sender: str = "usr_human") -> dict:
    return {
        "event": "message.send",
        "chat_id": "cnv_1",
        "chat_type": "direct",
        "trace_id": f"tr_{message_id}",
        "sender": {"id": sender, "nick_name": "Ada"},
        "payload": {
            "message_id": message_id,
            "message": {"fragments": [{"kind": "text", "text": "hello"}], "context": {"mentions": []}},
        },
    }


@pytest.fixture
def adapter(monkeypatch):
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT)
    a._store = Store()

    async def fake_handle_inbound(inbound):
        pass

    async def no_forwarded_approval(_inbound):
        return False

    def no_profile_sync(coro):
        coro.close()

    monkeypatch.setattr(a, "_handle_inbound", fake_handle_inbound)
    monkeypatch.setattr(a, "_handle_owner_forwarded_approval", no_forwarded_approval)
    monkeypatch.setattr(a, "_schedule_profile_sync", no_profile_sync)
    monkeypatch.setattr(a, "_resolve_inbound_sender_context", lambda inbound: inbound)
    return a


def _spikes(caplog) -> int:
    return sum("inbound rate spike" in r.getMessage() for r in caplog.records)


async def test_redelivered_duplicates_do_not_count(adapter, caplog):
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.inbound_trace"):
        for _ in range(INBOUND_RATE_WARN_THRESHOLD * 3):
            await adapter._on_message(frame("msg_same"))
    assert _spikes(caplog) == 0


async def test_self_echoes_do_not_count(adapter, caplog):
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.inbound_trace"):
        for i in range(INBOUND_RATE_WARN_THRESHOLD * 3):
            await adapter._on_message(frame(f"msg_echo_{i}", sender=AGENT))
    assert _spikes(caplog) == 0


async def test_distinct_messages_still_warn(adapter, caplog):
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.inbound_trace"):
        for i in range(INBOUND_RATE_WARN_THRESHOLD):
            await adapter._on_message(frame(f"msg_{i}"))
    assert _spikes(caplog) == 1
