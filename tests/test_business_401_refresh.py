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

from clawchat_gateway import api_client, connection, terminal_send, tools
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
@pytest.mark.parametrize("changes", [
    {"user_id": "other-agent"},
    {"base_url": "https://other.example.invalid"},
], ids=["account-changed", "endpoint-changed"])
@pytest.mark.parametrize("replacement_token", ["old-token", "current-token"])
async def test_adapter_inflight_repair_preserves_original_error_without_refresh(
    monkeypatch, changes, replacement_token,
):
    adapter = _adapter()
    request_config = adapter._clawchat_config
    script = _HttpScript(_envelope(401))
    monkeypatch.setattr(api_client, "urlopen", script)
    request_started = asyncio.Event()
    release_response = asyncio.Event()
    errors = []

    async def call(client):
        try:
            return await client.get_my_profile()
        except ClawChatApiError as exc:
            errors.append(exc)
            request_started.set()
            await release_response.wait()
            raise

    task = asyncio.create_task(adapter._rest_with_auth_retry(call))
    try:
        await asyncio.wait_for(request_started.wait(), timeout=5)
        replacement = replace(request_config, token=replacement_token, **changes)
        adapter._connection.config = replacement
        # Re-pair can also publish the replacement to the adapter during the call.
        adapter._clawchat_config = replacement
        release_response.set()
        with pytest.raises(ClawChatApiError) as caught:
            await asyncio.wait_for(task, timeout=5)
        assert caught.value is errors[0]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    adapter._connection.reactive_refresh.assert_not_awaited()
    assert adapter._clawchat_config is replacement
    assert script.authorizations == ["Bearer old-token"]
    assert script.requests[0].full_url == "https://example.invalid/v1/users/me"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"user_id": "other-agent"},
    {"base_url": "https://other.example.invalid"},
], ids=["account-changed", "endpoint-changed"])
async def test_adapter_refresh_success_with_changed_identity_preserves_original_error(
    monkeypatch, changes,
):
    adapter = _adapter()
    request_config = adapter._clawchat_config

    async def refresh():
        await asyncio.sleep(0)
        adapter._connection.config = replace(request_config, token="new-token", **changes)
        return RefreshOutcome(status="success")

    adapter._connection.reactive_refresh.side_effect = refresh
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
    assert adapter._clawchat_config is request_config
    adapter._connection.reactive_refresh.assert_awaited_once_with()
    assert script.authorizations == ["Bearer old-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("already_replaced", [False, True])
async def test_adapter_unchanged_empty_identity_and_trailing_slash_allow_retry(
    monkeypatch, already_replaced,
):
    adapter = _adapter()
    adapter._clawchat_config = replace(adapter._clawchat_config, user_id="")
    adapter._connection.config = replace(
        adapter._clawchat_config, base_url="https://example.invalid/",
        token="new-token" if already_replaced else "old-token",
    )
    script = _HttpScript(_envelope(401), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await _profile(adapter) == {"ok": True}
    assert adapter._connection.reactive_refresh.await_count == (0 if already_replaced else 1)
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


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
    refresh.assert_awaited_once_with(
        "old-token", user_id="test-agent", base_url="https://example.invalid",
    )
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
    refresh.assert_awaited_once_with(
        "old-token", user_id="test-agent", base_url="https://example.invalid",
    )


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
    refresh.assert_awaited_once_with(
        "old-token", user_id="test-agent", base_url="https://example.invalid",
    )
    assert script.authorizations == ["Bearer old-token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_identity", ["gateway", "tool", "both"])
async def test_tool_empty_identity_preserves_original_auth_without_adopting_token(
    monkeypatch, tool_profile, empty_identity,
):
    tool_config = replace(_config(), user_id="" if empty_identity != "gateway" else "test-agent")
    gateway_config = replace(
        _config("new-token"), user_id="" if empty_identity != "tool" else "test-agent",
    )
    monkeypatch.setattr(tools, "load_profile_config", lambda: tool_config)
    refresh = AsyncMock(return_value=gateway_config)
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401, msg="first rejection"))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await tools.get_account_profile() == {
        "error": "auth", "message": "first rejection",
        "meta": {"status": 200, "code": 401, "path": "/v1/users/me"},
    }
    assert not tools._same_rest_account(gateway_config, {
        "user_id": tool_config.user_id, "base_url": tool_config.base_url,
    })
    if empty_identity == "gateway":
        refresh.assert_awaited_once_with(
            "old-token", user_id="test-agent", base_url="https://example.invalid",
        )
    else:
        refresh.assert_not_awaited()
    assert script.authorizations == ["Bearer old-token"]


@pytest.mark.asyncio
async def test_tool_matching_identity_with_trailing_slash_adopts_gateway_token(monkeypatch, tool_profile):
    refresh = AsyncMock(return_value=replace(_config("new-token"), base_url="https://example.invalid/"))
    terminal_send.set_clawchat_mention_sender(SimpleNamespace(refresh_rest_credentials_for_tool=refresh))
    script = _HttpScript(_envelope(401), _envelope(0))
    monkeypatch.setattr(api_client, "urlopen", script)
    assert await tools.get_account_profile() == {"ok": True}
    refresh.assert_awaited_once_with(
        "old-token", user_id="test-agent", base_url="https://example.invalid",
    )
    assert script.authorizations == ["Bearer old-token", "Bearer new-token"]


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
        assert await adapter.refresh_rest_credentials_for_tool(
            "old-token", user_id="test-agent", base_url="https://example.invalid",
        ) is None
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
    assert await adapter.refresh_rest_credentials_for_tool(
        "old-token", user_id="test-agent", base_url="https://example.invalid",
    ) is None
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


async def _mixed_real_connection_refresh(monkeypatch, tmp_path, *, late_401):
    count = 6
    initial_count = count + int(late_401)
    # Construct the real connection without starting a socket or touching a profile DB.
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    conn = ClawChatConnection(_config(), on_message=AsyncMock())
    conn._owner_loop = asyncio.get_running_loop()
    # Isolate installation-state files and host device identity; retain native
    # lock checks (an unstarted connection requires no supervisor OS locks).
    monkeypatch.setattr(conn, "_install_is_blocked", lambda: False)
    monkeypatch.setattr(conn, "_refresh_device_id", lambda: "test-device")
    adapter = object.__new__(ClawChatAdapter)
    adapter._clawchat_config = conn.config
    adapter._connection = conn
    terminal_send.set_clawchat_mention_sender(adapter)
    all_failed = asyncio.Event()
    all_source_checks = asyncio.Event()
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    late_rejected = asyncio.Event()
    release_late = asyncio.Event()
    authorizations = []
    refresh_http_calls = []
    persisted = []
    source_checks = []
    late_tokens = []
    late_errors = []

    async def recheck_source(**kwargs):
        # Model an unchanged external credential source. Hold attempts until every
        # initial request has received its old-token business 401.
        assert conn.config.token == "old-token"
        source_checks.append(conn.config)
        if len(source_checks) == count:
            all_source_checks.set()
        await all_failed.wait()
        return "ok"

    source_check = AsyncMock(side_effect=recheck_source)
    monkeypatch.setattr(conn, "_recheck_activation_source", source_check)

    def http(request, timeout=None):
        assert request.full_url == "https://example.invalid/v1/users/me"
        authorization = request.get_header("Authorization")
        authorizations.append(authorization)
        assert authorization in ("Bearer old-token", "Bearer new-token")
        if authorization == "Bearer old-token":
            if authorizations.count(authorization) == initial_count:
                all_failed.set()
            return _Response(json.dumps(_envelope(401)).encode())
        assert persisted == [("new-token", "new-refresh-token")]
        assert conn.config.token == "new-token"
        return _Response(json.dumps(_envelope(0)).encode())

    async def auth_refresh(client, *, refresh_token, device_id):
        assert client._base_url == "https://example.invalid"
        assert client._token == ""
        assert client._user_id == "test-agent"
        refresh_http_calls.append((refresh_token, device_id))
        refresh_started.set()
        await release_refresh.wait()
        return RefreshResult(access_token="new-token", refresh_token="new-refresh-token")

    async def persist(access_token, refresh_token, *, expected_credentials):
        assert expected_credentials is source_checks[0]
        assert conn.config is expected_credentials
        assert conn.config.token == "old-token"
        persisted.append((access_token, refresh_token))
        return True

    # Keep the connection's factory, refresh control flow, result adoption, and
    # manager single-flight logic; stub exchange HTTP and durable persistence.
    monkeypatch.setattr(ClawChatApiClient, "auth_refresh", auth_refresh)
    monkeypatch.setattr(conn, "_persist_rotated_tokens", persist)
    conn._refresh_manager = conn._build_refresh_manager()
    assert isinstance(conn._refresh_manager, RefreshManager)
    assert conn.reactive_refresh.__func__ is ClawChatConnection.reactive_refresh
    assert conn._attempt_refresh.__func__ is ClawChatConnection._attempt_refresh

    async def delayed_profile(client):
        late_tokens.append(client._token)
        try:
            return await client.get_my_profile()
        except ClawChatApiError as exc:
            assert exc.kind == "auth" and exc.code == 401
            assert client._token == "old-token"
            late_errors.append(exc)
            # Delay delivery of the real parser's old-token auth error until the
            # six mixed callers have completed refresh and retried successfully.
            late_rejected.set()
            await release_late.wait()
            assert conn.config.token == "new-token"
            raise

    monkeypatch.setattr(api_client, "urlopen", http)
    tasks = []
    try:
        late_task = None
        if late_401:
            late_task = asyncio.create_task(adapter._rest_with_auth_retry(delayed_profile))
            tasks.append(late_task)
            await asyncio.wait_for(late_rejected.wait(), timeout=5)
        mixed_tasks = [asyncio.create_task(_profile(adapter)) for _ in range(3)]
        mixed_tasks += [asyncio.create_task(tools.get_account_profile()) for _ in range(3)]
        tasks.extend(mixed_tasks)
        await asyncio.wait_for(refresh_started.wait(), timeout=5)
        await asyncio.wait_for(all_source_checks.wait(), timeout=5)
        assert authorizations == ["Bearer old-token"] * initial_count
        assert conn.config.token == "old-token"
        assert persisted == []
        release_refresh.set()
        assert await asyncio.wait_for(asyncio.gather(*mixed_tasks), timeout=5) == [{"ok": True}] * count
        assert conn.config.token == "new-token"
        assert conn.config.refresh_token == "new-refresh-token"
        assert source_check.await_count == count
        assert persisted == [("new-token", "new-refresh-token")]
        if late_task is not None:
            assert not late_task.done()
            release_late.set()
            assert await asyncio.wait_for(late_task, timeout=5) == {"ok": True}
            assert late_tokens == ["old-token", "new-token"]
            assert len(late_errors) == 1
    finally:
        release_refresh.set()
        release_late.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert refresh_http_calls == [("old-refresh-token", "test-device")]
    assert authorizations.count("Bearer old-token") == initial_count
    assert authorizations.count("Bearer new-token") == initial_count
    assert source_check.await_count == count  # The late 401 must take the fast path.
    assert conn._credential_generation == 1


@pytest.mark.asyncio
async def test_mixed_adapter_and_tools_business_401_share_real_connection_refresh(
    monkeypatch, tool_profile, tmp_path,
):
    await _mixed_real_connection_refresh(monkeypatch, tmp_path, late_401=False)


@pytest.mark.asyncio
async def test_mixed_real_connection_late_401_reuses_token_without_second_refresh(
    monkeypatch, tool_profile, tmp_path,
):
    await _mixed_real_connection_refresh(monkeypatch, tmp_path, late_401=True)
