from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest

from clawchat_gateway.storage import ClawChatStore, MIGRATIONS
from clawchat_gateway.tool_grants import BASELINE_CATEGORY, PENDING_TTL_MS, resolve_turn_grants

NOW = 1791892800000
PAYLOAD = dict(grantees=[dict(id="usr_u", name="张三"), dict(id="usr_v", name="李四")],
               tools=["feishu_doc_read", "tool:terminal"], mode="direct", label="文档与命令")


@pytest.fixture
def store(tmp_path):
    result = ClawChatStore(tmp_path / "grants.sqlite")
    result.initialize()
    return result


def pending(store, code="000123", payload=None, account="a", now=NOW):
    store.create_pending_grant(account_id=account, code=code, payload=PAYLOAD if payload is None else payload, now=now)


def confirm(store, code="000123", account="a", now=NOW + 1):
    return store.confirm_pending_grant(account_id=account, code=code, now=now)


def consumed(store, code="000123"):
    with sqlite3.connect(store.db_path) as conn:
        return conn.execute("SELECT consumed_at FROM tool_grant_pending WHERE account_id = 'a' AND code = ?", (code,)).fetchone()[0]


def test_migration_13_upgrades_12_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        for version, name, sql in MIGRATIONS[:12]:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, NOW))
    ClawChatStore(path).initialize()
    ClawChatStore(path).initialize()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version, name FROM schema_migrations ORDER BY version DESC LIMIT 1").fetchone() == (14, "tool_grants_v2")
        for table, columns, primary in (
            ("tool_grants", ["account_id", "grantee_user_id", "grantee_name", "what_kind", "what", "mode", "label", "expires_at", "created_at"], ["account_id", "grantee_user_id", "what_kind", "what"]),
            ("tool_grant_pending", ["account_id", "code", "payload_json", "created_at", "expires_at", "consumed_at", "purpose", "payload_version"], ["account_id", "code"]),
        ):
            info = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
            assert [item[1] for item in info] == columns
            assert [item[1] for item in sorted(info, key=lambda item: item[5]) if item[5]] == primary
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 14


def test_pending_only_then_confirm_in_one_explicit_transaction(store, monkeypatch):
    pending(store)
    assert store.list_grants(account_id="a", now=NOW) == []
    with sqlite3.connect(store.db_path) as conn:
        payload_json, created_at, expires_at, used = conn.execute("SELECT payload_json, created_at, expires_at, consumed_at FROM tool_grant_pending").fetchone()
    assert json.loads(payload_json) == PAYLOAD | dict(tools=["tool:feishu_doc_read", "tool:terminal"])
    assert (created_at, expires_at, used) == (NOW, NOW + PENDING_TTL_MS, None)
    statements = []
    original = store._tool_grants_connection

    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "_tool_grants_connection", connect)
    rows = confirm(store)
    assert len(rows) == 4
    assert {r["mode"] for r in rows if r["what"] == "terminal"} == {"direct"}
    assert {r["mode"] for r in rows if r["what"] == "feishu_doc_read"} == {"direct"}
    assert all(r["created_at"] == NOW + 1 and r["label"] == PAYLOAD["label"] for r in rows)
    assert statements[0] == "BEGIN IMMEDIATE" and statements[-1] == "COMMIT"
    assert sum(s.startswith("BEGIN") for s in statements) == 1
    assert consumed(store) == NOW + 1
    assert store.list_grants(account_id="a", now=NOW + 1) == sorted(rows, key=lambda r: (r["grantee_user_id"], r["what"]))
    assert resolve_turn_grants(["owner", "usr_u", "usr_v"], "owner", rows, NOW + 1) == {"feishu_doc_read": "direct", "terminal": "direct"}


@pytest.mark.parametrize("code,account,when", [("999999", "a", NOW + 1), ("000123", "b", NOW + 1),
    ("000123", "a", NOW + PENDING_TTL_MS), ("000123", "a", NOW + PENDING_TTL_MS + 1)])
def test_invalid_or_expired_code_has_no_effect(store, code, account, when):
    pending(store)
    assert confirm(store, code, account, when) is None
    assert store.list_grants(account_id="a", now=NOW) == []
    assert consumed(store) is None


def test_replay_and_code_collision_cannot_revive(store):
    pending(store)
    with pytest.raises(sqlite3.IntegrityError):
        pending(store, payload=PAYLOAD | dict(tools=["other"]))
    assert len(confirm(store)) == 4
    assert confirm(store) is None
    with pytest.raises(sqlite3.IntegrityError):
        pending(store)
    assert consumed(store) == NOW + 1


def test_grantee_expiry_checked_again_at_confirmation(store):
    pending(store, payload=PAYLOAD | dict(expires_at=NOW + 10))
    assert confirm(store, now=NOW + 10) is None
    assert store.list_grants(account_id="a", now=NOW) == []
    assert consumed(store) is None


@pytest.mark.parametrize("bad_json", ["{bad", "null", json.dumps(PAYLOAD | dict(mode="invalid")),
    json.dumps(PAYLOAD | dict(tools=["clawchat_tool_grant"])), json.dumps(PAYLOAD | dict(expires_at="later"))])
def test_corrupted_pending_fails_closed(store, bad_json):
    pending(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tool_grant_pending SET payload_json = ?", (bad_json,))
    assert confirm(store) is None
    assert store.list_grants(account_id="a", now=NOW) == []
    assert consumed(store) is None


def test_upsert_failure_rolls_back_changes_and_leaves_code_unused(store, monkeypatch):
    pending(store)
    confirm(store)
    before = store.list_grants(account_id="a", now=NOW + 1)
    pending(store, "000124", PAYLOAD | dict(mode="ask", label="replacement", tools=["feishu_doc_read", "new_tool"]))
    original = store._upsert_tool_grant
    count = 0

    def fail_after_first(conn, row):
        nonlocal count
        count += 1
        if count == 2:
            raise sqlite3.OperationalError("injected upsert failure")
        original(conn, row)

    with monkeypatch.context() as patch:
        patch.setattr(store, "_upsert_tool_grant", fail_after_first)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            confirm(store, "000124", now=NOW + 2)
    assert count == 2
    assert store.list_grants(account_id="a", now=NOW + 2) == before
    assert consumed(store, "000124") is None
    assert len(confirm(store, "000124", now=NOW + 3)) == 4


def test_consumed_write_failure_also_rolls_back_upserts(store):
    pending(store)
    with sqlite3.connect(store.db_path) as conn:
        conn.executescript("""CREATE TRIGGER fail_consumed BEFORE UPDATE ON tool_grant_pending
            BEGIN SELECT RAISE(ABORT, 'injected consumed failure'); END;""")
    with pytest.raises(sqlite3.IntegrityError, match="consumed failure"):
        confirm(store)
    assert consumed(store) is None
    assert store.list_grants(account_id="a", now=NOW) == []


def test_two_connections_cannot_both_consume(store):
    pending(store)
    other = ClawChatStore(store.db_path)
    other.initialize()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, [store, other]))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_grants(account_id="a", now=NOW + 1)) == 4


def test_every_sensitive_exact_tool_remains_direct_at_write_and_resolve(store):
    pending(store, payload=PAYLOAD | dict(tools=["tool:" + name for name in BASELINE_CATEGORY]))
    rows = confirm(store)
    assert all(row["mode"] == "direct" for row in rows)
    # Protect against legacy or manually edited direct rows too.
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE tool_grants SET mode = 'direct'")
    rows = store.load_grants_for_users(account_id="a", user_ids=["usr_u"], now=NOW + 1)
    assert resolve_turn_grants(["usr_u"], "owner", rows, NOW + 1) == dict.fromkeys(BASELINE_CATEGORY, "direct")


def test_upsert_updates_all_fields(store):
    pending(store)
    confirm(store)
    payload = dict(grantees=[dict(id="usr_u", name="新名字")], tools=["feishu_doc_read"], mode="ask", expires_at=NOW + 99)
    pending(store, "000124", payload)
    rows = confirm(store, "000124", now=NOW + 2)
    assert rows == [dict(account_id="a", grantee_user_id="usr_u", grantee_name="新名字", what_kind="tool", what="feishu_doc_read",
                         mode="ask", label=None, expires_at=NOW + 99, created_at=NOW + 2)]
    assert rows[0] in store.list_grants(account_id="a", now=NOW + 2)


def test_revoke_counts_and_account_user_filters(store):
    for account in ("a", "b"):
        pending(store, account=account)
        confirm(store, account=account)
    assert store.revoke_grants(account_id="a", user_id="usr_u", tools=[]) == 0
    assert store.revoke_grants(account_id="a", user_id="usr_u", tools=["tool:terminal", "tool:terminal", "missing"]) == 1
    assert store.revoke_grants(account_id="a", user_id="usr_u", tools=["tool:terminal"]) == 0
    assert store.revoke_grants(account_id="a", user_id="usr_u") == 1
    assert store.revoke_grants(account_id="a", user_id="usr_v") == 2
    assert store.revoke_grants(account_id="a", user_id="usr_missing") == 0
    assert len(store.list_grants(account_id="b", now=NOW + 1)) == 4


def test_list_and_load_only_unexpired_account_rows(store):
    pending(store)
    confirm(store)
    pending(store, "000124", PAYLOAD | dict(tools=["expires"], expires_at=NOW + 10))
    confirm(store, "000124")
    pending(store, account="b")
    confirm(store, account="b")
    assert len(store.list_grants(account_id="a", now=NOW + 9)) == 6
    assert len(store.list_grants(account_id="a", now=NOW + 10)) == 4
    assert len(store.list_grants(account_id="a", now=NOW + 11)) == 4
    assert len(store.load_grants_for_users(account_id="a", user_ids=["usr_u", "usr_u"], now=NOW + 9)) == 3
    assert len(store.load_grants_for_users(account_id="a", user_ids=["usr_u"], now=NOW + 10)) == 2
    assert store.load_grants_for_users(account_id="a", user_ids=[], now=NOW) == []
    assert store.load_grants_for_users(account_id="a", user_ids=["' OR 1=1 --"], now=NOW) == []


def test_unavailable_storage_errors_propagate(tmp_path):
    store = ClawChatStore(tmp_path / "unavailable.sqlite")
    store._disabled = True
    for call in (lambda: pending(store), lambda: confirm(store),
                 lambda: store.revoke_grants(account_id="a", user_id="usr_u"),
                 lambda: store.list_grants(account_id="a", now=NOW),
                 lambda: store.load_grants_for_users(account_id="a", user_ids=["usr_u"], now=NOW)):
        with pytest.raises(RuntimeError, match="unavailable"):
            call()


@pytest.mark.parametrize("kwargs", [dict(code="123"), dict(code="１２３４５６"), dict(account_id=" "),
    dict(now=float("nan")), dict(payload=PAYLOAD | dict(grantees=[dict(id="usr_x", name="")])),
    dict(payload=PAYLOAD | dict(mode="invalid")), dict(payload=PAYLOAD | dict(tools=["clawchat_tool_grant"]))])
def test_invalid_pending_rejected(store, kwargs):
    with pytest.raises(ValueError):
        store.create_pending_grant(**(dict(account_id="a", code="000123", payload=PAYLOAD, now=NOW) | kwargs))


@pytest.mark.parametrize("now", [float("nan"), float("inf"), True, "later"])
def test_invalid_read_and_confirm_time(store, now):
    with pytest.raises(ValueError):
        confirm(store, now=now)
    with pytest.raises(ValueError):
        store.list_grants(account_id="a", now=now)
