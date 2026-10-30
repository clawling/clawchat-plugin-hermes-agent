"""Installation conflicts are terminal until an explicit local reclaim."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from dataclasses import replace
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from websockets.asyncio.server import serve

from clawchat_gateway import connection, install_identity as identity
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.protocol import build_connect_request
from clawchat_gateway.standalone_send import standalone_send
from clawchat_gateway.token_refresh import RefreshManager


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    monkeypatch.setattr(connection, "SUPERSEDED_CHECK_SECONDS", 0.03)
    monkeypatch.setattr(identity, "_host_device_id", lambda: "test-machine")
    monkeypatch.setattr(identity.socket, "gethostname", lambda: "test-host")
    identity._cache.clear()
    identity._failed_writes.clear()
    identity._failed_generations.clear()
    identity._warned_lock_paths.clear()
    yield
    identity._cache.clear()
    identity._failed_writes.clear()
    identity._failed_generations.clear()


def config(url="ws://127.0.0.1:1"):
    return ClawChatConfig(websocket_url=url, token="test-access", refresh_token="test-refresh",
                         user_id="usr_test_agent", owner_user_id="usr_test_owner",
                         reconnect_initial_delay_ms=10, reconnect_jitter_ratio=0)


def make_conn(url="ws://127.0.0.1:1", **kwargs):
    return ClawChatConnection(config(url), on_message=AsyncMock(), **kwargs)


def blocked(home, user_id="usr_test_agent"):
    state = {"user_id": user_id, "device_id": "test-device", "install_id": "a" * 24,
             "superseded_at": "2026-01-01T00:00:00+00:00", "by_client_version": "test-version",
             "owner_notice": {"status": "pending"}}
    assert identity.write_superseded(home, state)
    return state


def _external_reclaim(home):
    """Simulate reclaim in another process without touching this process's cache."""
    home = home.resolve()
    with identity._state_guard(home):
        identity._bump_reclaim_generation_unlocked(home)
        identity.superseded_path(home).unlink(missing_ok=True)


async def until(predicate, timeout=3):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.005)
    await asyncio.wait_for(wait(), timeout)


def test_identity_persisted_stable_and_private(tmp_path):
    first = identity.get_install_id(tmp_path)
    path = tmp_path / "clawchat" / "install_seed"
    assert re.fullmatch(r"[0-9A-Za-z_-]{8,64}", first)
    assert re.fullmatch(r"[0-9a-f]{32}\n", path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    identity._cache.clear()
    assert identity.get_install_id(tmp_path) == first
    assert "test-host" not in first


@pytest.mark.parametrize("difference", ["path", "host", "machine"])
def test_copied_profile_is_distinct(monkeypatch, tmp_path, difference):
    home = tmp_path / "original"
    original = identity.get_install_id(home)
    if difference == "path":
        copy = tmp_path / "copy"
        shutil.copytree(home, copy)
        home = copy
    elif difference == "host":
        monkeypatch.setattr(identity.socket, "gethostname", lambda: "another-host")
    else:
        monkeypatch.setattr(identity, "_host_device_id", lambda: "another-machine")
    identity._cache.clear()
    assert identity.get_install_id(home) != original


@pytest.mark.parametrize("failure", ["unwritable", "invalid"])
def test_identity_failure_falls_back_once(monkeypatch, tmp_path, caplog, failure):
    if failure == "unwritable":
        monkeypatch.setattr(identity.os, "open", lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError()))
    else:
        path = tmp_path / "clawchat" / "install_seed"
        path.parent.mkdir()
        path.write_text("bad seed")
    assert identity.get_install_id(tmp_path) is None
    assert identity.get_install_id(tmp_path) is None
    assert sum("identity unavailable" in r.message for r in caplog.records) == 1
    payload = build_connect_request(frame_id="trace_test", token="test-token", nonce="nonce",
                                    install_id=identity.get_install_id(tmp_path),
                                    capabilities=connection.build_connect_capabilities())['payload']
    assert "install_id" not in payload
    assert "install_conflict" not in payload["capabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sibling", [False, True])
async def test_connect_payload(tmp_path, sibling):
    conn = make_conn(connect_device_id="test-standalone" if sibling else None)
    conn._ws = SimpleNamespace(send=AsyncMock())
    await conn._handle_challenge({"payload": {"nonce": "test-nonce"}})
    payload = json.loads(conn._ws.send.call_args.args[0])["payload"]
    if sibling:
        assert "install_id" not in payload
        assert "install_conflict" not in payload["capabilities"]
    else:
        assert payload["install_id"] == identity.get_install_id(tmp_path)
        assert payload["capabilities"]["install_conflict"] is True


@asynccontextmanager
async def peer(*, prehello=False, reason=None, notice_ack=False, supersede=True, close_code=4010):
    requests = []
    closes = 0
    async def handler(ws):
        nonlocal closes
        await ws.send(json.dumps({"event": "connect.challenge", "payload": {"nonce": "test-nonce"}}))
        request = json.loads(await ws.recv())
        requests.append(request)
        sibling = request["payload"]["device_id"].endswith("-standalone")
        close = supersede and closes == 0 and not sibling
        if close:
            closes += 1
        if not (prehello and close):
            await ws.send(json.dumps({"type": "res", "requestId": request["trace_id"],
                                      "payload": {"type": "hello-ok"}}))
        if close:
            # Let the main task process hello-ok first in the READY variant.
            if not prehello:
                await asyncio.sleep(0.02)
            await ws.close(code=close_code, reason=reason if reason is not None else json.dumps({
                "reason": "superseded_by_other_install", "by_client_version": "winner-test"}))
        elif sibling and notice_ack:
            message = json.loads(await ws.recv())
            await ws.send(json.dumps({"event": "message.ack", "payload": {"message_id": message["payload"]["message_id"]},
                                      "trace_id": message["trace_id"]}))
            await ws.wait_closed()
        else:
            await ws.wait_closed()
    async with serve(handler, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        yield url, requests


@pytest.mark.asyncio
@pytest.mark.parametrize("prehello", [False, True], ids=["ready", "before-hello"])
@pytest.mark.parametrize("reason", [None, '{"reason":"superseded_by_other_install"}', "malformed"])
async def test_terminal_close_stays_alive_without_refresh(monkeypatch, tmp_path, prehello, reason):
    refresh = AsyncMock(side_effect=AssertionError("refresh forbidden"))
    monkeypatch.setattr(RefreshManager, "refresh", refresh)
    async with peer(prehello=prehello, reason=reason) as (url, requests):
        conn = make_conn(url)
        await conn.start()
        try:
            await until(lambda: conn.is_superseded)
            await until(lambda: identity.read_superseded(tmp_path)["owner_notice"]["status"] != "pending")
            state = identity.read_superseded(tmp_path)
            assert state["user_id"] == conn.config.user_id
            assert state["device_id"] == conn.session_device_id()
            assert state["install_id"] == identity.get_install_id(tmp_path)
            assert state["superseded_at"].endswith("+00:00")
            assert state["by_client_version"] == ("winner-test" if reason is None else None)
            assert state["owner_notice"]["status"] == "skipped"
            assert identity.superseded_path(tmp_path).stat().st_mode & 0o777 == 0o600
            assert await conn.wait_until_ready(timeout=0.05) is False
            assert conn.is_superseded and conn._settled_event.is_set()
            assert conn.instance_lock_status()["state"] == "acquired"
            assert identity.install_status(tmp_path)["instance_lock"]["state"] == "acquired"
            assert identity.install_status(tmp_path)["is_superseded"] is True
            assert set(conn._instance_locks) == {"profile", "identity"}
            assert not conn._supervisor_task.done()
            assert conn._takeover_streak == 0
            await asyncio.sleep(0.12)
            assert len(requests) == 1
            assert (await conn.reactive_refresh()).status == "skipped"
            refresh.assert_not_called()
            assert conn.config.token == "test-access" and not conn._auth_failed
        finally:
            await conn.stop()
        assert conn._state == ConnectionState.CLOSED


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["same", "unknown-user", "other", "corrupt"])
async def test_restart_blocks_before_connect_or_startup_refresh(monkeypatch, tmp_path, state):
    blocked(tmp_path, "usr_other_agent" if state == "other" else "usr_test_agent")
    if state == "corrupt":
        identity.superseded_path(tmp_path).write_text("broken json")
    dial = AsyncMock(side_effect=RuntimeError("dial intercepted"))
    refresh = AsyncMock()
    monkeypatch.setattr(connection, "_ws_connect", dial)
    # Would force a startup refresh if the superseded gate were too late.
    monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a, **_kw: True)
    monkeypatch.setattr(RefreshManager, "refresh", refresh)
    conn = make_conn()
    if state == "unknown-user":
        from dataclasses import replace
        conn._cfg = replace(conn._cfg, user_id="")
    if state == "other":
        # This case proves the disk record does not block a newly bound agent.
        monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a, **_kw: False)
    await conn.start()
    try:
        if state == "other":
            await until(lambda: dial.await_count > 0)
            assert not conn.is_superseded
        else:
            await until(lambda: conn.is_superseded)
            await asyncio.sleep(0.06)
            dial.assert_not_called()
            refresh.assert_not_called()
            assert conn._owner_notice_task is None
            assert set(conn._instance_locks) == {"profile"}
    finally:
        await conn.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cli", "slash", "external-cli"])
async def test_reclaim_reconnects(monkeypatch, tmp_path, capsys, method):
    from clawchat_cli import main
    from clawchat_gateway.commands import handle_clawchat_reclaim_command
    blocked(tmp_path)
    if method == "slash":
        monkeypatch.setattr(connection, "SUPERSEDED_CHECK_SECONDS", 60)
    async with peer(supersede=False) as (url, requests):
        conn = make_conn(url)
        await conn.start()
        try:
            await until(lambda: conn.is_superseded)
            assert not requests
            if method == "slash":
                assert "已清除" in await handle_clawchat_reclaim_command("")
            elif method == "external-cli":
                # Emulate a separate process: no in-process wakeup is available.
                monkeypatch.setattr(ClawChatConnection, "wake_reclaimed", lambda *_a: None)
                assert main(["reclaim"]) == 0
            else:
                assert main(["reclaim"]) == 0
                assert "30" in capsys.readouterr().out
            await until(lambda: conn.is_ready, timeout=0.8)
            assert len(requests) == 1
        finally:
            await conn.stop()


def test_cli_reclaim_no_state_and_status(tmp_path, capsys):
    from clawchat_cli import main
    assert main(["reclaim"]) == 0
    assert "未处于被取代" in capsys.readouterr().out
    blocked(tmp_path)
    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["install_id"] is None
    assert status["superseded"]["by_client_version"] == "test-version"
    assert status["superseded"]["owner_notice"]["status"] == "pending"
    assert status["is_superseded"] is True
    assert status["instance_lock"]["state"] == "unknown"
    assert "instance_lock_holder_record" in status


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sent", "failed", "skipped"])
async def test_notice_once_even_after_repeat_and_restart(monkeypatch, tmp_path, outcome):
    import clawchat_gateway.standalone_send as sender
    send = AsyncMock(return_value={"success": True, "message_id": "msg_test"})
    if outcome == "failed":
        send.side_effect = TimeoutError()
    monkeypatch.setattr(sender, "standalone_send", send)
    conn = make_conn()
    if outcome != "skipped":
        conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    conn._last_close_reason = '{"by_client_version":"winner"}'
    await conn._handle_install_superseded()
    await conn._owner_notice_task
    await conn._handle_install_superseded()
    state = identity.read_superseded(tmp_path)
    assert state["owner_notice"]["status"] == outcome
    if outcome == "sent":
        assert state["owner_notice"]["message_id"] == "msg_test"
        assert send.call_args.kwargs["_allow_superseded_notice"] is True
        assert "hermes clawchat reclaim" in send.call_args.args[2]
        assert "/clawchat-reclaim" in send.call_args.args[2]
    assert send.await_count == (0 if outcome == "skipped" else 1)
    await conn.stop()
    restarted = make_conn()
    await restarted.start()
    try:
        await until(lambda: restarted.is_superseded)
        assert restarted._owner_notice_task is None
        assert send.await_count == (0 if outcome == "skipped" else 1)
    finally:
        await restarted.stop()


@pytest.mark.asyncio
async def test_standalone_blocked_without_constructing_connection(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    blocked(tmp_path)
    constructor = AsyncMock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(sender, "ClawChatConnection", constructor)
    result = await standalone_send(config(), "cnv_test", "test message")
    assert "reclaim" in result["error"]
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_notice_real_sibling_connection_never_refreshes(monkeypatch, tmp_path):
    refresh = AsyncMock(side_effect=AssertionError("notice must not refresh"))
    monkeypatch.setattr(RefreshManager, "refresh", refresh)
    async with peer(notice_ack=True) as (url, requests):
        conn = make_conn(url)
        conn._store = SimpleNamespace(
            get_activation_credentials=lambda **_kw: None,
            get_revoked_refresh_fingerprint=lambda **_kw: None,
            get_activation_conversation=lambda **_kw: "cnv_" + "0" * 26,
        )
        await conn.start()
        try:
            await until(lambda: conn.is_superseded)
            # Force a near-expiry token only after the main connection is displaced.
            monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a, **_kw: True)
            await conn._owner_notice_task
            result = identity.read_superseded(tmp_path)["owner_notice"]
            assert result["status"] == "sent"
            assert len(requests) == 2
            assert "install_id" not in requests[1]["payload"]
            assert "install_conflict" not in requests[1]["payload"]["capabilities"]
            refresh.assert_not_called()
            assert conn.is_superseded and not conn._supervisor_task.done()
            await conn._handle_install_superseded()
            await asyncio.sleep(0.05)
            assert len(requests) == 2
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_refresh_skipped_and_no_logout_or_credential_clear(monkeypatch, tmp_path):
    blocked(tmp_path)
    for sibling in (None, "test-standalone"):
        conn = make_conn(connect_device_id=sibling)
        conn._refresh_manager = SimpleNamespace(refresh=AsyncMock())
        logout = AsyncMock()
        conn._on_auth_logout = logout
        assert (await conn._attempt_refresh(close_ws_on_success=True)).status == "skipped"
        conn._refresh_manager.refresh.assert_not_called()
        assert await conn._persist_auth_logout("test failure") is False
        assert await conn._persist_refresh_rejection("test-refresh") is False
        logout.assert_not_called()
        assert conn.config.token == "test-access"


@pytest.mark.asyncio
async def test_write_failure_still_blocks_and_reclaim_wakes(monkeypatch, tmp_path):
    conn = make_conn()
    real_replace = identity.os.replace
    def fail_replace(src, dst, *a):
        if Path(dst).name == "install_superseded.json":
            raise OSError("test write failure")
        return real_replace(src, dst, *a)
    monkeypatch.setattr(identity.os, "replace", fail_replace)
    conn._last_close_reason = "not-json"
    await conn._handle_install_superseded()
    assert conn._owner_notice_task is None
    assert conn.is_superseded
    assert not identity.superseded_path(tmp_path).exists()
    sibling = make_conn(connect_device_id="test-standalone")
    assert (await sibling.reactive_refresh()).status == "skipped"
    waiter = asyncio.create_task(conn._wait_if_superseded())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert identity.clear_superseded(tmp_path)
    conn._reclaim_event.set()
    assert await asyncio.wait_for(waiter, 0.5)
    assert not conn._superseded
    await conn.stop()


@pytest.mark.asyncio
async def test_stop_cancels_notice_and_reclaim_does_not_resurrect_state(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    entered = asyncio.Event()
    async def delayed(*_a, **_kw):
        entered.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(sender, "standalone_send", delayed)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    await conn._handle_install_superseded()
    await entered.wait()
    task = conn._owner_notice_task
    await conn.stop()
    assert task.cancelled()
    identity.clear_superseded(tmp_path)
    monkeypatch.setattr(sender, "standalone_send", AsyncMock(return_value={"success": True}))
    await conn._send_superseded_notice({"superseded_at": "old-event"})
    assert identity.read_superseded(tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fresh", "repair", "new-account", "clear-failed"])
async def test_successful_activation_clears_state(monkeypatch, tmp_path, caplog, mode):
    from clawchat_gateway import activate as activation
    blocked(tmp_path)
    monkeypatch.setattr(activation, "_load_config", lambda: (str(tmp_path / "config.yaml"), {}))
    monkeypatch.setattr(activation, "_read_existing_user_id", lambda *_a, **_kw: "")
    monkeypatch.setattr(activation, "_resolve_activation_device_id", lambda **_kw: "test-device")
    monkeypatch.setattr(activation.ClawChatApiClient, "agents_connect_check", AsyncMock(return_value={}))
    result = {"agent": {"id": "agt_test", "user_id": "usr_test_agent", "owner_id": "usr_test_owner"},
              "conversation": {"id": "cnv_test"}, "access_token": "test-access", "refresh_token": "test-refresh"}
    monkeypatch.setattr(activation, "agents_connect_with_retry", AsyncMock(return_value=result))
    monkeypatch.setattr(activation, "persist_activation", lambda **_kw: {"user_id": "usr_test_agent"})
    monkeypatch.setattr(activation, "get_clawchat_store", lambda: SimpleNamespace(upsert_activation=lambda **_kw: True))
    if mode == "clear-failed":
        monkeypatch.setattr(identity, "clear_superseded", Mock(side_effect=identity.StateLockUnavailable()))
        wake = Mock()
        monkeypatch.setattr(ClawChatConnection, "wake_reclaimed", wake)
    payload = await activation.activate("TESTCODE", base_url="https://example.invalid", repair=mode == "repair",
                                        new_account=mode == "new-account")
    assert payload["user_id"] == "usr_test_agent"
    if mode == "clear-failed":
        assert identity.read_superseded(tmp_path) is not None
        assert "activation succeeded but reclaim failed" in caplog.text
        assert "retry with hermes clawchat reclaim" in caplog.text
        wake.assert_not_called()
    else:
        assert identity.read_superseded(tmp_path) is None


@pytest.mark.asyncio
async def test_actual_connect_omits_identity_when_seed_creation_fails(monkeypatch, tmp_path):
    conn = make_conn()
    conn._ws = SimpleNamespace(send=AsyncMock())
    # A regular file in place of the directory fails seed creation without affecting devices.
    (tmp_path / "clawchat").write_text("not a directory")
    await conn._handle_challenge({"payload": {"nonce": "test-nonce"}})
    payload = json.loads(conn._ws.send.call_args.args[0])["payload"]
    assert "install_id" not in payload
    assert "install_conflict" not in payload["capabilities"]


@pytest.mark.asyncio
async def test_adapter_counts_superseded_as_started():
    from clawchat_gateway.adapter import ClawChatAdapter
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._spawn_plugin_report = lambda **_kw: None
    adapter._clawchat_config = config()
    adapter._connection = SimpleNamespace(start=AsyncMock(), wait_until_ready=AsyncMock(return_value=False),
                                          is_superseded=True, is_standby=False)
    assert await adapter.connect() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [None, "zh-CN", "es-ES"])
async def test_notice_language(monkeypatch, tmp_path, locale):
    import clawchat_gateway.standalone_send as sender
    send = AsyncMock(return_value={"error": "test failure"})
    monkeypatch.setattr(sender, "standalone_send", send)
    conn = make_conn(owner_locale=lambda: locale)
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    await conn._handle_install_superseded()
    await conn._owner_notice_task
    text = send.call_args.args[2]
    assert ("这个 agent" in text) == (locale in (None, "zh-CN"))
    assert ("This agent" in text) == (locale in (None, "es-ES"))
    assert identity.read_superseded(tmp_path)["owner_notice"]["status"] == "failed"
    await conn.stop()


@pytest.mark.asyncio
async def test_notice_total_timeout_is_failed_without_retry(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    async def hang(*_a, **_kw):
        await asyncio.Event().wait()
    send = AsyncMock(side_effect=hang)
    monkeypatch.setattr(sender, "standalone_send", send)
    monkeypatch.setattr(connection, "HANDSHAKE_TIMEOUT_SECONDS", -9.98)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    await conn._handle_install_superseded()
    await asyncio.wait_for(conn._owner_notice_task, 0.5)
    result = identity.read_superseded(tmp_path)["owner_notice"]
    assert result == {"status": "failed", "error_type": "TimeoutError"}
    await conn._handle_install_superseded()
    assert send.await_count == 1
    await conn.stop()


@pytest.mark.asyncio
async def test_terminal_reader_latch_blocks_sibling_before_disk_write(tmp_path):
    conn = make_conn()
    sibling = make_conn(connect_device_id="test-standalone")
    conn._superseded = True
    ClawChatConnection._live_supervisors[conn._supervisor_key] = conn
    try:
        assert not identity.superseded_path(tmp_path).exists()
        assert (await sibling.reactive_refresh()).status == "skipped"
    finally:
        ClawChatConnection._live_supervisors.pop(conn._supervisor_key, None)


@pytest.mark.asyncio
async def test_reclaim_is_profile_scoped(monkeypatch, tmp_path):
    from clawchat_gateway.commands import handle_clawchat_reclaim_command
    first = make_conn()
    blocked(tmp_path)
    other_home = tmp_path / "another-profile"
    monkeypatch.setattr(connection, "hermes_home", lambda: other_home)
    other = make_conn()
    blocked(other_home)
    await first.start()
    await other.start()
    try:
        await until(lambda: first.is_superseded and other.is_superseded)
        # Observe successful recovery without opening another network connection.
        first._run_one_connection = AsyncMock(return_value=False)
        assert "已清除" in await handle_clawchat_reclaim_command("")
        await until(lambda: not first._superseded)
        assert identity.read_superseded(tmp_path) is None
        assert identity.read_superseded(other_home) is not None
        assert other.is_superseded
    finally:
        await first.stop()
        await other.stop()


def test_failed_write_over_old_identity_still_blocks(monkeypatch, tmp_path):
    blocked(tmp_path, "usr_previous_agent")
    original = identity.superseded_path(tmp_path).read_bytes()
    state = {"user_id": "usr_test_agent", "owner_notice": {"status": "pending"}}
    real_replace, broken = identity.os.replace, [True]

    def replace_file(*args):
        if broken[0]:
            raise OSError("read only")
        return real_replace(*args)

    monkeypatch.setattr(identity.os, "replace", replace_file)
    recorded, created = identity.record_superseded(tmp_path, state)
    assert recorded == state and not created
    assert identity.read_superseded(tmp_path, "usr_test_agent") == state
    # Memory blocks this identity without changing the previous identity's file.
    assert identity.release_reclaimed_cache(tmp_path) is False
    assert identity.read_superseded(tmp_path, "usr_test_agent") == state
    # Disk recovery never causes an old cached creation to be written back.
    broken[0] = False
    assert identity.release_reclaimed_cache(tmp_path) is False
    assert identity.read_superseded(tmp_path, "usr_test_agent") == state
    assert identity.superseded_path(tmp_path).read_bytes() == original
    _external_reclaim(tmp_path)
    assert identity.release_reclaimed_cache(tmp_path) is True
    assert not identity._failed_writes
    assert not identity._failed_generations
    assert identity.read_superseded(tmp_path, "usr_test_agent") is None
    assert not identity.superseded_path(tmp_path).exists()


@pytest.mark.asyncio
async def test_legacy_takeover_still_reconnects_with_floor(monkeypatch, tmp_path):
    monkeypatch.setattr(connection, "TAKEOVER_BACKOFF_FLOOR_SECONDS", 0.05)
    reconnect_started = []
    async def observe(state):
        if state == ConnectionState.RECONNECTING:
            reconnect_started.append(asyncio.get_running_loop().time())
    async with peer(close_code=4001, reason="legacy takeover") as (url, requests):
        conn = make_conn(url, on_state_change=observe)
        await conn.start()
        try:
            await until(lambda: len(requests) == 2 and conn.is_ready)
            assert conn._takeover_streak == 1
            assert asyncio.get_running_loop().time() - reconnect_started[0] >= 0.05
            assert identity.read_superseded(tmp_path) is None
            assert not conn.is_superseded
            assert conn._owner_notice_task is None
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_stop_cancels_notices_from_consecutive_conflicts(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    calls = []
    async def hang(*_a, **_kw):
        calls.append(asyncio.current_task())
        await asyncio.Event().wait()
    monkeypatch.setattr(sender, "standalone_send", hang)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    await conn._handle_install_superseded()
    await until(lambda: len(calls) == 1)
    old_notice = conn._owner_notice_task
    identity.clear_superseded(tmp_path)
    # The standby loop resets the latch on reclaim before any later conflict.
    conn._superseded, conn._superseded_snapshot = False, None
    await conn._handle_install_superseded()
    await until(lambda: len(calls) == 2)
    latest_notice = conn._owner_notice_task
    await conn.stop()
    assert old_notice.cancelled() and latest_notice.cancelled()
    assert all(task.cancelled() for task in calls)


def test_status_on_empty_profile_creates_nothing(monkeypatch, tmp_path, capsys):
    from clawchat_cli import main
    home = tmp_path / "empty-profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    cache = dict(identity._cache)
    assert main(["status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["install_id"] is None
    assert result["is_superseded"] is False
    assert not home.exists()
    assert identity._cache == cache


@pytest.mark.parametrize("seed", ["", "invalid", "a" * 32 + "\n"])
def test_peek_reads_seed_without_using_or_changing_cache(tmp_path, seed):
    path = tmp_path / "clawchat" / "install_seed"
    path.parent.mkdir()
    path.write_text(seed)
    identity._cache[tmp_path.resolve()] = "cached-value"
    cache = dict(identity._cache)
    result = identity.peek_install_id(tmp_path)
    if seed.startswith("a"):
        assert re.fullmatch(r"[0-9a-f]{24}", result)
        assert result != "cached-value"
        assert identity.install_status(tmp_path)["install_id"] == result[:8]
    else:
        assert result is None
    assert path.read_text() == seed
    assert identity._cache == cache
    assert list(path.parent.iterdir()) == [path]


def test_existing_seed_never_overwritten(monkeypatch, tmp_path):
    path = tmp_path / "clawchat" / "install_seed"
    path.parent.mkdir()
    path.write_text("a" * 32 + "\n")
    before = path.read_bytes()
    monkeypatch.setattr(identity.os, "link", lambda *_a: pytest.fail("existing seed must be read directly"))
    assert identity.get_install_id(tmp_path) is not None
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_seed_published_only_after_complete_write_and_fsync(monkeypatch, tmp_path):
    path = tmp_path / "clawchat" / "install_seed"
    link = identity.os.link
    fsync = identity.os.fsync
    synced = []
    def sync(fd):
        fsync(fd)
        synced.append(fd)
    def publish(source, target):
        assert synced
        assert re.fullmatch(r"[0-9a-f]{32}\n", Path(source).read_text())
        if os.name != "nt":
            assert Path(source).stat().st_mode & 0o777 == 0o600
        assert not path.exists()
        link(source, target)
    monkeypatch.setattr(identity.os, "fsync", sync)
    monkeypatch.setattr(identity.os, "link", publish)
    assert identity.get_install_id(tmp_path) is not None
    assert list(path.parent.iterdir()) == [path]


def test_concurrent_seed_publisher_uses_existing_winner(monkeypatch, tmp_path):
    path = tmp_path / "clawchat" / "install_seed"
    def winner_publishes_first(*_a):
        path.write_text("b" * 32 + "\n")
        raise FileExistsError()
    monkeypatch.setattr(identity.os, "link", winner_publishes_first)
    assert identity.get_install_id(tmp_path) == identity.peek_install_id(tmp_path)
    assert path.read_text() == "b" * 32 + "\n"
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("failure", ["error", "unavailable", "not-implemented"])
def test_seed_link_failure_disables_identity(monkeypatch, tmp_path, failure):
    if failure == "unavailable":
        monkeypatch.delattr(identity.os, "link")
    else:
        def fail(*_a):
            if failure == "not-implemented":
                raise NotImplementedError()
            raise OSError("hard links unavailable")
        monkeypatch.setattr(identity.os, "link", fail)
    assert identity.get_install_id(tmp_path) is None
    assert not (tmp_path / "clawchat" / "install_seed").exists()
    assert list((tmp_path / "clawchat").iterdir()) == []


@pytest.mark.asyncio
async def test_unpaired_startup_does_not_acquire_identity_lock(monkeypatch, tmp_path):
    conn = make_conn()
    conn._cfg = replace(conn._cfg, token="", refresh_token="")
    acquire = connection.instance_lock.acquire
    kinds = []
    def observe(*args, **kwargs):
        kinds.append(kwargs["kind"])
        return acquire(*args, **kwargs)
    monkeypatch.setattr(connection.instance_lock, "acquire", observe)
    dial = AsyncMock()
    monkeypatch.setattr(connection, "_ws_connect", dial)
    await conn.start()
    try:
        await until(lambda: conn._activation_wait_logged)
        await asyncio.sleep(0.05)
        assert set(conn._instance_locks) == {"profile"}
        assert kinds and set(kinds) == {"profile"}
        assert not conn.is_superseded
        dial.assert_not_called()
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_restart_checks_superseded_before_loading_credentials(monkeypatch, tmp_path):
    blocked(tmp_path)
    conn = make_conn()
    monkeypatch.setattr(conn, "_load_startup_activation_credentials",
                        lambda: pytest.fail("blocked startup must not load credentials"))
    await conn.start()
    try:
        await until(lambda: conn.is_superseded)
        assert set(conn._instance_locks) == {"profile"}
    finally:
        await conn.stop()


@pytest.mark.asyncio
async def test_notice_result_does_not_revive_state_cleared_during_send(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    async def clear_during_send(*_a, **_kw):
        assert identity.clear_superseded(tmp_path)
        return {"success": True, "message_id": "msg_test"}
    monkeypatch.setattr(sender, "standalone_send", clear_during_send)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    await conn._handle_install_superseded()
    await conn._owner_notice_task
    assert identity.read_superseded(tmp_path) is None
    assert not identity.superseded_path(tmp_path).exists()
    assert (tmp_path / "clawchat" / "install_superseded.lock").exists()
    await conn.stop()


@pytest.mark.asyncio
async def test_simultaneous_connections_record_and_notify_once(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender
    send = AsyncMock(return_value={"success": True, "message_id": "msg_test"})
    monkeypatch.setattr(sender, "standalone_send", send)
    record = identity.record_superseded
    created = []
    def observe(home, state, **kwargs):
        result = record(home, state, **kwargs)
        created.append(result[1])
        return result
    monkeypatch.setattr(identity, "record_superseded", observe)
    first, second = make_conn(), make_conn()
    for conn in (first, second):
        conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    try:
        await asyncio.gather(first._handle_install_superseded(), second._handle_install_superseded())
        await until(lambda: identity.read_superseded(tmp_path)["owner_notice"]["status"] == "sent")
        assert sorted(created) == [False, True]
        assert send.await_count == 1
        assert first.is_superseded and second.is_superseded
    finally:
        await first.stop()
        await second.stop()


_STATE_CHILD = """
import json, sys, time
from pathlib import Path
from clawchat_gateway import install_identity as identity
home, action, payload = Path(sys.argv[1]), sys.argv[2], json.loads(sys.argv[3])
if action == "record":
    original_write = identity._write_superseded_unlocked
    def slow_write(*args):
        time.sleep(0.1)
        return original_write(*args)
    identity._write_superseded_unlocked = slow_write
    print("ready", flush=True)
    sys.stdin.readline()
    state, created = identity.record_superseded(home, payload)
    result = {"state": state, "created": created}
elif action == "update":
    original_read = identity._read_disk_superseded_unlocked
    def pause_after_read(*args):
        value = original_read(*args)
        print("read", flush=True)
        sys.stdin.readline()
        return value
    identity._read_disk_superseded_unlocked = pause_after_read
    result = identity.update_owner_notice(home, payload["superseded_at"], {"status": "sent"})
else:
    if action == "clear-observed":
        print("clearing", flush=True)
    result = identity.clear_superseded(home)
print(json.dumps(result), flush=True)
"""


def _state_command(home, action, payload):
    return [sys.executable, "-c", _STATE_CHILD, str(home), action, json.dumps(payload)]


def test_separate_processes_record_one_event(tmp_path):
    state = {"user_id": "usr_test_agent", "owner_notice": {"status": "pending"}}
    children = []
    try:
        for stamp in ("first-event", "second-event"):
            child = subprocess.Popen(_state_command(tmp_path, "record", {**state, "superseded_at": stamp}),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            children.append(child)
            assert child.stdout.readline().strip() == "ready"
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, errors = child.communicate(timeout=10)
            assert child.returncode == 0, errors
            results.append(json.loads(output))
        assert sorted(result["created"] for result in results) == [False, True]
        assert results[0]["state"] == results[1]["state"] == identity.read_superseded(tmp_path)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_clear_waits_for_entire_owner_notice_update(tmp_path):
    state = blocked(tmp_path)
    update = subprocess.Popen(_state_command(tmp_path, "update", state), stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    clear = None
    try:
        assert update.stdout.readline().strip() == "read"
        clear = subprocess.Popen(_state_command(tmp_path, "clear-observed", {}), stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        assert clear.stdout.readline().strip() == "clearing"
        # The updater has read the old state but not yet written its result.
        # Reclaim must wait for the whole transaction, then remove that result.
        with pytest.raises(subprocess.TimeoutExpired):
            clear.communicate(timeout=0.2)
        update.stdin.write("go\n")
        update.stdin.flush()
        output, errors = update.communicate(timeout=10)
        assert update.returncode == 0, errors
        assert json.loads(output) is True
        output, errors = clear.communicate(timeout=10)
        assert clear.returncode == 0, errors
        assert json.loads(output) is True
        assert identity.read_superseded(tmp_path) is None
        lock = tmp_path / "clawchat" / "install_superseded.lock"
        assert lock.exists()
        if os.name != "nt":
            assert lock.stat().st_mode & 0o777 == 0o600
    finally:
        for child in (update, clear):
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)


@pytest.mark.parametrize("failure", ["open", "lock", "import", "unsupported"])
def test_state_lock_unavailable_refuses_all_mutations(monkeypatch, tmp_path, caplog, failure):
    state = blocked(tmp_path)
    original = identity.superseded_path(tmp_path).read_bytes()
    if failure == "open":
        original_open = identity.os.open
        def fail_lock_open(path, *args, **kwargs):
            if Path(path).name == "install_superseded.lock":
                raise PermissionError()
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(identity.os, "open", fail_lock_open)
    else:
        def fail_lock(*_a, **_kw):
            errors = {"lock": OSError, "import": ImportError, "unsupported": NotImplementedError}
            raise errors[failure]("locking unavailable")
        monkeypatch.setattr(identity, "_lock_state_file", fail_lock)
    write = Mock(side_effect=AssertionError("unavailable lock must never allow a write"))
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    assert not identity.update_owner_notice(tmp_path, state["superseded_at"], {"status": "sent"})
    assert tmp_path.resolve() not in identity._failed_writes
    with pytest.raises(identity.StateLockUnavailable):
        identity.clear_superseded(tmp_path)
    # A durable event already blocks; it is returned, never re-cached in memory.
    assert identity.record_superseded(tmp_path, state) == (state, False)
    assert tmp_path.resolve() not in identity._failed_writes
    assert not identity.release_reclaimed_cache(tmp_path)
    assert identity.read_superseded(tmp_path) == state
    assert identity.install_status(tmp_path)["is_superseded"] is True
    assert identity.superseded_path(tmp_path).read_bytes() == original
    write.assert_not_called()
    assert "cannot lock superseded installation state" in caplog.text
    assert "cannot update superseded owner notice" in caplog.text
    assert sum("file lock unavailable" in record.message for record in caplog.records) == 1


def test_cached_creation_cannot_undo_other_process_reclaim(monkeypatch, tmp_path):
    state = {"user_id": "usr_test_agent", "superseded_at": "2026-01-01T00:00:00+00:00",
             "owner_notice": {"status": "pending"}}
    real_write, broken = identity._write_superseded_unlocked, [True]
    monkeypatch.setattr(identity, "_write_superseded_unlocked",
                        lambda home, value: False if broken[0] else real_write(home, value))
    # Process A cannot persist its creation and keeps it in memory.
    assert identity.record_superseded(tmp_path, state) == (state, False)
    assert identity.read_superseded(tmp_path) == state
    broken[0] = False
    # Process B persists the same identity's event; process C reclaims it
    # (simulated without touching A's in-memory cache).
    home = tmp_path.resolve()
    assert real_write(home, {**state, "superseded_at": "2026-01-01T00:00:01+00:00"})
    with identity._state_guard(home):
        identity._bump_reclaim_generation_unlocked(home)
        identity.superseded_path(home).unlink()
    # Only the generation change releases A's cache; it never re-creates an event.
    assert identity.release_reclaimed_cache(tmp_path) is True
    assert identity.read_superseded(tmp_path) is None
    assert home not in identity._failed_generations
    assert not identity.superseded_path(tmp_path).exists()


@pytest.mark.parametrize("location", ["disk", "memory", "none"])
def test_reclaim_fails_closed_when_generation_cannot_be_bumped(monkeypatch, tmp_path, location):
    state = _event_state()
    if location == "disk":
        state = blocked(tmp_path)
    elif location == "memory":
        with monkeypatch.context() as failure:
            failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
            assert identity.record_superseded(tmp_path, state) == (state, False)
    cached = dict(identity._failed_writes)
    generation = identity._read_reclaim_generation(tmp_path)
    monkeypatch.setattr(identity, "_bump_reclaim_generation_unlocked",
                        Mock(side_effect=OSError("read only")))
    # Without advancing the generation a reclaim could be overtaken by an event
    # still being recorded elsewhere, so it fails and changes nothing.
    with pytest.raises(OSError):
        identity.clear_superseded(tmp_path)
    assert identity.read_superseded(tmp_path) == (None if location == "none" else state)
    assert identity._failed_writes == cached
    assert identity._read_reclaim_generation(tmp_path) == generation
    assert identity.superseded_path(tmp_path).exists() == (location == "disk")


def test_reclaim_after_conflict_seen_wins_over_late_record(tmp_path):
    # The connection latched the conflict (snapshot), then a reclaim completed
    # while teardown was still running, before the event was recorded.
    snapshot = identity.read_reclaim_generation(tmp_path)
    home = tmp_path.resolve()
    home.joinpath("clawchat").mkdir(parents=True, exist_ok=True)
    assert identity.clear_superseded(tmp_path) is False
    state = _event_state()
    assert identity.record_superseded(tmp_path, state, snapshot=snapshot) == (state, False)
    assert identity.read_superseded(tmp_path) is None
    assert not identity._failed_writes


def test_lock_failure_over_durable_event_cannot_undo_reclaim(monkeypatch, tmp_path):
    state = blocked(tmp_path)
    unavailable = [True]
    real_lock = identity._lock_state_file

    def lock(fd, *, release=False):
        if unavailable[0] and not release:
            raise OSError("locking unavailable")
        return real_lock(fd, release=release)

    monkeypatch.setattr(identity, "_lock_state_file", lock)
    # Process A sees 4010 while the lock is unavailable: the disk event blocks it.
    assert identity.record_superseded(tmp_path, dict(state)) == (state, False)
    # The lock recovers and process B reclaims.
    unavailable[0] = False
    assert identity.clear_superseded(tmp_path) is True
    # There was no cached creation to release or write back.
    assert not identity.release_reclaimed_cache(tmp_path)
    assert identity.read_superseded(tmp_path) is None
    assert not identity.superseded_path(tmp_path).exists()


@pytest.mark.parametrize("unreadable", ["snapshot", "current"])
def test_unknown_generation_keeps_cache_without_writing(monkeypatch, tmp_path, unreadable):
    state, home = _event_state(), tmp_path.resolve()
    real_read = identity._read_reclaim_generation
    with monkeypatch.context() as failure:
        failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
        if unreadable == "snapshot":
            failure.setattr(identity, "_read_reclaim_generation", Mock(return_value=None))
        assert identity.record_superseded(home, state) == (state, False)
    snapshot = identity._failed_generations[home]
    assert snapshot == (None if unreadable == "snapshot" else "")
    _external_reclaim(home)
    if unreadable == "current":
        monkeypatch.setattr(identity, "_read_reclaim_generation", Mock(return_value=None))
    write = Mock(side_effect=AssertionError("a cached creation must never be written back"))
    lock = Mock(side_effect=AssertionError("cache release needs no state file lock"))
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    monkeypatch.setattr(identity, "_state_guard", lock)
    assert not identity.release_reclaimed_cache(home)
    assert identity._failed_writes[home] == state
    assert identity._failed_generations[home] == snapshot
    assert not identity.superseded_path(home).exists()
    if unreadable == "current":
        # A readable changed generation on a later check can release the cache.
        monkeypatch.setattr(identity, "_read_reclaim_generation", real_read)
        assert identity.release_reclaimed_cache(home)
        assert home not in identity._failed_writes and home not in identity._failed_generations
    write.assert_not_called()
    lock.assert_not_called()


@pytest.mark.parametrize("unknown_snapshot", [False, True])
def test_reclaim_between_fallback_disk_read_and_caching_never_revives_state(
    monkeypatch, tmp_path, unknown_snapshot,
):
    home, state = tmp_path.resolve(), _event_state("original-event")
    real_guard = identity._state_guard
    real_read = identity._read_superseded_unlocked
    real_generation = identity._read_reclaim_generation
    order = []

    def read_generation(home):
        order.append("generation")
        if unknown_snapshot and len(order) == 1:
            return None
        return real_generation(home)

    def unavailable(_home):
        order.append("lock")
        raise identity.StateLockUnavailable("locking unavailable")

    def read_before_other_processes(home, user_id=None):
        value = real_read(home, user_id)
        assert value is None
        order.append("disk")
        # After A reads no event, B persists another event and C reclaims it.
        # Use the real guard to simulate those processes without changing A's cache.
        with real_guard(home):
            assert identity._write_superseded_unlocked(home, _event_state("other-event"))
        with real_guard(home):
            identity._bump_reclaim_generation_unlocked(home)
            identity.superseded_path(home).unlink()
        return value

    monkeypatch.setattr(identity, "_read_reclaim_generation", read_generation)
    monkeypatch.setattr(identity, "_state_guard", unavailable)
    monkeypatch.setattr(identity, "_read_superseded_unlocked", read_before_other_processes)
    assert identity.record_superseded(home, state) == (state, False)
    assert order == ["generation", "lock", "disk"]
    assert identity._failed_generations[home] == (None if unknown_snapshot else "")
    assert not identity.superseded_path(home).exists()
    write = Mock(side_effect=AssertionError("reclaim must never be undone by an old cache"))
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    assert identity.release_reclaimed_cache(home) is (not unknown_snapshot)
    assert identity.release_reclaimed_cache(home) is False
    assert (home in identity._failed_writes) is unknown_snapshot
    assert (home in identity._failed_generations) is unknown_snapshot
    assert not identity.superseded_path(home).exists()
    write.assert_not_called()


def test_empty_reclaim_always_advances_generation(tmp_path):
    home = tmp_path.resolve()
    assert identity._read_reclaim_generation(home) == ""
    assert identity.clear_superseded(home) is False
    first = identity._read_reclaim_generation(home)
    assert first and re.fullmatch(r"[0-9a-f]{32}", first)
    assert identity.clear_superseded(home) is False
    assert identity._read_reclaim_generation(home) not in (None, first)
    assert not identity.superseded_path(home).exists()
    assert not identity._failed_writes and not identity._failed_generations


def test_repeated_record_preserves_cached_event_and_original_snapshot(monkeypatch, tmp_path):
    home, state = tmp_path.resolve(), _event_state("original-event")
    with monkeypatch.context() as failure:
        failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
        assert identity.record_superseded(home, state) == (state, False)
    snapshot = identity._failed_generations[home]
    _external_reclaim(home)
    assert identity.record_superseded(home, _event_state("later-event")) == (state, False)
    assert identity._failed_generations[home] == snapshot
    assert identity.release_reclaimed_cache(home)
    assert identity.read_superseded(home) is None
    assert not identity.superseded_path(home).exists()


def test_other_process_empty_reclaim_releases_memory_only_event(monkeypatch, tmp_path):
    state = _event_state()
    with monkeypatch.context() as failure:
        failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
        assert identity.record_superseded(tmp_path, state) == (state, False)
    # The other process has neither this cache nor a disk record, but still signals reclaim.
    result = subprocess.run(_state_command(tmp_path, "clear", {}), capture_output=True,
                            text=True, timeout=10, check=True)
    assert json.loads(result.stdout) is False
    assert identity._failed_writes[tmp_path.resolve()] == state
    assert identity.release_reclaimed_cache(tmp_path)
    assert identity.read_superseded(tmp_path) is None
    assert not identity.superseded_path(tmp_path).exists()


def test_notice_result_cannot_persist_memory_only_event(monkeypatch, tmp_path):
    home, state = tmp_path.resolve(), _event_state()
    with monkeypatch.context() as failure:
        failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
        assert identity.record_superseded(home, state) == (state, False)
    snapshot = identity._failed_generations[home]
    write = Mock(side_effect=AssertionError("notice must only update a disk event"))
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    assert not identity.update_owner_notice(home, state["superseded_at"], {"status": "sent"})
    assert not identity.superseded_path(home).exists()
    assert identity._failed_writes[home] == state
    assert identity._failed_generations[home] == snapshot
    write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cli", "slash"])
async def test_lock_unavailable_reclaim_fails_and_preserves_state(monkeypatch, tmp_path, capsys, command):
    from clawchat_cli import main
    from clawchat_gateway.commands import handle_clawchat_reclaim_command

    state = blocked(tmp_path)
    original = identity.superseded_path(tmp_path).read_bytes()
    monkeypatch.setattr(identity, "_lock_state_file", Mock(side_effect=OSError("locking unavailable")))
    wake = Mock()
    monkeypatch.setattr(ClawChatConnection, "wake_reclaimed", wake)
    if command == "cli":
        assert main(["reclaim"]) == 1
        assert "reclaim failed (StateLockUnavailable)" in capsys.readouterr().err
    else:
        assert "reclaim failed (StateLockUnavailable)" in await handle_clawchat_reclaim_command("")
    assert identity.superseded_path(tmp_path).read_bytes() == original
    assert identity.read_superseded(tmp_path) == state
    wake.assert_not_called()


@pytest.mark.asyncio
async def test_lock_unavailable_record_stays_alive_without_notice(monkeypatch, tmp_path):
    import clawchat_gateway.standalone_send as sender

    monkeypatch.setattr(identity, "_lock_state_file", Mock(side_effect=OSError("locking unavailable")))
    send, dial, refresh = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(sender, "standalone_send", send)
    monkeypatch.setattr(connection, "_ws_connect", dial)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    monkeypatch.setattr(conn, "_attempt_refresh", refresh)
    await conn._handle_install_superseded()
    await conn.start()
    try:
        await asyncio.sleep(connection.SUPERSEDED_CHECK_SECONDS * 3)
        assert conn.is_superseded and not conn._supervisor_task.done()
        assert conn._owner_notice_task is None
        assert tmp_path.resolve() in identity._failed_writes
        assert not identity.superseded_path(tmp_path).exists()
        send.assert_not_called()
        dial.assert_not_called()
        refresh.assert_not_called()
    finally:
        await conn.stop()


@pytest.mark.parametrize("source", ["env", "config", "sqlite", "unknown", "unreadable"])
@pytest.mark.parametrize("same_identity", [False, True])
def test_status_filters_current_identity_without_storage_writes(
    monkeypatch, tmp_path, capsys, source, same_identity,
):
    from clawchat_cli import main
    from clawchat_gateway import config as config_module, storage

    state = blocked(tmp_path)
    user_id = state["user_id"] if same_identity else "usr_other_agent"
    store = storage.ClawChatStore(tmp_path / "status.sqlite")
    if source == "sqlite":
        store.upsert_activation(platform="hermes", account_id="default", user_id=user_id,
                                conversation_id="cnv_test", owner_user_id="usr_test_owner",
                                access_token="test-access", refresh_token="test-refresh")
    monkeypatch.setattr(storage, "ClawChatStore", lambda: store)
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: store)
    monkeypatch.setattr(store, "initialize", Mock(side_effect=AssertionError("status must not initialize storage")))
    if source == "unreadable":
        monkeypatch.setattr(store, "get_activation_user_id", Mock(side_effect=OSError("read failed")))
    monkeypatch.setattr(config_module, "operator_clawchat_extra",
                        lambda: {"user_id": user_id} if source == "config" else {})
    monkeypatch.setattr(config_module, "_get_env",
                        lambda *names: user_id if source == "env" and "CLAWCHAT_USER_ID" in names else "")
    original = identity.superseded_path(tmp_path).read_bytes()
    assert main(["status"]) == 0
    result = json.loads(capsys.readouterr().out)
    known = source in ("env", "config", "sqlite")
    stale = known and not same_identity
    assert result["identity"] == (user_id[:8] if known else "unknown")
    assert result["is_superseded"] is (not stale)
    assert result["stale_state_for_other_identity"] is stale
    assert result["superseded"] == (None if stale else state)
    assert identity.superseded_path(tmp_path).read_bytes() == original
    assert not (tmp_path / "clawchat" / "install_seed").exists()
    if source != "sqlite":
        assert not store.db_path.exists()
    store.initialize.assert_not_called()


def test_status_config_identity_precedes_sqlite(monkeypatch):
    from clawchat_gateway import cli, config as config_module, storage

    monkeypatch.setattr(config_module.ClawChatConfig, "from_platform_config",
                        Mock(return_value=replace(config(), user_id="usr_config_agent")))
    store = Mock(side_effect=AssertionError("configured identity needs no SQLite activation lookup"))
    monkeypatch.setattr(storage, "ClawChatStore", store)
    assert cli._status_user_id() == "usr_config_agent"
    store.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", [None, "zh-CN", "en-US"])
@pytest.mark.parametrize("version", [None, "", "\n\t", "winner-1.2", "winner\n\t\x00" + "v" * 80])
async def test_notice_includes_only_printable_bounded_winner_version(monkeypatch, tmp_path, locale, version):
    import clawchat_gateway.standalone_send as sender

    send = AsyncMock(return_value={"success": True, "message_id": "msg_test"})
    monkeypatch.setattr(sender, "standalone_send", send)
    conn = make_conn(owner_locale=lambda: locale)
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    conn._last_close_reason = json.dumps({"by_client_version": version})
    try:
        await conn._handle_install_superseded()
        await conn._owner_notice_task
        text = send.call_args.args[2]
        clean = "".join(char for char in (version or "") if char.isprintable())[:64]
        assert ("The other installation runs plugin version" in text) == bool(clean and locale != "zh-CN")
        assert ("另一处安装的插件版本为" in text) == bool(clean and locale != "en-US")
        if clean:
            if locale != "zh-CN":
                assert f"plugin version {clean}." in text
            if locale != "en-US":
                assert f"插件版本为 {clean}。" in text
        assert "\t" not in text and "\x00" not in text
        assert "v" * 65 not in text
    finally:
        await conn.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["cli", "thread-cli", "slash", "activation-wakeup", "external"])
async def test_reclaim_cancels_all_pending_notices_before_reconnect(monkeypatch, tmp_path, method):
    from clawchat_cli import main
    from clawchat_gateway.commands import handle_clawchat_reclaim_command
    import clawchat_gateway.standalone_send as sender

    sends, delivered = [], []
    release = asyncio.Event()
    async def pending_send(*_a, **_kw):
        sends.append(asyncio.current_task())
        await release.wait()
        delivered.append(True)
        return {"success": True, "message_id": "msg_test"}
    monkeypatch.setattr(sender, "standalone_send", pending_send)
    if method != "external":
        monkeypatch.setattr(connection, "SUPERSEDED_CHECK_SECONDS", 60)
    update = Mock(wraps=identity.update_owner_notice)
    monkeypatch.setattr(identity, "update_owner_notice", update)
    async with peer() as (url, requests):
        conn = make_conn(url)
        conn._store = SimpleNamespace(
            get_activation_credentials=lambda **_kw: None,
            get_revoked_refresh_fingerprint=lambda **_kw: None,
            get_activation_conversation=lambda **_kw: "cnv_test",
        )
        await conn.start()
        try:
            await until(lambda: conn.is_superseded and len(sends) == 1)
            first = conn._owner_notice_task
            # An earlier conflict's task can still be pending when another is created.
            second = asyncio.create_task(conn._send_superseded_notice(identity.read_superseded(tmp_path)))
            conn._owner_notice_tasks.add(second)
            second.add_done_callback(conn._owner_notice_tasks.discard)
            await until(lambda: len(sends) == 2)
            assert len(requests) == 1
            if method == "slash":
                assert "已清除" in await handle_clawchat_reclaim_command("")
            elif method == "cli":
                assert main(["reclaim"]) == 0
            elif method == "thread-cli":
                assert await asyncio.to_thread(main, ["reclaim"]) == 0
            else:
                assert identity.clear_superseded(tmp_path)
                if method == "activation-wakeup":
                    ClawChatConnection.wake_reclaimed(tmp_path)
            await until(lambda: conn.is_ready and first.cancelled() and second.cancelled(), timeout=0.8)
            assert len(requests) == 2
            assert all(task.cancelled() for task in sends)
            release.set()
            await asyncio.sleep(0)
            assert not delivered
            assert identity.read_superseded(tmp_path) is None
            update.assert_not_called()
        finally:
            await conn.stop()


def test_notice_update_rejects_a_different_event(tmp_path):
    state = blocked(tmp_path)
    assert not identity.update_owner_notice(tmp_path, "old-event", {"status": "sent"})
    assert identity.read_superseded(tmp_path) == state


@pytest.mark.asyncio
async def test_memory_only_state_never_persists_and_external_generation_reconnects(monkeypatch, tmp_path):
    real_write, real_release = identity._write_superseded_unlocked, identity.release_reclaimed_cache
    write = Mock(return_value=False)
    releases = []
    def observe_release(home):
        result = real_release(home)
        releases.append(result)
        return result
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    monkeypatch.setattr(identity, "release_reclaimed_cache", observe_release)
    async with peer(supersede=False) as (url, requests):
        conn = make_conn(url)
        await conn._handle_install_superseded()
        assert conn._owner_notice_task is None
        await conn.start()
        try:
            await until(lambda: len(releases) >= 3)
            assert conn.is_superseded
            assert tmp_path.resolve() in identity._failed_writes
            assert not requests
            assert not identity.superseded_path(tmp_path).exists()
            # Restore disk writes and observe further standby cycles: no retry occurs.
            write.side_effect = real_write
            await until(lambda: len(releases) >= 6)
            assert write.call_count == 1
            assert not identity.superseded_path(tmp_path).exists()
            assert conn.is_superseded
            # Another process only bumps the generation; A's cache is untouched.
            _external_reclaim(tmp_path)
            assert tmp_path.resolve() in identity._failed_writes
            await until(lambda: conn.is_ready)
            assert True in releases
            assert len(requests) == 1
            assert write.call_count == 1
            assert identity.read_superseded(tmp_path) is None
            assert tmp_path.resolve() not in identity._failed_generations
            assert not identity.superseded_path(tmp_path).exists()
        finally:
            await conn.stop()


def test_empty_reclaim_explains_memory_only_recovery(tmp_path, capsys):
    from clawchat_cli import main
    assert main(["reclaim"]) == 0
    text = capsys.readouterr().out
    assert "cannot persist superseded installation" in text
    assert "磁盘无记录" in text
    assert "30 秒内看到这次 reclaim 并重连" in text
    assert identity._read_reclaim_generation(tmp_path)


def _event_state(stamp="test-event"):
    return {"user_id": "usr_test_agent", "superseded_at": stamp,
            "owner_notice": {"status": "pending"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["queued", "response-pending", "cancelled-watcher"])
async def test_refresh_worker_boundary_and_completed_rotation(monkeypatch, tmp_path, phase):
    from clawchat_gateway import activate, api_client
    import clawchat_gateway.standalone_send as sender
    from clawchat_gateway.storage import ClawChatStore

    monkeypatch.setattr(sender, "standalone_send", AsyncMock(return_value={"success": True, "message_id": "msg_test"}))
    store = ClawChatStore(tmp_path / "refresh.sqlite")
    cfg = replace(config(), base_url="http://127.0.0.1:1")
    store.upsert_activation(platform="hermes", account_id="default", user_id=cfg.user_id,
                            owner_user_id=cfg.owner_user_id, conversation_id="cnv_test",
                            access_token=cfg.token, refresh_token=cfg.refresh_token,
                            device_id="test-device")
    conn = ClawChatConnection(cfg, on_message=AsyncMock())
    conn._store = store
    assert conn._load_startup_activation_credentials()
    conn._refresh_manager = conn._build_refresh_manager()
    conn._refresh_manager._max_transient_retries = 0
    socket = conn._ws = SimpleNamespace(close=AsyncMock())
    dial = AsyncMock()
    monkeypatch.setattr(connection, "_ws_connect", dial)
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: store)
    env_writes = []
    monkeypatch.setattr(activate, "_write_env_values", lambda values: env_writes.append(dict(values)))
    reached, release = asyncio.Event(), asyncio.Event()
    http = Mock(return_value=api_client.RefreshResult("rotated-access", "rotated-refresh"))
    monkeypatch.setattr(api_client.ClawChatApiClient, "_auth_refresh_sync", http)

    async def delayed_worker(func, /, *args, **kwargs):
        # Model either a queued worker or an HTTP response awaiting delivery.
        # Execute the production guarded worker, without starting a real thread.
        if phase == "queued":
            reached.set()
            await release.wait()
            return func(*args, **kwargs)
        result = func(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(api_client.asyncio, "to_thread", delayed_worker)
    task = asyncio.create_task(conn._attempt_refresh(close_ws_on_success=True))
    try:
        await asyncio.wait_for(reached.wait(), 2)
        conn._last_close_code = 4010
        await conn._handle_install_superseded()
        await conn._owner_notice_task
        if phase == "cancelled-watcher":
            task.cancel()
            await asyncio.sleep(0)
        release.set()
        if phase == "cancelled-watcher":
            # The cancellation is honoured, but only after the rotation is persisted.
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        else:
            result = await asyncio.wait_for(task, 2)
            assert result.status == "skipped"
        current = store.get_activation_credentials(platform="hermes", account_id="default")
        if phase == "queued":
            http.assert_not_called()
            assert not env_writes
            assert current.access_token == cfg.token and current.refresh_token == cfg.refresh_token
        else:
            assert http.call_count == 1
            assert current.access_token == "rotated-access"
            assert current.refresh_token == "rotated-refresh"
            if phase == "response-pending":
                assert conn.config.token == "rotated-access"
                assert conn.config.refresh_token == "rotated-refresh"
            assert env_writes == [{"CLAWCHAT_TOKEN": "rotated-access", "CLAWCHAT_REFRESH_TOKEN": "rotated-refresh"}]
        assert (await conn.reactive_refresh()).status == "skipped"
        assert http.call_count == (0 if phase == "queued" else 1)
        assert not conn._refresh_pending_reconnect
        assert conn.is_superseded and not conn._auth_failed
        assert not conn._refresh_manager.logged_out
        socket.close.assert_not_called()
        dial.assert_not_called()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await conn.stop()


@pytest.mark.asyncio
async def test_failed_notice_write_then_external_reclaim_does_not_revive(monkeypatch, tmp_path):
    blocked(tmp_path)
    async with peer(supersede=False) as (url, requests):
        conn = make_conn(url)
        await conn.start()
        try:
            await until(lambda: conn.is_superseded)
            state = identity.read_superseded(tmp_path)
            with monkeypatch.context() as failure:
                failure.setattr(identity.os, "replace", Mock(side_effect=OSError("notice write failed")))
                assert not identity.update_owner_notice(tmp_path, state["superseded_at"], {"status": "sent"})
            assert tmp_path.resolve() not in identity._failed_writes
            # Independent interpreter clears disk; it cannot touch this process's cache.
            result = subprocess.run(_state_command(tmp_path, "clear", {}), capture_output=True,
                                    text=True, timeout=10, check=True)
            assert json.loads(result.stdout) is True
            assert not identity.release_reclaimed_cache(tmp_path)
            await until(lambda: conn.is_ready)
            assert len(requests) == 1
            assert not identity.superseded_path(tmp_path).exists()
        finally:
            await conn.stop()


def test_another_disk_event_does_not_replace_memory_without_reclaim(monkeypatch, tmp_path):
    old, newer = _event_state("old-event"), _event_state("new-event")
    with monkeypatch.context() as failure:
        failure.setattr(identity.os, "replace", Mock(side_effect=OSError("creation failed")))
        assert identity.record_superseded(tmp_path, old) == (old, False)
    assert identity.write_superseded(tmp_path, newer)
    assert not identity.release_reclaimed_cache(tmp_path)
    assert identity._failed_writes[tmp_path.resolve()] == old
    assert identity.read_superseded(tmp_path) == old
    assert json.loads(identity.superseded_path(tmp_path).read_text()) == newer
    _external_reclaim(tmp_path)
    assert identity.release_reclaimed_cache(tmp_path)
    assert identity.read_superseded(tmp_path) is None


@pytest.mark.parametrize("unknown_snapshot", [False, True])
def test_in_process_reclaim_clears_memory_and_snapshot(monkeypatch, tmp_path, unknown_snapshot):
    state = _event_state()
    with monkeypatch.context() as failure:
        failure.setattr(identity.os, "replace", Mock(side_effect=OSError("creation failed")))
        if unknown_snapshot:
            failure.setattr(identity, "_read_reclaim_generation", Mock(return_value=None))
        assert identity.record_superseded(tmp_path, state) == (state, False)
    assert identity.clear_superseded(tmp_path)
    assert not identity.release_reclaimed_cache(tmp_path)
    assert tmp_path.resolve() not in identity._failed_writes
    assert tmp_path.resolve() not in identity._failed_generations
    assert identity._read_reclaim_generation(tmp_path)
    assert not identity.superseded_path(tmp_path).exists()


def _mock_windows_lock(monkeypatch, *, failures, error_number):
    clock = [0.0]
    calls = []
    windows = SimpleNamespace(LK_LOCK=1, LK_NBLCK=2, LK_UNLCK=3)
    attempts = [0]
    def locking(_fd, mode, count):
        calls.append(mode)
        assert count == 1
        if mode != windows.LK_UNLCK:
            attempts[0] += 1
            if failures is None or attempts[0] <= failures:
                raise OSError(error_number, "lock held")
    windows.locking = locking
    def sleep(seconds):
        clock[0] += seconds
    monkeypatch.setitem(sys.modules, "msvcrt", windows)
    monkeypatch.setattr(identity, "_state_lock_backend", lambda: "windows")
    monkeypatch.setattr(identity, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    return windows, calls, clock


@pytest.mark.parametrize("error_number", [errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK)])
def test_windows_contention_retries_until_lock_is_owned(monkeypatch, tmp_path, caplog, error_number):
    windows, calls, clock = _mock_windows_lock(monkeypatch, failures=2, error_number=error_number)
    state = _event_state()
    assert identity.record_superseded(tmp_path, state) == (state, True)
    assert calls == [windows.LK_LOCK, windows.LK_LOCK, windows.LK_LOCK, windows.LK_UNLCK]
    assert clock[0] > 0
    assert identity.read_superseded(tmp_path) == state
    assert "file lock unavailable" not in caplog.text


@pytest.mark.parametrize("operation", ["record", "notice", "clear", "cli"])
def test_windows_contention_timeout_never_mutates_without_lock(monkeypatch, tmp_path, caplog, operation, capsys):
    state = _event_state()
    if operation != "record":
        assert identity.write_superseded(tmp_path, state)
    _mock_windows_lock(monkeypatch, failures=None, error_number=errno.EACCES)
    monkeypatch.setattr(identity, "STATE_LOCK_TIMEOUT_SECONDS", 0.12)
    write = Mock(side_effect=AssertionError("contended lock must never allow a write"))
    monkeypatch.setattr(identity, "_write_superseded_unlocked", write)
    if operation == "record":
        assert identity.record_superseded(tmp_path, state) == (state, False)
        assert identity._failed_writes[tmp_path.resolve()] == state
        assert not identity.superseded_path(tmp_path).exists()
    elif operation == "notice":
        assert not identity.update_owner_notice(tmp_path, state["superseded_at"], {"status": "sent"})
        assert tmp_path.resolve() not in identity._failed_writes
        assert "cannot update superseded owner notice" in caplog.text
    elif operation == "clear":
        with pytest.raises(identity.StateLockTimeoutError):
            identity.clear_superseded(tmp_path)
    else:
        from clawchat_cli import main
        assert main(["reclaim"]) == 1
        assert "reclaim failed" in capsys.readouterr().err
    if operation != "record":
        assert identity.read_superseded(tmp_path) == state
    write.assert_not_called()
    assert "file lock unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_windows_contention_timeout_suppresses_notice_and_reports_slash_failure(monkeypatch, tmp_path):
    from clawchat_gateway.commands import handle_clawchat_reclaim_command
    import clawchat_gateway.standalone_send as sender
    _mock_windows_lock(monkeypatch, failures=None, error_number=getattr(errno, "EDEADLOCK", errno.EDEADLK))
    monkeypatch.setattr(identity, "STATE_LOCK_TIMEOUT_SECONDS", 0.1)
    send = AsyncMock()
    monkeypatch.setattr(sender, "standalone_send", send)
    conn = make_conn()
    try:
        await conn._handle_install_superseded()
        assert conn.is_superseded and conn._owner_notice_task is None
        assert tmp_path.resolve() in identity._failed_writes
        assert "reclaim failed" in await handle_clawchat_reclaim_command("")
        assert tmp_path.resolve() in identity._failed_writes
        send.assert_not_called()
    finally:
        await conn.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_user", ["usr_test_agent", "usr_other_agent"])
async def test_empty_config_identity_filters_state_using_sqlite(monkeypatch, tmp_path, stored_user):
    from clawchat_gateway.storage import ClawChatStore
    state = blocked(tmp_path)
    store = ClawChatStore(tmp_path / "identity.sqlite")
    cfg = config()
    store.upsert_activation(platform="hermes", account_id="default", user_id=stored_user,
                            owner_user_id=cfg.owner_user_id, conversation_id="cnv_test",
                            access_token=cfg.token, refresh_token=cfg.refresh_token,
                            device_id="test-device")
    async with peer(supersede=False) as (url, requests):
        conn = make_conn(url)
        conn._cfg = replace(conn._cfg, user_id="", token="", refresh_token="", owner_user_id="")
        conn._store = store
        getter = store.get_activation_user_id
        config_ids_at_lookup = []
        def observe(**kwargs):
            config_ids_at_lookup.append(conn.config.user_id)
            return getter(**kwargs)
        monkeypatch.setattr(store, "get_activation_user_id", observe)
        await conn.start()
        try:
            if stored_user == state["user_id"]:
                await until(lambda: conn.is_superseded)
                assert not requests
                assert conn.config.user_id == "" and conn.config.token == ""
                assert set(conn._instance_locks) == {"profile"}
            else:
                await until(lambda: conn.is_ready)
                assert len(requests) == 1
                assert conn.config.user_id == stored_user
            assert config_ids_at_lookup and config_ids_at_lookup[0] == ""
        finally:
            await conn.stop()


@pytest.mark.asyncio
async def test_sqlite_identity_lookup_failure_remains_fail_closed(monkeypatch, tmp_path):
    blocked(tmp_path)
    conn = make_conn()
    conn._cfg = replace(conn._cfg, user_id="")
    conn._store = SimpleNamespace(get_activation_user_id=Mock(side_effect=OSError("read failed")))
    loader = Mock(side_effect=AssertionError("blocked startup must not load credentials"))
    monkeypatch.setattr(conn, "_load_startup_activation_credentials", loader)
    await conn.start()
    try:
        await until(lambda: conn.is_superseded)
        loader.assert_not_called()
        assert conn.config.user_id == ""
    finally:
        await conn.stop()


def test_sqlite_identity_getter_does_not_initialize_or_load_credentials(tmp_path, monkeypatch):
    import sqlite3
    from clawchat_gateway.storage import ClawChatStore
    path = tmp_path / "existing.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE activations (platform TEXT, account_id TEXT, user_id TEXT)")
        db.execute("INSERT INTO activations VALUES (?, ?, ?)", ("hermes", "default", "usr_other_agent"))
    store = ClawChatStore(path)
    monkeypatch.setattr(store, "initialize", Mock(side_effect=AssertionError("identity read must not initialize")))
    monkeypatch.setattr(store, "get_activation_credentials", Mock(side_effect=AssertionError("identity read must not load tokens")))
    assert store.get_activation_user_id(platform="hermes", account_id="default") == "usr_other_agent"
    assert not store._initialized
    missing = ClawChatStore(tmp_path / "absent.sqlite")
    with pytest.raises(sqlite3.OperationalError):
        missing.get_activation_user_id(platform="hermes", account_id="default")
    assert not missing.db_path.exists()


@pytest.mark.asyncio
async def test_pre_http_skip_is_not_retried_persisted_or_logged_out():
    from clawchat_gateway.api_client import RefreshSkipped
    request = AsyncMock(side_effect=RefreshSkipped(kind="installation_superseded", message="superseded",
                                                  connect_failed=True))
    persist, logout, rejected, sleep = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    manager = RefreshManager(build_client=lambda: SimpleNamespace(auth_refresh=request),
                             persist_tokens=persist, persist_logout=logout, persist_rejection=rejected,
                             device_id_provider=lambda: "test-device", sleep=sleep,
                             max_transient_retries=2, should_continue=lambda: True)
    outcome = await manager.refresh(access_token="test-access", refresh_token="test-refresh")
    assert outcome.status == "skipped" and request.await_count == 1
    sleep.assert_not_called()
    persist.assert_not_called()
    logout.assert_not_called()
    rejected.assert_not_called()
    assert not manager.logged_out


def test_reclaim_between_snapshot_and_lock_is_honoured(monkeypatch, tmp_path):
    state = _event_state()
    real_guard = identity._state_guard
    home = tmp_path.resolve()

    @contextmanager
    def guard_after_reclaim(path):
        # Another process completes a reclaim after the snapshot, before the lock.
        with real_guard(path):
            identity._bump_reclaim_generation_unlocked(home)
        with real_guard(path):
            yield

    monkeypatch.setattr(identity, "_state_guard", guard_after_reclaim)
    assert identity.record_superseded(tmp_path, state) == (state, False)
    assert identity.read_superseded(tmp_path) is None
    assert not identity.superseded_path(tmp_path).exists()
    assert not identity._failed_writes


def test_vanished_generation_does_not_release_cache(monkeypatch, tmp_path):
    home = tmp_path.resolve()
    with identity._state_guard(home):
        identity._bump_reclaim_generation_unlocked(home)
    state = _event_state()
    with monkeypatch.context() as failure:
        failure.setattr(identity, "_write_superseded_unlocked", Mock(return_value=False))
        assert identity.record_superseded(tmp_path, state) == (state, False)
    identity._reclaim_generation_path(home).unlink()
    assert identity.release_reclaimed_cache(tmp_path) is False
    assert identity.read_superseded(tmp_path) == state


@pytest.mark.asyncio
async def test_reclaim_during_teardown_wins_and_reconnects(tmp_path):
    conn = make_conn()
    try:
        # 4010 observed (latched with its snapshot); teardown is still running
        # when another process reclaims.
        conn._latch_superseded()
        assert identity.clear_superseded(tmp_path) is False
        await conn._handle_install_superseded()
        assert identity.read_superseded(tmp_path) is None
        assert conn._owner_notice_task is None
        assert await asyncio.wait_for(conn._wait_if_superseded(), 1) is True
        assert not conn._superseded and conn._superseded_snapshot is None
    finally:
        await conn.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reclaim_at", ["before-connect", "during-handshake"])
async def test_external_reclaim_within_production_poll_window_suppresses_notice(monkeypatch, tmp_path, reclaim_at):
    import clawchat_gateway.standalone_send as sender
    # Production timing: an out-of-process reclaim is only noticed by the 30 s
    # standby poll, while the notice may take up to its 20 s total timeout.
    monkeypatch.setattr(connection, "SUPERSEDED_CHECK_SECONDS", 30.0)
    assert connection.HANDSHAKE_TIMEOUT_SECONDS + 10.0 < 30.0
    frames, started = [], []

    class Sibling:
        def __init__(self, *_a, **_kw):
            pass

        def use_sibling_connect_device_id(self, suffix):
            return "test-device" + suffix

        async def start(self):
            started.append(reclaim_at)

        async def wait_until_ready(self, **_kw):
            # `hermes clawchat reclaim` from another process: no in-process wake.
            assert identity.clear_superseded(tmp_path) is True
            return True

        async def send_frame(self, frame, **_kw):
            frames.append(frame)
            return True

        async def stop(self):
            pass

    monkeypatch.setattr(sender, "ClawChatConnection", Sibling)
    conn = make_conn()
    conn._store = SimpleNamespace(get_activation_conversation=lambda **_kw: "cnv_test")
    original = identity.record_superseded

    def record_then_reclaim(*args, **kwargs):
        result = original(*args, **kwargs)
        if reclaim_at == "before-connect":
            assert identity.clear_superseded(tmp_path) is True
        return result

    monkeypatch.setattr(identity, "record_superseded", record_then_reclaim)
    try:
        await conn._handle_install_superseded()
        await asyncio.wait_for(conn._owner_notice_task, 2)
        assert frames == []
        # A notice reclaimed before it starts never even opens a connection.
        assert started == ([] if reclaim_at == "before-connect" else [reclaim_at])
        assert identity.read_superseded(tmp_path) is None
        assert not identity.superseded_path(tmp_path).exists()
    finally:
        await conn.stop()
