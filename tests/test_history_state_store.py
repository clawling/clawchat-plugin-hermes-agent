from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from clawchat_gateway import storage
from clawchat_gateway.rolling_summary import publish_summary, read_summary
from clawchat_gateway.storage import ClawChatStore, MIGRATIONS


@pytest.fixture
def store(tmp_path):
    result = ClawChatStore(tmp_path / "history.sqlite")
    result.initialize()
    return result


def test_history_cas_insert_update_stale_and_scoped_keys(store):
    assert store.get_history_state("a", "chat") is None
    assert not store.cas_history_state("a", "chat", 3, summary_ref="missing")
    assert store.get_history_state("a", "chat") is None
    pending = {"cut_id": "m9", "reason": "budget", "progress": ["m1"]}
    assert store.cas_history_state("a", "chat", 0, summary_ref="first", pending=pending,
                                   inject_next=1, open_history=12, open_context=40)
    first = store.get_history_state("a", "chat")
    assert first["gen"] == 1 and first["pending"] == pending
    assert first["summary_ref"] == "first" and first["inject_next"] == 1
    assert first["open_history"] == 12 and first["open_context"] == 40
    assert first["failures"] == first["retry_after_msgs"] == 0
    assert not store.cas_history_state("a", "chat", 0, summary_ref="stale")
    assert store.cas_history_state("a", "chat", 1, summary_ref="second", summary_through="m9",
                                   pending=None, failures=2, retry_after_msgs=4)
    second = store.get_history_state("a", "chat")
    assert second["gen"] == 2 and second["pending"] is None
    assert second["summary_ref"] == "second" and second["summary_through"] == "m9"
    assert second["failures"] == 2 and second["retry_after_msgs"] == 4
    assert second["open_history"] == 12 and second["updated_at"] >= first["updated_at"]
    assert not store.cas_history_state("a", "chat", 1, boundary="stale")
    assert store.get_history_state("a", "chat") == second
    assert store.cas_history_state("b", "chat", 0)
    assert store.cas_history_state("a", "other-chat", 0)
    assert store.get_history_state("b", "chat")["gen"] == 1


@pytest.mark.parametrize("generation", [0, 1])
def test_two_connections_have_exactly_one_cas_winner(store, generation):
    if generation:
        assert store.cas_history_state("a", "chat", 0)
    other = ClawChatStore(store.db_path)
    other.initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: item[0].cas_history_state(
            "a", "chat", generation, summary_ref=item[1],
        ), [(store, "one"), (other, "two")]))
    assert results.count(True) == 1
    row = store.get_history_state("a", "chat")
    assert row["gen"] == generation + 1
    assert row["summary_ref"] == ("one" if results[0] else "two")


def test_history_rejects_uncontrolled_columns_and_generations(store):
    for fields in ({"gen": 12}, {"updated_at": 0}, {"unknown": "value"}):
        with pytest.raises(ValueError):
            store.cas_history_state("a", "chat", 0, **fields)
    for generation in (-1, True, "1"):
        with pytest.raises(ValueError):
            store.cas_history_state("a", "chat", generation)
    assert store.get_history_state("a", "chat") is None


def test_held_messages_sort_scope_preserve_replays_and_cas_states(store):
    assert store.hold_inbound("a", "chat", "m2", 20, {"text": "second"})
    assert store.hold_inbound("a", "chat", "m1", 10, {"text": "first"})
    assert store.hold_inbound("a", "other", "m3", 15, {"text": "other chat"})
    assert store.hold_inbound("b", "chat", "m1", 1, {"text": "other account"})
    assert [row["message_id"] for row in store.list_held("a")] == ["m1", "m3", "m2"]
    assert [row["message_id"] for row in store.list_held("a", "chat")] == ["m1", "m2"]
    assert not store.mark_held_state("a", "chat", "m1", "dispatching", "dispatched")
    assert store.mark_held_state("a", "chat", "m1", "held", "dispatching")
    assert not store.mark_held_state("a", "chat", "m1", "held", "dispatching")
    assert not store.hold_inbound("a", "chat", "m1", 999, {"text": "replay"})
    row = store.list_held("a", "chat", states=("dispatching",))[0]
    assert row["payload"] == {"text": "first"} and row["seq"] == 10
    assert store.mark_held_state("a", "chat", "m1", "dispatching", "dispatched")
    assert [row["message_id"] for row in store.list_held("a", "chat")] == ["m2"]
    assert store.list_held("a", "chat", states=()) == []
    assert store.list_held("a", "chat", states=("dispatched",))[0]["message_id"] == "m1"
    with pytest.raises(ValueError):
        store.mark_held_state("a", "chat", "m2", "held", "unknown")
    with pytest.raises(ValueError):
        store.list_held("a", states=("unknown",))


def test_held_rows_survive_reopen_and_purge_only_old_dispatched(store, monkeypatch):
    monkeypatch.setattr(storage, "_now_ms", lambda: 1_000_000)
    for message in ("old-done", "old-held", "old-dispatching"):
        store.hold_inbound("a", "chat", message, 1, {"text": message})
    store.mark_held_state("a", "chat", "old-done", "held", "dispatched")
    store.mark_held_state("a", "chat", "old-dispatching", "held", "dispatching")
    monkeypatch.setattr(storage, "_now_ms", lambda: 5_000_000)
    store.hold_inbound("a", "chat", "new-done", 2, {"text": "new"})
    store.mark_held_state("a", "chat", "new-done", "held", "dispatched")
    reopened = ClawChatStore(store.db_path)
    assert len(reopened.list_held("a")) == 2
    assert reopened.purge_dispatched(3600) == 1
    rows = reopened.list_held("a", states=("held", "dispatching", "dispatched"))
    assert {row["message_id"] for row in rows} == {"old-held", "old-dispatching", "new-done"}


def test_migration_upgrades_existing_database_and_is_idempotent(tmp_path):
    path = tmp_path / "existing.sqlite"
    with sqlite3.connect(path) as conn:
        for version, name, sql in MIGRATIONS[:-1]:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, 1))
        conn.execute("INSERT INTO recalled_messages VALUES ('a', 'existing', 1)")
    ClawChatStore(path).initialize()
    ClawChatStore(path).initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version,name FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone() == (17, "history_state")
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 17
        assert conn.execute("SELECT message_id FROM recalled_messages").fetchone()[0] == "existing"
        for table, primary in (("clawchat_history_state", ["account_id", "conv_key"]),
                               ("clawchat_held_inbound", ["account_id", "conv_key", "message_id"])):
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            assert [item[1] for item in sorted(info, key=lambda item: item[5]) if item[5]] == primary
        conn.execute("INSERT INTO clawchat_history_state(account_id,conv_key,updated_at) VALUES ('a','chat',1)")
        assert conn.execute("SELECT gen FROM clawchat_history_state").fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO clawchat_held_inbound VALUES ('a','chat','m',1,'{}','unknown',1)")


def test_unavailable_storage_never_reports_success(store):
    store._disabled = True
    for operation in (
        lambda: store.get_history_state("a", "chat"),
        lambda: store.cas_history_state("a", "chat", 0),
        lambda: store.hold_inbound("a", "chat", "m", 1, {}),
        lambda: store.list_held("a"),
        lambda: store.mark_held_state("a", "chat", "m", "held", "dispatching"),
        lambda: store.purge_dispatched(3600),
    ):
        with pytest.raises(RuntimeError, match="unavailable"):
            operation()


def test_failed_commit_rolls_back_and_propagates(store, monkeypatch):
    store.hold_inbound("a", "chat", "existing", 1, {})

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("simulated commit failure")

    monkeypatch.setattr(store, "_tool_grants_connection", lambda: sqlite3.connect(
        store.db_path, factory=FailingCommit,
    ))
    for operation in (
        lambda: store.cas_history_state("a", "chat", 0, summary_ref="uncommitted"),
        lambda: store.hold_inbound("a", "chat", "new", 2, {}),
        lambda: store.mark_held_state("a", "chat", "existing", "held", "dispatched"),
    ):
        with pytest.raises(sqlite3.OperationalError, match="commit failure"):
            operation()
    assert store.get_history_state("a", "chat") is None
    rows = store.list_held("a")
    assert len(rows) == 1 and rows[0]["message_id"] == "existing" and rows[0]["state"] == "held"


def test_losing_summary_cas_keeps_authoritative_version(store, tmp_path):
    directory = tmp_path / "summaries"
    first = publish_summary(directory, "first", through="m1", boundary=None, as_of="2026-10-30T00:00:00Z")
    assert store.cas_history_state("a", "chat", 0, summary_ref=first, summary_through="m1")
    second = publish_summary(directory, "second", through="m2", boundary=None, as_of="2026-10-31T00:00:00Z")
    assert store.cas_history_state("a", "chat", 1, host_session="replacement")
    assert not store.cas_history_state("a", "chat", 1, summary_ref=second, summary_through="m2")
    row = store.get_history_state("a", "chat")
    assert row["summary_ref"] == first and row["summary_through"] == "m1"
    assert read_summary(directory, row["summary_ref"])[1] == "first"
    assert read_summary(directory, second)[1] == "second"
