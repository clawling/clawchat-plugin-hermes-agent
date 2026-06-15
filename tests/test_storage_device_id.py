from __future__ import annotations

import sqlite3

from clawchat_gateway.storage import ClawChatStore


def _store(tmp_path) -> ClawChatStore:
    s = ClawChatStore(tmp_path / "clawchat.sqlite")
    s.initialize()
    return s


def test_device_id_persisted_and_read(tmp_path):
    s = _store(tmp_path)
    s.upsert_activation(
        platform="hermes",
        account_id="default",
        user_id="usr_1",
        conversation_id="conv_1",
        owner_user_id="usr_owner",
        access_token="acc",
        refresh_token="ref",
        device_id="hermes-dev-pinned",
    )
    creds = s.get_activation_credentials(platform="hermes", account_id="default")
    assert creds is not None
    assert creds.device_id == "hermes-dev-pinned"
    assert creds.refresh_token == "ref"
    assert creds.activated_at is not None


def test_update_activation_tokens_keeps_identity(tmp_path):
    s = _store(tmp_path)
    s.upsert_activation(
        platform="hermes",
        account_id="default",
        user_id="usr_1",
        conversation_id="conv_1",
        owner_user_id="usr_owner",
        access_token="acc-old",
        refresh_token="ref-old",
        device_id="hermes-dev-1",
    )
    ok = s.update_activation_tokens(
        platform="hermes",
        account_id="default",
        access_token="acc-new",
        refresh_token="ref-new",
    )
    assert ok is True
    creds = s.get_activation_credentials(platform="hermes", account_id="default")
    assert creds.access_token == "acc-new"
    assert creds.refresh_token == "ref-new"
    # Identity + device id preserved.
    assert creds.user_id == "usr_1"
    assert creds.owner_user_id == "usr_owner"
    assert creds.device_id == "hermes-dev-1"


def test_clear_activation_credentials_keeps_identity(tmp_path):
    s = _store(tmp_path)
    s.upsert_activation(
        platform="hermes",
        account_id="default",
        user_id="usr_1",
        conversation_id="conv_1",
        owner_user_id="usr_owner",
        access_token="acc",
        refresh_token="ref",
        device_id="hermes-dev-1",
    )
    ok = s.clear_activation_credentials(platform="hermes", account_id="default")
    assert ok is True
    # Tokens gone → get_activation_credentials returns None (no access token).
    assert s.get_activation_credentials(platform="hermes", account_id="default") is None
    # But the identity row + device id survive for re-pair.
    conn = sqlite3.connect(s.db_path)
    try:
        row = conn.execute(
            "SELECT user_id, owner_user_id, conversation_id, device_id, access_token, refresh_token "
            "FROM activations WHERE platform='hermes' AND account_id='default'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "usr_1"
    assert row[1] == "usr_owner"
    assert row[2] == "conv_1"
    assert row[3] == "hermes-dev-1"
    assert row[4] is None
    assert row[5] is None


def test_update_activation_tokens_seeds_row_when_absent(tmp_path):
    # §C.2 env-only deployment: no activations row exists yet. The first refresh
    # must SEED the row from the supplied identity rather than returning
    # rowcount==0 (which would brick the agent on its first rotation).
    s = _store(tmp_path)
    assert s.get_activation_credentials(platform="hermes", account_id="default") is None
    ok = s.update_activation_tokens(
        platform="hermes",
        account_id="default",
        access_token="acc-new",
        refresh_token="ref-new",
        device_id="hermes-dev-pinned",
        seed_user_id="usr_1",
        seed_owner_user_id="usr_owner",
        seed_conversation_id="conv_1",
    )
    assert ok is True
    creds = s.get_activation_credentials(platform="hermes", account_id="default")
    assert creds is not None
    assert creds.access_token == "acc-new"
    assert creds.refresh_token == "ref-new"
    assert creds.user_id == "usr_1"
    assert creds.owner_user_id == "usr_owner"
    assert creds.device_id == "hermes-dev-pinned"
    # The seed must NOT mark the row bootstrapped (env row was never bootstrapped).
    conn = sqlite3.connect(s.db_path)
    try:
        row = conn.execute(
            "SELECT bootstrap_sent, conversation_id FROM activations "
            "WHERE platform='hermes' AND account_id='default'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 0
    assert row[1] == "conv_1"


def test_persist_rotated_tokens_seeds_db_when_absent(tmp_path, monkeypatch):
    # §C.2 persist_rotated_tokens (activate.py) must return True for an env-only
    # deployment: .env write succeeds AND the SQLite write seeds the missing row.
    from clawchat_gateway import activate, storage

    s = _store(tmp_path)
    monkeypatch.setattr(storage, "_store", s)
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: s)
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: s)

    env_writes: dict[str, str | None] = {}

    def fake_write_env(values):
        env_writes.update(values)
        from pathlib import Path

        return Path("/tmp/.env")

    monkeypatch.setattr(activate, "_write_env_values", fake_write_env)

    ok = activate.persist_rotated_tokens(
        access_token="acc-new",
        refresh_token="ref-new",
        device_id="hermes-dev-1",
        user_id="usr_1",
        owner_user_id="usr_owner",
        conversation_id="conv_1",
    )
    assert ok is True
    assert env_writes["CLAWCHAT_TOKEN"] == "acc-new"
    assert env_writes["CLAWCHAT_REFRESH_TOKEN"] == "ref-new"
    creds = s.get_activation_credentials(platform="hermes", account_id="default")
    assert creds is not None
    assert creds.access_token == "acc-new"
    assert creds.refresh_token == "ref-new"
    assert creds.user_id == "usr_1"


def test_set_activation_device_id_backfills_only_when_empty(tmp_path):
    # Env-booted / legacy rows have a NULL device_id. The connection backfills it
    # at connect from the token's `did` claim so the durable value is observable
    # in the DB — but it must NEVER clobber an already-set device id.
    s = _store(tmp_path)
    s.upsert_activation(
        platform="hermes",
        account_id="default",
        user_id="usr_1",
        conversation_id="conv_1",
        owner_user_id="usr_owner",
        access_token="acc",
        refresh_token="ref",
    )
    assert s.get_activation_credentials(platform="hermes", account_id="default").device_id is None

    s.set_activation_device_id(
        platform="hermes", account_id="default", device_id="hermes-host-frozen"
    )
    assert (
        s.get_activation_credentials(platform="hermes", account_id="default").device_id
        == "hermes-host-frozen"
    )

    # A second backfill with a different value is a no-op (only-if-empty).
    s.set_activation_device_id(
        platform="hermes", account_id="default", device_id="hermes-host-other"
    )
    assert (
        s.get_activation_credentials(platform="hermes", account_id="default").device_id
        == "hermes-host-frozen"
    )


def test_set_activation_device_id_noop_without_row(tmp_path):
    # No activations row yet (truly unpaired) → nothing to backfill, no error.
    s = _store(tmp_path)
    s.set_activation_device_id(
        platform="hermes", account_id="default", device_id="hermes-host-frozen"
    )
    assert s.get_activation_credentials(platform="hermes", account_id="default") is None


def test_legacy_row_without_device_id_backfills_none(tmp_path):
    # Simulate a legacy activations row written before migration 6 (no device id).
    s = _store(tmp_path)
    s.upsert_activation(
        platform="hermes",
        account_id="default",
        user_id="usr_1",
        conversation_id="conv_1",
        owner_user_id="usr_owner",
        access_token="acc",
        refresh_token="ref",
    )
    creds = s.get_activation_credentials(platform="hermes", account_id="default")
    assert creds.device_id is None  # caller falls back to get_device_id()
