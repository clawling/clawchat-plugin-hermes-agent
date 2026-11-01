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


def test_display_data_redactor_copies_sensitive_keys_and_values(assert_no_secrets):
    keys = ("access-token", "client Secret", "PASSWORD", "passwd", "Authorization",
            "api_key", "refresh", "Cookie", "credential_store", "key", "private-key",
            "Signature", "x_signature")
    source = {key: {"unlabelled": SECRET["access"]} for key in keys}
    source.update({diagnostic(): (diagnostic(),), "plain": [7, 1.5, True, None],
                   "bytes": diagnostic().encode(), "tuple": (diagnostic(),)})
    safe = api.redact_display_data(source)
    assert all(safe[key] == "***" for key in keys)
    assert safe["plain"] == [7, 1.5, True, None]
    assert isinstance(safe["tuple"], list)
    assert source["access-token"]["unlabelled"] == SECRET["access"]
    assert_no_secrets(safe)
    assert api.redact_display_data(SECRET["access"], SECRET["access"]) == "***"
    usage = api.redact_display_data({"max_tokens": 512, "prompt_tokens": 0, "refresh": None})
    assert usage == {"max_tokens": 512, "prompt_tokens": 0, "refresh": None}


def test_display_data_redactor_bounds_cycles_depth_and_failed_conversion(assert_no_secrets):
    cycle = []
    cycle.append(cycle)
    assert api.redact_display_data(cycle) == ["[redacted]"]
    nested = SECRET["access"]
    for _ in range(34):
        nested = [nested]
    safe = api.redact_display_data(nested)
    assert "[redacted]" in repr(safe)

    class Unprintable:
        def __str__(self):
            raise ValueError(SECRET["access"])

        def __repr__(self):
            raise ValueError(SECRET["refresh"])

    failed = api.redact_display_data(Unprintable())
    assert failed == api.DIAGNOSTIC_REDACTION_FAILED
    assert_no_secrets(safe, failed)


def test_debug_snapshot_redacts_both_files_without_changing_request(tmp_path, assert_no_secrets):
    import copy
    from pathlib import Path
    from clawchat_gateway.llm_context_debug import ClawChatLlmContextDebug

    debug = ClawChatLlmContextDebug(env={"CLAWCHAT_LLM_CONTEXT_DEBUG": "1"},
                                   default_snapshot_root=tmp_path)
    request = {
        "eventText": diagnostic(),
        "requestMessages": [{"id": "placeholder-message", "text": diagnostic(),
                             "authorization": SECRET["access"]}],
        "fullLlmInput": {"messages": [{"refresh_token": SECRET["refresh"]}], "key": SECRET["access"], "signature": SECRET["refresh"]},
    }
    output = {"toolResults": [{"result": {"authorization": SECRET["bearer"]},
                               "text": diagnostic()}]}
    warnings = [diagnostic()]
    original = copy.deepcopy((request, output, warnings))
    run = Path(debug.write_snapshot(visibility="request", trace={"messageId": "placeholder-message"},
                                   input=request, output=output, warnings=warnings))
    latest = tmp_path / "hermes" / "latest.json"
    assert run.parent.name == "runs"
    assert run.read_text() == latest.read_text()
    assert "placeholder-message" in run.read_text()
    assert json.loads(run.read_text())["input"]["requestMessages"][0]["authorization"] == "***"
    assert (request, output, warnings) == original
    assert_no_secrets(run.read_text(), latest.read_text())


@pytest.mark.parametrize("fails", [False, True])
async def test_tool_audit_wrapper_redacts_sqlite_columns(tmp_path, monkeypatch, assert_no_secrets, fails):
    import sqlite3
    from clawchat_gateway import plugin_tools
    from clawchat_gateway.storage import ClawChatStore

    store = ClawChatStore(tmp_path / "audit.db")
    monkeypatch.setattr(plugin_tools, "get_clawchat_store", lambda: store)
    args = {"nested": [{"refresh_token": SECRET["refresh"]}], "text": diagnostic()}
    result = {"authorization": SECRET["access"], "nested": [{"text": diagnostic()}]}

    async def call():
        if fails:
            raise RuntimeError(diagnostic())
        return result

    if fails:
        with pytest.raises(RuntimeError):
            await plugin_tools._recorded_tool_call("placeholder_tool", args, "default", call)
    else:
        assert await plugin_tools._recorded_tool_call("placeholder_tool", args, "default", call) is result
    with sqlite3.connect(store.db_path) as db:
        rows = db.execute("SELECT args_json, result_json, error FROM tool_calls").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["nested"][0]["refresh_token"] == "***"
    assert (rows[0][2] is not None) is fails
    assert args["nested"][0]["refresh_token"] == SECRET["refresh"]
    assert result["authorization"] == SECRET["access"]
    assert_no_secrets(rows)


def test_tool_audit_direct_writer_redacts_all_columns(tmp_path, assert_no_secrets):
    import sqlite3
    from clawchat_gateway.storage import ClawChatStore

    store = ClawChatStore(tmp_path / "audit.db")
    assert store.record_tool_call(platform="hermes", account_id="default", tool_name="placeholder_tool",
                                  args={"cookie": SECRET["access"], "key": SECRET["access"],
                                        "signature": SECRET["refresh"]},
                                  result={"credential": SECRET["refresh"]}, error=diagnostic())
    with sqlite3.connect(store.db_path) as db:
        row = db.execute("SELECT args_json, result_json, error FROM tool_calls").fetchone()
    assert_no_secrets(row)


@pytest.mark.parametrize("route", ["self_echo", "direct", "group"])
async def test_adapter_inbound_routing_logs_only_text_length(monkeypatch, caplog, assert_no_secrets,
                                                            clawchat_test_platform, route):
    from dataclasses import replace
    from clawchat_gateway.adapter import ClawChatAdapter

    instance = ClawChatAdapter({})
    instance._clawchat_config = replace(instance._clawchat_config,
                                       user_id="placeholder-agent", owner_user_id="placeholder-owner")
    instance._store = SimpleNamespace(claim_message_once=lambda **kw: True)
    instance._group_settings_ready.set()
    monkeypatch.setattr(instance, "_schedule_profile_sync", lambda coro: coro.close())
    monkeypatch.setattr(instance, "_resolve_inbound_sender_context", lambda inbound: replace(inbound, sender_relation="peer_user"))
    monkeypatch.setattr(instance, "_handle_owner_forwarded_approval", AsyncMock(return_value=False))
    dispatched = AsyncMock()
    monkeypatch.setattr(instance, "_handle_inbound", dispatched)
    # The host command registry is not loaded here; treat /restart as known.
    import clawchat_gateway.adapter as adapter_module
    monkeypatch.setattr(adapter_module, "_is_known_hermes_slash_command",
                        lambda value: str(value or "").startswith("/restart"))
    # The first credential crosses the old 80-character logging boundary.
    text = "/restart " + "x" * 51 + " access_token=" + SECRET["access"] + " " + diagnostic()
    assert text.index(SECRET["access"]) < 80 < text.index(SECRET["access"]) + len(SECRET["access"])
    frame = {
        "event": "message.send", "chat_id": "placeholder-chat", "trace_id": "placeholder-trace",
        "chat_type": "group" if route == "group" else "direct",
        "sender": {"id": "placeholder-agent" if route == "self_echo" else "placeholder-peer"},
        "payload": {"message_id": "placeholder-message",
                    "message": {"fragments": [{"kind": "text", "text": text}]}},
    }
    with caplog.at_level(logging.INFO):
        await instance._on_message(frame)
    dispatched.assert_not_awaited()
    assert "text_len=" in caplog.text
    assert ("self_echo" if route == "self_echo" else "command dropped") in caplog.text
    assert_no_secrets(caplog=caplog)


def test_adapter_source_has_no_text_head():
    from pathlib import Path
    from clawchat_gateway import adapter

    assert "text_head" not in Path(adapter.__file__).read_text()


def test_profile_yaml_error_redacts_result_exception_and_traceback(tmp_path, monkeypatch, capsys,
                                                                assert_no_secrets):
    import traceback
    from clawchat_gateway import profile

    path = tmp_path / "config.yaml"
    path.write_text("access_token=" + SECRET["access"] + ": [oops\n")
    monkeypatch.setattr(profile, "_hermes_home", lambda: tmp_path)
    with pytest.raises(profile.ProfileConfigError) as raised:
        profile.load_profile_config()
    assert raised.value.__suppress_context__
    assert "failed to read" in str(raised.value)
    assert profile.main(["get"]) == 1
    printed = capsys.readouterr()
    assert json.loads(printed.err)["error"] == "config"
    assert_no_secrets(raised.value, str(raised.value),
                      "".join(traceback.format_exception(raised.value)), printed.out, printed.err)


def test_activation_cli_failure_redacts_message_hint_and_url(monkeypatch, capsys, caplog, assert_no_secrets):
    from clawchat_gateway import cli

    async def fail(*args, **kwargs):
        raise api.ClawChatApiError(kind="activation", message=diagnostic(),
                                   path=SECRET["userinfo_url"] + "/?" + SECRET["query"],
                                   data={"hint": diagnostic(), "next_action": "retry_later"})

    monkeypatch.setattr(cli, "activate_and_maybe_restart", fail)
    args = SimpleNamespace(command="activate", code="placeholder-code", no_restart=True,
                           new_account=False, repair=False)
    assert cli.handle_clawchat_cli(args) == 1
    printed = capsys.readouterr()
    assert "activation failed" in printed.err
    assert "hint:" in printed.err
    assert_no_secrets(printed.out, printed.err, caplog=caplog)


def test_setup_defensively_redacts_returned_urls(monkeypatch, capsys, caplog, assert_no_secrets):
    from clawchat_gateway import setup

    async def succeed(*args, **kwargs):
        return {"user_id": "placeholder-user", "base_url": SECRET["userinfo_url"] + "/?" + SECRET["query"],
                "websocket_url": SECRET["userinfo_url"].replace("https:", "wss:") + "/?" + SECRET["query"]}

    inputs = iter(("placeholder-code", "https://example.invalid"))
    monkeypatch.setattr("builtins.input", lambda prompt: next(inputs))
    monkeypatch.setattr(setup, "activate_and_maybe_restart", succeed)
    setup.setup_clawchat_platform()
    printed = capsys.readouterr()
    assert "Base URL:" in printed.out
    assert "WebSocket URL:" in printed.out
    assert_no_secrets(printed.out, printed.err, caplog=caplog)


@pytest.mark.parametrize("helper", ["config", "validation", "unknown", "os", "api", "chat"])
def test_tool_error_helpers_redact_message_and_path(assert_no_secrets, helper):
    if helper == "config":
        result = tools._config_error(diagnostic())
    elif helper == "validation":
        result = tools._validation_error_from_exception(ValueError(diagnostic()))
    elif helper == "unknown":
        result = tools._unknown_error(RuntimeError(diagnostic()))
    elif helper == "os":
        result = tools._unknown_error(OSError(1, diagnostic()))
    elif helper == "chat":
        result = tools._invalid_chat_id_error(diagnostic())
    else:
        result = tools._api_error(api.ClawChatApiError(kind="api", message=diagnostic(), path=diagnostic()))
    assert_no_secrets(result)


@pytest.mark.parametrize("module_name,function_name,required", [
    ("llm_context_debug", "write_snapshot", {"redact_display_data", "redact_display_text"}),
    ("storage", "record_tool_call", {"redact_display_data", "redact_display_text"}),
])
def test_persisted_diagnostics_keep_redaction_calls(module_name, function_name, required):
    import ast
    from pathlib import Path

    path = Path(api.__file__).with_name(module_name + ".py")
    tree = ast.parse(path.read_text())
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == function_name)
    calls = {node.func.id for node in ast.walk(function)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert required <= calls


@pytest.mark.parametrize("handler", ["mention_message", "send_file", "react_message", "widget_send"])
async def test_plugin_tool_diagnostic_logs_redact_free_strings(monkeypatch, caplog, assert_no_secrets, handler):
    from clawchat_gateway import plugin_tools, widget_send

    result = {"sent": False, "messageId": diagnostic(), "error": diagnostic(),
              "mentions": [{"authorization": SECRET["access"]}], "cookie": SECRET["refresh"]}
    args = {"chatId": diagnostic(), "chatType": diagnostic(), "chat_id": diagnostic(),
            "targetMessageId": diagnostic(), "emoji": diagnostic(), "text": diagnostic(),
            "mentions": [{"userId": diagnostic()}], "widget": "placeholder-widget"}
    monkeypatch.setattr(tools, handler, AsyncMock(return_value=result))
    monkeypatch.setattr(plugin_tools, "_record_tool_call", lambda **kw: None)
    monkeypatch.setattr(widget_send, "check_widget_for_send", lambda doc: {"id": diagnostic()})
    with caplog.at_level(logging.INFO):
        returned = await getattr(plugin_tools, "handle_clawchat_" + handler)(args, task_id=diagnostic())
    # Diagnostic copies must not change the actual tool result.
    assert json.loads(returned) == result
    assert handler in caplog.text
    assert_no_secrets(caplog=caplog)


@pytest.mark.parametrize("outcome", ["missing_owner", "exception", "failed_result", "success"])
async def test_group_command_receipt_logs_redact_commands_and_errors(caplog, assert_no_secrets, outcome):
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway.group_command_receipt import GroupCommandReceipt

    receipt = GroupCommandReceipt("placeholder-group", "Placeholder Group", "placeholder-message", diagnostic())
    send = AsyncMock(return_value=SimpleNamespace(success=outcome == "success", error=diagnostic()))
    if outcome == "exception":
        send.side_effect = RuntimeError(diagnostic())
    instance = SimpleNamespace(
        _owner_direct_chat_id=lambda: "" if outcome == "missing_owner" else "placeholder-chat",
        send=send, _react_group_command_receipt=AsyncMock(),
    )
    with caplog.at_level(logging.INFO):
        result = await ClawChatAdapter._send_group_command_receipt(instance, receipt, "placeholder body", {}, {}, "normal")
    assert (result is not None) is (outcome == "success")
    if outcome != "missing_owner":
        assert send.call_args.args[1].startswith(receipt.header())
    if outcome == "exception":
        assert "RuntimeError:" in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
    assert_no_secrets(caplog=caplog)


def test_model_output_debug_log_redacts_text(monkeypatch, caplog, assert_no_secrets):
    from clawchat_gateway import adapter

    monkeypatch.setenv(adapter.DEBUG_PROMPT_INJECTION_ENV, "1")
    monkeypatch.setattr(adapter, "write_llm_context_snapshot", lambda **kw: None)
    with caplog.at_level(logging.WARNING):
        adapter.ClawChatAdapter._debug_hermes_output(SimpleNamespace(), phase="final",
                                                     chat_id="placeholder-chat", message_id="placeholder-message",
                                                     text=diagnostic())
    assert adapter.DEBUG_HERMES_OUTPUT_BEGIN in caplog.text
    assert_no_secrets(caplog=caplog)


def test_activation_persists_original_urls_and_returns_safe_display_copy(tmp_path, monkeypatch, assert_no_secrets):
    from clawchat_gateway import activate

    url = SECRET["userinfo_url"] + "/?" + SECRET["query"]
    ws_url = url.replace("https:", "wss:")
    config = {}
    saved = []
    monkeypatch.setattr(activate, "_load_config", lambda: (tmp_path / "config.yaml", config))
    monkeypatch.setattr(activate, "_get_env", lambda key: ws_url if key == "CLAWCHAT_WEBSOCKET_URL" else "")
    monkeypatch.setattr(activate, "_write_env_values", lambda values: tmp_path / "placeholder.env")
    monkeypatch.setattr(activate, "_warn_open_credentials", lambda path: None)
    monkeypatch.setattr(activate, "_write_config", lambda path, data: saved.append(data))
    monkeypatch.setattr(activate, "_active_profile_name", lambda: "default")
    result = activate.persist_activation(access_token=SECRET["access"], refresh_token=SECRET["refresh"],
                                         user_id="placeholder-user", owner_user_id="placeholder-owner", base_url=url)
    extra = saved[0]["platforms"]["clawchat"]["extra"]
    assert extra["base_url"] == url
    assert extra["websocket_url"] == ws_url
    assert_no_secrets(result)


def test_activation_config_and_migration_failure_logs_drop_raw_traceback(monkeypatch, caplog, assert_no_secrets):
    from clawchat_gateway import activate

    def fail():
        raise RuntimeError(diagnostic())

    monkeypatch.setattr(activate, "_load_config", fail)
    with caplog.at_level(logging.WARNING):
        activate.ensure_clawchat_host_defaults_on_load()
        assert activate.migrate_legacy_config_tokens() == {
            "migrated_token": False, "migrated_refresh": False, "stripped": False,
        }
    assert "RuntimeError:" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert_no_secrets(caplog=caplog)


def test_connect_error_display_and_guidance_redact_defensively(assert_no_secrets):
    import traceback

    error = api.ClawChatApiError(kind="activation", message=diagnostic(),
                                data={"hint": diagnostic(), "next_action": "retry_later"})
    assert error.hint is not None
    assert_no_secrets(str(error), error.hint, api.format_connect_guidance(error),
                      "".join(traceback.format_exception(error)))


def test_activation_backend_comparison_log_redacts_both_urls(caplog, assert_no_secrets):
    from clawchat_gateway import activate

    config = {"platforms": {"clawchat": {"extra": {
        "user_id": "placeholder-user", "base_url": SECRET["userinfo_url"] + "/?" + SECRET["query"],
    }}}}
    with caplog.at_level(logging.INFO):
        assert activate._read_existing_user_id(config, base_url="https://example.invalid/other?" + SECRET["query"]) == ""
    assert "different backend" in caplog.text
    assert_no_secrets(caplog=caplog)
