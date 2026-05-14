from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path


class _Ctx:
    def __init__(self) -> None:
        self.tools = {}
        self.skills = {}
        self.hooks = {}

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools[name] = {
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            **kwargs,
        }

    def register_skill(self, name, path, description=""):
        self.skills[name] = {"path": path, "description": description}

    def register_hook(self, name, handler):
        self.hooks.setdefault(name, []).append(handler)


def _load_root_plugin():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_git_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_git_plugin_registers_tools_and_skill(monkeypatch):
    module = _load_root_plugin()
    monkeypatch.setattr(module, "_install_gateway", lambda: None)
    ctx = _Ctx()

    module.register(ctx)

    assert set(ctx.tools) == {
        "clawchat_activate",
        "clawchat_get_account_profile",
        "clawchat_get_user_profile",
        "clawchat_list_account_friends",
        "clawchat_update_account_profile",
        "clawchat_upload_avatar_image",
        "clawchat_upload_media_file",
    }
    assert ctx.tools["clawchat_activate"]["toolset"] == "clawchat"
    assert ctx.tools["clawchat_activate"]["is_async"] is True
    assert ctx.tools["clawchat_update_account_profile"]["is_async"] is True
    assert ctx.tools["clawchat_upload_avatar_image"]["is_async"] is True
    assert "upload" in ctx.tools["clawchat_upload_avatar_image"]["schema"]["description"]
    assert "clawchat_update_account_profile" in ctx.tools["clawchat_upload_avatar_image"]["schema"]["description"]
    assert "clawchat" in ctx.skills


def test_git_plugin_handlers_accept_task_id(monkeypatch):
    module = _load_root_plugin()

    from clawchat_gateway import tools

    async def fake_update_account_profile(nickname=None, avatar_url=None, bio=None):
        return {"updated": {"nickname": nickname, "avatar_url": avatar_url, "bio": bio}}

    monkeypatch.setattr(tools, "update_account_profile", fake_update_account_profile)

    result = asyncio.run(
        module._handle_clawchat_update_account_profile(
            {"nickname": "bot", "avatar_url": "https://cdn/avatar.png", "bio": "hi"},
            task_id="trace-123",
        )
    )

    assert json.loads(result) == {
        "updated": {"nickname": "bot", "avatar_url": "https://cdn/avatar.png", "bio": "hi"}
    }


def test_clawchat_activate_handler_uses_shared_activation_runner(monkeypatch):
    module = _load_root_plugin()
    called = []

    async def fake_runner(code: str, *, base_url: str, restart: bool):
        called.append((code, base_url, restart))
        return {"ok": True, "user_id": "agent-1"}

    import clawchat_gateway.activate as activate_mod

    monkeypatch.setattr(activate_mod, "activate_and_maybe_restart", fake_runner)

    result = asyncio.run(
        module._handle_clawchat_activate(
            {"code": "ABC123", "baseUrl": "https://chat.example"},
            task_id="trace-activate",
        )
    )

    assert called == [("ABC123", "https://chat.example", True)]
    assert json.loads(result) == {"ok": True, "user_id": "agent-1"}
