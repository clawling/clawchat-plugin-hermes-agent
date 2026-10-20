"""``message.recall`` — the Hermes-side ledger purge.

Until this file, the recall path had **zero** tests on the Hermes side while the
sibling OpenClaw adapter had eleven. The reason was structural, not neglect:
``adapter.py`` cannot be imported without the Hermes host package, so nothing in
this repository could reach ``_handle_message_recall`` at all. ``conftest.py``
removes that blocker; read its docstring for what the host double does and does
not prove.

Everything below runs against a **real** ``ClawChatStore`` on a real SQLite file
in ``tmp_path``. No store mock: a recall that "deletes" a row is only meaningful
if the row is really gone, and the row being gone is only meaningful if the text
can no longer be read back out. So the headline test asserts on
``list_recent_group_messages`` — the query that actually feeds a recalled
message back into a prompt — rather than on a call count.

Two of the tests here pin bugs that were fixed without a regression test:

* ``f6fca72`` — the reply-preview cache was left holding the recalled text, so a
  later reply could quote a withdrawn message back into the chat where every
  human sees it.
* ``596f3ca`` — a *failed* purge was logged as ``recall purged``, because
  ``ClawChatStore._write`` swallows write errors and returns ``None``. The only
  operator-visible signal said the opposite of what happened.

Both were verified to fail against the pre-fix code before being committed here.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.storage import ClawChatStore

ACCOUNT = "default"  # the literal the persist path writes under; see _handle_message_recall


@pytest.fixture
def store(tmp_path) -> ClawChatStore:
    s = ClawChatStore(tmp_path / "clawchat.db")
    s.initialize()
    assert not s._disabled, "test store failed to initialize; the rest is meaningless"
    return s


@pytest.fixture
def adapter(store) -> ClawChatAdapter:
    a = ClawChatAdapter({})
    a._store = store
    return a


def seed_message(store: ClawChatStore, *, chat_id: str, message_id: str, text: str) -> None:
    """Write one ordinary inbound group message, the way the persist path does."""
    rowid = store.insert_message(
        platform="clawchat",
        account_id=ACCOUNT,
        kind="message",
        direction="inbound",
        event_type="message.send",
        trace_id=None,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        raw={"payload": {"message_id": message_id}},
    )
    assert rowid is not None, "seeding failed; the store is not writable"


def recall_frame(*, chat_id: str, target: str, slot_id: str | None = None) -> dict:
    """The server's recall downlink, shaped as the hub sends it.

    ``payload.message_id`` is the recall's OWN slot id (``rcl:<target>``), not
    the id to delete. Getting these two backwards deletes nothing and reads
    exactly like working code — which is why it is asserted on below.
    """
    return {
        "event": "message.recall",
        "chat_id": chat_id,
        "chat_type": "group",
        "sender": {"id": "usr_the_human_who_recalled"},
        "payload": {
            "message_id": slot_id if slot_id is not None else f"rcl:{target}",
            "target_message_id": target,
        },
    }


# --------------------------------------------------------------------------
# The purge itself
# --------------------------------------------------------------------------


def test_recall_stops_the_message_being_read_back_out_of_group_history(adapter, store):
    """The user-visible point of the whole feature.

    ``list_recent_group_messages`` is what feeds prior group context into a
    prompt. If a recalled message survives there, the agent can read a withdrawn
    message aloud, which is the exact harm recall exists to prevent.
    """
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="the withdrawn text")
    before = store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)
    assert any(r["text"] == "the withdrawn text" for r in before)

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    after = store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)
    assert not any(r["text"] == "the withdrawn text" for r in after), (
        "the recalled text is still readable out of group history"
    )


def test_recall_deletes_the_target_not_the_recall_slot_id(adapter, store):
    """``payload.message_id`` is ``rcl:<target>``; deleting by it erases nothing.

    Seeds a decoy row keyed by the slot id so a wrong-field implementation has
    something to delete and therefore still looks like it worked.
    """
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")
    seed_message(store, chat_id="cnv_1", message_id="rcl:msg_regret", text="decoy")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    texts = {r["text"] for r in store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)}
    assert "withdrawn" not in texts, "deleted by the wrong field: the target survived"
    assert "decoy" in texts, "deleted the recall slot row instead of the target"


def test_recall_leaves_every_other_message_alone(adapter, store):
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")
    seed_message(store, chat_id="cnv_1", message_id="msg_keep", text="innocent bystander")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    texts = {r["text"] for r in store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)}
    assert texts == {"innocent bystander"}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"message_id": "rcl:x"}, id="target-absent"),
        pytest.param({"target_message_id": ""}, id="target-empty"),
        pytest.param({"target_message_id": None}, id="target-null"),
        pytest.param({"target_message_id": 12345}, id="target-not-a-string"),
    ],
)
def test_a_recall_with_no_usable_target_deletes_nothing(adapter, store, payload):
    """An empty id would match every row whose message_id is ''. Refuse, don't guess."""
    seed_message(store, chat_id="cnv_1", message_id="msg_keep", text="innocent bystander")

    adapter._handle_message_recall(
        {"event": "message.recall", "chat_id": "cnv_1", "payload": payload}
    )

    texts = {r["text"] for r in store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)}
    assert texts == {"innocent bystander"}


# --------------------------------------------------------------------------
# Regression: f6fca72 — the reply-preview cache
# --------------------------------------------------------------------------
#
# _reply_preview_by_message_id backs OUTBOUND message.send/message.reply frames
# as an inline quote every human in the chat sees — not just a model turn. The
# group coalescer's batch delay can exceed the 2-minute recall window, so the
# quote can be emitted well after the recall landed.


def prime_preview(adapter, *, chat_id: str, message_id: str, text: str) -> None:
    adapter._reply_preview_by_message_id[message_id] = {
        "id": message_id,
        "nick_name": "Ada",
        "fragments": [{"kind": "text", "text": text}],
    }
    adapter._reply_preview_order.append(message_id)
    adapter._last_inbound_message_id_by_chat[chat_id] = message_id


def test_recall_evicts_the_reply_preview_so_the_text_cannot_be_quoted_back(adapter, store):
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "msg_regret" not in adapter._reply_preview_by_message_id, (
        "a recalled message is still quotable in an outbound frame"
    )


def test_recall_drops_the_id_from_the_preview_fifo_so_the_cap_stays_honest(adapter, store):
    """The deque is what enforces the 512-entry bound. A stale id there would
    later evict a live entry instead of itself."""
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "msg_regret" not in adapter._reply_preview_order


def test_recall_clears_the_last_inbound_pointer_when_it_names_the_recalled_message(adapter):
    """That pointer is the default target for an un-targeted reaction: left
    behind, a same-turn reaction lands on a message that no longer exists."""
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "cnv_1" not in adapter._last_inbound_message_id_by_chat


def test_recall_leaves_the_last_inbound_pointer_alone_when_it_names_something_else(adapter):
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")
    adapter._last_inbound_message_id_by_chat["cnv_1"] = "msg_newer"

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert adapter._last_inbound_message_id_by_chat["cnv_1"] == "msg_newer", (
        "clobbered the pointer to a message that was never recalled"
    )


def test_the_preview_is_evicted_even_with_no_store_at_all(adapter):
    """Eviction must not be downstream of the ledger delete: an unconfigured
    store is not a reason to keep quoting a withdrawn message."""
    adapter._store = None
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "msg_regret" not in adapter._reply_preview_by_message_id


def test_the_preview_is_evicted_even_when_the_ledger_delete_raises(adapter):
    calls = []

    class ExplodingStore:
        def recall_message(self, *, account_id, message_id):
            calls.append((account_id, message_id))
            raise RuntimeError("disk on fire")

    adapter._store = ExplodingStore()
    prime_preview(adapter, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert calls == [(ACCOUNT, "msg_regret")]
    assert "msg_regret" not in adapter._reply_preview_by_message_id


# --------------------------------------------------------------------------
# Regression: 596f3ca — never report a failed purge as a successful one
# --------------------------------------------------------------------------
#
# A successful recall writes a tombstone with the purge; a failed transaction
# leaves neither. There is no retry, so the failure log is the operator's signal.
# Saying "purged" when nothing was purged closes the investigation prematurely.


def corrupt(store: ClawChatStore) -> None:
    """Make the next write really fail, the way SQLITE_BUSY / a bad disk would.

    ``_write`` catches the sqlite error and returns ``None``; the caller's own
    ``except`` never fires. That asymmetry is the entire bug.
    """
    store.db_path.write_bytes(b"this is not a database")


def test_a_failed_purge_is_never_logged_as_purged(adapter, store, caplog):
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")
    corrupt(store)

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    text = caplog.text
    assert "recall purged" not in text, "a failed purge reported itself as a success"
    assert "recall purge unavailable" in text
    assert any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a purge that did not happen must not be INFO"
    )


def test_an_already_absent_row_is_a_no_op_not_a_purge(adapter, store, caplog):
    """rows=0 is a normal replay outcome, not a failure and not a purge."""
    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_never_stored"))

    assert "recall no-op" in caplog.text
    assert "reason=already_absent" in caplog.text
    assert "recall purged" not in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), (
        "a normal replay must not warn"
    )


def test_a_real_purge_is_logged_as_purged_with_its_row_count(adapter, store, caplog):
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "recall purged" in caplog.text
    assert "rows=1" in caplog.text


def test_having_no_store_is_logged_rather_than_silently_ignored(adapter, caplog):
    adapter._store = None

    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        adapter._handle_message_recall(recall_frame(chat_id="cnv_1", target="msg_regret"))

    assert "reason=no_store" in caplog.text
    assert "recall purged" not in caplog.text


def test_a_missing_target_is_logged_rather_than_silently_ignored(adapter, caplog):
    with caplog.at_level(logging.DEBUG, logger="clawchat_gateway.adapter"):
        adapter._handle_message_recall({"event": "message.recall", "chat_id": "cnv_1", "payload": {}})

    assert "reason=missing_target_message_id" in caplog.text


# --------------------------------------------------------------------------
# Characterization: a KNOWN divergence from the OpenClaw adapter
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_recall_the_agent_itself_sent_never_reaches_the_purge(adapter, store):
    """Pins a divergence from OpenClaw. **This is not an endorsement.**

    Hermes routes ``message.recall`` through ``_on_message``, which drops any
    frame whose ``sender.id`` equals this agent's own user id *before* the
    recall branch is reached. OpenClaw's adapter bypasses the inbound filter
    chain for recall entirely, so it purges in this case and Hermes does not.

    The consequence: when the recalled message was sent BY this agent, Hermes
    keeps the withdrawn text in its ledger forever — there is no tombstone and
    no retry, so nothing ever clears it.

    If you are here because you changed that behaviour deliberately, this test
    is the one to update, along with the OpenClaw side. If you are here because
    a test broke unexpectedly, you have probably just changed which frames the
    self-echo filter eats.
    """
    adapter._clawchat_config = dataclasses.replace(
        adapter._clawchat_config, user_id="agt_self"
    )
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    frame = recall_frame(chat_id="cnv_1", target="msg_regret")
    frame["sender"] = {"id": "agt_self"}
    await adapter._on_message(frame)

    texts = {r["text"] for r in store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)}
    assert texts == {"withdrawn"}, (
        "the self-echo filter no longer eats self-sent recalls — if that was "
        "intentional, this divergence from OpenClaw is now closed and the note "
        "should be retired"
    )


@pytest.mark.asyncio
async def test_a_recall_from_another_user_does_reach_the_purge(adapter, store):
    """The control for the test above: routing works for the ordinary case."""
    adapter._clawchat_config = dataclasses.replace(
        adapter._clawchat_config, user_id="agt_self"
    )
    seed_message(store, chat_id="cnv_1", message_id="msg_regret", text="withdrawn")

    await adapter._on_message(recall_frame(chat_id="cnv_1", target="msg_regret"))

    texts = {r["text"] for r in store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)}
    assert texts == set()
