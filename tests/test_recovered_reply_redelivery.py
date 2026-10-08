"""A final reply the host redelivers after a gateway restart does not show up twice.

Hermes records every final text reply in its delivery ledger
(``gateway/delivery_ledger.py``) and marks the row delivered only when the
adapter's ``send()`` returns success. A send that returned failure — the
ClawChat ack never came back (read-loop stall, reconnect), or the gateway was
killed mid-send — stays ``failed`` / ``attempting``, and the next gateway boot
sends it again with an English prefix ("♻️ Recovered reply — the gateway
restarted during delivery, so this may be a duplicate: …"), up to 24 hours
later. When the original had in fact reached the ClawChat server (only the ack was lost),
the chat showed the old reply a second time, under that prefix.

The plugin now recognises the host's recovered-reply prefixes, drops them, and
sends the reply under the ``message_id`` its first attempt used. The ClawChat server stores
inbox rows per (recipient, message_id) and clients dedupe by message_id, so a
reply that already arrived is not shown again, and one that never arrived is
delivered — without the prefix.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from clawchat_gateway import storage
from clawchat_gateway.adapter import ClawChatAdapter

CHAT = "cnv_direct"
RECOVERED = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
RECONNECTED = (
    "♻️ Recovered reply — the messaging platform reconnected after the original "
    "delivery failed, so this may be a duplicate:\n\n"
)
FLOOD = (
    "♻️ Recovered reply — the messaging platform's rate limit refused the original, so part of "
    "it may already have arrived above:\n\n"
)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reset = getattr(storage, "reset_clawchat_store_for_test", None)
    if callable(reset):
        reset()
    yield tmp_path
    if callable(reset):
        reset()


def _adapter(monkeypatch, *, ack: bool):
    """A fresh adapter = a fresh gateway process sharing the profile's store."""
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a._known_chat_types[CHAT] = "direct"
    a.frames = []

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        a.frames.append(frame)
        return ack

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    return a


def _message_id(frame):
    return frame["payload"]["message_id"]


def _text(frame):
    payload = frame["payload"]
    body = payload.get("message", {}).get("body") or payload.get("message", {})
    fragments = body.get("fragments") or payload.get("fragments") or []
    return "".join(f.get("text", "") for f in fragments if f.get("kind") == "text")


def test_redelivery_after_a_lost_ack_reuses_the_first_message_id(home, monkeypatch):
    first = _adapter(monkeypatch, ack=False)
    result = asyncio.run(first.send(CHAT, "The report is ready."))
    assert result.success is False  # the host ledgers this as failed
    original_id = _message_id(first.frames[-1])

    rebooted = _adapter(monkeypatch, ack=True)
    result = asyncio.run(rebooted.send(CHAT, RECOVERED + "The report is ready."))

    assert result.success is True
    assert len(rebooted.frames) == 1
    assert _message_id(rebooted.frames[0]) == original_id
    assert _text(rebooted.frames[0]) == "The report is ready."


def test_redelivery_of_a_reply_that_was_acked_reuses_its_message_id(home, monkeypatch):
    first = _adapter(monkeypatch, ack=True)
    asyncio.run(first.send(CHAT, "Done, see the summary above."))
    original_id = _message_id(first.frames[-1])

    rebooted = _adapter(monkeypatch, ack=True)
    result = asyncio.run(rebooted.send(CHAT, RECOVERED + "Done, see the summary above."))

    assert result.success is True
    assert [_message_id(f) for f in rebooted.frames] == [original_id]
    assert "Recovered reply" not in _text(rebooted.frames[0])


@pytest.mark.parametrize("marker", [RECOVERED, RECONNECTED, FLOOD])
def test_a_recovered_reply_with_no_earlier_attempt_goes_out_without_the_prefix(home, monkeypatch, marker):
    rebooted = _adapter(monkeypatch, ack=True)
    result = asyncio.run(rebooted.send(CHAT, marker + "Here is what I found."))

    assert result.success is True
    assert len(rebooted.frames) == 1
    assert _text(rebooted.frames[0]) == "Here is what I found."


def test_an_ordinary_reply_with_the_same_text_is_not_treated_as_a_redelivery(home, monkeypatch):
    first = _adapter(monkeypatch, ack=True)
    asyncio.run(first.send(CHAT, "OK"))
    original_id = _message_id(first.frames[-1])

    later = _adapter(monkeypatch, ack=True)
    asyncio.run(later.send(CHAT, "OK"))

    assert _message_id(later.frames[-1]) != original_id
