from __future__ import annotations

import asyncio
import socket
from urllib.error import URLError

import pytest

from clawchat_gateway import api_client as api_client_mod
from clawchat_gateway.api_client import (
    ClawChatApiClient,
    ClawChatApiError,
    agents_connect_with_retry,
)


class _FakeClient:
    def __init__(self, behaviors):
        # behaviors: list of either an exception to raise or a dict to return
        self._behaviors = list(behaviors)
        self.calls = 0

    async def agents_connect(self, *, code: str):  # noqa: D401
        self.calls += 1
        outcome = self._behaviors.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr(api_client_mod.asyncio, "sleep", _instant)


async def test_retries_connect_failed_then_succeeds():
    client = _FakeClient([
        ClawChatApiError("transport", "name resolution failed", connect_failed=True),
        {"agent": {"id": "a"}},
    ])
    result = await agents_connect_with_retry(client, code="CODE")
    assert result == {"agent": {"id": "a"}}
    assert client.calls == 2


async def test_does_not_retry_ambiguous_failure():
    # A timeout is ambiguous: the single-use code may already be consumed.
    client = _FakeClient([
        ClawChatApiError("transport", "timed out", connect_failed=False),
        {"agent": {"id": "a"}},
    ])
    with pytest.raises(ClawChatApiError, match="timed out"):
        await agents_connect_with_retry(client, code="CODE")
    assert client.calls == 1


async def test_gives_up_after_max_connect_retries():
    client = _FakeClient([
        ClawChatApiError("transport", "connection refused", connect_failed=True),
        ClawChatApiError("transport", "connection refused", connect_failed=True),
        ClawChatApiError("transport", "connection refused", connect_failed=True),
        ClawChatApiError("transport", "connection refused", connect_failed=True),
    ])
    with pytest.raises(ClawChatApiError, match="connection refused"):
        await agents_connect_with_retry(client, code="CODE")
    # initial attempt + ACTIVATION_CONNECT_RETRIES
    assert client.calls == api_client_mod.ACTIVATION_CONNECT_RETRIES + 1


# --- _call_json_sync connect_failed classification (the double-spend surface) ---

async def _raises_with(monkeypatch, exc):
    def fake_urlopen(*_a, **_k):
        raise exc

    monkeypatch.setattr(api_client_mod, "urlopen", fake_urlopen)
    client = ClawChatApiClient(base_url="https://example.com")
    with pytest.raises(ClawChatApiError) as ei:
        await client.get_my_profile()
    return ei.value


async def test_connection_refused_is_connect_failed(monkeypatch):
    err = await _raises_with(monkeypatch, URLError(ConnectionRefusedError()))
    assert err.connect_failed is True


async def test_dns_failure_is_connect_failed(monkeypatch):
    err = await _raises_with(monkeypatch, URLError(socket.gaierror("name resolution")))
    assert err.connect_failed is True


async def test_timeout_is_not_connect_failed(monkeypatch):
    # urlopen surfaces timeouts as a bare TimeoutError, not URLError.
    err = await _raises_with(monkeypatch, TimeoutError("timed out"))
    assert err.connect_failed is False


async def test_generic_url_error_is_not_connect_failed(monkeypatch):
    err = await _raises_with(monkeypatch, URLError("some other reason"))
    assert err.connect_failed is False


# --- per-attempt ceiling (bounds unbounded DNS) ---

async def test_attempt_ceiling_times_out_and_is_not_retried():
    class HangClient:
        def __init__(self):
            self.calls = 0

        async def agents_connect(self, *, code: str):
            self.calls += 1
            await asyncio.Event().wait()  # never completes

    client = HangClient()
    with pytest.raises(ClawChatApiError, match="timed out"):
        await agents_connect_with_retry(client, code="X", attempt_ceiling=0.05)
    # A ceiling hit is ambiguous, so it must NOT be retried.
    assert client.calls == 1
