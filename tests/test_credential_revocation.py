"""Local credential deletion and server revocation stop stale-token recovery."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import sqlite3
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError

import pytest

from clawchat_gateway import activate, api_client, config, connection, storage, token_refresh
from clawchat_gateway.api_client import ClawChatApiError, RefreshResult, auth_refresh_with_retry
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.token_refresh import RefreshOutcome

NOW = 2_000_000_000


def _cfg(*, fresh=False):
    payload = base64.urlsafe_b64encode(json.dumps({
        "iat": NOW, "exp": NOW + (172800 if fresh else 86400),
    }).encode()).decode().rstrip("=")
    return ClawChatConfig(
        websocket_url="wss://example.invalid/ws", base_url="https://example.invalid",
        token=f"header.{payload}.signature",
        refresh_token="fresh-refresh" if fresh else "old-refresh",
        user_id="test-agent", owner_user_id="test-owner", reconnect_jitter_ratio=0.0,
    )


def _seed(store, cfg):
    store.upsert_activation(
        platform="hermes", account_id="default", user_id=cfg.user_id,
        owner_user_id=cfg.owner_user_id, conversation_id="test-conversation",
        access_token=cfg.token, refresh_token=cfg.refresh_token, activated_at=NOW * 1000,
        device_id="hermes-test-device",
    )


def _delete(store):
    with sqlite3.connect(store.db_path) as db:
        db.execute("DELETE FROM activations WHERE platform = ? AND account_id = ?", ("hermes", "default"))


class _Socket:
    def __init__(self, *, reason=None, close_code=None):
        self.closes = 0
        self.close_code = close_code
        self.close_reason = None
        self.reason = reason
        self.tokens = []
        self.frames = asyncio.Queue()
        self.frames.put_nowait(json.dumps({
            "version": "2", "event": "connect.challenge", "payload": {"nonce": "test-nonce"},
        }))

    async def close(self):
        self.closes += 1
        self.frames.put_nowait(None)

    async def send(self, raw):
        frame = json.loads(raw)
        assert frame["event"] == "connect"
        self.tokens.append(frame["payload"]["token"])
        self.frames.put_nowait(json.dumps({
            "version": "2", "event": "hello-fail" if self.reason else "hello-ok",
            "trace_id": frame["trace_id"],
            "payload": {"reason": self.reason} if self.reason else {},
        }))

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return frame


class _PollReached(Exception):
    pass


@pytest.fixture
def stores(tmp_path, monkeypatch):
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
    # All worker calls here touch fake HTTP or the test SQLite file. Execute
    # those short writes deterministically; HTTP races use explicit Events.
    async def to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(connection.asyncio, "to_thread", to_thread)
    return store


def _conn(monkeypatch, cfg):
    conn = ClawChatConnection(cfg, on_message=lambda *_args: None)
    monkeypatch.setattr(conn, "_refresh_device_id", lambda: "hermes-test-device")
    conn._refresh_manager = conn._build_refresh_manager()
    conn._refresh_manager._max_transient_retries = 0
    return conn


def _no_refresh(monkeypatch, conn):
    refresh = AsyncMock(side_effect=AssertionError("revoked credentials must not refresh"))
    monkeypatch.setattr(conn._refresh_manager, "refresh", refresh)
    return refresh


async def test_deleted_db_credentials_close_live_socket_and_stop_recovery(stores, monkeypatch, caplog):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    refresh = _no_refresh(monkeypatch, conn)
    logout = conn._on_auth_logout = AsyncMock()
    sockets = []
    real_sleep = asyncio.sleep

    async def connect(*_args, **_kwargs):
        assert not sockets, "must not reconnect with deleted credentials"
        socket = _Socket()
        sockets.append(socket)
        return socket

    async def poll(delay):
        assert delay == 2.0
        assert conn._state == ConnectionState.READY
        _delete(stores)
        # The same poll must detect deletion before a due proactive refresh.
        monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_args: True)
        await real_sleep(0)

    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    original_wait = conn._wait_for_activation_credentials

    polls = []

    async def wait_poll(delay):
        assert delay == 2.0 and len(sockets) == 1
        assert conn._state == ConnectionState.AUTH_FAILED
        polls.append(delay)
        if len(polls) == 3:
            raise _PollReached

    async def wait():
        monkeypatch.setattr(connection.asyncio, "sleep", wait_poll)
        return await original_wait()

    monkeypatch.setattr(conn, "_wait_for_activation_credentials", wait)
    with pytest.raises(_PollReached):
        await asyncio.wait_for(conn._supervisor(), timeout=2)
    assert sockets[0].closes >= 1 and sockets[0].tokens == [cfg.token]
    assert conn._state == ConnectionState.AUTH_FAILED and conn._auth_failed
    assert conn._rejected_activation_token == cfg.token and not conn._stopping
    assert not conn.config.token and not conn.config.refresh_token
    assert not conn.config.user_id and not conn.config.owner_user_id
    await conn._maybe_proactive_refresh()
    assert (await conn.reactive_refresh()).status == "skipped"
    await conn._dispatch_inbound({"event": "hello-fail", "payload": {"reason": "authentication failed"}})
    assert conn._state == ConnectionState.AUTH_FAILED and not conn._stopping
    refresh.assert_not_called()
    logout.assert_not_called()
    assert "activation_credentials_revoked" in caplog.text and "wait_activation" in caplog.text
    assert "activation credentials deleted" in caplog.text and "auth_logout" not in caplog.text
    assert cfg.token not in caplog.text and cfg.refresh_token not in caplog.text


async def test_first_activation_wait_never_connects_or_refreshes(stores, monkeypatch):
    cfg = replace(_cfg(), token="", refresh_token="", user_id="", owner_user_id="")
    conn = _conn(monkeypatch, cfg)
    connect = AsyncMock(side_effect=AssertionError("unactivated agent must wait"))
    monkeypatch.setattr(connection, "_ws_connect", connect)
    refresh = _no_refresh(monkeypatch, conn)
    polls = []

    async def poll(delay):
        assert delay == 2.0 and conn._state == ConnectionState.DISCONNECTED
        polls.append(delay)
        if len(polls) == 3:
            raise _PollReached

    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with pytest.raises(_PollReached):
        await conn._supervisor()
    connect.assert_not_called()
    refresh.assert_not_called()
    assert not conn._auth_failed


@pytest.mark.parametrize("read_fails", [False, True])
async def test_env_bootstrap_is_not_suspended_by_missing_row_or_read_errors(stores, monkeypatch, caplog, read_fails):
    cfg = _cfg()
    conn = _conn(monkeypatch, cfg)
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    refresh = _no_refresh(monkeypatch, conn)
    if read_fails:
        def fail_read(**_kwargs):
            raise RuntimeError("test database unavailable")

        monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    polls = []

    async def poll(delay):
        assert delay == 2.0
        polls.append(delay)
        if len(polls) == connection.CREDENTIAL_READ_FAILURE_LIMIT + 2:
            raise _PollReached

    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with pytest.raises(_PollReached):
        await conn._watch_activation_credentials()
    assert conn._state == ConnectionState.READY and socket.closes == 0
    assert conn.config == cfg and not conn._auth_failed
    assert not conn._activation_source_suspended and conn._credential_read_failures == 0
    assert "activation_credentials_revoked" not in caplog.text
    refresh.assert_not_called()


@pytest.mark.parametrize("recovery", ["same", "changed", "deleted"])
async def test_db_read_failures_suspend_then_poll_and_recover(stores, monkeypatch, caplog, recovery):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    refresh = _no_refresh(monkeypatch, conn)
    logout = conn._on_auth_logout = AsyncMock()
    reader = stores.get_activation_credentials
    control = {"fail": True}
    sockets, ready_polls, suspended_polls, activation_polls, writes = [], [], [], [], []
    real_sleep = asyncio.sleep

    def read(**kwargs):
        if control["fail"]:
            raise RuntimeError("test credential source unavailable")
        return reader(**kwargs)

    async def connect(*_args, **_kwargs):
        assert len(sockets) < 2
        if sockets:
            assert not conn._activation_source_suspended
            assert conn.config.token == (fresh.token if recovery == "changed" else cfg.token)
        socket = _Socket()
        sockets.append(socket)
        return socket

    async def poll(delay):
        assert delay == 2.0
        if conn._state == ConnectionState.READY:
            if len(sockets) == 2:
                conn._stopping = True
                await sockets[-1].close()
            else:
                ready_polls.append(delay)
                assert conn._credential_read_failures < connection.CREDENTIAL_READ_FAILURE_LIMIT
                assert sockets[0].closes == 0
        elif conn._state == ConnectionState.SUSPENDED:
            suspended_polls.append(delay)
            assert len(sockets) == 1 and sockets[0].closes >= 1
            assert conn.config == cfg and conn._using_activation_db_credentials
            assert not conn._auth_failed and conn._rejected_activation_token is None
            assert conn._rejected_refresh_fingerprint is None
            assert (await conn.reactive_refresh()).status == "skipped"
            await conn._maybe_proactive_refresh()
            assert not writes
            if len(suspended_polls) == 2:
                control["fail"] = False
                if recovery == "changed":
                    _seed(stores, fresh)
                elif recovery == "deleted":
                    _delete(stores)
        elif conn._state == ConnectionState.AUTH_FAILED:
            assert recovery == "deleted" and len(sockets) == 1
            activation_polls.append(delay)
            if len(activation_polls) == 2:
                raise _PollReached
        else:
            pytest.fail(f"unexpected poll state {conn._state}")
        await real_sleep(0)

    monkeypatch.setattr(stores, "get_activation_credentials", read)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        if recovery == "deleted":
            with pytest.raises(_PollReached):
                await asyncio.wait_for(conn._supervisor(), timeout=2)
        else:
            await asyncio.wait_for(conn._supervisor(), timeout=2)
    assert len(ready_polls) == connection.CREDENTIAL_READ_FAILURE_LIMIT - 1
    assert suspended_polls == [2.0, 2.0] and conn._credential_read_failures == 0
    assert "activation_credentials_unreadable" in caplog.text and "action=suspend" in caplog.text
    assert not writes
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    refresh.assert_not_called()
    logout.assert_not_called()
    if recovery == "deleted":
        assert conn._state == ConnectionState.AUTH_FAILED and conn._rejected_activation_token == cfg.token
    else:
        assert "activation_credentials_readable" in caplog.text
        assert [socket.tokens for socket in sockets] == [[cfg.token], [fresh.token if recovery == "changed" else cfg.token]]


@pytest.mark.parametrize("path", ["retry", "persist", "refresh"])
async def test_shared_read_failure_limit_stops_refresh_and_preserves_credentials(stores, monkeypatch, path):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    writes = []
    refresh = AsyncMock(return_value=RefreshOutcome("transient", error="test transient"))
    monkeypatch.setattr(conn._refresh_manager, "refresh", refresh)

    def fail_read(**_kwargs):
        raise RuntimeError("test credential source unavailable")

    monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    for failure in range(1, connection.CREDENTIAL_READ_FAILURE_LIMIT + 1):
        if path == "retry":
            assert await conn._refresh_should_continue(cfg) is (failure < connection.CREDENTIAL_READ_FAILURE_LIMIT)
        elif path == "persist":
            assert not await conn._persist_rotated_tokens(fresh.token, fresh.refresh_token)
        else:
            outcome = await conn.reactive_refresh()
            assert outcome.status == ("transient" if failure < connection.CREDENTIAL_READ_FAILURE_LIMIT else "skipped")
        assert conn._credential_read_failures == failure
        if failure < connection.CREDENTIAL_READ_FAILURE_LIMIT:
            assert conn._state == ConnectionState.READY and socket.closes == 0
    assert conn._state == ConnectionState.SUSPENDED and socket.closes >= 1
    assert conn.config == cfg and not conn._auth_failed and not writes
    assert conn._rejected_activation_token is None and conn._rejected_refresh_fingerprint is None
    assert not await conn._persist_refresh_rejection(cfg.refresh_token, expected_credentials=cfg)
    assert not await conn._persist_auth_logout("test revoked", expected_credentials=cfg)
    assert not writes
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    assert (await conn.reactive_refresh()).status == "skipped"
    if path == "refresh":
        assert refresh.await_count == connection.CREDENTIAL_READ_FAILURE_LIMIT - 1
    else:
        refresh.assert_not_called()


async def test_successful_read_resets_shared_failure_count(stores, monkeypatch):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    reader = stores.get_activation_credentials

    def fail_read(**_kwargs):
        raise RuntimeError("test credential source unavailable")

    monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    assert await conn._refresh_should_continue(cfg)
    assert not await conn._persist_rotated_tokens("test-access", "test-refresh")
    assert conn._credential_read_failures == 2
    monkeypatch.setattr(stores, "get_activation_credentials", reader)
    assert await conn._recheck_activation_source() == "ok"
    assert conn._credential_read_failures == 0
    monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    for _ in range(connection.CREDENTIAL_READ_FAILURE_LIMIT - 1):
        assert await conn._refresh_should_continue(cfg)
    assert not conn._activation_source_suspended
    assert not await conn._refresh_should_continue(cfg)
    assert conn._activation_source_suspended


async def test_suspension_ends_pending_handshake_without_rejecting_token(stores, monkeypatch):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.HANDSHAKING
    conn._hello_wait = asyncio.get_running_loop().create_future()
    socket = conn._ws = _Socket()

    def fail_read(**_kwargs):
        raise RuntimeError("test credential source unavailable")

    monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    for _ in range(connection.CREDENTIAL_READ_FAILURE_LIMIT - 1):
        assert await conn._refresh_should_continue(cfg)
        assert not conn._hello_wait.done()
    assert not await conn._refresh_should_continue(cfg)
    assert conn._hello_wait.result() is False and socket.closes >= 1
    assert conn.config == cfg and conn._state == ConnectionState.SUSPENDED
    assert not conn._auth_failed and conn._rejected_activation_token is None


async def test_existing_row_rotation_closes_and_reconnects_with_new_token(stores, monkeypatch, caplog):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    refresh = _no_refresh(monkeypatch, conn)
    sockets = []
    real_sleep = asyncio.sleep

    async def connect(*_args, **_kwargs):
        assert len(sockets) < 2
        socket = _Socket()
        sockets.append(socket)
        return socket

    async def poll(delay):
        if delay == 2.0 and conn._state == ConnectionState.READY:
            if len(sockets) == 1:
                _seed(stores, fresh)
            else:
                conn._stopping = True
                await sockets[-1].close()
        await real_sleep(0)

    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        await asyncio.wait_for(conn._supervisor(), timeout=2)
    assert [socket.tokens for socket in sockets] == [[cfg.token], [fresh.token]]
    assert sockets[0].closes >= 1 and not conn._auth_failed
    assert "activation_credentials_changed" in caplog.text
    assert "activation_credentials_revoked" not in caplog.text
    refresh.assert_not_called()


async def test_deleted_credentials_reject_old_row_then_resume_new_activation(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    _delete(stores)
    conn._state = ConnectionState.READY
    conn._ws = _Socket()

    async def tick(_delay):
        return None

    monkeypatch.setattr(connection.asyncio, "sleep", tick)
    await conn._watch_activation_credentials()
    refresh = _no_refresh(monkeypatch, conn)
    connected, polls = [], []

    async def connect():
        connected.append(conn.config.token)
        assert conn.config.token == fresh.token and not conn._auth_failed
        conn._stopping = True

    async def poll(delay):
        assert not connected and conn._state == ConnectionState.AUTH_FAILED
        assert conn._rejected_activation_token == cfg.token
        polls.append(delay)
        _seed(stores, cfg if len(polls) == 1 else fresh)
        assert len(polls) <= 2

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    await conn._supervisor()
    assert connected == [fresh.token] and polls == [2.0, 2.0]
    assert conn._rejected_activation_token is None
    refresh.assert_not_called()


@pytest.mark.parametrize("close_code", [None, 1005])
async def test_server_kick_then_hello_rejection_and_permanent_refresh_stops(stores, monkeypatch, caplog, close_code):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    logout = conn._on_auth_logout = AsyncMock()
    sockets, refreshes, polls, backoffs = [], [], [], []
    real_sleep = asyncio.sleep

    def urlopen(request, timeout=None):
        refreshes.append(json.loads(request.data))
        raise HTTPError(request.full_url, 401, "revoked", {}, io.BytesIO(b'{"code":10003}'))

    async def connect(*_args, **_kwargs):
        assert len(sockets) < 2, "server revocation must cap connect attempts at two"
        socket = _Socket(reason="authentication failed" if sockets else None, close_code=close_code)
        sockets.append(socket)
        return socket

    async def poll(delay):
        if conn._state == ConnectionState.AUTH_FAILED:
            assert delay == 2.0 and len(sockets) == 2 and len(refreshes) == 1
            polls.append(delay)
            if len(polls) == 3:
                raise _PollReached
        elif conn._state == ConnectionState.RECONNECTING:
            backoffs.append(delay)
        elif conn._state == ConnectionState.READY:
            assert len(sockets) == 1
            await sockets[0].close()
        await real_sleep(0)

    monkeypatch.setattr(api_client, "urlopen", urlopen)
    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        with pytest.raises(_PollReached):
            await asyncio.wait_for(asyncio.create_task(conn._supervisor()), timeout=2)
    assert backoffs == [0.5]
    assert [socket.tokens for socket in sockets] == [[cfg.token], [cfg.token]]
    assert all(socket.closes >= 1 for socket in sockets)
    assert conn._state == ConnectionState.AUTH_FAILED and not conn._stopping
    assert not conn.config.token and not conn.config.refresh_token
    assert refreshes == [{"refresh_token": cfg.refresh_token}]
    await conn.reactive_refresh()
    await conn._maybe_proactive_refresh()
    assert len(refreshes) == 1
    logout.assert_awaited_once()
    assert "auth_logout" in caplog.text and "hello_fail_refresh" in caplog.text


@pytest.mark.parametrize("watcher_observes", [False, True])
async def test_in_flight_refresh_cannot_recreate_deleted_row(stores, monkeypatch, watcher_observes):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    started, release = asyncio.Event(), asyncio.Event()
    writes = []
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")

    class Client:
        async def auth_refresh(self, **_kwargs):
            started.set()
            await release.wait()
            return RefreshResult(fresh.token, fresh.refresh_token)

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    refresh_task = asyncio.create_task(conn.reactive_refresh())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        _delete(stores)
        if watcher_observes:
            async def tick(_delay):
                return None

            monkeypatch.setattr(connection.asyncio, "sleep", tick)
            await conn._watch_activation_credentials()
        release.set()
        outcome = await asyncio.wait_for(refresh_task, timeout=2)
    finally:
        release.set()
        if not refresh_task.done():
            refresh_task.cancel()
        await asyncio.gather(refresh_task, return_exceptions=True)
    assert outcome.status == "skipped" and not writes
    assert conn._state == ConnectionState.AUTH_FAILED and socket.closes >= 1
    assert not conn.config.token and not conn.config.refresh_token
    with sqlite3.connect(stores.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone() == (0,)


async def test_late_success_result_is_not_adopted_after_revocation(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    conn = _conn(monkeypatch, cfg)

    async def refresh(**_kwargs):
        await conn._enter_auth_failed(reason="activation credentials deleted")
        return RefreshOutcome("success", fresh.token, fresh.refresh_token)

    monkeypatch.setattr(conn._refresh_manager, "refresh", refresh)
    assert (await conn.reactive_refresh()).status == "skipped"
    assert not conn.config.token and not conn._refresh_pending_reconnect


@pytest.mark.parametrize("mutation", ["clear", "rotate", "delete"])
async def test_db_refresh_conditional_write_rejects_external_changes(stores, monkeypatch, mutation):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    writes = []
    update = stores.update_activation_tokens

    def external_change(**kwargs):
        assert kwargs["expected_access_token"] == cfg.token
        # Another process changes the row after the connection guard read.
        if mutation == "clear":
            stores.clear_activation_credentials(platform="hermes", account_id="default")
        elif mutation == "rotate":
            _seed(stores, fresh)
        else:
            _delete(stores)
        return update(**kwargs)

    monkeypatch.setattr(stores, "update_activation_tokens", external_change)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    assert not await conn._persist_rotated_tokens(fresh.token, fresh.refresh_token)
    assert not writes
    with sqlite3.connect(stores.db_path) as db:
        row = db.execute("SELECT access_token, refresh_token FROM activations").fetchone()
    assert row == {
        "clear": (None, None), "rotate": (fresh.token, fresh.refresh_token), "delete": None,
    }[mutation]


async def test_env_first_refresh_still_seeds_activation(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    conn = _conn(monkeypatch, cfg)

    class Client:
        async def auth_refresh(self, **_kwargs):
            return RefreshResult(fresh.token, fresh.refresh_token)

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    assert (await conn.reactive_refresh()).status == "success"
    credentials = stores.get_activation_credentials(platform="hermes", account_id="default")
    assert credentials.access_token == fresh.token and credentials.refresh_token == fresh.refresh_token
    assert conn._using_activation_db_credentials


async def test_in_flight_old_refresh_does_not_overwrite_new_activation(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    started, release = asyncio.Event(), asyncio.Event()
    writes = []
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")

    class Client:
        async def auth_refresh(self, **_kwargs):
            started.set()
            await release.wait()
            return RefreshResult("late-old-access", "late-old-refresh")

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    task = asyncio.create_task(conn.reactive_refresh())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        await conn._enter_auth_failed(reason="activation credentials deleted")
        _seed(stores, fresh)
        assert await conn._wait_for_activation_credentials()
        release.set()
        assert (await asyncio.wait_for(task, timeout=2)).status == "skipped"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    credentials = stores.get_activation_credentials(platform="hermes", account_id="default")
    assert credentials.access_token == fresh.token and conn.config.token == fresh.token
    assert not writes and not conn._auth_failed


async def test_concurrent_reactive_refresh_waiters_share_success(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    conn = _conn(monkeypatch, cfg)
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    class Client:
        async def auth_refresh(self, **_kwargs):
            calls.append(True)
            started.set()
            await release.wait()
            return RefreshResult(fresh.token, fresh.refresh_token)

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    tasks = [asyncio.create_task(conn.reactive_refresh()) for _ in range(2)]
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert [outcome.status for outcome in outcomes] == ["success", "success"]
    assert calls == [True] and conn.config.token == fresh.token


def test_conditional_update_never_seeds_even_with_bootstrap_identity(stores):
    cfg = _cfg()
    assert stores.update_activation_tokens(
        platform="hermes", account_id="default", access_token="late-access",
        refresh_token="late-refresh", expected_access_token=cfg.token,
        seed_user_id=cfg.user_id, seed_owner_user_id=cfg.owner_user_id,
    ) is False
    with sqlite3.connect(stores.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM activations").fetchone() == (0,)


@pytest.mark.parametrize("sqlite_source", [False, True])
def test_persistence_order_depends_on_credential_source(stores, monkeypatch, sqlite_source):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    if sqlite_source:
        _seed(stores, cfg)
    calls = []
    update = stores.update_activation_tokens

    def write_db(**kwargs):
        calls.append("sqlite")
        return update(**kwargs)

    def write_env(_values):
        calls.append("env")
        credentials = stores.get_activation_credentials(platform="hermes", account_id="default")
        if sqlite_source:
            assert credentials.access_token == fresh.token
        else:
            assert credentials is None
        return "test.env"

    monkeypatch.setattr(stores, "update_activation_tokens", write_db)
    monkeypatch.setattr(activate, "_write_env_values", write_env)
    assert activate.persist_rotated_tokens(
        access_token=fresh.token, refresh_token=fresh.refresh_token,
        user_id=cfg.user_id, owner_user_id=cfg.owner_user_id,
        expected_access_token=cfg.token if sqlite_source else None,
    )
    assert calls == (["sqlite", "env"] if sqlite_source else ["env", "sqlite"])


async def test_proactive_waiter_closes_socket_after_rest_adopts_refresh(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    ready, closed, started, release = (asyncio.Event() for _ in range(4))
    connected, closes, http_calls = [], [], []
    real_sleep = asyncio.sleep

    class Socket:
        async def close(self):
            assert conn.config.token == fresh.token
            closes.append(conn._refresh_pending_reconnect)
            closed.set()

    class Client:
        async def auth_refresh(self, **_kwargs):
            http_calls.append(True)
            started.set()
            await release.wait()
            return RefreshResult(fresh.token, fresh.refresh_token)

    async def connect():
        connected.append(conn.config.token)
        if len(connected) == 1:
            conn._ws = Socket()
            await conn._set_state(ConnectionState.READY)
            ready.set()
            await closed.wait()
            conn._ws = None
        else:
            assert connected == [cfg.token, fresh.token]
            conn._stopping = True

    async def tick(_delay):
        await real_sleep(0)

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", tick)
    supervisor = asyncio.create_task(conn._supervisor())
    tasks = []
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
        tasks.append(asyncio.create_task(conn.reactive_refresh()))
        await asyncio.wait_for(started.wait(), timeout=2)
        monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_args: True)
        tasks.append(asyncio.create_task(conn._maybe_proactive_refresh()))
        await real_sleep(0)
        release.set()
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        await asyncio.wait_for(supervisor, timeout=2)
    finally:
        release.set()
        for task in [*tasks, supervisor]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, supervisor, return_exceptions=True)
    assert outcomes[0].status == "success"
    assert closes == [True] and http_calls == [True]
    assert connected == [cfg.token, fresh.token]


@pytest.mark.parametrize("entry", ["startup", "activation_poll"])
async def test_startup_refresh_deletion_waits_without_connecting(stores, monkeypatch, entry):
    cfg = _cfg()
    if entry == "startup":
        _seed(stores, cfg)
        initial = cfg
    else:
        initial = replace(cfg, token="", refresh_token="", user_id="", owner_user_id="")
    conn = _conn(monkeypatch, initial)
    connect = AsyncMock(side_effect=AssertionError("revoked startup must not connect"))
    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(connection, "_ws_connect", connect)
    monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_args: True)
    states, polls, refreshes = [], [], []

    async def state_changed(state):
        states.append(state)

    class Client:
        async def auth_refresh(self, **_kwargs):
            refreshes.append(True)
            _delete(stores)
            return RefreshResult("late-access", "late-refresh")

    async def poll(delay):
        assert delay == 2.0
        if not refreshes:
            assert entry == "activation_poll"
            _seed(stores, cfg)
            return
        assert conn._state == ConnectionState.AUTH_FAILED
        polls.append(delay)
        if len(polls) == 3:
            raise _PollReached

    conn._on_state_change = state_changed
    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with pytest.raises(_PollReached):
        await conn._supervisor()
    connect.assert_not_called()
    assert ConnectionState.CONNECTING not in states
    assert refreshes == [True] and polls == [2.0] * 3
    assert not conn.config.token and not conn.config.refresh_token
    assert not conn._refresh_pending_reconnect and not conn._stopping


@pytest.mark.parametrize("stop_check", [1, 2, 3])
@pytest.mark.parametrize("async_check", [False, True])
async def test_retry_guard_stops_at_each_checkpoint_with_last_transient(stop_check, async_check):
    error = ClawChatApiError("api", "test transient", code=1)
    calls, checks, sleeps = [], [], []

    class Client:
        async def auth_refresh(self, **_kwargs):
            calls.append(True)
            raise error

    def check():
        checks.append(True)
        return len(checks) < stop_check

    async def async_guard():
        return check()

    async def sleep(delay):
        sleeps.append(delay)

    with pytest.raises(ClawChatApiError) as info:
        await auth_refresh_with_retry(
            Client(), refresh_token="test-refresh", device_id="hermes-test-device",
            max_transient_retries=None, sleep=sleep,
            should_continue=async_guard if async_check else check,
        )
    assert info.value is error and not api_client.is_permanent_refresh_error(info.value)
    assert calls == [True] and len(checks) == stop_check
    assert len(sleeps) == (1 if stop_check == 3 else 0)


async def test_unbounded_proactive_transient_retry_observes_deletion(stores, monkeypatch, caplog):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    conn._refresh_manager._max_transient_retries = None
    logout = conn._on_auth_logout = AsyncMock()
    socket = _Socket()
    connected, http_calls, retries, polls, outcomes = [], [], [], [], []
    attempt_refresh = conn._attempt_refresh

    class Client:
        async def auth_refresh(self, **_kwargs):
            http_calls.append(True)
            assert len(http_calls) <= 2, "revoked refresh must stop its unbounded retry loop"
            raise ClawChatApiError("transport", "test unavailable", connect_failed=True)

    async def retry_sleep(delay):
        assert delay > 0
        retries.append(delay)
        if len(retries) == 2:
            _delete(stores)

    async def attempt(*, close_ws_on_success):
        outcome = await attempt_refresh(close_ws_on_success=close_ws_on_success)
        outcomes.append(outcome.status)
        return outcome

    async def connect():
        connected.append(conn.config.token)
        assert len(connected) == 1
        conn._ws = socket
        await conn._set_state(ConnectionState.READY)
        await conn._watch_activation_credentials()

    async def poll(delay):
        assert delay == 2.0
        if conn._state == ConnectionState.AUTH_FAILED:
            assert connected == [cfg.token] and len(http_calls) == 2
            polls.append(delay)
            if len(polls) == 3:
                raise _PollReached

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    monkeypatch.setattr(conn, "_attempt_refresh", attempt)
    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(conn._refresh_manager, "_sleep", retry_sleep)
    monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_args: conn._state == ConnectionState.READY)
    monkeypatch.setattr(connection.asyncio, "sleep", poll)
    with pytest.raises(_PollReached):
        await conn._supervisor()
    assert outcomes == ["skipped"]
    assert socket.closes >= 1 and len(retries) == 2 and polls == [2.0] * 3
    assert not conn.config.token and not conn.config.refresh_token
    await conn.reactive_refresh()
    assert len(http_calls) == 2
    logout.assert_not_called()
    assert "activation_credentials_revoked" in caplog.text and "auth_logout" not in caplog.text


@pytest.mark.parametrize("condition,allowed", [
    ("read_error", True), ("bootstrap", True), ("auth_failed", False), ("refresh_changed", False),
])
async def test_refresh_retry_source_guard_preserves_read_errors(stores, monkeypatch, caplog, condition, allowed):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    socket = conn._ws = _Socket()
    if condition == "read_error":
        def fail_read(**_kwargs):
            raise RuntimeError("test database unavailable")

        monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
    elif condition == "bootstrap":
        conn._using_activation_db_credentials = False
        _delete(stores)
    elif condition == "auth_failed":
        conn._auth_failed = True
    else:
        conn._cfg = replace(cfg, refresh_token="different-refresh")
    if condition == "read_error":
        for _ in range(connection.CREDENTIAL_READ_FAILURE_LIMIT - 1):
            assert await conn._refresh_should_continue(cfg)
            assert socket.closes == 0
        assert not await conn._refresh_should_continue(cfg)
        assert socket.closes >= 1 and conn._state == ConnectionState.SUSPENDED
        assert conn.config == cfg and not conn._auth_failed
        assert conn._rejected_activation_token is None
    else:
        assert await conn._refresh_should_continue(cfg) is allowed
        assert socket.closes == 0
    assert "activation_credentials_revoked" not in caplog.text


@pytest.mark.parametrize("change", ["delete", "rotate"])
async def test_reconnect_rechecks_credentials_changed_during_backoff(stores, monkeypatch, change):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    refresh = _no_refresh(monkeypatch, conn)
    connected, backoffs, polls = [], [], []

    async def connect():
        connected.append(conn.config.token)
        if len(connected) == 1:
            await conn._set_state(ConnectionState.DISCONNECTED)
        else:
            assert change == "rotate" and conn.config.token == fresh.token
            conn._stopping = True

    async def sleep(delay):
        if conn._state == ConnectionState.RECONNECTING:
            assert delay == 0.5 and connected == [cfg.token]
            backoffs.append(delay)
            if change == "delete":
                _delete(stores)
            else:
                _seed(stores, fresh)
        else:
            assert change == "delete" and delay == 2.0
            assert conn._state == ConnectionState.AUTH_FAILED and connected == [cfg.token]
            polls.append(delay)
            if len(polls) == 3:
                raise _PollReached

    monkeypatch.setattr(conn, "_run_one_connection", connect)
    monkeypatch.setattr(connection.asyncio, "sleep", sleep)
    if change == "delete":
        with pytest.raises(_PollReached):
            await conn._supervisor()
    else:
        await conn._supervisor()
    assert backoffs == [0.5]
    assert connected == ([cfg.token] if change == "delete" else [cfg.token, fresh.token])
    refresh.assert_not_called()


@pytest.mark.parametrize("change", ["delete", "rotate"])
async def test_refresh_rechecks_source_before_issuing_http(stores, monkeypatch, change):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    refresh = _no_refresh(monkeypatch, conn)
    if change == "delete":
        _delete(stores)
    else:
        _seed(stores, fresh)
    assert (await conn.reactive_refresh()).status == "skipped"
    refresh.assert_not_called()
    assert socket.closes >= 1
    if change == "delete":
        assert conn._state == ConnectionState.AUTH_FAILED and not conn.config.token
    else:
        assert conn.config.token == fresh.token and not conn._auth_failed


async def test_unbounded_refresh_retry_suspends_when_source_stays_unreadable(stores, monkeypatch):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.READY
    socket = conn._ws = _Socket()
    conn._refresh_manager._max_transient_retries = None
    logout = conn._on_auth_logout = AsyncMock()
    http_calls, sleeps, writes = [], [], []

    def fail_read(**_kwargs):
        raise RuntimeError("test credential source unavailable")

    class Client:
        async def auth_refresh(self, **_kwargs):
            http_calls.append(True)
            monkeypatch.setattr(stores, "get_activation_credentials", fail_read)
            raise ClawChatApiError("transport", "test transient", connect_failed=True)

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    monkeypatch.setattr(conn._refresh_manager, "_sleep", sleep)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    assert (await conn.reactive_refresh()).status == "skipped"
    assert http_calls == [True] and len(sleeps) == 1
    assert conn._activation_source_suspended and conn._state == ConnectionState.SUSPENDED
    assert socket.closes >= 1 and conn.config == cfg and not writes
    assert conn._rejected_activation_token is None and conn._rejected_refresh_fingerprint is None
    logout.assert_not_called()


@pytest.mark.parametrize("adopted", [False, True])
async def test_late_permanent_preserves_new_activation_and_session(stores, monkeypatch, caplog, adopted):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    conn._state = ConnectionState.READY
    old_socket = conn._ws = _Socket()
    started, release = asyncio.Event(), asyncio.Event()
    writes, calls = [], []
    logout = conn._on_auth_logout = AsyncMock()
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")

    class Client:
        async def auth_refresh(self, *, refresh_token, device_id):
            calls.append(refresh_token)
            if len(calls) == 1:
                started.set()
                await release.wait()
                raise ClawChatApiError("auth", "test revoked", status=401, code=10003)
            return RefreshResult("next-access", "next-refresh")

    monkeypatch.setattr(conn, "_build_refresh_client", Client)
    task = asyncio.create_task(conn.reactive_refresh())
    with caplog.at_level("INFO", logger="clawchat_gateway.connection"):
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            if adopted:
                _delete(stores)
                assert await conn._recheck_activation_source() == "revoked"
            _seed(stores, fresh)
            if adopted:
                assert await conn._wait_for_activation_credentials()
                await conn._set_state(ConnectionState.READY)
                conn._ws = _Socket()
            socket = conn._ws
            release.set()
            assert (await asyncio.wait_for(task, timeout=2)).status == "skipped"
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    credentials = stores.get_activation_credentials(platform="hermes", account_id="default")
    assert credentials.access_token == fresh.token and credentials.refresh_token == fresh.refresh_token
    assert conn.config == (fresh if adopted else cfg)
    assert conn._state == ConnectionState.READY and socket.closes == 0
    assert not conn._auth_failed and not conn._refresh_manager.logged_out and not writes
    assert conn._rejected_refresh_fingerprint is None
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    assert "refresh_result_discarded" in caplog.text and "auth_logout" not in caplog.text
    logout.assert_not_called()
    if adopted:
        assert old_socket.closes >= 1
        # The manager's old-token latch must not block a request for the new token.
        assert (await conn.reactive_refresh()).status == "success"
        assert calls == [cfg.refresh_token, fresh.refresh_token]


@pytest.mark.parametrize("change", ["access", "refresh", "auth_failed"])
async def test_permanent_callbacks_are_bound_to_request_credentials(stores, monkeypatch, change):
    cfg = _cfg()
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    if change == "access":
        conn._cfg = replace(cfg, token="different-access")
    elif change == "refresh":
        conn._cfg = replace(cfg, refresh_token="different-refresh")
    else:
        conn._auth_failed = True
    writes = []
    logout = conn._on_auth_logout = AsyncMock()
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    assert not await conn._persist_refresh_rejection(cfg.refresh_token, expected_credentials=cfg)
    assert not await conn._persist_auth_logout("test revoked", expected_credentials=cfg)
    assert not writes and conn._rejected_refresh_fingerprint is None
    assert stores.get_revoked_refresh_fingerprint(platform="hermes", account_id="default") is None
    assert stores.get_activation_credentials(platform="hermes", account_id="default").access_token == cfg.token
    logout.assert_not_called()


async def test_conditional_logout_preserves_row_rotated_after_guard(stores, monkeypatch):
    cfg, fresh = _cfg(), _cfg(fresh=True)
    _seed(stores, cfg)
    conn = _conn(monkeypatch, cfg)
    assert conn._load_startup_activation_credentials()
    clear = stores.clear_activation_credentials
    writes = []
    logout = conn._on_auth_logout = AsyncMock()

    def rotate_then_clear(**kwargs):
        assert kwargs["expected_access_token"] == cfg.token
        _seed(stores, fresh)
        return clear(**kwargs)

    monkeypatch.setattr(stores, "clear_activation_credentials", rotate_then_clear)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: writes.append(values) or "test.env")
    assert not await conn._persist_auth_logout("test revoked", expected_credentials=cfg)
    assert stores.get_activation_credentials(platform="hermes", account_id="default").access_token == fresh.token
    assert conn.config == cfg and not writes
    logout.assert_not_called()
