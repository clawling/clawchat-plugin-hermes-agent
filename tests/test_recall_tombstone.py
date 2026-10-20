"""The recall tombstone — what makes ``message.recall`` order-independent.

``docs/client-integration.md`` §9.8 requires more than a delete:

    Write a permanent local tombstone keyed by ``target_message_id`` and
    consult it on every ingest path.

A delete alone loses two races, and this repo's own code documented the second
one as an accepted limitation:

1. **The recall arrives before the message.** Live delivery and inbox replay can
   both carry a recall, and a replay can carry the original and its recall
   back-to-back. The purge then runs against a row that has not been written
   yet, and the ingest that follows puts it in for good.
2. **A Kafka redelivery of the original.** The server can redeliver a
   ``message.send`` after the recall was handled, resurrecting the row.

The tombstone closes both by making arrival order irrelevant: the ingest path
refuses a tombstoned ``message_id`` no matter when it shows up.

Everything below runs against a real ``ClawChatStore`` on a real SQLite file.
"""

from __future__ import annotations

import pytest

from clawchat_gateway.storage import ClawChatStore

ACCOUNT = "default"


@pytest.fixture
def store(tmp_path) -> ClawChatStore:
    s = ClawChatStore(tmp_path / "clawchat.db")
    s.initialize()
    assert not s._disabled, "test store failed to initialize; the rest is meaningless"
    return s


def claim(store: ClawChatStore, *, message_id: str, text: str, chat_id: str = "cnv_1",
          account_id: str = ACCOUNT) -> bool | None:
    """Ingest one inbound group message the way the persist path does."""
    return store.claim_message_once(
        platform="clawchat",
        account_id=account_id,
        kind="message",
        direction="inbound",
        event_type="message.send",
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        raw={"payload": {"message_id": message_id}},
    )


def test_recall_before_the_message_keeps_it_out_of_the_ledger(store):
    """Race 1: the purge runs first, and the message must still never land."""
    assert store.recall_message(account_id=ACCOUNT, message_id="msg_regret") == 0

    assert claim(store, message_id="msg_regret", text="the withdrawn text") is False
    rows = store.list_recent_group_messages(ACCOUNT, "cnv_1", 10)
    assert [r["text"] for r in rows] == []


def test_recall_still_purges_a_row_that_already_landed(store):
    assert claim(store, message_id="msg_regret", text="the withdrawn text") is True

    assert store.recall_message(account_id=ACCOUNT, message_id="msg_regret") == 1
    assert store.list_recent_group_messages(ACCOUNT, "cnv_1", 10) == []


def test_tombstone_is_permanent_across_reopen(tmp_path):
    """Race 2: a redelivery after a restart is still refused."""
    db = tmp_path / "clawchat.db"
    first = ClawChatStore(db)
    first.initialize()
    first.recall_message(account_id=ACCOUNT, message_id="msg_regret")

    second = ClawChatStore(db)
    second.initialize()
    assert claim(second, message_id="msg_regret", text="the withdrawn text") is False


def test_repeated_recall_is_idempotent(store):
    assert claim(store, message_id="m1", text="x") is True
    assert store.recall_message(account_id=ACCOUNT, message_id="m1") == 1
    assert store.recall_message(account_id=ACCOUNT, message_id="m1") == 0


def test_tombstone_is_scoped_to_account_and_message_id(store):
    store.recall_message(account_id=ACCOUNT, message_id="m1")

    # same id, a different account -> untouched
    assert claim(store, message_id="m1", text="other account", account_id="other") is True
    # a different id, same account -> untouched
    assert claim(store, message_id="m2", text="unrelated") is True


def test_empty_message_id_tombstones_nothing(store):
    """An empty id would otherwise tombstone every row the store never keyed."""
    assert store.recall_message(account_id=ACCOUNT, message_id="") == 0
    assert claim(store, message_id="m1", text="unrelated") is True


def test_is_message_recalled_reports_the_tombstone(store):
    assert store.is_message_recalled(account_id=ACCOUNT, message_id="m1") is False
    store.recall_message(account_id=ACCOUNT, message_id="m1")
    assert store.is_message_recalled(account_id=ACCOUNT, message_id="m1") is True
    assert store.is_message_recalled(account_id=ACCOUNT, message_id="") is False
