from __future__ import annotations

import json

import pytest

from clawchat_gateway import plugin_tools, tools


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def send_friend_request(self, *, user_id: str, greeting: str | None = None) -> dict:
        self.calls.append(("send", {"user_id": user_id, "greeting": greeting}))
        return {"request": {"id": 7, "to_user_id": user_id, "greeting": greeting}}

    async def list_friend_requests(self, *, direction: str = "incoming") -> dict:
        self.calls.append(("list", direction))
        return {"requests": [{"id": 7, "status": "pending"}]}

    async def accept_friend_request(self, request_id: int) -> dict:
        self.calls.append(("accept", request_id))
        return {"ok": True}

    async def reject_friend_request(self, request_id: int) -> dict:
        self.calls.append(("reject", request_id))
        return {"ok": True}

    async def remove_friend(self, friend_user_id: str) -> dict:
        self.calls.append(("remove", friend_user_id))
        return {"ok": True}


@pytest.mark.asyncio
async def test_friendship_tool_handlers_map_params_to_client(monkeypatch):
    client = Client()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    assert await tools.send_friend_request("usr_peer", "hello") == {
        "request": {"id": 7, "to_user_id": "usr_peer", "greeting": "hello"}
    }
    assert await tools.list_friend_requests("outgoing") == {
        "requests": [{"id": 7, "status": "pending"}]
    }
    assert await tools.accept_friend_request(7) == {"ok": True}
    assert await tools.reject_friend_request(8) == {"ok": True}
    assert await tools.remove_friend("usr_peer") == {"ok": True}

    assert client.calls == [
        ("send", {"user_id": "usr_peer", "greeting": "hello"}),
        ("list", "outgoing"),
        ("accept", 7),
        ("reject", 8),
        ("remove", "usr_peer"),
    ]


@pytest.mark.asyncio
async def test_friendship_tool_handlers_validate_inputs(monkeypatch):
    client = Client()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    assert (await tools.send_friend_request("", None))["error"] == "validation"
    assert (await tools.list_friend_requests("sideways"))["error"] == "validation"
    assert (await tools.accept_friend_request(0))["error"] == "validation"
    assert (await tools.reject_friend_request("abc"))["error"] == "validation"
    assert (await tools.remove_friend(""))["error"] == "validation"
    assert client.calls == []


def test_register_tools_includes_friendship_tool_schemas():
    registered: dict[str, dict] = {}

    class Ctx:
        def register_tool(self, name, _namespace, schema, handler, **_kwargs):
            registered[name] = {"schema": schema, "handler": handler}

    plugin_tools.register_tools(Ctx())

    assert "clawchat_upload_media_file" not in registered
    assert set(registered) >= {
        "clawchat_send_friend_request",
        "clawchat_list_friend_requests",
        "clawchat_accept_friend_request",
        "clawchat_reject_friend_request",
        "clawchat_remove_friend",
    }
    assert registered["clawchat_send_friend_request"]["schema"]["parameters"]["required"] == ["userId"]
    assert registered["clawchat_list_friend_requests"]["schema"]["parameters"]["properties"]["direction"][
        "enum"
    ] == ["incoming", "outgoing"]
    assert registered["clawchat_accept_friend_request"]["schema"]["parameters"]["required"] == ["requestId"]
    assert registered["clawchat_reject_friend_request"]["schema"]["parameters"]["required"] == ["requestId"]
    assert registered["clawchat_remove_friend"]["schema"]["parameters"]["required"] == ["friendUserId"]


@pytest.mark.asyncio
async def test_plugin_tool_handlers_return_json(monkeypatch):
    client = Client()
    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))

    result = await plugin_tools.handle_clawchat_send_friend_request(
        {"userId": "usr_peer", "greeting": "hello"}
    )

    assert json.loads(result)["request"]["to_user_id"] == "usr_peer"
