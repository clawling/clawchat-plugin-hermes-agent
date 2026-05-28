from __future__ import annotations

import copy
import importlib
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path

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


def test_platform_config_exposes_no_reply_mode_or_stream_tuning():
    platform_config = SimpleNamespace(
        extra={
            "websocket_url": "wss://example.test/ws",
        }
    )

    config = ClawChatConfig.from_platform_config(platform_config)

    assert not hasattr(config, "reply_mode")
    assert not hasattr(config, "stream_flush_interval_ms")
    assert not hasattr(config, "stream_min_chunk_chars")
    assert not hasattr(config, "stream_max_buffer_chars")
    assert not hasattr(config, "show_tools_output")
    assert not hasattr(config, "show_think_output")


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
    assert "show_tools_output" not in extra
    assert "show_think_output" not in extra
    assert "streaming" not in saved_config
    assert saved_config["display"]["platforms"]["clawchat"] == {
        "tool_progress": "off",
        "long_running_notifications": False,
        "show_reasoning": False,
    }
    assert env_values == {
        "CLAWCHAT_TOKEN": "token",
        "CLAWCHAT_REFRESH_TOKEN": None,
    }


def test_runtime_defaults_write_display_defaults_without_streaming(monkeypatch, tmp_path):
    home = tmp_path / "hermes"
    config_path = home / "config.yaml"
    config_path.parent.mkdir()
    config_path.write_text(
        """
platforms:
  clawchat:
    extra: {}
""".lstrip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    changed = configure_clawchat_display_defaults()

    assert changed is True
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    extra = config["platforms"]["clawchat"]["extra"]
    assert "streaming" not in config
    assert "show_tools_output" not in extra
    assert "show_think_output" not in extra
    assert config["display"]["platforms"]["clawchat"] == {
        "tool_progress": "off",
        "long_running_notifications": False,
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


def _install_fake_approval_module(monkeypatch, *, blocking=True):
    calls = []
    tools_module = sys.modules.get("tools") or ModuleType("tools")
    approval_module = ModuleType("tools.approval")

    def has_blocking_approval(session_key):
        calls.append(("has", session_key))
        return blocking

    def resolve_gateway_approval(session_key, choice, resolve_all=False):
        calls.append(("resolve", session_key, choice, resolve_all))
        return 1 if blocking else 0

    approval_module.has_blocking_approval = has_blocking_approval
    approval_module.resolve_gateway_approval = resolve_gateway_approval
    tools_module.approval = approval_module
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.approval", approval_module)
    return calls


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
    adapter._inbound_window = {}
    adapter._known_chat_types = {}
    adapter._owner_approval_routes = {}
    adapter._active_runs_by_id = {}
    adapter._active_chat_runs = {}
    adapter._completed_run_ids = set()
    adapter._completed_run_order = []
    adapter._run_counter = 0
    return adapter


def _sent_events(adapter):
    return [frame["event"] for frame, _kwargs in adapter._connection.frames]


def _load_plugin_module():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_tools_plugin", plugin_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


STREAM_LIFECYCLE_EVENTS = {
    "message.created",
    "message.add",
    "message.done",
    "message.failed",
}


@pytest.mark.asyncio
async def test_adapter_buffers_official_hermes_stream_lifecycle_until_finalize(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "Hey! I'm ▉",
        reply_to="incoming-1",
        metadata={"notify": True, "chat_type": "direct"},
    )
    edit_result = await adapter.edit_message(
        "chat-1",
        result.message_id,
        "Hey! I'm doing well ▉",
        stream_id="abc",
        chunk_index=3,
    )

    assert result.success is True
    assert edit_result.success is True
    assert adapter.REQUIRES_EDIT_FINALIZE is True
    assert _sent_events(adapter) == []

    final_result = await adapter.edit_message(
        "chat-1",
        result.message_id,
        "Hey! I'm doing well",
        finalize=True,
    )

    assert final_result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "Hey! I'm doing well"}
    ]
    assert "▉" not in str(frame)
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))


@pytest.mark.asyncio
async def test_adapter_run_complete_sends_one_final_complete_message(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "draft ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )
    await adapter.edit_message(
        "chat-1",
        result.message_id,
        "intermediate ▉",
    )

    assert _sent_events(adapter) == []

    await adapter.on_run_complete(
        "chat-1",
        "final response",
        message_id=result.message_id,
    )

    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "final response"}
    ]
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))


@pytest.mark.asyncio
async def test_non_stream_send_sends_complete_message_immediately(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "final response",
        metadata={"notify": True, "chat_type": "direct"},
    )

    assert result.success is True
    assert result.message_id not in adapter._active_runs_by_id
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "final response"}
    ]


@pytest.mark.asyncio
async def test_group_text_that_resembles_tool_progress_is_not_filtered_by_adapter(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "group-1",
        'search_docs: "runtime settings"',
        metadata={"notify": True, "chat_type": "group"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": 'search_docs: "runtime settings"'}
    ]


@pytest.mark.asyncio
async def test_group_tool_call_text_is_not_filtered_by_adapter(monkeypatch):
    adapter = _adapter(monkeypatch)
    content = '<tool_call>{"name":"search_docs"}</tool_call>'

    result = await adapter.send(
        "group-1",
        content,
        metadata={"notify": True, "chat_type": "group"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": content}
    ]


@pytest.mark.asyncio
async def test_group_think_text_is_not_filtered_by_adapter(monkeypatch):
    adapter = _adapter(monkeypatch)
    content = "<think>private draft</think>visible response"

    result = await adapter.send(
        "group-1",
        content,
        metadata={"notify": True, "chat_type": "group"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": content}
    ]


@pytest.mark.asyncio
async def test_group_approval_prompt_without_chat_type_metadata_routes_to_owner(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._known_chat_types = {"group-1": "group"}
    content = (
        "Reply `/approve` to execute, `/approve session` to approve this pattern "
        "for the session, `/approve always` to approve permanently, or `/deny` to cancel."
    )

    result = await adapter.send("group-1", content)

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["chat_id"] == "usr_owner"
    fragments = frame["payload"]["message"]["body"]["fragments"]
    assert fragments[0]["kind"] == "text"
    assert "ClawChat group group-1 requires owner attention." in fragments[0]["text"]
    assert fragments[1]["kind"] == "approval_request"
    assert [action["id"] for action in fragments[1]["actions"]] == ["approve", "deny"]


@pytest.mark.asyncio
async def test_group_exec_approval_routes_to_owner_with_session_payload(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._known_chat_types = {"group-1": "group"}

    result = await adapter.send_exec_approval(
        chat_id="group-1",
        command="rm -rf /tmp/example",
        session_key="agent:main:clawchat:group:group-1:usr_sender",
        description="dangerous command",
    )

    assert result.success is True
    frame = adapter._connection.frames[0][0]
    assert frame["chat_id"] == "usr_owner"
    fragments = frame["payload"]["message"]["body"]["fragments"]
    assert fragments[0]["kind"] == "text"
    approval = fragments[1]
    assert approval["kind"] == "approval_request"
    assert approval["actions"][0]["payload"] == {
        "type": "exec_approval",
        "session_key": "agent:main:clawchat:group:group-1:usr_sender",
        "decision": "once",
    }


@pytest.mark.asyncio
async def test_owner_direct_approve_resolves_forwarded_group_approval(monkeypatch):
    calls = _install_fake_approval_module(monkeypatch)
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._owner_approval_routes = {
        "usr_owner": "agent:main:clawchat:group:group-1:usr_sender"
    }
    frame = {
        "event": "message.send",
        "chat_id": "dm-owner",
        "chat_type": "direct",
        "sender": {"id": "usr_owner", "name": "Owner"},
        "payload": {
            "message_id": "in-1",
            "message": {
                "body": {"fragments": [{"kind": "text", "text": "/approve always"}]},
                "context": {"mentions": [], "reply": None},
            },
        },
    }

    await adapter._on_message(frame)

    assert ("resolve", "agent:main:clawchat:group:group-1:usr_sender", "always", False) in calls
    assert _sent_events(adapter) == ["message.reply"]
    ack_frame = adapter._connection.frames[0][0]
    assert ack_frame["chat_id"] == "dm-owner"


@pytest.mark.asyncio
async def test_owner_interaction_submit_resolves_exec_approval_payload(monkeypatch):
    calls = _install_fake_approval_module(monkeypatch)
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    frame = {
        "event": "interaction.submit",
        "chat_id": "dm-owner",
        "chat_type": "direct",
        "sender": {"id": "usr_owner", "name": "Owner"},
        "payload": {
            "action": {
                "payload": {
                    "type": "exec_approval",
                    "session_key": "agent:main:clawchat:group:group-1:usr_sender",
                    "decision": "session",
                }
            }
        },
    }

    await adapter._on_message(frame)

    assert ("resolve", "agent:main:clawchat:group:group-1:usr_sender", "session", False) in calls
    assert _sent_events(adapter) == ["message.reply"]
    ack_frame = adapter._connection.frames[0][0]
    assert ack_frame["chat_id"] == "dm-owner"


@pytest.mark.asyncio
async def test_send_image_sends_complete_media_immediately(monkeypatch):
    adapter = _adapter(monkeypatch)
    uploaded_urls = []

    async def fake_upload_outbound_media(urls, **_kwargs):
        uploaded_urls.append(list(urls))
        return [
            {
                "kind": "image",
                "url": "https://cdn/uploaded.png",
                "mime": "image/png",
                "size": 12,
                "name": "image.png",
            }
        ]

    monkeypatch.setattr(
        "clawchat_gateway.adapter.upload_outbound_media",
        fake_upload_outbound_media,
    )

    result = await adapter.send_image(
        "chat-1",
        "https://cdn/image.png",
        caption="draft image",
        metadata={"notify": True, "chat_type": "direct"},
    )

    assert result.success is True
    assert uploaded_urls == [["https://cdn/image.png"]]
    assert _sent_events(adapter) == ["message.reply"]
    assert result.message_id not in adapter._active_runs_by_id
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "draft image"},
        {
            "kind": "image",
            "url": "https://cdn/uploaded.png",
            "mime": "image/png",
            "size": 12,
            "name": "image.png",
        },
    ]


@pytest.mark.asyncio
async def test_send_image_file_sends_complete_file_immediately(monkeypatch):
    adapter = _adapter(monkeypatch)
    uploaded_urls = []

    async def fake_upload_outbound_media(urls, **_kwargs):
        uploaded_urls.append(list(urls))
        return [
            {
                "kind": "file",
                "url": "https://cdn/report.pdf",
                "mime": "application/pdf",
                "size": 3456,
                "name": "report.pdf",
            }
        ]

    monkeypatch.setattr(
        "clawchat_gateway.adapter.upload_outbound_media",
        fake_upload_outbound_media,
    )

    result = await adapter.send_image_file(
        "chat-1",
        "/tmp/report.pdf",
        caption="PDF",
        metadata={"notify": True, "chat_type": "direct"},
    )

    assert result.success is True
    assert uploaded_urls == [["/tmp/report.pdf"]]
    assert _sent_events(adapter) == ["message.reply"]
    assert result.message_id not in adapter._active_runs_by_id
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "PDF"},
        {
            "kind": "file",
            "url": "https://cdn/report.pdf",
            "mime": "application/pdf",
            "size": 3456,
            "name": "report.pdf",
        },
    ]


@pytest.mark.asyncio
async def test_send_message_media_files_send_complete_media_immediately(monkeypatch):
    adapter = _adapter(monkeypatch)
    uploaded_urls = []

    async def fake_upload_outbound_media(urls, **kwargs):
        uploaded_urls.append((list(urls), tuple(kwargs["media_local_roots"])))
        return [
            {
                "kind": "file",
                "url": "https://cdn/report.pdf",
                "mime": "application/pdf",
                "size": 3456,
                "name": "report.pdf",
            }
        ]

    monkeypatch.setattr(
        "clawchat_gateway.adapter.upload_outbound_media",
        fake_upload_outbound_media,
    )

    result = await adapter.send(
        "chat-1",
        "PDF",
        metadata={
            "chat_type": "direct",
            "_clawchat_immediate_media_send": True,
        },
        media_files=[("/tmp/report.pdf", False)],
        _clawchat_media_files_validated=True,
    )

    assert result.success is True
    assert uploaded_urls == [(["/tmp/report.pdf"], (str(Path("/tmp").resolve()),))]
    assert _sent_events(adapter) == ["message.reply"]
    assert result.message_id not in adapter._active_runs_by_id
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "PDF"},
        {
            "kind": "file",
            "url": "https://cdn/report.pdf",
            "mime": "application/pdf",
            "size": 3456,
            "name": "report.pdf",
        },
    ]


@pytest.mark.asyncio
async def test_plugin_patches_send_message_media_delivery_for_clawchat(monkeypatch):
    module = _load_plugin_module()
    tools_module = ModuleType("tools")
    send_message_tool_module = ModuleType("tools.send_message_tool")

    async def original_send_to_platform(*_args, **_kwargs):
        return {"error": "original media whitelist"}

    send_message_tool_module._send_to_platform = original_send_to_platform
    tools_module.send_message_tool = send_message_tool_module
    monkeypatch.setitem(sys.modules, "tools", tools_module)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", send_message_tool_module)

    class _Platform:
        value = "clawchat"

    platform = _Platform()

    class _FakeAdapter:
        def __init__(self):
            self.calls = []

        async def send(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(success=True, message_id="msg-1", error=None)

    fake_adapter = _FakeAdapter()
    gateway_run = ModuleType("gateway.run")
    gateway_run._gateway_runner_ref = lambda: SimpleNamespace(adapters={platform: fake_adapter})
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)

    module._patch_send_message_media_delivery()
    result = await send_message_tool_module._send_to_platform(
        platform,
        SimpleNamespace(),
        "chat-1",
        "",
        thread_id="thread-1",
        media_files=[("/tmp/report.pdf", False)],
    )

    assert result == {"success": True, "message_id": "msg-1"}
    assert fake_adapter.calls == [
        {
            "chat_id": "chat-1",
            "content": "",
            "metadata": {
                "thread_id": "thread-1",
                "_clawchat_immediate_media_send": True,
            },
            "media_files": [("/tmp/report.pdf", False)],
            "_clawchat_media_files_validated": True,
        }
    ]


@pytest.mark.asyncio
async def test_on_run_complete_without_message_id_uses_latest_stream_run(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "hello ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )

    complete_result = await adapter.on_run_complete("chat-1", "hello world")

    assert complete_result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message_id"] == result.message_id
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "hello world"}
    ]
    assert not STREAM_LIFECYCLE_EVENTS & set(_sent_events(adapter))


@pytest.mark.asyncio
async def test_duplicate_run_complete_after_finalize_is_idempotent(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "hello ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )
    final_result = await adapter.edit_message(
        "chat-1",
        result.message_id,
        "hello world",
        finalize=True,
    )
    duplicate_result = await adapter.on_run_complete(
        "chat-1",
        "hello world",
        message_id=result.message_id,
    )

    assert final_result.success is True
    assert duplicate_result.success is True
    assert _sent_events(adapter) == ["message.reply"]


@pytest.mark.asyncio
async def test_adapter_run_failed_does_not_emit_stream_lifecycle_frames(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft ▉",
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
    assert adapter._store.inserted[-1]["event_type"] == "message.error"
    assert adapter._store.inserted[-1]["text"] == "runtime failed"


@pytest.mark.asyncio
async def test_edit_buffers_without_transport_failure(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )
    adapter._connection.send_results.append(False)

    edit_result = await adapter.edit_message(
        "chat-1",
        result.message_id,
        "undelivered final",
    )

    assert edit_result.success is True
    assert adapter._active_runs_by_id[result.message_id].last_text == "undelivered final"
    assert _sent_events(adapter) == []
    assert adapter._store.updated == []


@pytest.mark.asyncio
async def test_run_complete_send_failure_keeps_run_active_and_failed_visible(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )
    adapter._connection.send_results.append(False)

    complete_result = await adapter.on_run_complete(
        "chat-1",
        "undelivered final",
        message_id=result.message_id,
    )

    assert complete_result.success is False
    assert complete_result.error == "clawchat complete reply dropped"
    assert adapter._active_runs_by_id[result.message_id].last_text == "draft"
    assert result.message_id not in adapter._completed_run_ids
    assert adapter._store.updated[-1]["event_type"] == "message.error"
    assert adapter._store.updated[-1]["text"] == "clawchat complete reply dropped"
