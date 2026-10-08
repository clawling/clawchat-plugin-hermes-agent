"""System receipts must not start a second turn after a gateway restart.

A ``permission_result`` receipt is a ``sender.id == "system"`` frame, so it is
intercepted before the ledger's message-id claim that dedups ordinary inbound
messages. Its only dedup was ``permission_result._seen_request_ids``, an
in-process set: after a restart the server's reliable-inbox replay delivered the
same receipt again and the agent answered it a second time (seen in the
2026-10-08 batch3 e2e: one ``friend.accept approved`` receipt handled at 12:17
and again after the 12:29 restart).

The same holds for the per-event moment-comment note, which a replayed
``notify.signal`` could emit twice across a restart. Both now claim a once-only
row in the plugin ledger (SQLite), which survives restarts.
"""

from __future__ import annotations

import pytest

from clawchat_gateway import permission_result as pr
from clawchat_gateway.adapter import ClawChatAdapter


class LedgerStore:
    """In-memory stand-in for the ledger's once-only claim (survives 'restarts')."""

    def __init__(self) -> None:
        self.claimed: set[tuple] = set()

    def claim_message_once(self, **kwargs):
        key = (kwargs.get("direction"), kwargs.get("kind"), kwargs.get("message_id"))
        if key in self.claimed:
            return False
        self.claimed.add(key)
        return True


class BrokenStore:
    def claim_message_once(self, **_kwargs):
        return None


def receipt_frame(request_id: str = "prq_1") -> dict:
    return {
        "event": "message.send",
        "chat_id": "cnv_owner",
        "chat_type": "direct",
        "trace_id": "tr_1",
        "sender": {"id": "system"},
        "payload": {
            "message_id": f"msg_{request_id}",
            "message": {"fragments": [{"kind": "text", "text": "receipt"}]},
            "metadata": {
                "kind": "permission_result",
                "request_id": request_id,
                "operation": "friend.accept",
                "outcome": "approved",
            },
        },
    }


def new_adapter(monkeypatch, store) -> tuple[ClawChatAdapter, list]:
    a = ClawChatAdapter({})
    a._store = store
    dispatched: list = []

    async def fake_handle_inbound(inbound):
        dispatched.append(inbound)

    monkeypatch.setattr(a, "_handle_inbound", fake_handle_inbound)
    return a, dispatched


def restart() -> None:
    """A process restart loses every in-memory dedup set."""
    pr._seen_request_ids.clear()


@pytest.fixture(autouse=True)
def _fresh_process():
    restart()
    yield
    restart()


@pytest.mark.asyncio
async def test_receipt_replayed_after_restart_starts_no_second_turn(monkeypatch):
    store = LedgerStore()
    a, dispatched = new_adapter(monkeypatch, store)
    await a._on_message(receipt_frame())
    assert len(dispatched) == 1

    restart()
    b, dispatched_after = new_adapter(monkeypatch, store)
    await b._on_message(receipt_frame())

    assert dispatched_after == []


@pytest.mark.asyncio
async def test_a_different_receipt_after_restart_still_runs(monkeypatch):
    store = LedgerStore()
    a, _ = new_adapter(monkeypatch, store)
    await a._on_message(receipt_frame("prq_1"))

    restart()
    b, dispatched = new_adapter(monkeypatch, store)
    await b._on_message(receipt_frame("prq_2"))

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_receipt_still_runs_when_the_ledger_is_unavailable(monkeypatch):
    # A receipt is the agent's only word on a pending request: losing it is
    # worse than a rare duplicate, and the in-process set still collapses live
    # redeliveries.
    a, dispatched = new_adapter(monkeypatch, BrokenStore())
    await a._on_message(receipt_frame())
    await a._on_message(receipt_frame())

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_moment_comment_note_replayed_after_restart_runs_once(monkeypatch):
    store = LedgerStore()

    def make() -> tuple[ClawChatAdapter, list]:
        a, dispatched = new_adapter(monkeypatch, store)
        monkeypatch.setattr(a, "_owner_user_id", lambda: "usr_owner")
        monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: "cnv_owner")
        return a, dispatched

    a, first = make()
    await a._emit_moment_comment_note("mom_1", False, event_id="evt_1")
    assert len(first) == 1

    b, second = make()
    await b._emit_moment_comment_note("mom_1", False, event_id="evt_1")
    assert second == []

    c, third = make()
    await c._emit_moment_comment_note("mom_1", False, event_id="evt_2")
    assert len(third) == 1


@pytest.mark.asyncio
async def test_real_ledger_dedups_a_receipt_across_store_reopen(monkeypatch, tmp_path):
    from clawchat_gateway.storage import ClawChatStore

    db = tmp_path / "clawchat.db"
    a, first = new_adapter(monkeypatch, ClawChatStore(db))
    await a._on_message(receipt_frame())
    assert len(first) == 1

    restart()
    b, second = new_adapter(monkeypatch, ClawChatStore(db))
    await b._on_message(receipt_frame())
    assert second == []
