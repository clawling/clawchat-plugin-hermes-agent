"""Inbound replay dedup must fail closed.

``_on_message`` claims every inbound ``message.send`` / ``message.reply`` in the
plugin ledger before dispatch. The claim is tri-state: ``True`` (first sight),
``False`` (duplicate or recall-tombstoned), ``None`` (store unavailable: no
store, store disabled at init, SQLite error, or the wrapper swallowed an
exception). The bug: only ``False`` was skipped, so ``None`` dispatched the
message — a live redelivery / device replay during a store outage re-ran the
LLM turn. The outbound reply paths already refuse to send on ``None``, so that
turn could not even be delivered.
"""

from __future__ import annotations

import logging

import pytest

from clawchat_gateway.adapter import ClawChatAdapter


class FakeStore:
    def __init__(self, result=None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls = 0

    def claim_message_once(self, **_kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


def frame(message_id: str = "msg_1") -> dict:
    return {
        "event": "message.send",
        "chat_id": "cnv_1",
        "chat_type": "direct",
        "trace_id": "tr_1",
        "sender": {"id": "usr_human", "nick_name": "Ada"},
        "payload": {
            "message_id": message_id,
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {"mentions": []},
            },
        },
    }


@pytest.fixture
def adapter(monkeypatch) -> tuple[ClawChatAdapter, list]:
    a = ClawChatAdapter({})
    dispatched: list = []

    async def fake_handle_inbound(inbound):
        dispatched.append(inbound)

    async def no_forwarded_approval(_inbound):
        return False

    def no_profile_sync(coro):
        coro.close()

    monkeypatch.setattr(a, "_handle_inbound", fake_handle_inbound)
    monkeypatch.setattr(a, "_handle_owner_forwarded_approval", no_forwarded_approval)
    monkeypatch.setattr(a, "_schedule_profile_sync", no_profile_sync)
    monkeypatch.setattr(a, "_resolve_inbound_sender_context", lambda inbound: inbound)
    return a, dispatched


@pytest.mark.asyncio
async def test_first_sight_claim_dispatches(adapter):
    a, dispatched = adapter
    a._store = FakeStore(True)

    await a._on_message(frame())

    assert a._store.calls == 1
    assert len(dispatched) == 1, "a freshly claimed message must reach the agent"


@pytest.mark.asyncio
async def test_duplicate_claim_is_skipped_and_logged_as_duplicate(adapter, caplog):
    a, dispatched = adapter
    a._store = FakeStore(False)

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        await a._on_message(frame())

    assert dispatched == []
    assert "inbound duplicate skipped" in caplog.text
    assert "claim_unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_unavailable_claim_is_skipped_and_warned(adapter, caplog):
    a, dispatched = adapter
    a._store = FakeStore(None)

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        await a._on_message(frame())

    assert dispatched == [], "store unavailable must not re-run the agent turn"
    assert "reason=claim_unavailable" in caplog.text
    assert "inbound duplicate skipped" not in caplog.text
    assert any(
        r.levelno >= logging.WARNING and "claim_unavailable" in r.getMessage()
        for r in caplog.records
    ), "an outage-driven drop must be WARNING, not INFO"


@pytest.mark.asyncio
async def test_raising_claim_is_skipped_and_warned(adapter, caplog):
    a, dispatched = adapter
    a._store = FakeStore(exc=RuntimeError("database is locked"))

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        await a._on_message(frame())

    assert dispatched == []
    assert "reason=claim_unavailable" in caplog.text


@pytest.mark.asyncio
async def test_no_store_at_all_is_skipped_and_warned(adapter, caplog):
    a, dispatched = adapter
    a._store = None

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        await a._on_message(frame())

    assert dispatched == []
    assert "reason=claim_unavailable" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("weird", [0, 1, "true", object()])
async def test_non_bool_claim_result_is_not_treated_as_success(adapter, weird):
    a, dispatched = adapter
    a._store = FakeStore(weird)

    await a._on_message(frame())

    assert dispatched == []
