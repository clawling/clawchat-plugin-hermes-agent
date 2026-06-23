from __future__ import annotations

import json

import pytest

from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError


def _client() -> ClawChatApiClient:
    return ClawChatApiClient(base_url="https://api.example.com", token="t", device_id="d1")


# ---------------------------------------------------------------------------
# register_app
# ---------------------------------------------------------------------------


async def test_register_app_posts_payload(monkeypatch) -> None:
    client = _client()
    captured: dict = {}

    async def fake_call(method, path, *, body=None, extra_headers=None):
        captured.update(method=method, path=path, body=body)
        return {"app": {"id": "agtapp_1", "app_id": "lw1", "name": "Dash", "url": "https://x"}}

    monkeypatch.setattr(client, "_call_json", fake_call)
    res = await client.register_app(name="Dash", app_id="lw1", url="https://x")

    assert captured["method"] == "POST"
    assert captured["path"] == "/v1/agents/me/apps"
    assert b"lw1" in captured["body"]
    assert res["app"]["app_id"] == "lw1"


async def test_register_app_body_contains_all_fields(monkeypatch) -> None:
    client = _client()
    captured: dict = {}

    async def fake_call(method, path, *, body=None, extra_headers=None):
        captured["body"] = body
        return {"app": {}}

    monkeypatch.setattr(client, "_call_json", fake_call)
    await client.register_app(name="MyApp", app_id="lw2", url="https://example.com")

    payload = json.loads(captured["body"])
    assert payload == {"name": "MyApp", "app_id": "lw2", "url": "https://example.com"}


async def test_register_app_validates_empty_name(monkeypatch) -> None:
    client = _client()
    with pytest.raises(ClawChatApiError) as exc_info:
        await client.register_app(name="   ", app_id="lw1", url="https://x")
    assert exc_info.value.kind == "validation"


async def test_register_app_validates_empty_app_id(monkeypatch) -> None:
    client = _client()
    with pytest.raises(ClawChatApiError) as exc_info:
        await client.register_app(name="Dash", app_id="", url="https://x")
    assert exc_info.value.kind == "validation"


async def test_register_app_validates_empty_url(monkeypatch) -> None:
    client = _client()
    with pytest.raises(ClawChatApiError) as exc_info:
        await client.register_app(name="Dash", app_id="lw1", url="  ")
    assert exc_info.value.kind == "validation"


# ---------------------------------------------------------------------------
# list_apps
# ---------------------------------------------------------------------------


async def test_list_apps_gets_correct_path(monkeypatch) -> None:
    client = _client()
    captured: dict = {}

    async def fake_call(method, path, *, body=None, extra_headers=None):
        captured.update(method=method, path=path)
        return {"apps": []}

    monkeypatch.setattr(client, "_call_json", fake_call)
    res = await client.list_apps()

    assert captured["method"] == "GET"
    assert captured["path"] == "/v1/agents/me/apps"
    assert res == {"apps": []}


# ---------------------------------------------------------------------------
# unregister_app
# ---------------------------------------------------------------------------


async def test_unregister_app_deletes_correct_path(monkeypatch) -> None:
    client = _client()
    captured: dict = {}

    async def fake_call(method, path, *, body=None, extra_headers=None):
        captured.update(method=method, path=path)
        return {}

    monkeypatch.setattr(client, "_call_json", fake_call)
    await client.unregister_app("lw1")

    assert captured["method"] == "DELETE"
    assert captured["path"] == "/v1/agents/me/apps/lw1"


async def test_unregister_app_validates_empty_app_id(monkeypatch) -> None:
    client = _client()
    with pytest.raises(ClawChatApiError) as exc_info:
        await client.unregister_app("  ")
    assert exc_info.value.kind == "validation"
