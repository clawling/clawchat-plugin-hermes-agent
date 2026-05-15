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
        self.platforms = {}
        self.commands = {}

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

    def register_platform(self, name, label, adapter_factory, check_fn, **kwargs):
        self.platforms[name] = {
            "label": label,
            "adapter_factory": adapter_factory,
            "check_fn": check_fn,
            **kwargs,
        }

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


def _load_root_plugin():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_git_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_git_plugin_registers_tools_without_skill(monkeypatch, tmp_path):
    module = _load_root_plugin()
    monkeypatch.setattr(module, "_configure_runtime_defaults", lambda: None, raising=False)
    skill = tmp_path / "skills" / "clawchat" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: clawchat\n---\n", encoding="utf-8")
    monkeypatch.setattr(module, "_plugin_dir", lambda: tmp_path)
    ctx = _Ctx()

    module.register(ctx)

    assert set(ctx.tools) == {
        "clawchat_get_account_profile",
        "clawchat_get_user_profile",
        "clawchat_list_account_friends",
        "clawchat_update_account_profile",
        "clawchat_upload_avatar_image",
        "clawchat_upload_media_file",
    }
    assert ctx.tools["clawchat_update_account_profile"]["is_async"] is True
    assert ctx.tools["clawchat_upload_avatar_image"]["is_async"] is True
    assert "upload" in ctx.tools["clawchat_upload_avatar_image"]["schema"]["description"]
    assert "clawchat_update_account_profile" in ctx.tools["clawchat_upload_avatar_image"]["schema"]["description"]
    assert ctx.skills == {}
    assert "clawchat-activate" in ctx.commands


def test_git_plugin_handlers_accept_task_id(monkeypatch):
    _load_root_plugin()

    from clawchat_gateway import plugin_tools
    from clawchat_gateway import tools

    async def fake_update_account_profile(nickname=None, avatar_url=None, bio=None):
        return {"updated": {"nickname": nickname, "avatar_url": avatar_url, "bio": bio}}

    monkeypatch.setattr(tools, "update_account_profile", fake_update_account_profile)

    result = asyncio.run(
        plugin_tools.handle_clawchat_update_account_profile(
            {"nickname": "bot", "avatar_url": "https://cdn/avatar.png", "bio": "hi"},
            task_id="trace-123",
        )
    )

    assert json.loads(result) == {
        "updated": {"nickname": "bot", "avatar_url": "https://cdn/avatar.png", "bio": "hi"}
    }
