from __future__ import annotations

import copy
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest
import yaml

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.runtime_defaults import configure_clawchat_display_defaults


def _load_activate(monkeypatch, tmp_path, raw_config):
    saved_config = {}
    env_values = {}

    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")
    hermes_config.get_config_path = lambda: str(tmp_path / "config.yaml")
    hermes_config.get_env_path = lambda: str(tmp_path / ".env")
    hermes_config.read_raw_config = lambda: copy.deepcopy(raw_config)
    hermes_config.remove_env_value = lambda key: env_values.update({key: None})
    hermes_config.save_config = lambda config: saved_config.update(copy.deepcopy(config))
    hermes_config.save_env_value = lambda key, value: env_values.update({key: value})

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)

    import clawchat_gateway.activate as activate

    return importlib.reload(activate), saved_config, env_values


def test_platform_config_ignores_reply_mode_surface(monkeypatch):
    monkeypatch.setenv("CLAWCHAT_REPLY_MODE", "static")
    platform_config = SimpleNamespace(
        extra={
            "websocket_url": "wss://example.test/ws",
            "reply_mode": "stream",
        }
    )

    config = ClawChatConfig.from_platform_config(platform_config)

    assert not hasattr(config, "reply_mode")


def test_persist_activation_does_not_create_reply_mode_or_streaming(monkeypatch, tmp_path):
    activate, saved_config, env_values = _load_activate(monkeypatch, tmp_path, {})

    activate.persist_activation(
        access_token="token",
        user_id="user",
        owner_user_id="owner",
        agent_id="agent",
        refresh_token=None,
        base_url="https://app.clawling.com",
    )

    extra = saved_config["platforms"]["clawchat"]["extra"]
    assert "reply_mode" not in extra
    assert "streaming" not in saved_config
    assert env_values == {
        "CLAWCHAT_TOKEN": "token",
        "CLAWCHAT_REFRESH_TOKEN": None,
    }


def test_persist_activation_preserves_legacy_reply_mode_and_existing_streaming(monkeypatch, tmp_path):
    activate, saved_config, _env_values = _load_activate(
        monkeypatch,
        tmp_path,
        {
            "platforms": {"clawchat": {"extra": {"reply_mode": "static"}}},
            "streaming": {
                "enabled": False,
                "transport": "none",
                "edit_interval": 9,
                "buffer_threshold": 99,
            },
        },
    )

    activate.persist_activation(
        access_token="token",
        user_id="user",
        owner_user_id="owner",
        agent_id="",
        refresh_token="refresh",
        base_url="https://chat.example.test",
    )

    extra = saved_config["platforms"]["clawchat"]["extra"]
    assert extra["reply_mode"] == "static"
    assert saved_config["streaming"] == {
        "enabled": False,
        "transport": "none",
        "edit_interval": 9,
        "buffer_threshold": 99,
    }


def test_runtime_defaults_do_not_write_or_remove_reply_mode_or_streaming(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    config_path = home / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
platforms:
  clawchat:
    extra:
      reply_mode: static
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    changed = configure_clawchat_display_defaults()

    assert changed is True
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    extra = config["platforms"]["clawchat"]["extra"]
    assert extra["reply_mode"] == "static"
    assert "streaming" not in config
    assert extra["show_tools_output"] is False
    assert extra["show_think_output"] is False
    assert config["display"]["platforms"]["clawchat"] == {
        "tool_progress": "off",
        "show_reasoning": False,
    }


def test_runtime_defaults_preserve_existing_streaming(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    config_path = home / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
platforms:
  clawchat:
    extra: {}
streaming:
  enabled: false
  transport: none
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    configure_clawchat_display_defaults()

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["streaming"] == {"enabled": False, "transport": "none"}


class _FakeConnection:
    def __init__(self):
        self.frames = []
        self.send_results = []

    async def send_frame(self, frame, **kwargs):
        self.frames.append((frame, kwargs))
        if self.send_results:
            return self.send_results.pop(0)
        return True


class _FakeStore:
    def __init__(self):
        self.claimed = []
        self.updated = []
        self.inserted = []

    def claim_message_once(self, **kwargs):
        self.claimed.append(kwargs)
        return True

    def update_message_by_identity(self, **kwargs):
        self.updated.append(kwargs)

    def insert_message(self, **kwargs):
        self.inserted.append(kwargs)


def _load_adapter_class(monkeypatch):
    gateway = ModuleType("gateway")
    gateway_config = ModuleType("gateway.config")
    gateway_platforms = ModuleType("gateway.platforms")
    gateway_base = ModuleType("gateway.platforms.base")

    class _Platform(str):
        CLAWCHAT = "clawchat"

    class _BasePlatformAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    class _MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MessageType:
        TEXT = "text"

    class _SendResult:
        def __init__(self, success, error=None, message_id=None):
            self.success = success
            self.error = error
            self.message_id = message_id

    gateway_config.Platform = _Platform
    gateway_base.BasePlatformAdapter = _BasePlatformAdapter
    gateway_base.MessageEvent = _MessageEvent
    gateway_base.MessageType = _MessageType
    gateway_base.SendResult = _SendResult
    gateway_platforms.base = gateway_base
    gateway.config = gateway_config
    gateway.platforms = gateway_platforms

    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "gateway.platforms", gateway_platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gateway_base)

    import clawchat_gateway.adapter as adapter_module

    return importlib.reload(adapter_module).ClawChatAdapter


def _adapter(monkeypatch, extra=None):
    ClawChatAdapter = _load_adapter_class(monkeypatch)
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._clawchat_config = ClawChatConfig.from_platform_config(
        SimpleNamespace(
            extra={
                "websocket_url": "wss://example.test/ws",
                "token": "token",
                "user_id": "usr_agent",
                **(extra or {}),
            }
        )
    )
    adapter._connection = _FakeConnection()
    adapter._store = _FakeStore()
    adapter._memory_root = None
    adapter._active_runs_by_id = {}
    adapter._active_chat_runs = {}
    adapter._completed_run_ids = set()
    adapter._completed_run_order = []
    adapter._run_counter = 0
    return adapter


def _sent_events(adapter):
    return [frame["event"] for frame, _kwargs in adapter._connection.frames]


STREAM_LIFECYCLE_EVENTS = {
    "message.created",
    "message.add",
    "message.done",
    "message.failed",
}


@pytest.mark.asyncio
async def test_adapter_uses_complete_messages_even_with_old_streaming_config(monkeypatch):
    adapter = _adapter(
        monkeypatch,
        {
            "reply_mode": "stream",
            "stream": {"flush_interval_ms": 1},
        }
    )

    result = await adapter.send(
        "chat-1",
        "first chunk",
        reply_to="incoming-1",
        metadata={"notify": True, "chat_type": "direct"},
    )
    await adapter.edit_message(
        "chat-1",
        result.message_id,
        "first chunk and second chunk",
        finalize=True,
    )

    assert result.success is True
    assert "message.reply" in _sent_events(adapter)
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))


@pytest.mark.asyncio
async def test_adapter_run_complete_does_not_emit_stream_lifecycle_frames(monkeypatch):
    adapter = _adapter(monkeypatch, {"reply_mode": "stream"})

    result = await adapter.send(
        "chat-1",
        "draft",
        metadata={"notify": True, "chat_type": "direct"},
    )
    await adapter.on_run_complete(
        "chat-1",
        "final response",
        message_id=result.message_id,
    )

    assert _sent_events(adapter).count("message.reply") >= 1
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))


@pytest.mark.asyncio
async def test_adapter_run_failed_does_not_emit_stream_lifecycle_frames(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft",
        metadata={"notify": True, "chat_type": "direct"},
    )

    await adapter.on_run_failed(
        "chat-1",
        "runtime failed",
        message_id=result.message_id,
    )

    assert "message.failed" not in _sent_events(adapter)
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))
    assert result.message_id not in adapter._active_runs_by_id
    assert result.message_id in adapter._completed_run_ids
    assert adapter._store.inserted[-1]["event_type"] == "message.failed"
    assert adapter._store.inserted[-1]["text"] == "runtime failed"


@pytest.mark.asyncio
async def test_adapter_does_not_require_clawchat_config_reply_mode_attribute(monkeypatch):
    adapter = _adapter(monkeypatch, {"reply_mode": "stream"})
    assert not hasattr(adapter._clawchat_config, "reply_mode")

    result = await adapter.send(
        "chat-1",
        "complete response",
        metadata={"notify": True, "chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]


@pytest.mark.asyncio
async def test_edit_complete_reply_update_failure_is_visible(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft",
        metadata={"notify": True, "chat_type": "direct"},
    )
    adapter._connection.send_results.append(False)

    edit_result = await adapter.edit_message(
        "chat-1",
        result.message_id,
        "undelivered final",
    )

    assert edit_result.success is False
    assert edit_result.error == "clawchat complete reply update dropped"
    assert adapter._active_runs_by_id[result.message_id].last_text == "draft"
    assert adapter._store.updated[-1]["event_type"] == "message.failed"
    assert adapter._store.updated[-1]["text"] == "clawchat complete reply update dropped"


@pytest.mark.asyncio
async def test_run_complete_update_failure_keeps_run_active_and_failed_visible(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft",
        metadata={"notify": True, "chat_type": "direct"},
    )
    adapter._connection.send_results.append(False)

    complete_result = await adapter.on_run_complete(
        "chat-1",
        "undelivered final",
        message_id=result.message_id,
    )

    assert complete_result.success is False
    assert complete_result.error == "clawchat complete reply update dropped"
    assert adapter._active_runs_by_id[result.message_id].last_text == "draft"
    assert result.message_id not in adapter._completed_run_ids
    assert adapter._store.updated[-1]["event_type"] == "message.failed"
    assert adapter._store.updated[-1]["text"] == "clawchat complete reply update dropped"
