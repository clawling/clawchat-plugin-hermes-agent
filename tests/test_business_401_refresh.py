"""Bearer business 401 classification and gateway-owned REST retry contracts."""

import asyncio
import io
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError

import pytest

from clawchat_gateway import api_client, terminal_send, tools
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError, RefreshResult
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection
from clawchat_gateway.token_refresh import RefreshManager, RefreshOutcome


def _config(token="old-token"):
    return ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        base_url="https://example.invalid",
        media_base_url="https://media.example.invalid",
        token=token,
        refresh_token="old-refresh-token",
        user_id="test-agent",
    )


def _client(token="old-token"):
    return ClawChatApiClient(
        base_url=_config().base_url, token=token, user_id="test-agent",
        device_id="test-device",
    )


class _Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _envelope(code, *, msg="access rejected", data=None):
    return {"code": code, "msg": msg, "data": data if data is not None else {"ok": True}}


class _HttpScript:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        assert self.payloads, "unexpected extra HTTP request"
        return _Response(json.dumps(self.payloads.pop(0)).encode())

    @property
    def authorizations(self):
        return [request.get_header("Authorization") for request in self.requests]


@pytest.fixture(autouse=True)
def _inline_http_worker(monkeypatch):
    # Keep the real parser and deterministic scheduling; urlopen is always stubbed.
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(api_client.asyncio, "to_thread", inline)


@pytest.mark.parametrize("token,code,kind", [
    ("old-token", 401, "auth"),
    (" old-token ", 401, "auth"),
    ("", 401, "api"),
    (" \t ", 401, "api"),
    ("old-token", "401", "api"),
    ("old-token", 401.0, "api"),
    ("old-token", True, "api"),
    ("old-token", 40301, "api"),
    ("old-token", 21003, "api"),
])
def test_json_business_401_bearer_and_strict_code_matrix(monkeypatch, token, code, kind):
    script = _HttpScript(_envelope(code, data={"hint": "test hint"}))
    monkeypatch.setattr(api_client, "urlopen", script)
    with pytest.raises(ClawChatApiError) as caught:
        _client(token)._call_json_sync("GET", "/test", None, {})
    error = caught.value
    assert error.kind == kind
    assert error.status == 200
    assert error.path == "/test"
    assert error.code == code
    assert error.data == {"hint": "test hint"}
    assert len(script.requests) == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("token", ["old-token", ""])
@pytest.mark.parametrize("method", ["_call_json_sync", "_call_envelope_sync"])
def test_http_401_403_remain_auth(monkeypatch, status, token, method):
    def reject(request, timeout=None):
        raise HTTPError(request.full_url, status, "rejected", {},
                        io.BytesIO(json.dumps(_envelope(40301)).encode()))

    monkeypatch.setattr(api_client, "urlopen", reject)
    with pytest.raises(ClawChatApiError) as caught:
        getattr(_client(token), method)("GET", "/test", None, {})
    assert caught.value.kind == "auth"
    assert caught.value.status == status


def test_envelope_bearer_business_401_raises_auth_and_redacts_message(monkeypatch):
    script = _HttpScript(_envelope(401, msg="access_token=old-token", data={"hint": "retry"}))
    monkeypatch.setattr(api_client, "urlopen", script)
    with pytest.raises(ClawChatApiError) as caught:
        _client()._call_envelope_sync("GET", "/test", None, {})
    error = caught.value
    assert (error.kind, error.code, error.status, error.path) == ("auth", 401, 200, "/test")
    assert error.data == {"hint": "retry"}
    assert "old-token" not in str(error)
    assert "old-token" not in error.message


@pytest.mark.parametrize("token,code", [
    ("old-token", 21003), ("old-token", "401"), ("old-token", 401.0),
    ("old-token", True), ("", 401), (" \t ", 401),
])
def test_other_envelopes_remain_unchanged(monkeypatch, token, code):
    payload = _envelope(code)
    monkeypatch.setattr(api_client, "urlopen", _HttpScript(payload))
    assert _client(token)._call_envelope_sync("GET", "/test", None, {}) == payload


def test_json_business_401_redacts_message_at_error_boundary(monkeypatch):
    monkeypatch.setattr(api_client, "urlopen", _HttpScript(
        _envelope(401, msg="access_token=old-token"),
    ))
    with pytest.raises(ClawChatApiError) as caught:
        _client()._call_json_sync("GET", "/test", None, {})
    assert "old-token" not in str(caught.value)
    assert "old-token" not in caught.value.message


class _Connection:
    rest_retry_with_current_token_allowed = ClawChatConnection.rest_retry_with_current_token_allowed

    def __init__(self, cfg, *, status="success", new_token="new-token"):
        self.config = cfg
        self._auth_failed = False
        self._allow_superseded_notice = False
        self._activation_source_suspended = False
        self.blocked = False
        self.locks_lost = False
        self._owner_loop = asyncio.get_running_loop()
        self.refresh_loops = []

        async def refresh():
            self.refresh_loops.append(asyncio.get_running_loop())
            if status == "success":
                self.config = replace(self.config, token=new_token)
            return RefreshOutcome(status=status)

        self.reactive_refresh = AsyncMock(side_effect=refresh)

    def _install_is_blocked(self):
        return self.blocked

    def _instance_locks_ready(self):
        return not self.locks_lost

    def session_device_id(self):
        return "test-device"


def _adapter(*, status="success", new_token="new-token", token="old-token"):
    # Only the REST methods are under test; avoid gateway startup and storage.
    adapter = object.__new__(ClawChatAdapter)
    adapter._clawchat_config = _config(token)
    adapter._connection = _Connection(adapter._clawchat_config, status=status, new_token=new_token)
    return adapter


async def _profile(adapter):
    return await adapter._rest_with_auth_retry(lambda client: client.get_my_profile())


@pytest.mark.asyncio
async def test_adapter_business_401_refreshes_once_and_retries_new_authorization(monkeypatch):
    adapter = _adapter()
    script = _HttpScript(_envelope(401), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await _profile(adapter) == {"ok": True}
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["transient", "permanent", "skipped"])
async def test_adapter_non_success_preserves_original_error_without_retry(monkeypatch, status):
    adapter = _adapter(status=status)
    script = _HttpScript(_envelope(401))
    monkeypatch.setattr(api_client, "urlopen", script)
    errors = []

    async def call(client):
        try:
            return await client.get_my_profile()
        except ClawChatApiError as exc:
            errors.append(exc)
            raise

    with pytest.raises(ClawChatApiError) as caught:
        await adapter._rest_with_auth_retry(call)
    assert caught.value is errors[0]
    assert caught.value.kind == "auth"
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token"]


@pytest.mark.asyncio
async def test_adapter_retry_401_raises_retry_error_without_second_refresh(monkeypatch):
    adapter = _adapter()
    script = _HttpScript(_envelope(401, msg="first rejection"),
                         _envelope(401, msg="retry rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    with pytest.raises(ClawChatApiError, match="retry rejection") as caught:
        await _profile(adapter)
    assert caught.value.kind == "auth"
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


@pytest.mark.asyncio
async def test_adapter_already_replaced_token_retries_without_refresh(monkeypatch):
    adapter = _adapter()
    adapter._connection.config = replace(adapter._connection.config, token="current-token")
    script = _HttpScript(_envelope(401), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await _profile(adapter) == {"ok": True}
    adapter._connection.reactive_refresh.assert_not_awaited()
    assert script.authorizations == ["Bearer old-token", "Bearer current-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("guard", [
    "_auth_failed", "_allow_superseded_notice", "blocked", "_activation_source_suspended",
    "locks_lost",
])
async def test_adapter_replaced_token_cannot_bypass_connection_guards(monkeypatch, guard):
    adapter = _adapter(status="skipped")
    adapter._connection.config = replace(adapter._connection.config, token="current-token")
    setattr(adapter._connection, guard, True)
    assert not adapter._connection.rest_retry_with_current_token_allowed()
    script = _HttpScript(_envelope(401))
    monkeypatch.setattr(api_client, "urlopen", script)
    with pytest.raises(ClawChatApiError):
        await _profile(adapter)
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token"]


def test_stopping_connection_never_reuses_rotated_token():
    connection = object.__new__(ClawChatConnection)
    connection._auth_failed = False
    connection._allow_superseded_notice = False
    connection._activation_source_suspended = False
    connection._install_is_blocked = lambda: False
    connection._stopping = True
    assert not connection.rest_retry_with_current_token_allowed()


@pytest.mark.asyncio
@pytest.mark.parametrize("new_token", ["", " \t ", "old-token"])
async def test_adapter_success_without_replacement_token_does_not_retry(monkeypatch, new_token):
    adapter = _adapter(new_token=new_token)
    script = _HttpScript(_envelope(401))
    monkeypatch.setattr(api_client, "urlopen", script)
    with pytest.raises(ClawChatApiError):
        await _profile(adapter)
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert len(script.requests) == 1


@pytest.fixture
def tool_profile(monkeypatch):
    monkeypatch.setattr(tools, "load_profile_config", lambda: _config())
    monkeypatch.setattr(tools, "_resolve_tool_device_id", lambda token: "test-device")
    terminal_send.clear_clawchat_mention_sender()


@pytest.mark.asyncio
async def test_tool_registered_gateway_business_401_refreshes_once(monkeypatch, tool_profile):
    adapter = _adapter()
    terminal_send.set_clawchat_mention_sender(adapter)
    script = _HttpScript(_envelope(401), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await tools.get_account_profile() == {"ok": True}
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


@pytest.mark.asyncio
async def test_tool_sender_returning_none_preserves_original_auth_result(monkeypatch, tool_profile):
    refresh = AsyncMock(return_value=None)
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401, msg="first rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await tools.get_account_profile() == {
        "error": "auth", "message": "first rejection",
        "meta": {"status": 200, "code": 401, "path": "/v1/users/me"},
    }
    refresh.assert_awaited_once_with("old-token")
    assert len(script.requests) == 1


@pytest.mark.asyncio
async def test_tool_retry_401_returns_retry_error_without_second_refresh(monkeypatch, tool_profile):
    adapter = _adapter()
    terminal_send.set_clawchat_mention_sender(adapter)
    script = _HttpScript(_envelope(401, msg="first rejection"),
                         _envelope(401, msg="retry rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    result = await tools.get_account_profile()
    assert result["error"] == "auth"
    assert result["message"] == "retry rejection"
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", [None, SimpleNamespace()])
async def test_tool_without_refresh_capable_sender_returns_original_auth(monkeypatch, tool_profile, sender):
    if sender is not None:
        terminal_send.set_clawchat_mention_sender(sender)
    script = _HttpScript(_envelope(401, msg="first rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    result = await tools.get_account_profile()
    assert result["error"] == "auth"
    assert result["message"] == "first rejection"
    assert len(script.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", " \t "])
async def test_tool_no_bearer_business_401_is_api_without_refresh(monkeypatch, tool_profile, token):
    monkeypatch.setattr(tools, "load_profile_config", lambda: _config(token))
    refresh = AsyncMock(return_value=_config("new-token"))
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert (await tools.get_account_profile())["error"] == "api"
    refresh.assert_not_awaited()
    assert len(script.requests) == 1


@pytest.mark.asyncio
async def test_tool_proxy_keeps_constructor_fields_and_new_token_for_later_calls(monkeypatch, tool_profile):
    refresh = AsyncMock(return_value=_config("new-token"))
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401), _envelope(0), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    client, error = tools._build_client()
    assert error is None
    original = (client._base_url, client._user_id, client._device_id, client._media_base_url)
    # A previously retrieved wrapper must use the replacement client too.
    profile = client.get_my_profile
    assert await profile() == {"ok": True}
    assert await profile() == {"ok": True}
    assert original == (client._base_url, client._user_id, client._device_id, client._media_base_url)
    assert script.authorizations == ["Bearer old-token", "Bearer new-token", "Bearer new-token"]
    assert client._headers({}, None)["authorization"] == "Bearer new-token"
    refresh.assert_awaited_once_with("old-token")


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"base_url": "https://other.example.invalid"},
    {"base_url": ""},
    {"user_id": "other-agent"},
])
async def test_tool_never_adopts_gateway_token_for_another_endpoint_or_account(
    monkeypatch, tool_profile, changes,
):
    refresh = AsyncMock(return_value=replace(_config("new-token"), **changes))
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401, msg="first rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    result = await tools.get_account_profile()
    assert result["error"] == "auth"
    assert result["message"] == "first rejection"
    refresh.assert_awaited_once_with("old-token")
    assert script.authorizations == ["Bearer old-token"]


@pytest.mark.asyncio
async def test_orchestration_tool_refreshes_401_and_preserves_21003(monkeypatch, tool_profile):
    adapter = _adapter()
    terminal_send.set_clawchat_mention_sender(adapter)
    monkeypatch.setattr("clawchat_gateway.host_tools_guard.orchestrate_handler_origin", lambda: {
        "conversation_id": "test-conversation", "initiator_id": "test-owner",
    })
    forbidden = _envelope(21003, msg="owner policy forbids this operation")
    script = _HttpScript(_envelope(401), forbidden)
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await tools.orchestrate_list_agents(purpose="test") == forbidden
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


@pytest.mark.asyncio
async def test_tool_refresh_runs_on_owner_loop_in_another_thread(monkeypatch, tool_profile):
    adapter = _adapter()
    owner_loop = asyncio.new_event_loop()
    started = threading.Event()

    def run_owner():
        asyncio.set_event_loop(owner_loop)
        owner_loop.call_soon(started.set)
        try:
            owner_loop.run_forever()
        finally:
            owner_loop.close()

    thread = threading.Thread(target=run_owner, daemon=True)
    thread.start()
    try:
        assert started.wait(timeout=5)
        adapter._connection._owner_loop = owner_loop
        terminal_send.set_clawchat_mention_sender(adapter)
        script = _HttpScript(_envelope(401), _envelope(0))
        monkeypatch.setattr(api_client, "urlopen", script)
        assert await asyncio.wait_for(tools.get_account_profile(), timeout=5) == {"ok": True}
        assert adapter._connection.refresh_loops == [owner_loop]
        adapter._connection.reactive_refresh.assert_awaited_once_with()
        assert script.authorizations == ["Bearer old-token", "Bearer new-token"]
    finally:
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["absent", "stopped", "closed"])
async def test_tool_refresh_unavailable_owner_loop_returns_none(state):
    adapter = _adapter()
    loop = None if state == "absent" else asyncio.new_event_loop()
    if state == "closed":
        loop.close()
    adapter._connection._owner_loop = loop
    try:
        assert await adapter.refresh_rest_credentials_for_tool("old-token") is None
        adapter._connection.reactive_refresh.assert_not_awaited()
    finally:
        if loop is not None and not loop.is_closed():
            loop.close()


@pytest.mark.asyncio
async def test_tool_refresh_scheduling_failure_closes_coroutine_and_hides_error(monkeypatch, caplog):
    adapter = _adapter()
    adapter._connection._owner_loop = SimpleNamespace(is_closed=lambda: False, is_running=lambda: True)
    scheduled = []

    def fail(coroutine, loop):
        scheduled.append(coroutine)
        raise RuntimeError("access_token=old-token")

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fail)
    assert await adapter.refresh_rest_credentials_for_tool("old-token") is None
    assert len(scheduled) == 1
    assert scheduled[0].cr_frame is None
    assert "credential refresh failed" in caplog.text
    assert "old-token" not in caplog.text


@pytest.mark.asyncio
async def test_tool_refresh_exception_returns_original_error_without_secret_log(monkeypatch, tool_profile, caplog):
    adapter = _adapter()
    adapter._connection.reactive_refresh.side_effect = RuntimeError("access_token=old-token")
    terminal_send.set_clawchat_mention_sender(adapter)
    script = _HttpScript(_envelope(401, msg="first rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    result = await tools.get_account_profile()
    assert result["error"] == "auth"
    assert result["message"] == "first rejection"
    assert len(script.requests) == 1
    assert "credential refresh failed" in caplog.text
    assert "old-token" not in caplog.text
    assert "old-token" not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("via_tools", [False, True], ids=["adapter", "tools"])
async def test_concurrent_business_401_calls_share_real_refresh_manager(monkeypatch, tool_profile, via_tools):
    count = 6
    adapter = _adapter()
    terminal_send.set_clawchat_mention_sender(adapter)
    all_failed = asyncio.Event()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    authorizations = []
    refresh_http_calls = []
    persisted = []

    def http(request, timeout=None):
        authorization = request.get_header("Authorization")
        authorizations.append(authorization)
        assert authorization in ("Bearer old-token", "Bearer new-token")
        if authorization == "Bearer old-token":
            if authorizations.count(authorization) == count:
                all_failed.set()
            return _Response(json.dumps(_envelope(401)).encode())
        return _Response(json.dumps(_envelope(0)).encode())

    async def auth_refresh(*, refresh_token, device_id):
        refresh_http_calls.append((refresh_token, device_id))
        refresh_started.set()
        await release_refresh.wait()
        return RefreshResult(access_token="new-token", refresh_token="new-refresh-token")

    async def persist(access_token, refresh_token):
        persisted.append((access_token, refresh_token))
        return True

    manager = RefreshManager(
        build_client=lambda: SimpleNamespace(auth_refresh=auth_refresh),
        persist_tokens=persist,
        persist_logout=AsyncMock(),
        device_id_provider=lambda: "test-device",
    )
    waiters = 0
    all_waiting = asyncio.Event()

    async def reactive_refresh():
        nonlocal waiters
        await all_failed.wait()
        waiters += 1
        if waiters == count:
            all_waiting.set()
        outcome = await manager.refresh(
            access_token="old-token", refresh_token="old-refresh-token",
        )
        if outcome.status == "success":
            assert persisted == [(outcome.access_token, outcome.refresh_token)]
            adapter._connection.config = replace(
                adapter._connection.config,
                token=outcome.access_token, refresh_token=outcome.refresh_token,
            )
        return outcome

    adapter._connection.reactive_refresh = AsyncMock(side_effect=reactive_refresh)
    monkeypatch.setattr(api_client, "urlopen", http)
    tasks = [asyncio.create_task(tools.get_account_profile() if via_tools else _profile(adapter))
             for _ in range(count)]
    try:
        await asyncio.wait_for(all_waiting.wait(), timeout=5)
        await asyncio.wait_for(refresh_started.wait(), timeout=5)
        release_refresh.set()
        assert await asyncio.wait_for(asyncio.gather(*tasks), timeout=5) == [{"ok": True}] * count
    finally:
        release_refresh.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert refresh_http_calls == [("old-refresh-token", "test-device")]
    assert authorizations.count("Bearer old-token") == count
    assert authorizations.count("Bearer new-token") == count
    assert adapter._connection.reactive_refresh.await_count == count
