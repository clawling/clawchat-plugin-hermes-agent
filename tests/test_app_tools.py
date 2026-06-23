"""Tests for register_app / list_apps / unregister_app tool wrappers and handlers."""
from __future__ import annotations

import json

import pytest

from clawchat_gateway import plugin_tools, tools


class AppClient:
    """Minimal stub mirroring ClawChatApiClient app methods."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def register_app(self, *, name: str, app_id: str, url: str) -> dict:
        self.calls.append(("register", {"name": name, "app_id": app_id, "url": url}))
        return {"app": {"id": "agtapp_1", "app_id": app_id, "name": name, "url": url}}

    async def list_apps(self) -> dict:
        self.calls.append(("list",))
        return {"apps": [{"id": "agtapp_1", "app_id": "lw1", "name": "Dash", "url": "https://x"}]}

    async def unregister_app(self, app_id: str) -> dict:
        self.calls.append(("unregister", app_id))
        return {"ok": True}


# ---------------------------------------------------------------------------
# tools.py wrapper — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_app_wrapper_validates_empty_name(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.register_app(name="", app_id="lw1", url="https://x")
    assert res.get("error") == "validation"
    assert client.calls == []


@pytest.mark.asyncio
async def test_register_app_wrapper_validates_empty_app_id(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.register_app(name="Dash", app_id="", url="https://x")
    assert res.get("error") == "validation"
    assert client.calls == []


@pytest.mark.asyncio
async def test_register_app_wrapper_validates_empty_url(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.register_app(name="Dash", app_id="lw1", url="  ")
    assert res.get("error") == "validation"
    assert client.calls == []


@pytest.mark.asyncio
async def test_unregister_app_wrapper_validates_empty_app_id(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.unregister_app(app_id="")
    assert res.get("error") == "validation"
    assert client.calls == []


# ---------------------------------------------------------------------------
# tools.py wrapper — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_app_wrapper_calls_client(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.register_app(name="Dash", app_id="lw1", url="https://x")
    assert res == {"app": {"id": "agtapp_1", "app_id": "lw1", "name": "Dash", "url": "https://x"}}
    assert client.calls == [("register", {"name": "Dash", "app_id": "lw1", "url": "https://x"})]


@pytest.mark.asyncio
async def test_list_apps_wrapper_calls_client(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.list_apps()
    assert "apps" in res
    assert client.calls == [("list",)]


@pytest.mark.asyncio
async def test_unregister_app_wrapper_calls_client(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    res = await tools.unregister_app(app_id="lw1")
    assert res == {"ok": True}
    assert client.calls == [("unregister", "lw1")]


# ---------------------------------------------------------------------------
# plugin_tools.py handlers — tolerate both appId and app_id keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_register_app_accepts_appId(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_register_app(
        {"name": "Dash", "appId": "lw1", "url": "https://x"}
    )
    payload = json.loads(result)
    assert payload["app"]["app_id"] == "lw1"


@pytest.mark.asyncio
async def test_handle_register_app_accepts_app_id(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_register_app(
        {"name": "Dash", "app_id": "lw2", "url": "https://x"}
    )
    payload = json.loads(result)
    assert payload["app"]["app_id"] == "lw2"


@pytest.mark.asyncio
async def test_handle_list_apps_returns_json(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_list_apps({})
    payload = json.loads(result)
    assert "apps" in payload


@pytest.mark.asyncio
async def test_handle_unregister_app_accepts_appId(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_unregister_app({"appId": "lw1"})
    payload = json.loads(result)
    assert payload == {"ok": True}
    assert client.calls == [("unregister", "lw1")]


@pytest.mark.asyncio
async def test_handle_unregister_app_accepts_app_id(monkeypatch):
    client = AppClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_unregister_app({"app_id": "lw2"})
    payload = json.loads(result)
    assert payload == {"ok": True}
    assert client.calls == [("unregister", "lw2")]


# ---------------------------------------------------------------------------
# register_tools — schema registration
# ---------------------------------------------------------------------------


def test_register_tools_includes_app_tool_schemas():
    registered: dict[str, dict] = {}

    class Ctx:
        def register_tool(self, name, _namespace, schema, handler, **_kwargs):
            registered[name] = {"schema": schema, "handler": handler}

    plugin_tools.register_tools(Ctx())

    assert set(registered) >= {
        "clawchat_register_app",
        "clawchat_list_apps",
        "clawchat_unregister_app",
    }
    reg_schema = registered["clawchat_register_app"]["schema"]
    assert set(reg_schema["parameters"]["required"]) == {"name", "appId", "url"}
    unreg_schema = registered["clawchat_unregister_app"]["schema"]
    assert unreg_schema["parameters"]["required"] == ["appId"]
