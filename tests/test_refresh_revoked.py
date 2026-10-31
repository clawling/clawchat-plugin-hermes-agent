"""Durable revoked-refresh guard and permanent-failure regression coverage."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from clawchat_gateway import activate, api_client, config, connection, storage, token_refresh
from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError, RefreshResult
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.storage import ClawChatStore

NOW = 2_000_000_000
REJECTED = "rejected-refresh-test-credential"
NEW_REFRESH = "new-activation-refresh-test-credential"


def _jwt(exp=NOW + 86400, iat=NOW):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "iat": iat}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _cfg(refresh=REJECTED, *, near=False):
    return ClawChatConfig(
        websocket_url="wss://example.invalid/ws", base_url="https://example.invalid",
        token=_jwt(NOW + 60 if near else NOW + 86400), refresh_token=refresh,
        user_id="test-agent", owner_user_id="test-owner",
    )


def _seed(store, cfg):
    store.upsert_activation(
        platform="hermes", account_id="default", user_id=cfg.user_id,
        owner_user_id=cfg.owner_user_id, conversation_id="test-conversation",
        access_token=cfg.token, refresh_token=cfg.refresh_token, activated_at=NOW * 1000,
        device_id="hermes-test-device",
    )


class _Response:
    def __init__(self, body, status=200):
        self.body, self.status = body, status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class _Socket:
    def __init__(self):
        self.closes = 0

    async def close(self):
        self.closes += 1


class _PollReached(Exception):
    pass


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(activate, "hermes_home", lambda: tmp_path)
    store = ClawChatStore(tmp_path / "clawchat.sqlite")
    store.initialize()
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(config, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(config, "_read_hermes_env_value", lambda _name: "")
    monkeypatch.setattr(token_refresh, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic))
    monkeypatch.setattr(storage, "_now_ms", lambda: NOW * 1000)
    monkeypatch.setattr(activate, "_write_env_values", lambda _values: str(tmp_path / ".env"))
    return store


def _conn(monkeypatch, cfg):
    conn = ClawChatConnection(cfg, on_message=lambda *_args: None, proactive_jitter=lambda: 0.0)
    monkeypatch.setattr(conn, "_refresh_device_id", lambda: "hermes-test-device")
    conn._refresh_manager = conn._build_refresh_manager()
    conn._refresh_manager._max_transient_retries = 0
    return conn


def _http(monkeypatch, body=b'{"code":10003}', *, status=200, http_error=False):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(json.loads(request.data))
        if http_error:
            raise HTTPError(request.full_url, status, "test response", {}, io.BytesIO(body))
        return _Response(body, status)

    monkeypatch.setattr(api_client, "urlopen", urlopen)
    return calls


async def _stop_at_poll(monkeypatch, conn):
    async def poll(delay):
        assert delay == connection.ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS == 2.0
        raise _PollReached

    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with pytest.raises(_PollReached):
        await conn._supervisor()
    assert conn._state == ConnectionState.AUTH_FAILED
    assert not conn._stopping
    assert not conn.config.token and not conn.config.refresh_token


@pytest.mark.parametrize("source", ["sqlite", "dotenv", "legacy-extra", "direct-config"])
async def test_access_token_only_without_marker_connects(stores, tmp_path, monkeypatch, source):
    cfg = _cfg("", near=True)
    expected_token = cfg.token
    if source == "sqlite":
        _seed(stores, cfg)
        cfg = replace(cfg, token="", user_id="", owner_user_id="")
    elif source == "dotenv":
        (tmp_path / ".env").write_text(f"CLAWCHAT_TOKEN={cfg.token}\nCLAWCHAT_REFRESH_TOKEN=\n")
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
            "user_id": "test-agent", "owner_user_id": "test-owner",
        }))
    elif source == "legacy-extra":
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
            "token": cfg.token, "user_id": "test-agent", "owner_user_id": "test-owner",
        }))
    calls = _http(monkeypatch)
    conn = _conn(monkeypatch, cfg)
    connected = []

    async def connect():
        assert conn.config.token == expected_token
        assert conn._state == ConnectionState.CONNECTING and not conn._auth_failed
        connected.append(conn.config)
        assert (await conn.reactive_refresh()).status == "skipped"
        assert conn._state == ConnectionState.CONNECTING and not conn._auth_failed
        conn._stopping = True

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    await conn._supervisor()
    assert len(connected) == 1 and calls == []


@pytest.mark.parametrize("refresh", ["", REJECTED])
async def test_marker_read_failure_preserves_credentials_and_connects(stores, monkeypatch, caplog, refresh):
    def fail_read(**_kwargs):
        raise RuntimeError("test marker store unavailable")

    monkeypatch.setattr(stores, "get_revoked_refresh_fingerprint", fail_read)
    cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
        "token": _jwt(), "refresh_token": refresh,
        "user_id": "test-agent", "owner_user_id": "test-owner",
    }))
    assert cfg.token == _jwt() and cfg.refresh_token == refresh
    conn = _conn(monkeypatch, cfg)
    calls = _http(monkeypatch)
    connected = []

    async def connect():
        assert conn.config == cfg
        assert conn._state == ConnectionState.CONNECTING and not conn._auth_failed
        connected.append(True)
        conn._stopping = True

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    await conn._supervisor()
    assert connected == [True] and calls == []
    assert "revoked-refresh guard read failed" in caplog.text


@pytest.mark.parametrize("read_fails", [False, True])
async def test_fresh_install_wait_preserves_original_state(stores, monkeypatch, caplog, read_fails):
    if read_fails:
        def fail_read(**_kwargs):
            raise RuntimeError("test marker store unavailable")

        monkeypatch.setattr(stores, "get_revoked_refresh_fingerprint", fail_read)
    cfg = replace(_cfg(""), token="", user_id="", owner_user_id="")
    conn = _conn(monkeypatch, cfg)
    original_state = conn._state
    calls = _http(monkeypatch)

    async def poll(delay):
        assert delay == 2.0
        assert conn._state == original_state
        raise _PollReached

    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        with pytest.raises(_PollReached):
            await conn._supervisor()
    assert conn._state == original_state == ConnectionState.DISCONNECTED
    assert not conn._auth_failed and not conn._stopping and calls == []
    assert "activation_wait" in caplog.text and "auth_failed" not in caplog.text


@pytest.mark.parametrize("marker_source", ["sqlite", "memory", "both"])
@pytest.mark.parametrize("refresh,usable", [(None, False), ("", False), (" ", False), (REJECTED, False), (NEW_REFRESH, True)])
async def test_marker_rejects_only_empty_or_matching_refresh(stores, monkeypatch, marker_source, refresh, usable):
    conn = _conn(monkeypatch, _cfg())
    if marker_source in ("sqlite", "both"):
        stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    if marker_source in ("memory", "both"):
        conn._rejected_refresh_fingerprint = hashlib.sha256(REJECTED.encode()).hexdigest()
    if marker_source == "memory":
        def fail_read(**_kwargs):
            raise RuntimeError("test marker store unavailable")

        monkeypatch.setattr(stores, "get_revoked_refresh_fingerprint", fail_read)
    assert conn._refresh_credentials_usable(refresh) is usable


@pytest.mark.parametrize("marker_source", ["none", "sqlite", "memory"])
async def test_marker_clear_writes_only_with_a_marker_or_latch(stores, monkeypatch, marker_source):
    conn = _conn(monkeypatch, _cfg(NEW_REFRESH))
    if marker_source == "sqlite":
        stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    elif marker_source == "memory":
        conn._rejected_refresh_fingerprint = hashlib.sha256(REJECTED.encode()).hexdigest()
    clears = []
    original_clear = stores.clear_revoked_refresh_token

    def clear(**kwargs):
        clears.append(kwargs)
        return original_clear(**kwargs)

    monkeypatch.setattr(stores, "clear_revoked_refresh_token", clear)
    conn._clear_refresh_rejection()
    conn._clear_refresh_rejection()
    assert len(clears) == (0 if marker_source == "none" else 1)
    assert conn._rejected_refresh_fingerprint is None


@pytest.mark.parametrize("marker_exists", [False, True])
def test_legacy_extra_fallback_after_env_tombstone_requires_marker_to_reject(stores, tmp_path, marker_exists):
    (tmp_path / ".env").write_text("CLAWCHAT_TOKEN=\nCLAWCHAT_REFRESH_TOKEN=\n")
    if marker_exists:
        stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
        "token": _jwt(), "refresh_token": REJECTED,
    }))
    assert cfg.token == ("" if marker_exists else _jwt())
    assert cfg.refresh_token == ("" if marker_exists else REJECTED)


async def test_prior_permanent_failure_keeps_auth_failed_while_waiting(stores, monkeypatch):
    cfg = replace(_cfg(""), token="", user_id="", owner_user_id="")
    conn = _conn(monkeypatch, cfg)
    conn._auth_failed = True
    await _stop_at_poll(monkeypatch, conn)


@pytest.mark.parametrize("http_error", [False, True])
@pytest.mark.parametrize("status", [500, 429, 404])
async def test_non_200_code_400_keeps_credentials_and_writes_no_marker(stores, monkeypatch, status, http_error):
    cfg = _cfg()
    _seed(stores, cfg)
    calls = _http(monkeypatch, b'{"code":400}', status=status, http_error=http_error)
    conn = _conn(monkeypatch, cfg)
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    clears = []

    def clear(**kwargs):
        clears.append(kwargs)
        return activate.CredentialClearResult(True, True)

    monkeypatch.setattr(activate, "clear_persisted_credentials", clear)
    outcome = await conn.reactive_refresh()
    assert outcome.status == "transient"
    assert conn.config == cfg and not conn._auth_failed
    assert conn._state == ConnectionState.READY and socket.closes == 0
    assert len(calls) == 1 and clears == []
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    assert conn._rejected_refresh_fingerprint is None and conn._rejected_activation_token is None
    credentials = stores.get_activation_credentials(platform="hermes", account_id="default")
    assert credentials.access_token == cfg.token and credentials.refresh_token == cfg.refresh_token


@pytest.mark.parametrize("status,code", [(401, 10003), (200, 400)])
async def test_permanent_http_refresh_cases_still_logout(stores, monkeypatch, status, code):
    cfg = _cfg()
    _seed(stores, cfg)
    calls = _http(monkeypatch, json.dumps({"code": code}).encode(), status=status, http_error=status != 200)
    conn = _conn(monkeypatch, cfg)
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    assert (await conn.reactive_refresh()).status == "permanent"
    assert conn._state == ConnectionState.AUTH_FAILED and socket.closes >= 1
    assert not conn.config.token and not conn.config.refresh_token and len(calls) == 1
    assert stores.get_activation_credentials(platform="hermes", account_id="default") is None
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") == hashlib.sha256(REJECTED.encode()).hexdigest()


@pytest.mark.parametrize("status,code,permanent", [
    (500, 400, False), (429, 400, False), (404, 400, False),
    (401, 10003, True), (200, 400, True), (None, 400, True),
    (401, "10003", False),
])
def test_permanent_classifier_respects_http_status(status, code, permanent):
    error = ClawChatApiError("validation", "test response", status=status, code=code)
    assert api_client.is_permanent_refresh_error(error) is permanent


async def test_access_only_hello_rejection_waits_for_a_different_access_token(stores, monkeypatch):
    cfg = _cfg("")
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    calls = _http(monkeypatch)
    connected = []
    polls = []
    new_cfg = replace(cfg, token=_jwt(NOW + 2 * 86400, NOW + 1))

    async def connect():
        connected.append(conn.config.token)
        if len(connected) == 1:
            assert conn._using_activation_db_credentials
            conn._state = ConnectionState.HANDSHAKING
            conn._ws = _Socket()
            conn._hello_wait = asyncio.get_running_loop().create_future()
            conn._pending_connect_id = "test-trace"
            await conn._dispatch_inbound({
                "version": "2", "event": "hello-fail", "trace_id": "test-trace",
                "payload": {"reason": "authentication failed"},
            })
            assert conn._state == ConnectionState.AUTH_FAILED and not conn._stopping
            assert conn._rejected_activation_token == cfg.token
            assert conn._hello_wait.result() is False
        else:
            assert conn.config.token == new_cfg.token
            assert conn._rejected_activation_token is None
            assert polls == [2.0, 2.0, 2.0]
            conn._stopping = True

    async def poll(delay):
        polls.append(delay)
        assert len(connected) == 1 and conn._state == ConnectionState.AUTH_FAILED
        assert not conn.config.token and conn._rejected_activation_token == cfg.token
        assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
        assert conn._load_startup_activation_credentials() is False
        assert conn._rejected_activation_token == cfg.token
        if len(polls) == 3:
            _seed(stores, new_cfg)
        elif len(polls) > 3:
            pytest.fail("new access token was not adopted")

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    await conn._supervisor()
    assert connected == [cfg.token, new_cfg.token] and calls == []


@pytest.mark.parametrize("loader", ["startup", "poll"])
@pytest.mark.parametrize("different_access,different_refresh,adopted", [
    (False, True, False), (True, False, False), (True, True, True),
])
async def test_activation_adoption_requires_both_latches_to_pass(
    stores, monkeypatch, loader, different_access, different_refresh, adopted,
):
    original = _cfg()
    cfg = replace(original, token="", refresh_token="", user_id="", owner_user_id="")
    conn = _conn(monkeypatch, cfg)
    conn._rejected_activation_token = original.token
    fingerprint = hashlib.sha256(REJECTED.encode()).hexdigest()
    conn._rejected_refresh_fingerprint = fingerprint
    stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    candidate = replace(
        original,
        token=_jwt(NOW + 2 * 86400, NOW + 1) if different_access else original.token,
        refresh_token=NEW_REFRESH if different_refresh else REJECTED,
    )
    _seed(stores, candidate)
    if loader == "startup":
        assert conn._load_startup_activation_credentials() is adopted
    elif adopted:
        assert await conn._wait_for_activation_credentials() is True
    else:
        async def poll(_delay):
            raise _PollReached

        monkeypatch.setattr(connection.asyncio, "sleep", poll)
        with pytest.raises(_PollReached):
            await conn._wait_for_activation_credentials()
    if adopted:
        assert conn.config.token == candidate.token and conn.config.refresh_token == candidate.refresh_token
        assert conn._rejected_activation_token is None and conn._rejected_refresh_fingerprint is None
        assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    else:
        assert conn.config == cfg and conn._rejected_activation_token == original.token
        assert conn._rejected_refresh_fingerprint == fingerprint
        assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") == fingerprint


async def test_activation_watch_preserves_access_latch_until_adoption(stores, monkeypatch):
    cfg = _cfg("")
    conn = _conn(monkeypatch, cfg)
    conn._state = ConnectionState.READY
    conn._ws = _Socket()
    conn._rejected_activation_token = cfg.token
    _seed(stores, replace(cfg, owner_user_id="changed-test-owner"))

    async def poll(_delay):
        return None

    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    await conn._watch_activation_credentials()
    assert conn._rejected_activation_token == cfg.token
    assert conn._load_startup_activation_credentials() is False


@pytest.mark.parametrize("different_access", [False, True])
async def test_refresh_success_clears_access_latch_only_for_a_different_token(stores, monkeypatch, different_access):
    cfg = _cfg(NEW_REFRESH)
    conn = _conn(monkeypatch, cfg)
    conn._rejected_activation_token = cfg.token
    access = _jwt(NOW + 2 * 86400, NOW + 1) if different_access else cfg.token

    class Manager:
        async def refresh(self, **_kwargs):
            return token_refresh.RefreshOutcome("success", access_token=access, refresh_token="test-refresh")

    conn._refresh_manager = Manager()
    assert (await conn.reactive_refresh()).status == "success"
    assert conn._rejected_activation_token == (None if different_access else cfg.token)


async def test_marker_precedes_both_failed_clears_and_contains_no_plaintext(stores, monkeypatch, caplog):
    cfg = _cfg()
    _seed(stores, cfg)
    env_path = stores.db_path.parent / ".env"
    env_text = f"CLAWCHAT_TOKEN={cfg.token}\nCLAWCHAT_REFRESH_TOKEN={cfg.refresh_token}\n"
    env_path.write_text(env_text, encoding="utf-8")
    calls = _http(monkeypatch)
    seen = []

    def fail_env(values):
        assert values == {"CLAWCHAT_TOKEN": "", "CLAWCHAT_REFRESH_TOKEN": ""}
        seen.append(("env", stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default")))
        raise OSError("test clear failure")

    def fail_db(*, platform, account_id, expected_access_token, expected_refresh_token):
        assert (platform, account_id) == ("hermes", "default")
        assert (expected_access_token, expected_refresh_token) == (cfg.token, cfg.refresh_token)
        seen.append(("sqlite", stores.get_revoked_refresh_fingerprint(platform=platform, account_id=account_id)))
        return False

    monkeypatch.setattr(activate, "_write_env_values", fail_env)
    monkeypatch.setattr(stores, "clear_activation_credentials", fail_db)
    conn = _conn(monkeypatch, cfg)
    # Bootstrap credentials authorize both failed clears after the .env pair
    # matches. An adopted SQLite pair instead stops if its conditional clear fails.
    assert not conn._using_activation_db_credentials
    outcome = await conn.reactive_refresh()
    assert outcome.status == "permanent"
    fingerprint = hashlib.sha256(REJECTED.encode()).hexdigest()
    assert seen == [("env", fingerprint), ("sqlite", fingerprint)]
    assert env_path.read_text(encoding="utf-8") == env_text
    persisted = stores.get_activation_credentials(platform="hermes", account_id="default")
    assert (persisted.access_token, persisted.refresh_token) == (cfg.token, cfg.refresh_token)
    assert not conn.config.token and not conn.config.refresh_token
    with sqlite3.connect(stores.db_path) as db:
        row = db.execute("SELECT fingerprint, rejected_at FROM revoked_refresh_tokens").fetchone()
    assert row == (fingerprint, NOW * 1000)
    assert REJECTED not in repr(row)
    assert REJECTED not in caplog.text
    assert "env_cleared=False sqlite_cleared=False" in caplog.text
    assert len(calls) == 1
    assert (await conn.reactive_refresh()).status == "permanent"
    assert len(calls) == 1


@pytest.mark.parametrize("source", ["sqlite", "dotenv", "legacy-extra", "direct-config"])
async def test_restart_guard_suppresses_all_credential_sources(stores, tmp_path, monkeypatch, source):
    cfg = _cfg(near=True)
    stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    # A new store and connection instance represent a process restart.
    reopened = ClawChatStore(stores.db_path)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: reopened)
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: reopened)
    if source == "sqlite":
        _seed(reopened, cfg)
        cfg = replace(cfg, token="", refresh_token="")
    elif source == "dotenv":
        (tmp_path / ".env").write_text(f"CLAWCHAT_TOKEN={cfg.token}\nCLAWCHAT_REFRESH_TOKEN={REJECTED}\n")
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
            "user_id": "test-agent", "owner_user_id": "test-owner",
        }))
        assert not cfg.token and not cfg.refresh_token
    elif source == "legacy-extra":
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={
            "token": cfg.token, "refresh_token": REJECTED,
            "user_id": "test-agent", "owner_user_id": "test-owner",
        }))
        assert not cfg.token and not cfg.refresh_token
    calls = _http(monkeypatch)
    conn = _conn(monkeypatch, cfg)
    await _stop_at_poll(monkeypatch, conn)
    assert calls == []
    assert reopened.get_revoked_refresh_fingerprint(platform="hermes", account_id="default")


@pytest.mark.parametrize("source", ["sqlite", "dotenv", "legacy-extra", "direct-config"])
async def test_empty_refresh_credentials_with_marker_wait_without_http(stores, tmp_path, monkeypatch, source):
    stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    cfg = _cfg("")
    if source == "sqlite":
        _seed(stores, cfg)
    elif source == "dotenv":
        (tmp_path / ".env").write_text(f"CLAWCHAT_TOKEN={cfg.token}\nCLAWCHAT_REFRESH_TOKEN=\n")
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={"token": "legacy-access"}))
    elif source == "legacy-extra":
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={"token": cfg.token}))
    calls = _http(monkeypatch)
    await _stop_at_poll(monkeypatch, _conn(monkeypatch, cfg))
    assert calls == []


@pytest.mark.parametrize("path", ["startup", "proactive", "rest", "hello-fail"])
async def test_permanent_failure_closes_socket_and_waits_after_exactly_one_refresh(stores, monkeypatch, path):
    cfg = _cfg(near=True)
    _seed(stores, cfg)
    calls = _http(monkeypatch)
    conn = _conn(monkeypatch, cfg)
    socket = conn._ws = _Socket()
    conn._state = ConnectionState.READY
    if path == "startup":
        await _stop_at_poll(monkeypatch, conn)
    elif path == "proactive":
        await conn._maybe_proactive_refresh()
    elif path == "rest":
        assert (await conn.reactive_refresh()).status == "permanent"
    else:
        conn._state = ConnectionState.HANDSHAKING
        conn._hello_wait = asyncio.get_running_loop().create_future()
        conn._pending_connect_id = "test-trace"
        await conn._dispatch_inbound({
            "version": "2", "event": "hello-fail", "trace_id": "test-trace",
            "payload": {"reason": "authentication failed"},
        })
        assert conn._hello_wait.result() is False
    assert socket.closes >= 1
    assert conn._state == ConnectionState.AUTH_FAILED
    assert conn._auth_failed and not conn._stopping
    assert not conn.config.token and not conn.config.refresh_token
    await conn.reactive_refresh()
    assert len(calls) == 1
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default")


async def test_new_activation_is_adopted_marker_cleared_refresh_then_connect(stores, monkeypatch):
    stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    _seed(stores, _cfg(near=True))
    conn = _conn(monkeypatch, replace(_cfg(), token="", refresh_token=""))
    polls = []

    async def activate_on_poll(delay):
        polls.append(delay)
        _seed(stores, _cfg(NEW_REFRESH, near=True))

    monkeypatch.setattr(connection.asyncio, "sleep", activate_on_poll)
    rotated = _jwt()
    calls = _http(monkeypatch, json.dumps({
        "code": 0, "data": {"access_token": rotated, "refresh_token": "rotated-test-refresh"},
    }).encode())
    connected = []

    async def connect():
        connected.append(conn.config)
        conn._stopping = True

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    await conn._supervisor()
    assert polls == [2.0]
    assert calls == [{"refresh_token": NEW_REFRESH}]
    assert len(connected) == 1 and connected[0].token == rotated
    assert connected[0].refresh_token == "rotated-test-refresh"
    assert not conn._auth_failed
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    assert stores.get_activation_credentials(platform="hermes", account_id="default").access_token == rotated


@pytest.mark.parametrize("http_error", [True, False])
@pytest.mark.parametrize("status,body,permanent", [
    (401, b'{"code":10003}', True), (500, b'{"code":10003}', True),
    (500, b'{"code":400}', False), (429, b'{"code":400}', False),
    (404, b'{"code":400}', False), (200, b'{"code":400}', True),
    (502, b'<html>gateway failure</html>', False),
    (401, b'{"code":"10003"}', False), (500, b'{"code":"400"}', False),
    (500, b'{"code":10003.0}', True), (500, b'not-json', False),
    (500, b'{"code":0,"data":{"access_token":"a","refresh_token":"r"}}', False),
])
async def test_refresh_business_classification(stores, monkeypatch, http_error, status, body, permanent):
    calls = _http(monkeypatch, body, status=status, http_error=http_error)
    client = ClawChatApiClient(base_url="https://example.invalid")
    with pytest.raises(ClawChatApiError) as caught:
        await client.auth_refresh(refresh_token=REJECTED, device_id="hermes-test-device")
    assert api_client.is_permanent_refresh_error(caught.value) is permanent
    assert caught.value.status == status
    if status != 200 and not permanent:
        assert caught.value.kind == "transport" and caught.value.code is None
    elif status == 200 and caught.value.code == 400:
        assert caught.value.kind == "validation"
    assert len(calls) == 1


async def test_proactive_refresh_waits_for_expiry_window(stores, monkeypatch):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    calls = _http(monkeypatch, json.dumps({
        "code": 0, "data": {"access_token": _jwt(NOW + 2 * 86400), "refresh_token": NEW_REFRESH},
    }).encode())
    await conn._maybe_proactive_refresh()
    monkeypatch.setattr(token_refresh.time, "time", lambda: NOW + 86400 - 7201)
    await conn._maybe_proactive_refresh()
    assert calls == []
    monkeypatch.setattr(token_refresh.time, "time", lambda: NOW + 86400 - 7200)
    await conn._maybe_proactive_refresh()
    await conn._maybe_proactive_refresh()
    assert len(calls) == 1
    assert token_refresh.proactive_margin_seconds(NOW + 1200, NOW) == 1800
    assert token_refresh.proactive_margin_seconds(NOW + 14400, NOW) == 3600
    assert token_refresh.proactive_margin_seconds(NOW + 86400, NOW) == 7200
    assert connection.ACTIVATION_CREDENTIAL_POLL_INTERVAL_SECONDS == 2.0
    assert cfg.reconnect_initial_delay_ms == 500 and cfg.reconnect_max_delay_ms == 15000


@pytest.mark.parametrize("jitter", [-1, 1])
async def test_network_retry_backoff_is_capped_at_30_seconds(stores, monkeypatch, jitter):
    assert api_client.REFRESH_RETRY_BASE_SECONDS == 1.0
    assert api_client.REFRESH_RETRY_BACKOFF_CAP_SECONDS == 30.0

    def extreme(low, high):
        assert low == -high
        return high if jitter == 1 else low

    monkeypatch.setattr(api_client.random, "uniform", extreme)
    delays = []

    async def sleep(delay):
        delays.append(delay)

    class Client:
        calls = 0

        async def auth_refresh(self, **_kwargs):
            self.calls += 1
            if self.calls <= 9:
                raise ClawChatApiError("transport", "test refused", connect_failed=True)
            return RefreshResult("test-access", "test-refresh")

    client = Client()
    await api_client.auth_refresh_with_retry(
        client, refresh_token=REJECTED, device_id="hermes-test-device", sleep=sleep,
    )
    assert delays == [min(30.0, min(30.0, 2 ** n) * (1 + jitter * .25)) for n in range(9)]
    assert max(delays) <= 30.0


@pytest.mark.parametrize("env_fail,db_result", [(False, True), (True, True), ("false", True), (False, False), (False, "raise"), (True, False)])
def test_clear_persisted_credentials_reports_each_store(stores, monkeypatch, caplog, env_fail, db_result):
    seen = []

    def env(values):
        seen.append("env")
        assert values == {"CLAWCHAT_TOKEN": "", "CLAWCHAT_REFRESH_TOKEN": ""}
        if env_fail == "false":
            return False
        if env_fail:
            raise OSError("test env failure")
        return stores.db_path.parent / ".env"

    def db(**_kwargs):
        seen.append("sqlite")
        if db_result == "raise":
            raise OSError("test database failure")
        return db_result

    monkeypatch.setattr(activate, "_write_env_values", env)
    monkeypatch.setattr(stores, "clear_activation_credentials", db)
    result = activate.clear_persisted_credentials()
    assert result.env_cleared is (not env_fail)
    assert result.sqlite_cleared is (db_result is True)
    assert seen == ["env", "sqlite"]
    if env_fail or db_result is not True:
        assert any(record.levelname == "WARNING" for record in caplog.records)


async def test_socket_teardown_preserves_auth_failed(stores, monkeypatch):
    cfg = _cfg()
    _seed(stores, cfg)
    calls = _http(monkeypatch)
    conn = _conn(monkeypatch, cfg)
    socket = _Socket()

    async def connect(*_args, **_kwargs):
        return socket

    async def read_loop(_socket):
        await conn._dispatch_inbound({
            "version": "2", "event": "hello-fail", "trace_id": "test-trace",
            "payload": {"reason": "authentication failed"},
        })

    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(conn, "_read_loop", read_loop)
    assert await conn._run_one_connection() is False
    assert conn._state == ConnectionState.AUTH_FAILED and conn._auth_failed
    assert not conn._stopping and conn._ws is None
    assert socket.closes >= 1 and len(calls) == 1
    await _stop_at_poll(monkeypatch, conn)
    assert len(calls) == 1


@pytest.mark.parametrize("source", ["sqlite", "dotenv", "legacy-extra"])
async def test_fresh_startup_credentials_clear_marker_and_connect(stores, tmp_path, monkeypatch, source):
    stores.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    cfg = _cfg(NEW_REFRESH, near=True)
    if source == "sqlite":
        _seed(stores, cfg)
        cfg = replace(cfg, token="", refresh_token="")
    else:
        extra = {"user_id": "test-agent", "owner_user_id": "test-owner"}
        if source == "dotenv":
            (tmp_path / ".env").write_text(f"CLAWCHAT_TOKEN={cfg.token}\nCLAWCHAT_REFRESH_TOKEN={NEW_REFRESH}\n")
        else:
            extra.update(token=cfg.token, refresh_token=NEW_REFRESH)
        cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra))
    calls = _http(monkeypatch, json.dumps({
        "code": 0, "data": {"access_token": _jwt(), "refresh_token": "rotated-test-refresh"},
    }).encode())
    conn = _conn(monkeypatch, cfg)
    connected = []

    async def connect():
        connected.append(conn.config.refresh_token)
        conn._stopping = True

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    await conn._supervisor()
    assert calls == [{"refresh_token": NEW_REFRESH}]
    assert connected == ["rotated-test-refresh"]
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None


def test_existing_database_migrates_without_losing_activation(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as db:
        for version, name, sql in storage.MIGRATIONS[:-1]:
            db.executescript(sql)
            db.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, NOW * 1000))
        db.execute(
            "INSERT INTO activations (platform, account_id, activated_at, updated_at, user_id) VALUES (?, ?, ?, ?, ?)",
            ("hermes", "default", NOW * 1000, NOW * 1000, "test-agent"),
        )
    upgraded = ClawChatStore(path)
    assert upgraded.mark_revoked_refresh_token(platform="hermes", account_id="default", refresh_token=REJECTED)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM schema_migrations WHERE version = 12").fetchone() == ("revoked_refresh_tokens",)
        assert db.execute("SELECT user_id FROM activations").fetchone() == ("test-agent",)
    assert upgraded.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") == hashlib.sha256(REJECTED.encode()).hexdigest()
