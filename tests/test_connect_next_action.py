"""Connect-code failure guidance: ``data.{hint,next_action,retry_after_seconds}``.

On a failed ``POST /v1/agents/connect`` (and ``/connect/check``) the backend's
error envelope ``data`` carries a hint sentence and a closed ``next_action``
enum. These tests pin:

* the three fields are exposed as attributes on ``ClawChatApiError``;
* ``wait_retry`` re-redeems exactly once after ``retry_after_seconds`` (capped
  at 120s, default 60s), composing with the transport retry without looping;
* ``ask_owner_new_code`` / ``reconnect`` / ``stop`` / unknown values never retry;
* the CLI and the in-chat command surface next_action + hint, keeping the
  existing ``activation failed (...)`` prefix line;
* an old server (fields absent) behaves and prints exactly as before.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from clawchat_gateway import api_client as api_mod
from clawchat_gateway import cli as cli_mod
from clawchat_gateway import commands as commands_mod
from clawchat_gateway.api_client import (
    ClawChatApiClient,
    ClawChatApiError,
    agents_connect_with_retry,
    format_connect_guidance,
)

HINT = "connect code expired; ask your owner for a new one. See https://agent-connection.clawling.com/start.md#connect-code-errors"
CONNECT = "/v1/agents/connect"


def _api_error(code: int, data: dict | None, *, msg: str = "connect failed") -> ClawChatApiError:
    return ClawChatApiError("api", msg, status=200, path=CONNECT, code=code, data=data)


def _rate_limited(retry_after: Any = 60, *, include_retry: bool = True) -> ClawChatApiError:
    data: dict[str, Any] = {"hint": "rate limited", "next_action": "wait_retry"}
    if include_retry:
        data["retry_after_seconds"] = retry_after
    return _api_error(16025, data, msg="rate limit exceeded for this connect code, slow down")


_OK = {"agent": {"id": "agt_1", "user_id": "usr_1", "owner_id": "usr_owner"}, "conversation": {"id": "c1"}}


class _FakeClient:
    """Plays back a scripted sequence of outcomes for ``agents_connect``."""

    def __init__(self, outcomes: list[Any]) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def agents_connect(self, **_kwargs: Any) -> dict:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class _Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


async def _redeem(client: _FakeClient, sleeps: _Sleeps) -> dict:
    return await agents_connect_with_retry(
        client,  # type: ignore[arg-type]
        code="K7RM4TQP",
        backoff=(1.0, 2.0),
        attempt_ceiling=None,
        sleep=sleeps,
    )


# ---------------------------------------------------------------- parsing


def _envelope_error(monkeypatch: pytest.MonkeyPatch, envelope: dict) -> ClawChatApiError:
    client = ClawChatApiClient(base_url="https://example.invalid", device_id="dev-1")
    monkeypatch.setattr(client, "_request_envelope_sync", lambda *_a, **_k: (envelope, 200))
    with pytest.raises(ClawChatApiError) as info:
        client._call_json_sync("POST", CONNECT, b"{}", {})
    return info.value


@pytest.mark.parametrize(
    ("code", "next_action"),
    [
        (16008, "ask_owner_new_code"),
        (16023, "ask_owner_new_code"),
        (16024, "reconnect"),
        (16014, "stop"),
        (16025, "wait_retry"),
    ],
)
def test_envelope_guidance_is_exposed_on_the_error(
    monkeypatch: pytest.MonkeyPatch, code: int, next_action: str
) -> None:
    data: dict[str, Any] = {"hint": HINT, "next_action": next_action}
    if next_action == "wait_retry":
        data["retry_after_seconds"] = 60
    exc = _envelope_error(monkeypatch, {"code": code, "msg": "nope", "data": data})

    assert (exc.kind, exc.code, exc.message) == ("api", code, "nope")
    assert exc.hint == HINT
    assert exc.next_action == next_action
    assert exc.retry_after_seconds == (60 if next_action == "wait_retry" else None)


def test_absent_guidance_reads_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = _envelope_error(monkeypatch, {"code": 16008, "msg": "invalid connect code", "data": {}})
    assert (exc.hint, exc.next_action, exc.retry_after_seconds) == (None, None, None)
    exc = _envelope_error(monkeypatch, {"code": 16008, "msg": "invalid connect code"})
    assert (exc.hint, exc.next_action, exc.retry_after_seconds) == (None, None, None)
    assert format_connect_guidance(exc) == []


def test_malformed_guidance_fields_are_ignored() -> None:
    exc = _api_error(16025, {"hint": 5, "next_action": ["wait_retry"], "retry_after_seconds": "60"})
    assert (exc.hint, exc.next_action, exc.retry_after_seconds) == (None, None, None)


def test_unknown_next_action_is_kept_verbatim() -> None:
    exc = _api_error(16099, {"hint": HINT, "next_action": "phone_home"})
    assert exc.next_action == "phone_home"
    lines = format_connect_guidance(exc)
    assert any("next_action=phone_home" in line for line in lines)


# ---------------------------------------------------------------- retry policy


@pytest.mark.parametrize("next_action", ["ask_owner_new_code", "reconnect", "stop", "phone_home"])
async def test_non_wait_retry_actions_never_retry(next_action: str) -> None:
    err = _api_error(16008, {"hint": HINT, "next_action": next_action})
    client, sleeps = _FakeClient([err, _OK]), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    assert info.value is err
    assert client.calls == 1
    assert sleeps.delays == []


async def test_absent_fields_fail_once_like_before() -> None:
    err = _api_error(16008, None, msg="invalid connect code")
    client, sleeps = _FakeClient([err, _OK]), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    assert info.value is err
    assert client.calls == 1
    assert sleeps.delays == []


async def test_wait_retry_waits_then_retries_once_and_succeeds() -> None:
    client, sleeps = _FakeClient([_rate_limited(60), _OK]), _Sleeps()

    assert await _redeem(client, sleeps) == _OK
    assert client.calls == 2
    assert sleeps.delays == [60]


async def test_wait_retry_retries_only_once() -> None:
    client, sleeps = _FakeClient([_rate_limited(30), _rate_limited(30), _OK]), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    assert client.calls == 2
    assert sleeps.delays == [30]
    assert info.value.next_action == "wait_retry"
    assert info.value.wait_retried is True


async def test_wait_retry_then_dead_code_is_not_retried_again() -> None:
    dead = _api_error(16023, {"hint": HINT, "next_action": "ask_owner_new_code"})
    client, sleeps = _FakeClient([_rate_limited(5), dead, _OK]), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    assert info.value is dead
    assert client.calls == 2
    assert sleeps.delays == [5]


@pytest.mark.parametrize(
    ("retry_after", "include", "expected"),
    [
        (500, True, 120),
        (120, True, 120),
        (1, True, 1),
        (None, False, 60),
        (0, True, 60),
        (-5, True, 60),
        ("60", True, 60),
        (True, True, 60),
        (None, True, 60),
    ],
)
async def test_wait_retry_delay_is_capped_and_defaulted(
    retry_after: Any, include: bool, expected: float
) -> None:
    client, sleeps = _FakeClient([_rate_limited(retry_after, include_retry=include), _OK]), _Sleeps()

    assert await _redeem(client, sleeps) == _OK
    assert sleeps.delays == [expected]


def _refused() -> ClawChatApiError:
    return ClawChatApiError("transport", "connection refused", path=CONNECT, connect_failed=True)


async def test_transport_retry_then_wait_retry_compose() -> None:
    client, sleeps = _FakeClient([_refused(), _rate_limited(60), _OK]), _Sleeps()

    assert await _redeem(client, sleeps) == _OK
    assert client.calls == 3
    assert sleeps.delays == [1.0, 60]


async def test_wait_retry_then_transport_retry_compose_without_looping() -> None:
    client, sleeps = _FakeClient(
        [_rate_limited(60), _refused(), _rate_limited(60), _OK]
    ), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    # wait-retry budget (1) and transport budget are independent and both finite.
    assert client.calls == 3
    assert sleeps.delays == [60, 1.0]
    assert info.value.next_action == "wait_retry"


async def test_transport_retry_budget_is_unchanged() -> None:
    client, sleeps = _FakeClient([_refused(), _refused(), _refused(), _OK]), _Sleeps()

    with pytest.raises(ClawChatApiError) as info:
        await _redeem(client, sleeps)

    assert info.value.connect_failed is True
    assert client.calls == 3  # 1 + ACTIVATION_CONNECT_RETRIES (2)
    assert sleeps.delays == [1.0, 2.0]


async def test_default_sleep_is_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps = _Sleeps()
    monkeypatch.setattr(api_mod.asyncio, "sleep", sleeps)
    client = _FakeClient([_rate_limited(7), _OK])

    result = await agents_connect_with_retry(client, code="K7RM4TQP", attempt_ceiling=None)  # type: ignore[arg-type]

    assert result == _OK
    assert sleeps.delays == [7]


# ---------------------------------------------------------------- surfaces


def _run_cli_failing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], exc: ClawChatApiError
) -> str:
    async def _runner(*_a: Any, **_k: Any) -> dict:
        raise exc

    monkeypatch.setattr(cli_mod, "activate_and_maybe_restart", _runner)
    monkeypatch.setattr(cli_mod, "resolve_activation_base_url", lambda: "https://example.invalid")
    parser = argparse.ArgumentParser(prog="hermes clawchat")
    cli_mod.setup_clawchat_cli(parser)
    args = parser.parse_args(["activate", "CODE"])
    assert cli_mod.handle_clawchat_cli(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    return captured.err


def test_cli_old_server_output_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _run_cli_failing(monkeypatch, capsys, _api_error(16008, None, msg="invalid connect code"))
    assert err == (
        "clawchat: activation failed (api /v1/agents/connect status=200 code=16008): "
        "invalid connect code\n"
    )


@pytest.mark.parametrize("next_action", ["ask_owner_new_code", "reconnect", "stop", "phone_home"])
def test_cli_prints_next_action_and_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], next_action: str
) -> None:
    exc = _api_error(16023, {"hint": HINT, "next_action": next_action}, msg="connect code expired")
    lines = _run_cli_failing(monkeypatch, capsys, exc).splitlines()

    assert lines[0] == (
        "clawchat: activation failed (api /v1/agents/connect status=200 code=16023): "
        "connect code expired"
    )
    assert f"clawchat: next_action={next_action}" in lines
    assert f"clawchat: hint: {HINT}" in lines


def test_cli_wait_retry_reports_retry_after_and_that_it_already_retried(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import dataclasses

    exc = dataclasses.replace(_rate_limited(60), wait_retried=True)
    lines = _run_cli_failing(monkeypatch, capsys, exc).splitlines()

    assert lines[0].startswith("clawchat: activation failed (api /v1/agents/connect status=200 code=16025): ")
    assert "clawchat: next_action=wait_retry retry_after_seconds=60" in lines
    assert any("already waited and retried once" in line for line in lines)


async def test_chat_command_surfaces_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = _api_error(16024, {"hint": HINT, "next_action": "reconnect"}, msg="already paired")

    async def _runner(*_a: Any, **_k: Any) -> dict:
        raise exc

    monkeypatch.setattr(commands_mod, "activate_and_maybe_restart", _runner)
    monkeypatch.setattr(commands_mod, "resolve_activation_base_url", lambda: "https://example.invalid")

    out = await commands_mod.handle_clawchat_activate_command("CODE")

    assert out.splitlines()[0] == (
        "clawchat: activation failed (api /v1/agents/connect status=200 code=16024): already paired"
    )
    assert "clawchat: next_action=reconnect" in out
    assert f"clawchat: hint: {HINT}" in out


async def test_chat_command_old_server_error_still_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    exc = _api_error(16008, None, msg="invalid connect code")

    async def _runner(*_a: Any, **_k: Any) -> dict:
        raise exc

    monkeypatch.setattr(commands_mod, "activate_and_maybe_restart", _runner)
    monkeypatch.setattr(commands_mod, "resolve_activation_base_url", lambda: "https://example.invalid")

    with pytest.raises(ClawChatApiError) as info:
        await commands_mod.handle_clawchat_activate_command("CODE")
    assert info.value is exc


@pytest.mark.parametrize("data", [None, {"hint": HINT, "next_action": "ask_owner_new_code"}])
def test_gateway_setup_prints_guidance_then_still_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], data: dict | None
) -> None:
    from clawchat_gateway import setup as setup_mod

    exc = _api_error(16023, data, msg="connect code expired")

    async def _runner(*_a: Any, **_k: Any) -> dict:
        raise exc

    answers = iter(["CODE", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(setup_mod, "activate_and_maybe_restart", _runner)

    with pytest.raises(ClawChatApiError) as info:
        setup_mod.setup_clawchat_platform()
    assert info.value is exc
    out = capsys.readouterr().out
    if data is None:
        assert "next_action" not in out and "hint" not in out
    else:
        assert "clawchat: next_action=ask_owner_new_code" in out
        assert f"clawchat: hint: {HINT}" in out


def test_non_200_envelope_keeps_guidance_data(monkeypatch):
    """A proxy-level non-200 (e.g. 429) still carries the envelope's data."""
    import io
    import json as _json
    from urllib.error import HTTPError

    body = _json.dumps(
        {
            "code": 16025,
            "msg": "rate limited",
            "data": {"hint": HINT, "next_action": "wait_retry", "retry_after_seconds": 60},
        }
    ).encode("utf-8")

    def _urlopen(request, timeout=None):
        raise HTTPError(request.full_url, 429, "Too Many Requests", None, io.BytesIO(body))

    monkeypatch.setattr(api_mod, "urlopen", _urlopen)
    client = ClawChatApiClient(base_url="https://api.example.test")
    import asyncio

    with pytest.raises(ClawChatApiError) as info:
        asyncio.run(client.agents_connect(code="K7RM4TQP"))
    exc = info.value
    assert exc.status == 429
    assert exc.code == 16025
    assert exc.next_action == "wait_retry"
    assert exc.retry_after_seconds == 60
    assert exc.hint == HINT
