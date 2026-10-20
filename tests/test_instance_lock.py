"""Host-local ownership gates sockets and refreshes, with crash-safe takeover."""
from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve

from clawchat_gateway import connection, instance_lock
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.storage import ActivationCredentials
from clawchat_gateway.token_refresh import RefreshOutcome


_CHILD = """
import json, sys
from pathlib import Path
from clawchat_gateway.instance_lock import acquire
path, home, kind, hold = sys.argv[1:]
result = acquire(Path(path), kind=kind, profile_home=Path(home))
print(json.dumps(dict(outcome=result.outcome, holder=result.holder)), flush=True)
if hold == 'yes' and result.outcome == 'acquired':
    sys.stdin.read()
result.release()
"""


def _command(path, home=None, kind="profile", hold=False):
    return [sys.executable, "-c", _CHILD, str(path), str(home or path.parent), kind, "yes" if hold else "no"]


def _probe(path, home=None, kind="profile"):
    result = subprocess.run(_command(path, home, kind), capture_output=True, text=True, timeout=10, check=True)
    return json.loads(result.stdout)


@contextmanager
def _holder(path, home=None, kind="profile"):
    with subprocess.Popen(_command(path, home, kind, True), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            record = json.loads(proc.stdout.readline())
            assert record["outcome"] == "acquired", record
            yield proc
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock and file modes")
def test_posix_refcounts_metadata_and_persistent_inode(tmp_path):
    path = tmp_path / "clawchat" / "gateway.lock"
    first = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
    second = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
    assert first.outcome == second.outcome == "acquired"
    inode = path.stat().st_ino
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert first.holder["pid"] == os.getpid()
    assert {"hostname", "started_at", "profile_home", "version"} <= first.holder.keys()
    assert "user_id" not in first.holder
    try:
        held = _probe(path)
        assert held["outcome"] == "held"
        assert held["holder"]["pid"] == os.getpid()
        first.release()
        first.release()  # Releasing a handle twice cannot decrement another reference.
        assert _probe(path)["outcome"] == "held"
    finally:
        first.release()
        second.release()
    assert path.exists() and path.stat().st_ino == inode
    assert _probe(path)["outcome"] == "acquired"


@pytest.mark.skipif(os.name == "nt", reason="SIGKILL and POSIX flock")
def test_killed_holder_and_garbage_leftover(tmp_path):
    path = tmp_path / "gateway.lock"
    path.write_text("partial holder record")
    with _holder(path) as proc:
        held = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
        assert held.outcome == "held" and held.holder["pid"] == proc.pid
        path.write_text("incomplete JSON")
        held = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
        assert held.outcome == "held" and held.holder == {}
        proc.kill()
        proc.wait(timeout=10)
        acquired = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
        try:
            assert acquired.outcome == "acquired"
        finally:
            acquired.release()
    path.write_text("garbage")
    acquired = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
    acquired.release()
    assert acquired.outcome == "acquired" and path.exists()


@pytest.mark.skipif(os.name == "nt", reason="Inject POSIX flock failures")
def test_unsupported_warns_once_and_open_access_is_not_contention(tmp_path, monkeypatch, caplog):
    import fcntl

    def fail(*args):
        raise OSError(errno.ENOLCK, "locking unavailable")

    monkeypatch.setattr(fcntl, "flock", fail)
    path = tmp_path / "gateway.lock"
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            assert instance_lock.acquire(path, kind="profile", profile_home=tmp_path).outcome == "unsupported"
    assert len([r for r in caplog.records if "unsupported" in r.message]) == 1
    assert str(path) in caplog.text
    monkeypatch.setattr(instance_lock.os, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError(errno.EACCES, "open denied")))
    assert instance_lock.acquire(tmp_path / "other.lock", kind="profile", profile_home=tmp_path).outcome == "unsupported"


@pytest.mark.parametrize("error,expected", [(None, "acquired"), (errno.EACCES, "held"),
                                             (errno.EDEADLK, "held"), (errno.ENOLCK, "unsupported")])
def test_windows_backend_offsets_and_outcomes(tmp_path, monkeypatch, error, expected):
    calls = []

    def locking(fd, mode, nbytes):
        calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR), nbytes))
        if mode == 1 and error is not None:
            raise OSError(error, "injected lock failure")

    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=locking))
    monkeypatch.setattr(instance_lock, "_backend", lambda: "windows")
    result = instance_lock.acquire(tmp_path / "gateway.lock", kind="profile", profile_home=tmp_path)
    assert result.outcome == expected
    assert calls == [(1, instance_lock.WINDOWS_LOCK_OFFSET, 1)]
    if expected == "acquired":
        assert json.loads(result.path.read_text())["pid"] == os.getpid()
    result.release()
    if expected == "acquired":
        assert calls[-1] == (2, instance_lock.WINDOWS_LOCK_OFFSET, 1)


@pytest.mark.skipif(os.name != "nt", reason="Requires native Windows byte-range lock enforcement")
def test_native_windows_metadata_readable_while_locked(tmp_path):
    path = tmp_path / "gateway.lock"
    with _holder(path) as proc:
        result = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
        assert result.outcome == "held" and result.holder["pid"] == proc.pid
    assert _probe(path)["outcome"] == "acquired"


@pytest.fixture
def homes(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(connection, "hermes_home", lambda: root)
    monkeypatch.setattr(connection, "hermes_root", lambda: root)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    monkeypatch.setattr(connection, "INSTANCE_LOCK_RETRY_SECONDS", 0.05)
    monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_: True)
    monkeypatch.setattr(ClawChatConnection, "_warn_if_device_id_volatile", lambda _: None)
    monkeypatch.setattr(ClawChatConnection, "_refresh_device_id", lambda _: "test-device")
    return root


def _connection(monkeypatch, home, *, user="usr_test", override=None):
    monkeypatch.setattr(connection, "hermes_home", lambda: home)
    conn = ClawChatConnection(ClawChatConfig(
        websocket_url="ws://127.0.0.1:1", base_url="http://127.0.0.1:1",
        token="test-token", refresh_token="test-refresh",
        user_id=user, owner_user_id="usr_owner", reconnect_jitter_ratio=0,
        reconnect_max_retries=0,
    ), on_message=AsyncMock(), connect_device_id=override)
    manager = SimpleNamespace(refresh=AsyncMock(return_value=RefreshOutcome(status="skipped")), reset_latch=lambda: None)
    monkeypatch.setattr(conn, "_build_refresh_manager", lambda: manager)
    return conn, manager


def _park_connection(monkeypatch, conn):
    connected = asyncio.Event()

    async def run():
        connected.set()
        await conn._set_state(ConnectionState.READY)
        await asyncio.Event().wait()

    monkeypatch.setattr(conn, "_run_one_connection", run)
    return connected


async def _wait_for(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), 5)


@asynccontextmanager
async def _peer():
    attempts = []

    async def handler(ws):
        attempts.append(ws)
        await ws.send(json.dumps({"type": "event", "event": "connect.challenge", "payload": {"nonce": "test-nonce"}}))
        request = json.loads(await ws.recv())
        await ws.send(json.dumps({"type": "res", "requestId": request["trace_id"], "payload": {"type": "hello-ok"}}))
        await ws.wait_closed()

    try:
        server = await serve(handler, "127.0.0.1", 0)
    except OSError as exc:
        if exc.errno in (errno.EPERM, errno.EACCES) or "could not bind on any address" in str(exc):
            pytest.skip("Sandbox forbids local loopback sockets")
        raise
    async with server:
        yield f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", attempts


@pytest.mark.asyncio
async def test_profile_standby_kill_takeover_real_websocket(homes, monkeypatch, caplog):
    conn, manager = _connection(monkeypatch, homes)
    path = homes / "clawchat" / "gateway.lock"
    async with _peer() as (url, attempts):
        conn._cfg = replace(conn.config, websocket_url=url)
        with _holder(path, homes) as proc:
            try:
                await conn.start()
                await _wait_for(lambda: conn._state == ConnectionState.STANDBY)
                await asyncio.sleep(0.12)
                assert not attempts
                manager.refresh.assert_not_awaited()
                assert (await conn.reactive_refresh()).status == "skipped"
                manager.refresh.assert_not_awaited()
                status = conn.instance_lock_status()
                assert status["kind"] == "profile" and status["holder"]["pid"] == proc.pid
                assert len([r for r in caplog.records if "event=instance_lock_standby" in r.message]) == 1
                proc.kill()
                proc.wait(timeout=10)
                await _wait_for(lambda: conn.is_ready)
                assert len(attempts) == 1
                manager.refresh.assert_awaited_once()
                assert conn.instance_lock_status()["state"] == "acquired"
            finally:
                await conn.stop()
    assert _probe(path)["outcome"] == "acquired"


@pytest.mark.asyncio
async def test_profile_standby_gates_and_prompt_stop(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes)
    connected = _park_connection(monkeypatch, conn)
    monkeypatch.setattr(conn, "_load_startup_activation_credentials", lambda: pytest.fail("Activation load before profile ownership"))
    with _holder(homes / "clawchat" / "gateway.lock"):
        try:
            await conn.start()
            await _wait_for(lambda: conn._state == ConnectionState.STANDBY)
            assert not await conn.wait_until_ready(timeout=0.2)
            assert conn.is_standby
            assert not connected.is_set()
            assert (await conn._attempt_refresh(close_ws_on_success=False)).status == "skipped"
            await conn._maybe_proactive_refresh()
            manager.refresh.assert_not_awaited()
            monkeypatch.setattr(connection, "INSTANCE_LOCK_RETRY_SECONDS", 30)
            started = time.monotonic()
            await asyncio.wait_for(conn.stop(), 0.5)
            assert time.monotonic() - started < 0.5
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_profile_takeover_without_sockets(homes, monkeypatch, caplog):
    conn, manager = _connection(monkeypatch, homes)
    connected = _park_connection(monkeypatch, conn)
    clock = [0.0]
    monkeypatch.setattr(connection, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with caplog.at_level(logging.INFO), _holder(homes / "clawchat" / "gateway.lock") as proc:
        try:
            await conn.start()
            await _wait_for(lambda: conn._state == ConnectionState.STANDBY)
            clock[0] = 599.0
            await asyncio.sleep(0.12)
            assert len([r for r in caplog.records if "event=instance_lock_standby" in r.message]) == 1
            clock[0] = 600.0
            await _wait_for(lambda: len([r for r in caplog.records if "event=instance_lock_standby" in r.message]) == 2)
            assert not connected.is_set()
            manager.refresh.assert_not_awaited()
            proc.kill()
            proc.wait(timeout=10)
            await asyncio.wait_for(connected.wait(), 1)
            manager.refresh.assert_awaited_once()
            assert "event=instance_lock_acquired" in caplog.text
            assert "waited_seconds=600.0" in caplog.text
        finally:
            await conn.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("user,blocked", [("usr_same", True), ("usr_different", False)])
async def test_identity_lock_across_profiles(homes, monkeypatch, user, blocked):
    first_home = homes / "profiles" / "first"
    conn, manager = _connection(monkeypatch, homes / "profiles" / "second", user=user)
    connected = _park_connection(monkeypatch, conn)
    path = instance_lock.identity_lock_path(homes, "usr_same")
    with _holder(path, first_home, "identity") as proc:
        try:
            await conn.start()
            await _wait_for(lambda: connected.is_set() or conn._state == ConnectionState.STANDBY)
            if blocked:
                assert not connected.is_set()
                assert conn.instance_lock_status()["kind"] == "identity"
                assert conn.instance_lock_status()["holder"]["pid"] == proc.pid
                manager.refresh.assert_not_awaited()
            else:
                assert connected.is_set()
                manager.refresh.assert_awaited_once()
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_sibling_device_is_exempt(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes)
    conn.use_sibling_connect_device_id("-sender")
    connected = _park_connection(monkeypatch, conn)

    def forbidden(*a, **k):
        pytest.fail("Sibling sender attempted an instance lock")

    monkeypatch.setattr(instance_lock, "acquire", forbidden)
    try:
        await conn.start()
        await asyncio.wait_for(connected.wait(), 1)
        manager.refresh.assert_awaited_once()
        assert conn.instance_lock_status()["state"] == "exempt"
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_supersede_holds_lock_across_stop(homes, monkeypatch):
    prior, _ = _connection(monkeypatch, homes)
    ready = _park_connection(monkeypatch, prior)
    fresh, _ = _connection(monkeypatch, homes)
    fresh_ready = _park_connection(monkeypatch, fresh)
    path = homes / "clawchat" / "gateway.lock"
    identity = instance_lock.identity_lock_path(homes, prior.config.user_id)
    original_stop = prior.stop
    observed = []

    async def check_stop():
        for lock_path in (path, identity):
            observed.append(_probe(lock_path)["outcome"])
        await original_stop()
        for lock_path in (path, identity):
            observed.append(_probe(lock_path)["outcome"])

    try:
        await prior.start()
        await asyncio.wait_for(ready.wait(), 1)
        monkeypatch.setattr(prior, "stop", check_stop)
        await fresh.start()
        await asyncio.wait_for(fresh_ready.wait(), 1)
        assert observed == ["held"] * 4
        assert _probe(path)["outcome"] == _probe(identity)["outcome"] == "held"
    finally:
        await original_stop()
        await fresh.stop()
    assert _probe(path)["outcome"] == _probe(identity)["outcome"] == "acquired"


@pytest.mark.asyncio
async def test_activation_poll_locks_identity_before_refresh(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes, user="")
    monkeypatch.setattr(conn, "_load_startup_activation_credentials", lambda: False)
    conn._cfg = replace(conn.config, token="", refresh_token="", owner_user_id="")
    credentials = ActivationCredentials(access_token="new-token", refresh_token="new-refresh",
                                        user_id="usr_activated", owner_user_id="usr_owner", activated_at=1)
    conn._store = SimpleNamespace(get_activation_credentials=lambda **k: credentials)
    monkeypatch.setattr(conn, "_refresh_rejection_fingerprints", lambda: set())
    connected = _park_connection(monkeypatch, conn)
    path = instance_lock.identity_lock_path(homes, credentials.user_id)
    with _holder(path, homes / "profiles" / "other", "identity") as proc:
        try:
            await conn.start()
            await _wait_for(lambda: conn._state == ConnectionState.STANDBY)
            manager.refresh.assert_not_awaited()
            assert not connected.is_set()
            proc.kill()
            proc.wait(timeout=10)
            await asyncio.wait_for(connected.wait(), 2)
            manager.refresh.assert_awaited_once()
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_identity_change_releases_old_and_blocks_new(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes, user="usr_old")
    try:
        assert await conn._wait_for_instance_locks()
        old_path = instance_lock.identity_lock_path(homes, "usr_old")
        new_path = instance_lock.identity_lock_path(homes, "usr_new")
        with _holder(new_path, homes / "profiles" / "other", "identity"):
            conn._cfg = replace(conn.config, user_id="usr_new")
            assert (await conn.reactive_refresh()).status == "skipped"
            manager.refresh.assert_not_awaited()
            wait = asyncio.create_task(conn._wait_for_instance_locks())
            try:
                await _wait_for(lambda: conn._state == ConnectionState.STANDBY)
                assert _probe(old_path)["outcome"] == "acquired"
            finally:
                wait.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await wait
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_unsupported_locks_allow_start_and_refresh(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes)
    ready = _park_connection(monkeypatch, conn)
    monkeypatch.setattr(instance_lock, "acquire", lambda path, **kw: instance_lock.LockResult("unsupported", path, kw["kind"], {}))
    try:
        await conn.start()
        await asyncio.wait_for(ready.wait(), 1)
        manager.refresh.assert_awaited_once()
        assert conn.instance_lock_status()["state"] == "unsupported"
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_direct_socket_and_refresh_calls_require_ownership(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes)
    conn._refresh_manager = manager
    conn._instance_lock_enforced = True  # As after start(), before the supervisor locks.
    socket = AsyncMock()
    monkeypatch.setattr(connection, "_ws_connect", socket)
    assert await conn._run_one_connection() is False
    assert (await conn.reactive_refresh()).status == "skipped"
    manager.refresh.assert_not_awaited()
    socket.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_accepts_standby(homes, monkeypatch):
    from clawchat_gateway.adapter import ClawChatAdapter

    conn, _ = _connection(monkeypatch, homes)
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._connection = conn
    adapter._clawchat_config = conn.config
    adapter._auth_failed = False
    monkeypatch.setattr(adapter, "_spawn_plugin_report", lambda **kw: None)
    conn._on_state_change = adapter._on_state_change
    with _holder(homes / "clawchat" / "gateway.lock"):
        try:
            assert await asyncio.wait_for(adapter.connect(), 1)
            assert conn._state == ConnectionState.STANDBY
            assert not adapter._auth_failed
            assert ClawChatConnection._live_supervisors[conn._supervisor_key] is conn
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_refresh_worker_retains_lock_until_request_finishes(homes, monkeypatch):
    import threading
    from clawchat_gateway.api_client import ClawChatApiClient, RefreshResult

    conn, _ = _connection(monkeypatch, homes)
    entered, finished = threading.Event(), threading.Event()

    def request(*args):
        entered.set()
        assert finished.wait(timeout=5)
        return RefreshResult("new-access", "new-refresh")

    monkeypatch.setattr(ClawChatApiClient, "_auth_refresh_sync", request)
    assert await conn._wait_for_instance_locks()
    client = conn._build_refresh_client()
    results = []
    worker = threading.Thread(target=lambda: results.append(client._auth_refresh_sync("refresh", "device")))
    worker.start()
    path = homes / "clawchat" / "gateway.lock"
    try:
        await _wait_for(entered.is_set)
        await conn.stop()
        assert _probe(path)["outcome"] == "held"
        finished.set()
        worker.join(timeout=2)
        assert not worker.is_alive() and results
        assert _probe(path)["outcome"] == "acquired"
        with pytest.raises(Exception, match="released"):
            client._auth_refresh_sync("refresh", "device")
    finally:
        finished.set()
        worker.join(timeout=5)
        await conn.stop()


@pytest.mark.asyncio
async def test_refresh_rechecks_identity_after_credential_watch(homes, monkeypatch):
    conn, manager = _connection(monkeypatch, homes)
    conn._refresh_manager = manager
    conn._instance_lock_enforced = True
    assert await conn._wait_for_instance_locks()

    async def changed():
        conn._cfg = replace(conn.config, user_id="usr_another")
        return "ok"

    monkeypatch.setattr(conn, "_recheck_activation_source", changed)
    try:
        assert (await conn.reactive_refresh()).status == "skipped"
        manager.refresh.assert_not_awaited()
        assert not await conn._refresh_should_continue(conn.config)
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_cancelled_supersede_does_not_strand_references(homes, monkeypatch):
    prior, _ = _connection(monkeypatch, homes)
    ready = _park_connection(monkeypatch, prior)
    fresh, _ = _connection(monkeypatch, homes)
    stopping = asyncio.Event()
    original_stop = prior.stop

    async def stalled_stop():
        stopping.set()
        await asyncio.Event().wait()

    await prior.start()
    try:
        await asyncio.wait_for(ready.wait(), 1)
        monkeypatch.setattr(prior, "stop", stalled_stop)
        replacement = asyncio.create_task(fresh.start())
        await asyncio.wait_for(stopping.wait(), 1)
        replacement.cancel()
        with pytest.raises(asyncio.CancelledError):
            await replacement
        assert not fresh._instance_locks
    finally:
        await original_stop()
        await fresh.stop()
    assert _probe(homes / "clawchat" / "gateway.lock")["outcome"] == "acquired"


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock")
def test_holder_record_write_failure_keeps_the_lock(tmp_path, monkeypatch, caplog):
    def full_disk(*args):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(instance_lock.os, "ftruncate", full_disk)
    path = tmp_path / "gateway.lock"
    with caplog.at_level(logging.WARNING):
        result = instance_lock.acquire(path, kind="profile", profile_home=tmp_path)
    try:
        assert result.outcome == "acquired"
        assert "holder record write failed" in caplog.text
        assert _probe(path)["outcome"] == "held"
    finally:
        result.release()
    assert _probe(path)["outcome"] == "acquired"


@pytest.mark.asyncio
async def test_cancelled_stop_still_releases_locks(homes, monkeypatch):
    conn, _ = _connection(monkeypatch, homes)
    ready = _park_connection(monkeypatch, conn)
    closing = asyncio.Event()

    class _StuckSocket:
        async def close(self):
            closing.set()
            await asyncio.Event().wait()

    await conn.start()
    try:
        await asyncio.wait_for(ready.wait(), 1)
        conn._ws = _StuckSocket()
        stopper = asyncio.create_task(conn.stop())
        await asyncio.wait_for(closing.wait(), 1)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert not conn._instance_locks
        assert _probe(homes / "clawchat" / "gateway.lock")["outcome"] == "acquired"
    finally:
        conn._ws = None
        await conn.stop()


@pytest.mark.asyncio
async def test_unstarted_connection_is_not_gated(homes, monkeypatch):
    # Only start() runs the supervisor that takes the locks; a never-started
    # connection keeps the old direct-call behaviour.
    conn, manager = _connection(monkeypatch, homes)
    assert conn._instance_locks_ready()
    assert conn.instance_lock_status()["state"] == "unlocked"
