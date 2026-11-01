"""Credentials are removed before diagnostics reach logs, results or report bodies."""

import io
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import HTTPError, URLError

import pytest

from conftest import FAKE_SECRETS as SECRET
from clawchat_gateway import api_client as api
from clawchat_gateway import connection, liveware_sample, tools


class Response(io.BytesIO):
    status = 200


def client():
    return api.ClawChatApiClient(
        base_url="https://example.invalid", token=SECRET["access"], device_id="placeholder-device",
    )


def diagnostic():
    return (f'access_token={SECRET["access"]} refresh_token={SECRET["refresh"]} '
            f'Authorization: {SECRET["bearer"]} {SECRET["userinfo_url"]}/?{SECRET["query"]}')


def test_display_redactor_covers_short_keys_and_userinfo(assert_no_secrets):
    text = api.redact_display_text(diagnostic())
    assert "https://example.invalid/" in text
    assert "***" in text
    assert_no_secrets(text)


@pytest.mark.parametrize("scheme", ["Bearer", "Basic"])
def test_display_redactor_handles_authorization_scheme(scheme, assert_no_secrets):
    assert_no_secrets(api.redact_display_text(f"Authorization: {scheme} placeholder-bearer-g10"))


@pytest.mark.parametrize("key", ["token", "access_token", "refresh_token", "key", "secret", "password", "signature"])
def test_display_redactor_handles_query_and_quoted_fields(key, assert_no_secrets):
    for text in (f"https://example.invalid/?{key}=placeholder-query-g10&keep=ok",
                 json.dumps({key: SECRET["refresh"]})):
        assert_no_secrets(api.redact_display_text(text))


async def test_connect_start_log_redacts_url_before_formatting(monkeypatch, caplog, assert_no_secrets):
    cfg = SimpleNamespace(
        websocket_url=SECRET["userinfo_url"].replace("https:", "wss:") + "/ws?" + SECRET["query"],
        token=SECRET["access"], base_url="https://example.invalid",
        heartbeat_interval_ms=1000, heartbeat_timeout_ms=1000,
    )
    conn = object.__new__(connection.ClawChatConnection)
    conn._cfg = cfg
    conn._allow_superseded_notice = False
    conn._credential_generation = 0
    conn._account_id = "placeholder-account"
    conn._send_queue = []
    conn._tracker = SimpleNamespace(next_connect=lambda: (1, 0))
    conn._install_is_blocked = lambda: False
    conn._instance_locks_ready = lambda: True
    conn._record_connection = lambda *a, **k: None
    conn._finish_current_connection = lambda *a, **k: None
    connect = AsyncMock(side_effect=RuntimeError("placeholder stop before transport"))
    monkeypatch.setattr(connection, "_ws_connect", connect)
    with caplog.at_level(logging.INFO):
        with pytest.raises(RuntimeError, match="placeholder stop"):
            await conn._run_one_connection()
    connect.assert_awaited_once()
    assert connect.call_args.args[0] == cfg.websocket_url
    assert "connect_start" in caplog.text
    assert_no_secrets(caplog=caplog)


@pytest.mark.parametrize("failure", ["http", "http_reason", "envelope", "url"])
async def test_api_errors_reach_tool_json_and_logs_without_credentials(monkeypatch, caplog, assert_no_secrets, failure):
    http = client()

    def urlopen(request, **kwargs):
        body = json.dumps({"code": 123, "msg": diagnostic()}).encode()
        if failure == "http":
            raise HTTPError(request.full_url, 500, diagnostic(), {}, Response(body))
        if failure == "http_reason":
            raise HTTPError(request.full_url, 500, diagnostic(), {}, Response(b"not JSON"))
        if failure == "url":
            raise URLError(diagnostic())
        return Response(body)

    monkeypatch.setattr(api, "urlopen", urlopen)
    monkeypatch.setattr(tools, "_build_client", lambda: (http, None))
    with caplog.at_level(logging.INFO):
        with pytest.raises(api.ClawChatApiError) as raised:
            await http.get_my_profile()
        # Check the actual exception formatting boundary, including traceback output.
        logging.getLogger(__name__).exception("API diagnostic: %s", raised.value,
                                              exc_info=(type(raised.value), raised.value, raised.value.__traceback__))
        result = await tools.get_account_profile()
    assert result["error"] == ("transport" if failure == "url" else "api")
    if failure == "http_reason":
        assert "code" not in result["meta"]
    elif failure != "url":
        assert result["meta"]["code"] == 123
    assert_no_secrets(json.dumps(result), raised.value, caplog=caplog)


async def test_onboarding_version_report_body_drops_nested_credentials(tmp_path, monkeypatch, caplog, assert_no_secrets):
    from clawchat_gateway import adapter
    from clawchat_gateway.onboarding_report import read_onboarding_report

    directory = tmp_path / "clawchat"
    directory.mkdir()
    (directory / "onboarding.json").write_text(json.dumps({
        "wiki_report_id": "placeholder-report",
        "capability_tier": 3,
        "token": SECRET["access"],
        "Authorization": SECRET["bearer"],
        "nested": {"refresh_token": SECRET["refresh"]},
        "capabilities": {"mcp": True, "token": SECRET["query"],
                         "nested": {"Authorization": SECRET["userinfo_url"]}},
    }))
    bodies = []

    def capture(request, **kwargs):
        bodies.append(request.data)
        return Response(b'{"code":0,"data":{}}')

    monkeypatch.setattr(api, "urlopen", capture)
    monkeypatch.setattr(adapter, "read_onboarding_report", lambda: read_onboarding_report(tmp_path))
    fake = SimpleNamespace(
        _clawchat_config=SimpleNamespace(base_url="https://example.invalid", token=SECRET["access"], user_id="placeholder-user"),
        _connection=SimpleNamespace(session_device_id=lambda: "placeholder-device"),
    )
    await adapter.ClawChatAdapter._report_plugin_version(fake, authenticated=True)
    assert len(bodies) == 1
    body = json.loads(bodies[0])
    assert body["wiki_report_id"] == "placeholder-report"
    assert body["capabilities"] == {"mcp": True}
    assert_no_secrets(body, bodies, caplog=caplog)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
async def test_liveware_login_failure_redacts_tool_and_sample_errors(monkeypatch, caplog, assert_no_secrets, stream):
    raw = diagnostic().encode()
    proc = SimpleNamespace(returncode=1, communicate=AsyncMock(
        return_value=(raw, b"") if stream == "stdout" else (b"", raw)))
    execute = AsyncMock(return_value=proc)
    monkeypatch.setattr(tools, "load_profile_config", lambda: SimpleNamespace(token=SECRET["access"], agent_id="placeholder-agent"))
    monkeypatch.setattr(tools, "resolve_liveware_path", lambda: "placeholder-liveware")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", execute)
    result = await tools.liveware_login()
    assert result["error"] == "subprocess"
    with pytest.raises(liveware_sample.LivewareSampleError) as raised:
        await liveware_sample.liveware_login(liveware_path="placeholder-liveware", token=SECRET["access"], exec=execute)
    with caplog.at_level(logging.WARNING):
        logging.getLogger(__name__).warning("sample login failed: %s", raised.value)
    assert_no_secrets(json.dumps(result), raised.value, caplog=caplog)


def test_secret_assertion_failure_never_repeats_the_credential(assert_no_secrets):
    with pytest.raises(pytest.fail.Exception) as raised:
        assert_no_secrets({"nested": [SECRET["access"].encode()]})
    assert "output 1" in str(raised.value)
    assert "credential #1" in str(raised.value)
    assert_no_secrets(raised.value)


ESCAPED_TAIL = "placeholder-escaped-tail-g10"


@pytest.mark.parametrize("text", [
    json.dumps({"password": 'prefix"' + ESCAPED_TAIL}),
    json.dumps({"Authorization": 'Bearer x"' + ESCAPED_TAIL}),
    json.dumps({"msg": json.dumps({"refresh_token": 'a"' + ESCAPED_TAIL})}),
    json.dumps({"msg": json.dumps({"secret": 'a\\"' + ESCAPED_TAIL})}),
    "password='a\\'" + ESCAPED_TAIL + "'",
])
def test_display_redactor_quoted_values_survive_escaped_quotes(text, assert_no_secrets):
    redacted = api.redact_display_text(text)
    assert "***" in redacted
    assert_no_secrets(redacted, secrets=(ESCAPED_TAIL,))


async def test_api_error_with_escaped_quote_in_credential_stays_redacted(monkeypatch, caplog, assert_no_secrets):
    http = client()
    msg = json.dumps({"password": 'prefix"' + ESCAPED_TAIL})

    def urlopen(request, **kwargs):
        body = json.dumps({"code": 123, "msg": msg}).encode()
        raise HTTPError(request.full_url, 500, "placeholder reason", {}, Response(body))

    monkeypatch.setattr(api, "urlopen", urlopen)
    monkeypatch.setattr(tools, "_build_client", lambda: (http, None))
    with caplog.at_level(logging.INFO):
        result = await tools.get_account_profile()
    assert result["error"] == "api"
    assert_no_secrets(json.dumps(result), caplog=caplog, secrets=(ESCAPED_TAIL,))
