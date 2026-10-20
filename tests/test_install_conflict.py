"""Installation conflicts are terminal until an explicit local reclaim."""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    yield
    identity._cache.clear()
    identity._failed_writes.clear()


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
    monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a: True)
    monkeypatch.setattr(RefreshManager, "refresh", refresh)
    conn = make_conn()
    if state == "unknown-user":
        from dataclasses import replace
        conn._cfg = replace(conn._cfg, user_id="")
    if state == "other":
        # This case proves the disk record does not block a newly bound agent.
        monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a: False)
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
    assert len(status["install_id"]) == 8
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
            monkeypatch.setattr(connection, "is_token_near_expiry", lambda *_a: True)
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
        assert await conn._persist_rotated_tokens("rotated", "rotated-refresh") is False
        logout.assert_not_called()
        assert conn.config.token == "test-access"


@pytest.mark.asyncio
async def test_write_failure_still_blocks_and_reclaim_wakes(monkeypatch, tmp_path):
    conn = make_conn()
    def fail_replace(*_a):
        raise OSError("test write failure")
    monkeypatch.setattr(identity.os, "replace", fail_replace)
    conn._last_close_reason = "not-json"
    await conn._handle_install_superseded()
    await conn._owner_notice_task
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
@pytest.mark.parametrize("mode", ["fresh", "repair", "new-account"])
async def test_successful_activation_clears_state(monkeypatch, tmp_path, mode):
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
    await activation.activate("TESTCODE", base_url="https://example.invalid", repair=mode == "repair",
                              new_account=mode == "new-account")
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
    state = {"user_id": "usr_test_agent", "owner_notice": {"status": "pending"}}
    monkeypatch.setattr(identity.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("read only")))
    assert not identity.write_superseded(tmp_path, state)
    assert identity.read_superseded(tmp_path, "usr_test_agent") == state


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
    await conn._handle_install_superseded()
    await until(lambda: len(calls) == 2)
    latest_notice = conn._owner_notice_task
    await conn.stop()
    assert old_notice.cancelled() and latest_notice.cancelled()
    assert all(task.cancelled() for task in calls)
