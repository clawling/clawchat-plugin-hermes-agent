from __future__ import annotations

import copy
import importlib
import asyncio
import json
import logging
import sys
from types import ModuleType, SimpleNamespace
from pathlib import Path

import pytest
import yaml

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.clawchat_metadata import owner_metadata_from_agent
from clawchat_gateway.group_message_coalescer import format_coalesced_group_text
from clawchat_gateway.inbound import InboundMessage, parse_inbound_message
from clawchat_gateway.mention_message import (
    build_context_mentions,
    build_mention_message_fragments,
    normalize_mention_targets,
    validate_mention_payload,
)
from clawchat_gateway.runtime_defaults import configure_clawchat_allow_all
from clawchat_gateway.llm_context_debug import ClawChatLlmContextDebug
import clawchat_gateway.llm_context_hooks as llm_context_hooks


def test_snapshot_preserves_injection_groups_and_llm_request(tmp_path: Path) -> None:
    debugger = ClawChatLlmContextDebug(
        env={
            "CLAWCHAT_LLM_CONTEXT_DEBUG": "1",
            "CLAWCHAT_LLM_CONTEXT_SNAPSHOT_DIR": str(tmp_path),
        }
    )
    injection_parts = [
        {
            "id": "turn-metadata",
            "group": "metadata",
            "target": "system.channel_prompt",
            "content": "## ClawChat Turn Metadata\nchat_id: cnv_1",
        }
    ]
    request_messages = [
        {"role": "system", "content": "base\n\n## ClawChat Turn Metadata\nchat_id: cnv_1"},
        {"role": "user", "content": "hello"},
    ]
    placement_checks = [
        {
            "partId": "turn-metadata",
            "target": "system.channel_prompt",
            "found": True,
            "messageIndex": 0,
        }
    ]

    snapshot_path = debugger.write_snapshot(
        visibility="full_llm_input",
        trace={"messageId": "msg_1", "traceId": "trace_1"},
        context={"injectionParts": injection_parts},
        input={
            "requestMessages": request_messages,
            "placementChecks": placement_checks,
            "fullLlmInput": {"messages": request_messages},
        },
    )

    assert snapshot_path is not None
    body = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    assert body["context"]["injectionParts"] == injection_parts
    assert body["input"]["requestMessages"] == request_messages
    assert body["input"]["placementChecks"] == placement_checks
    assert body["input"]["fullLlmInput"] == {"messages": request_messages}


def test_pre_api_request_writes_grouped_injections_and_full_llm_input(
    monkeypatch,
    tmp_path: Path,
) -> None:
    env = {
        "CLAWCHAT_LLM_CONTEXT_DEBUG": "1",
        "CLAWCHAT_LLM_CONTEXT_CAPTURE_FULL_INPUT": "1",
        "CLAWCHAT_LLM_CONTEXT_SNAPSHOT_DIR": str(tmp_path),
    }
    monkeypatch.setattr(llm_context_hooks.os, "environ", env)
    llm_context_hooks.clear_pending_injection_parts()
    injection_parts = [
        {
            "id": "group-message-metadata",
            "group": "message_metadata",
            "target": "system.channel_prompt",
            "content": "## ClawChat Group Message Metadata\nmessage_count: 1",
        }
    ]
    llm_context_hooks.remember_injection_parts(
        platform="clawchat",
        user_message="hello",
        parts=injection_parts,
        trace={"messageId": "msg_1", "chatId": "cnv_1"},
    )
    request_messages = [
        {
            "role": "system",
            "content": "base\n\n## ClawChat Group Message Metadata\nmessage_count: 1",
        },
        {"role": "user", "content": "hello"},
    ]

    llm_context_hooks._clawchat_pre_api_request(
        platform="clawchat",
        user_message="hello",
        session_id="session_1",
        request={"messages": request_messages, "model": "test-model"},
    )

    body = json.loads((tmp_path / "hermes" / "latest.json").read_text(encoding="utf-8"))
    assert body["visibility"] == "full_llm_input"
    assert body["trace"]["messageId"] == "msg_1"
    assert body["trace"]["sessionId"] == "session_1"
    assert body["context"]["injectionParts"] == injection_parts
    assert body["input"]["requestMessages"] == request_messages
    assert body["input"]["fullLlmInput"]["messages"] == request_messages
    assert body["input"]["placementChecks"] == [
        {
            "partId": "group-message-metadata",
            "group": "message_metadata",
            "target": "system.channel_prompt",
            "found": True,
            "messageIndex": 0,
            "role": "system",
        }
    ]


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


def test_platform_config_exposes_no_reply_mode_or_stream_tuning(monkeypatch):
    monkeypatch.delenv("CLAWCHAT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWCHAT_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("clawchat_gateway.config._read_hermes_env_value", lambda name: "")
    monkeypatch.setattr("clawchat_gateway.config._read_env_file_value", lambda name: "")
    platform_config = SimpleNamespace(
        extra={
            "websocket_url": "wss://example.test/ws",
            "token": "config-token",
            "refresh_token": "config-refresh",
        }
    )

    config = ClawChatConfig.from_platform_config(platform_config)

    assert not hasattr(config, "reply_mode")
    assert not hasattr(config, "stream_flush_interval_ms")
    assert not hasattr(config, "stream_min_chunk_chars")
    assert not hasattr(config, "stream_max_buffer_chars")
    assert not hasattr(config, "show_tools_output")
    assert not hasattr(config, "show_think_output")
    assert config.token == ""
    assert config.refresh_token == ""
    assert config.runtime_status_messages is False


def test_platform_config_can_enable_runtime_status_messages(monkeypatch):
    monkeypatch.delenv("CLAWCHAT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWCHAT_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("clawchat_gateway.config._read_hermes_env_value", lambda name: "")
    monkeypatch.setattr("clawchat_gateway.config._read_env_file_value", lambda name: "")

    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(
            extra={
                "websocket_url": "wss://example.test/ws",
                "runtime_status_messages": True,
            }
        )
    )

    assert config.runtime_status_messages is True


@pytest.mark.asyncio
async def test_update_account_profile_refreshes_owner_metadata(monkeypatch, tmp_path):
    from clawchat_gateway import tools

    class _Client:
        def __init__(self):
            self.patch = None

        async def update_my_profile(self, **patch):
            self.patch = patch
            return {"user": {"id": "usr_agent", **patch}}

    client = _Client()
    pull_calls = []

    async def pull_owner_metadata(
        root,
        passed_client,
        agent_id,
        *,
        connected_user_id="",
        owner_user_id="",
    ):
        pull_calls.append(
            {
                "root": root,
                "client": passed_client,
                "agent_id": agent_id,
                "connected_user_id": connected_user_id,
                "owner_user_id": owner_user_id,
            }
        )
        return {
            "ok": True,
            "target_type": "owner",
            "target_id": "owner",
            "metadata": {"agent_nickname": "Agent New"},
        }

    monkeypatch.setattr(tools, "_build_client", lambda: (client, None))
    monkeypatch.setattr(tools, "_resolve_memory_root", lambda: (tmp_path, None))
    monkeypatch.setattr(
        tools,
        "_resolve_clawchat_config",
        lambda: {
            "agent_id": "agt_agent",
            "user_id": "usr_agent",
            "owner_user_id": "usr_owner",
        },
    )
    monkeypatch.setattr(tools, "pull_owner_metadata", pull_owner_metadata)

    result = await tools.update_account_profile(nickname="Agent New")

    assert client.patch == {"nickname": "Agent New"}
    assert pull_calls == [
        {
            "root": tmp_path,
            "client": client,
            "agent_id": "agt_agent",
            "connected_user_id": "usr_agent",
            "owner_user_id": "usr_owner",
        }
    ]
    assert result["owner_metadata_sync"]["metadata"]["agent_nickname"] == "Agent New"


@pytest.mark.asyncio
async def test_update_account_profile_surfaces_owner_metadata_sync_config_error(monkeypatch):
    from clawchat_gateway import tools

    class _Client:
        async def update_my_profile(self, **patch):
            return {"user": {"id": "usr_agent", **patch}}

    monkeypatch.setattr(tools, "_build_client", lambda: (_Client(), None))
    monkeypatch.setattr(
        tools,
        "_resolve_memory_root",
        lambda: (None, {"error": "config", "message": "ClawChat memory root is not configured"}),
    )

    result = await tools.update_account_profile(bio="new bio")

    assert result["user"]["bio"] == "new bio"
    assert result["owner_metadata_sync"] == {
        "error": "config",
        "message": "ClawChat memory root is not configured",
        "target_type": "owner",
        "target_id": "owner",
    }


def test_persist_activation_writes_clawchat_display_defaults_without_top_level_streaming(
    monkeypatch, tmp_path
):
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
    assert extra["output_visibility"] == "normal"
    assert extra["runtime_status_messages"] is False
    assert "streaming" not in saved_config
    assert saved_config["agent"]["gateway_notify_interval"] == 0
    assert saved_config["agent"]["gateway_timeout_warning"] == 0
    assert saved_config["display"]["busy_input_mode"] == "queue"
    assert saved_config["display"]["busy_ack_enabled"] is False
    assert saved_config["display"]["background_process_notifications"] == "off"
    assert saved_config["display"]["tool_progress_command"] is False
    assert saved_config["display"]["platforms"]["clawchat"] == {
        "tool_progress": "off",
        "show_reasoning": False,
        "streaming": False,
        "interim_assistant_messages": True,
        "long_running_notifications": False,
        "busy_ack_detail": False,
        "cleanup_progress": False,
    }
    assert env_values == {
        "CLAWCHAT_TOKEN": "token",
        "CLAWCHAT_REFRESH_TOKEN": None,
    }


def test_persist_activation_overwrites_global_display_and_preserves_platform_values(
    monkeypatch, tmp_path
):
    activate, saved_config, _env_values = _load_activate(
        monkeypatch,
        tmp_path,
        {
            "platforms": {
                "clawchat": {
                    "extra": {
                        "runtime_status_messages": True,
                    }
                }
            },
            "display": {
                "busy_input_mode": "interrupt",
                "busy_ack_enabled": True,
                "background_process_notifications": "all",
                "tool_progress_command": True,
                "platforms": {
                    "clawchat": {
                        "tool_progress": "all",
                    }
                }
            },
            "agent": {
                "gateway_notify_interval": 180,
                "gateway_timeout_warning": 900,
            },
        },
    )

    activate.persist_activation(
        access_token="token",
        user_id="user",
        owner_user_id="owner",
        agent_id="agent",
        refresh_token=None,
        base_url="https://app.clawling.com",
    )

    assert saved_config["display"]["busy_input_mode"] == "queue"
    assert saved_config["display"]["busy_ack_enabled"] is False
    assert saved_config["display"]["background_process_notifications"] == "off"
    assert saved_config["display"]["tool_progress_command"] is False
    assert saved_config["agent"]["gateway_notify_interval"] == 0
    assert saved_config["agent"]["gateway_timeout_warning"] == 0
    assert saved_config["platforms"]["clawchat"]["extra"]["output_visibility"] == "full"
    assert saved_config["platforms"]["clawchat"]["extra"]["runtime_status_messages"] is True
    assert saved_config["display"]["platforms"]["clawchat"] == {
        "tool_progress": "all",
        "show_reasoning": False,
        "streaming": False,
        "interim_assistant_messages": True,
        "long_running_notifications": False,
        "busy_ack_detail": False,
        "cleanup_progress": False,
    }


def test_activation_persists_tokens_to_sqlite(monkeypatch, tmp_path):
    activate, saved_config, env_values = _load_activate(monkeypatch, tmp_path, {})

    calls = []

    class Store:
        def upsert_activation(self, **kwargs):
            calls.append(kwargs)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def agents_connect(self, *, code):
            return {
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "agent": {
                    "id": "agent",
                    "user_id": "user",
                    "owner_id": "owner",
                },
                "conversation": {"id": "conv-activation"},
            }

    monkeypatch.setattr(activate, "ClawChatApiClient", Client)
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: Store())

    payload = asyncio.run(activate.activate("CODE", base_url="https://app.clawling.com"))

    assert payload["user_id"] == "user"
    assert saved_config["platforms"]["clawchat"]["extra"]["user_id"] == "user"
    assert env_values["CLAWCHAT_TOKEN"] == "access-token"
    assert calls == [
        {
            "platform": "hermes",
            "account_id": "default",
            "user_id": "user",
            "conversation_id": "conv-activation",
            "owner_user_id": "owner",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
        }
    ]


def test_runtime_defaults_do_not_write_display_defaults(monkeypatch, tmp_path):
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

    changed = configure_clawchat_allow_all()

    assert changed is True
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    extra = config["platforms"]["clawchat"]["extra"]
    assert "streaming" not in config
    assert "show_tools_output" not in extra
    assert "show_think_output" not in extra
    assert "display" not in config


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

    configure_clawchat_allow_all()

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
        self.activation_conversation = None

    def claim_message_once(self, **kwargs):
        self.claimed.append(kwargs)
        return True

    def update_message_by_identity(self, **kwargs):
        self.updated.append(kwargs)

    def insert_message(self, **kwargs):
        self.inserted.append(kwargs)

    def get_activation_conversation(self, **_kwargs):
        return self.activation_conversation


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
    adapter._conversation_metadata_versions = {}
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
async def test_metadata_invalidation_validates_all_group_scope_fields(monkeypatch):
    adapter = _adapter(monkeypatch)
    calls = []

    async def refresh_conversation_metadata(
        conversation_id,
        *,
        expected_changed_fields=(),
        **_kwargs,
    ):
        calls.append((conversation_id, expected_changed_fields))
        return True

    async def refresh_agent_behavior(*_args, **_kwargs):
        raise AssertionError("group title/description should not refresh owner metadata")

    adapter._refresh_conversation_metadata = refresh_conversation_metadata
    adapter._refresh_agent_behavior = refresh_agent_behavior

    await adapter._handle_metadata_invalidated(
        {
            "chat_id": "cnv_group",
            "chat_type": "group",
            "payload": {"scope": ["title", "description"], "version": 42},
        }
    )

    assert calls == [("cnv_group", ("group_title", "group_description"))]
    assert adapter._conversation_metadata_versions["cnv_group"] == 42


@pytest.mark.asyncio
async def test_metadata_invalidation_empty_direct_scope_refreshes_behavior(monkeypatch):
    adapter = _adapter(monkeypatch)
    calls = []

    async def refresh_agent_behavior(
        conversation_id,
        *,
        expected_changed_fields=(),
        **_kwargs,
    ):
        calls.append((conversation_id, expected_changed_fields))
        return True

    async def refresh_conversation_metadata(*_args, **_kwargs):
        raise AssertionError("direct all-scope invalidation should refresh behavior")

    adapter._refresh_agent_behavior = refresh_agent_behavior
    adapter._refresh_conversation_metadata = refresh_conversation_metadata

    await adapter._handle_metadata_invalidated(
        {
            "chat_id": "cnv_direct",
            "chat_type": "direct",
            "payload": {"version": 43},
        }
    )

    assert calls == [("cnv_direct", ())]
    assert adapter._conversation_metadata_versions["cnv_direct"] == 43


def test_metadata_invalidation_warns_when_scoped_field_did_not_change(
    monkeypatch,
    caplog,
):
    adapter = _adapter(monkeypatch)
    caplog.set_level(logging.WARNING, logger="clawchat_gateway.adapter")

    adapter._validate_metadata_changed_fields(
        target_type="group",
        target_id="cnv_group",
        before={"group_title": "old", "group_description": "same"},
        after={"group_title": "new", "group_description": "same"},
        expected_changed_fields=("group_title", "group_description"),
    )

    assert "changed_fields=group_title" in caplog.text
    assert "unchanged_fields=group_description" in caplog.text


def test_group_prompt_injects_agent_profile_metadata(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._read_memory_metadata = lambda target_type, target_id: {
        ("owner", "owner"): {
            "agent_user_id": "usr_agent",
            "agent_nickname": "Hermes Bot",
            "agent_avatar_url": "https://cdn.example/agent.png",
            "agent_bio": "I help the group.",
            "agent_behavior": "Reply tersely.",
        },
        ("group", "cnv_group"): {},
    }.get((target_type, target_id), {})

    prompt = adapter._compose_channel_prompt(
        InboundMessage(
            chat_id="cnv_group",
            chat_type="group",
            sender_id="usr_sender",
            sender_name="Sender",
            text="hello",
            raw_message={},
        )
    )

    assert "## ClawChat Agent Profile" in prompt
    assert "agent_user_id: usr_agent" in prompt
    assert "agent_nickname: Hermes Bot" in prompt
    assert "agent_avatar_url: https://cdn.example/agent.png" in prompt
    assert "agent_bio: I help the group." in prompt
    assert "## ClawChat Agent Behavior" in prompt
    assert "Reply tersely." in prompt


def test_owner_metadata_writes_agent_user_id_not_config_agent_id() -> None:
    metadata = owner_metadata_from_agent(
        {
            "agent": {
                "id": "agt_record",
                "user_id": "usr_agent",
                "owner_id": "usr_owner",
                "nickname": "Hermes Bot",
            }
        },
        connected_user_id="usr_fallback",
        owner_user_id="usr_owner_fallback",
    )

    assert metadata["agent_user_id"] == "usr_agent"
    assert metadata["agent_owner_id"] == "usr_owner"
    assert "agent_id" not in metadata


def test_group_prompt_injects_agent_user_id_from_config_when_metadata_missing(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._read_memory_metadata = lambda _target_type, _target_id: {}

    prompt = adapter._compose_channel_prompt(
        InboundMessage(
            chat_id="cnv_group",
            chat_type="group",
            sender_id="usr_sender",
            sender_name="Sender",
            text="hello",
            raw_message={},
        )
    )

    assert "## ClawChat Agent Profile" in prompt
    assert "agent_user_id: usr_agent" in prompt


def test_direct_prompt_injects_sender_metadata_without_message_text(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._read_memory_metadata = lambda target_type, target_id: {
        ("user", "usr_sender"): {
            "nickname": "Peer",
            "avatar_url": "https://cdn.example/peer.png",
            "bio": "Peer bio",
            "profile_type": "agent",
        }
    }.get((target_type, target_id), {})

    prompt = adapter._compose_channel_prompt(
        InboundMessage(
            chat_id="cnv_direct",
            chat_type="direct",
            sender_id="usr_sender",
            sender_name="Peer",
            text="private body text",
            raw_message={},
        )
    )

    assert "## ClawChat Turn Metadata\nchat_type: direct\nchat_id: cnv_direct" in prompt
    assert "## ClawChat Peer Profile" in prompt
    assert "nickname: Peer" in prompt
    assert "## ClawChat Sender Metadata" in prompt
    assert "sender_id: usr_sender" in prompt
    assert "sender_name: Peer" in prompt
    assert "sender_profile_type: agent" in prompt
    assert "sender_is_agent_owner: false" in prompt
    assert "private body text" not in prompt
    assert "## ClawChat Message Blocks" not in prompt
    assert "[message]" not in prompt


def test_owner_direct_prompt_injects_sender_metadata_without_peer_profile(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._read_memory_metadata = lambda target_type, target_id: {
        ("owner", "owner"): {"agent_owner_nickname": "Owner"}
    }.get((target_type, target_id), {})

    prompt = adapter._compose_channel_prompt(
        InboundMessage(
            chat_id="cnv_owner",
            chat_type="direct",
            sender_id="usr_owner",
            sender_name="Owner",
            text="owner body text",
            raw_message={},
        )
    )

    assert "## ClawChat Sender Metadata" in prompt
    assert "sender_id: usr_owner" in prompt
    assert "sender_name: Owner" in prompt
    assert "sender_profile_type: user" in prompt
    assert "sender_is_agent_owner: true" in prompt
    assert "## ClawChat Peer Profile" not in prompt
    assert "owner body text" not in prompt


def test_group_prompt_injects_group_message_metadata_without_message_text(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._read_memory_metadata = lambda target_type, target_id: {
        ("group", "cnv_group"): {
            "group_title": "Ops",
            "group_description": "Coordinate work.",
            "group_owner_id": "usr_owner",
        },
        ("owner", "owner"): {"agent_owner_id": "usr_owner"},
        ("user", "usr_alice"): {"nickname": "Alice", "profile_type": "user"},
        ("user", "usr_bob"): {"nickname": "Bob", "profile_type": "agent"},
    }.get((target_type, target_id), {})
    batch = [
        _group_envelope(
            sender_id="usr_alice",
            sender_name="Alice",
            text="first group body",
            context_mentions=[],
        ),
        _group_envelope(
            sender_id="usr_bob",
            sender_name="Bob",
            text="@Hermes second group body",
            context_mentions=[
                {"kind": "mention", "user_id": "usr_agent", "display": "Hermes"}
            ],
        ),
    ]
    inbound = InboundMessage(
        chat_id="cnv_group",
        chat_type="group",
        sender_id="usr_bob",
        sender_name="Bob",
        text=format_coalesced_group_text(
            [
                parse_inbound_message(frame, adapter._clawchat_config)
                for frame in batch
            ],
            idle_seconds=10,
            max_wait_seconds=30,
        ),
        raw_message={"clawchat_group_batch": True, "messages": batch},
        was_mentioned=True,
        mentioned_user_ids=["usr_agent"],
        mentioned_users=[{"id": "usr_agent", "display": "Hermes"}],
    )

    prompt = adapter._compose_channel_prompt(inbound)

    assert "## ClawChat Group Profile" in prompt
    assert "group_title: Ops" in prompt
    assert "## ClawChat Group Message Metadata" in prompt
    assert "message_count: 2" in prompt
    assert "[message 1]" in prompt
    assert "sender_id: usr_alice" in prompt
    assert "sender_name: Alice" in prompt
    assert "sender_profile_type: user" in prompt
    assert "sender_is_agent_owner: false" in prompt
    assert "sender_is_group_owner: false" in prompt
    assert "mention_routing: no_structured_mentions" in prompt
    assert "[message 2]" in prompt
    assert "sender_id: usr_bob" in prompt
    assert "mentioned_users: usr_agent(Hermes)" in prompt
    assert "mention_routing: addressed_to_current_agent" in prompt
    assert "first group body" not in prompt
    assert "second group body" not in prompt
    assert "mentions:" not in prompt
    assert "[message]\n" not in prompt


def test_group_prompt_falls_back_to_legacy_agent_id_metadata(monkeypatch):
    adapter = _adapter(monkeypatch)
    adapter._read_memory_metadata = lambda target_type, target_id: {
        ("owner", "owner"): {"agent_id": "usr_legacy"}
    }.get((target_type, target_id), {})

    prompt = adapter._compose_channel_prompt(
        InboundMessage(
            chat_id="cnv_group",
            chat_type="group",
            sender_id="usr_sender",
            sender_name="Sender",
            text="hello",
            raw_message={},
        )
    )

    assert "## ClawChat Agent Profile" in prompt
    assert "agent_user_id: usr_legacy" in prompt
    assert "agent_id: usr_legacy" not in prompt


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
async def test_runtime_status_messages_are_suppressed_by_default(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send_or_update_status(
        "chat-1",
        "lifecycle",
        "⚠️ Empty response from model — retrying (1/3)",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == []


@pytest.mark.parametrize(
    "content",
    [
        "⚠️ provider stream interrupted (RemoteProtocolError) after 12.3s — reconnecting, retry 1/3",
        "⚠ Auxiliary memory failed: provider timed out",
        "⚠ No auxiliary LLM provider configured — context compression will drop middle turns without a summary.",
        "⚠ Compression model small-model (provider) context is 65,536 tokens, but the main model threshold was 100,000 tokens.",
        "⚠️ No response from provider for 180s (model: test-model, context: ~20,000 tokens). Reconnecting...",
        "❌ Connection to provider failed after 3 attempts. The provider may be experiencing issues — try again in a moment.",
        "⚠️ Connection to provider dropped (ReadTimeout). Reconnecting…",
        "🔄 Reconnected — resuming",
        "🔄 Primary model failed — switching to fallback: model-b via provider-b",
        "⚠ Compression summary failed: upstream timeout. Inserted a fallback context marker.",
        "ℹ Configured compression model 'aux-model' failed (timeout). Recovered using main model — check auxiliary.compression.model in config.yaml.",
        "🔌 Detected stale connections from a previous provider issue — cleaned up automatically. Proceeding with fresh connection.",
        "📦 Preflight compression: ~120,000 tokens >= 100,000 threshold. This may take a moment.",
        "⏳ Nous Portal rate limit active — resets in 12m.",
        "⚠️ Empty/malformed response — switching to fallback...",
        "⚠️ Max retries (3) for invalid responses — trying fallback...",
        "❌ Max retries (3) exceeded for invalid responses. Giving up.",
        "🗜️ Context reduced to 80,000 tokens (was 120,000), retrying...",
        "⚠️ Rate limited — switching to fallback provider...",
        "⚠️  Request payload too large (413) — compression attempt 1/3...",
        "🗜️ Compressed 42 → 18 messages, retrying...",
        "🗜️ Context too large (~120,000 tokens) — compressing (1/3)...",
        "⚠️ Non-retryable error (HTTP 401) — trying fallback...",
        "❌ Non-retryable error (HTTP 401): Unauthorized",
        "⚠️ Max retries (3) exhausted — trying fallback...",
        "❌ Rate limited after 3 retries — quota exhausted",
        "❌ API failed after 3 retries — connection reset",
        "⏱️ Rate limited. Waiting 2.0s (attempt 2/3)...",
        "⏳ Retrying in 2.0s (attempt 1/3)...",
        "⚠️ Tool guardrail halted write_file: repeated_args",
        "↻ Stream interrupted — using delivered content as final response",
        "↻ Empty response after tool calls — using earlier content as final answer",
        "⚠️ Model returned empty after tool calls — nudging to continue",
        "↻ Thinking-only response — prefilling to continue (1/2)",
        "⚠️ Empty response from model — retrying (1/3)",
        "⚠️ Model returning empty responses — switching to fallback provider...",
        "↻ Switched to fallback: fallback-model (provider)",
        "⚠️ Model produced reasoning but no visible response after all retries. Returning empty.",
        "❌ Model returned no content after all retries. No fallback providers configured.",
        "⚠️ Iteration budget exhausted (90/90) — asking model to summarise",
    ],
)
@pytest.mark.asyncio
async def test_hermes_lifecycle_status_messages_sent_through_send_are_suppressed_by_default(
    monkeypatch, content
):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        content,
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == []


@pytest.mark.asyncio
async def test_runtime_status_messages_can_be_enabled(monkeypatch):
    adapter = _adapter(monkeypatch, {"runtime_status_messages": True})

    result = await adapter.send_or_update_status(
        "chat-1",
        "lifecycle",
        "⚠️ Empty response from model — retrying (1/3)",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "⚠️ Empty response from model — retrying (1/3)"}
    ]


@pytest.mark.asyncio
async def test_runtime_status_messages_follow_output_visibility_config(monkeypatch):
    adapter = _adapter(monkeypatch, {"runtime_status_messages": False})
    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")
    hermes_config.read_raw_config = lambda: {
        "platforms": {
            "clawchat": {
                "extra": {
                    "output_visibility": "full",
                    "runtime_status_messages": True,
                }
            }
        }
    }
    hermes_config.save_config = lambda _config: None
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)

    result = await adapter.send_or_update_status(
        "chat-1",
        "lifecycle",
        "⚠️ Empty response from model — retrying (1/3)",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]


@pytest.mark.asyncio
async def test_hermes_lifecycle_status_messages_sent_through_send_can_be_enabled(monkeypatch):
    adapter = _adapter(monkeypatch, {"runtime_status_messages": True})
    content = "⚠️ Empty response from model — retrying (1/3)"

    result = await adapter.send(
        "chat-1",
        content,
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": content}
    ]


@pytest.mark.asyncio
async def test_configured_progress_messages_are_not_suppressed_as_runtime_status(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "⏳ Working — 3 min — iteration 9/90, terminal",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "⏳ Working — 3 min — iteration 9/90, terminal"}
    ]


@pytest.mark.asyncio
async def test_empty_response_notice_is_suppressed_by_default(monkeypatch):
    adapter = _adapter(monkeypatch)

    result = await adapter.send(
        "chat-1",
        "⚠️ The model returned no response after processing tool results. Try again.",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert _sent_events(adapter) == []


@pytest.mark.asyncio
async def test_empty_response_notice_is_suppressed_on_run_complete(monkeypatch):
    adapter = _adapter(monkeypatch)
    result = await adapter.send(
        "chat-1",
        "draft ▉",
        metadata={"notify": True, "chat_type": "direct"},
    )

    final_result = await adapter.on_run_complete(
        "chat-1",
        (
            "⚠️ The model returned no response after processing tool results. "
            "This can happen with some models — try again or rephrase your question."
        ),
        message_id=result.message_id,
    )

    assert final_result.success is True
    assert _sent_events(adapter) == []
    assert result.message_id not in adapter._active_runs_by_id
    assert result.message_id in adapter._completed_run_ids


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
    adapter._store.activation_conversation = "dm-owner"
    adapter._known_chat_types = {"group-1": "group"}
    content = (
        "Reply `/approve` to execute, `/approve session` to approve this pattern "
        "for the session, `/approve always` to approve permanently, or `/deny` to cancel."
    )

    result = await adapter.send("group-1", content)

    assert result.success is True
    assert _sent_events(adapter) == ["message.reply"]
    frame = adapter._connection.frames[0][0]
    assert frame["chat_id"] == "dm-owner"
    fragments = frame["payload"]["message"]["body"]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["kind"] == "text"
    assert "ClawChat group group-1 requires owner attention." in fragments[0]["text"]
    assert "Reply `/approve` to execute" in fragments[0]["text"]


@pytest.mark.asyncio
async def test_group_exec_approval_routes_to_owner_with_session_payload(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._store.activation_conversation = "dm-owner"
    adapter._known_chat_types = {"group-1": "group"}

    result = await adapter.send_exec_approval(
        chat_id="group-1",
        command="rm -rf /tmp/example",
        session_key="agent:main:clawchat:group:group-1:usr_sender",
        description="dangerous command",
    )

    assert result.success is True
    frame = adapter._connection.frames[0][0]
    assert frame["chat_id"] == "dm-owner"
    fragments = frame["payload"]["message"]["body"]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["kind"] == "text"
    assert "ClawChat group group-1 requires owner attention." in fragments[0]["text"]
    assert "Command approval required:" in fragments[0]["text"]
    assert "```shell\nrm -rf /tmp/example\n```" in fragments[0]["text"]
    assert "Reason: dangerous command" in fragments[0]["text"]
    assert "Choose:" in fragments[0]["text"]
    assert "Text fallback" not in fragments[0]["text"]
    assert adapter._owner_approval_routes == {
        "dm-owner": "agent:main:clawchat:group:group-1:usr_sender",
    }


@pytest.mark.asyncio
async def test_direct_exec_approval_sends_text_only_for_unsupported_clients(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})

    result = await adapter.send_exec_approval(
        chat_id="dm-owner",
        command="python3 -c \"import hermes\"",
        session_key="agent:main:clawchat:direct:dm-owner",
        description="script execution via -e/-c flag",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    frame = adapter._connection.frames[0][0]
    fragments = frame["payload"]["message"]["body"]["fragments"]
    assert len(fragments) == 1
    assert fragments[0]["kind"] == "text"
    assert "Command approval required:" in fragments[0]["text"]
    assert "python3 -c \"import hermes\"" in fragments[0]["text"]
    assert "Reason: script execution via -e/-c flag" in fragments[0]["text"]
    assert "Choose:" in fragments[0]["text"]
    assert "Text fallback" not in fragments[0]["text"]


@pytest.mark.asyncio
async def test_direct_exec_approval_formats_multiline_command_separately_from_reason(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    command = (
        'find /opt/data -name "hermes" -o -name "hermes.py" 2>/dev/null | head -10\n'
        'which python3 && python3 -c "import hermes" 2>&1 | head -5'
    )

    result = await adapter.send_exec_approval(
        chat_id="dm-owner",
        command=command,
        session_key="agent:main:clawchat:direct:dm-owner",
        description="script execution via -e/-c flag",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    fragments = adapter._connection.frames[0][0]["payload"]["message"]["body"]["fragments"]
    text = fragments[0]["text"]
    assert f"```shell\n{command}\n```" in text
    assert "head -5\n```\n\nReason: script execution via -e/-c flag" in text
    assert "Choose:" in text
    assert len(fragments) == 1


@pytest.mark.asyncio
async def test_direct_exec_approval_preserves_original_empty_command_block(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})

    result = await adapter.send_exec_approval(
        chat_id="dm-owner",
        command="",
        session_key="agent:main:clawchat:direct:dm-owner",
        description="delete in root path",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    fragments = adapter._connection.frames[0][0]["payload"]["message"]["body"]["fragments"]
    text = fragments[0]["text"]
    assert text == (
        "Command approval required:\n"
        "```shell\n"
        "\n"
        "```\n\n"
        "Reason: delete in root path\n\n"
        "Choose:\n"
        "- Approve Once - reply /approve\n"
        "- Approve Session - reply /approve session\n"
        "- Always Approve - reply /approve always\n"
        "- Deny - reply /deny"
    )
    assert len(fragments) == 1


@pytest.mark.asyncio
async def test_exec_approval_fallback_includes_full_command(monkeypatch):
    adapter = _adapter(monkeypatch)
    command = "printf '" + ("x" * 240) + "' && rm -rf /tmp/example"

    result = await adapter.send_exec_approval(
        chat_id="dm-owner",
        command=command,
        session_key="agent:main:clawchat:direct:dm-owner",
        description="dangerous command",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    fragments = adapter._connection.frames[0][0]["payload"]["message"]["body"]["fragments"]
    assert command in fragments[0]["text"]


@pytest.mark.asyncio
async def test_group_exec_approval_does_not_fallback_to_owner_user_id(monkeypatch):
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._known_chat_types = {"group-1": "group"}

    result = await adapter.send_exec_approval(
        chat_id="group-1",
        command="rm -rf /tmp/example",
        session_key="agent:main:clawchat:group:group-1:usr_sender",
        description="dangerous command",
    )

    assert result.success is False
    assert result.error == "clawchat owner direct chat unavailable"
    assert adapter._connection.frames == []
    assert adapter._owner_approval_routes == {}


@pytest.mark.asyncio
async def test_owner_direct_approve_resolves_forwarded_group_approval(monkeypatch):
    calls = _install_fake_approval_module(monkeypatch)
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._owner_approval_routes = {
        "dm-owner": "agent:main:clawchat:group:group-1:usr_sender"
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
async def test_owner_direct_always_alias_resolves_forwarded_group_approval(monkeypatch):
    calls = _install_fake_approval_module(monkeypatch)
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._owner_approval_routes = {
        "dm-owner": "agent:main:clawchat:group:group-1:usr_sender"
    }
    frame = {
        "event": "message.send",
        "chat_id": "dm-owner",
        "chat_type": "direct",
        "sender": {"id": "usr_owner", "name": "Owner"},
        "payload": {
            "message_id": "in-1",
            "message": {
                "body": {"fragments": [{"kind": "text", "text": "/always"}]},
                "context": {"mentions": [], "reply": None},
            },
        },
    }

    await adapter._on_message(frame)

    assert ("resolve", "agent:main:clawchat:group:group-1:usr_sender", "always", False) in calls
    assert _sent_events(adapter) == ["message.reply"]


@pytest.mark.asyncio
async def test_owner_direct_cancel_alias_denies_forwarded_group_approval(monkeypatch):
    calls = _install_fake_approval_module(monkeypatch)
    adapter = _adapter(monkeypatch, {"owner_user_id": "usr_owner"})
    adapter._owner_approval_routes = {
        "dm-owner": "agent:main:clawchat:group:group-1:usr_sender"
    }
    frame = {
        "event": "message.send",
        "chat_id": "dm-owner",
        "chat_type": "direct",
        "sender": {"id": "usr_owner", "name": "Owner"},
        "payload": {
            "message_id": "in-1",
            "message": {
                "body": {"fragments": [{"kind": "text", "text": "/cancel"}]},
                "context": {"mentions": [], "reply": None},
            },
        },
    }

    await adapter._on_message(frame)

    assert ("resolve", "agent:main:clawchat:group:group-1:usr_sender", "deny", False) in calls
    assert _sent_events(adapter) == ["message.reply"]


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


def test_normalize_mention_targets_requires_display() -> None:
    with pytest.raises(ValueError, match=r"mentions\[0\]\.display"):
        normalize_mention_targets([{"userId": "usr_123"}])


def test_normalize_mention_targets_strips_leading_display_at() -> None:
    assert normalize_mention_targets([{"userId": " usr_123 ", "display": " @Alice "}]) == [
        {"userId": "usr_123", "display": "Alice"}
    ]


def test_build_mention_payload_requires_fragment_display_and_matching_context() -> None:
    mentions = normalize_mention_targets([{"userId": "usr_123", "display": "Alice"}])

    fragments = build_mention_message_fragments(mentions=mentions, text="请看")
    context_mentions = build_context_mentions(mentions)

    assert fragments == [
        {"kind": "mention", "user_id": "usr_123", "display": "Alice"},
        {"kind": "text", "text": " 请看"},
    ]
    assert context_mentions == [
        {"kind": "mention", "user_id": "usr_123", "display": "Alice"}
    ]
    validate_mention_payload(fragments, context_mentions)


def test_text_is_not_reparsed_as_mention_display() -> None:
    mentions = normalize_mention_targets([{"userId": "usr_123", "display": "Alice"}])

    assert build_mention_message_fragments(mentions=mentions, text="@Bob 请看") == [
        {"kind": "mention", "user_id": "usr_123", "display": "Alice"},
        {"kind": "text", "text": " @Bob 请看"},
    ]


def test_validate_mention_payload_rejects_context_mismatch() -> None:
    with pytest.raises(ValueError, match="context.mentions must match mention fragments"):
        validate_mention_payload(
            [{"kind": "mention", "user_id": "usr_123", "display": "Alice"}],
            [{"kind": "mention", "user_id": "usr_other", "display": "Other"}],
        )


def test_validate_mention_payload_rejects_missing_fragment_display() -> None:
    with pytest.raises(ValueError, match="mention fragment requires display"):
        validate_mention_payload(
            [{"kind": "mention", "user_id": "usr_123"}],
            [{"kind": "mention", "user_id": "usr_123", "display": "Alice"}],
        )


def test_group_message_prompt_separates_sender_and_mention_display() -> None:
    text = format_coalesced_group_text(
        [
            InboundMessage(
                chat_id="cnv_group",
                chat_type="group",
                sender_id="usr_sender",
                sender_name="Alice",
                text="@Alice 请看",
                raw_message={},
                was_mentioned=False,
                mentioned_user_ids=["usr_mentioned"],
                mentioned_users=[{"id": "usr_mentioned", "display": "Alice"}],
                sender_profile_type="user",
            )
        ],
        idle_seconds=10,
        max_wait_seconds=30,
    )

    assert text == "ClawChat group messages:\n[message 1] Alice: @Alice 请看"
    assert "user_id: usr_sender" not in text
    assert "mentions:" not in text
    assert "sender_name:" not in text
    assert "mentioned_users:" not in text


def _group_envelope(
    *,
    fragments=None,
    context_mentions,
    sender_id="usr_sender",
    sender_name="Sender",
    text=None,
    message_id="msg_123",
):
    if fragments is None:
        fragments = [{"kind": "text", "text": text or ""}]
    return {
        "version": "2",
        "event": "message.send",
        "chat_id": "cnv_group",
        "chat_type": "group",
        "sender": {"id": sender_id, "nick_name": sender_name},
        "payload": {
            "message_id": message_id,
            "message": {
                "body": {"fragments": fragments},
                "context": {"mentions": context_mentions, "reply": None},
            },
        },
    }


def test_group_mention_mode_requires_context_mentions_for_dispatch() -> None:
    config = ClawChatConfig(
        websocket_url="wss://example.test/ws",
        user_id="usr_agent",
        group_mode="mention",
    )

    inbound = parse_inbound_message(
        _group_envelope(
            fragments=[
                {"kind": "mention", "user_id": "usr_agent", "display": "Agent"},
                {"kind": "text", "text": " hi"},
            ],
            context_mentions=[],
        ),
        config,
    )

    assert inbound is None


def test_context_mentions_drive_dispatch_and_fragment_display_drives_llm_context() -> None:
    config = ClawChatConfig(
        websocket_url="wss://example.test/ws",
        user_id="usr_agent",
        group_mode="mention",
    )

    inbound = parse_inbound_message(
        _group_envelope(
            fragments=[
                {"kind": "mention", "user_id": "usr_agent", "display": "Agent"},
                {"kind": "text", "text": " hi"},
            ],
            context_mentions=[
                {"kind": "mention", "user_id": "usr_agent", "display": "Agent"}
            ],
        ),
        config,
    )

    assert inbound is not None
    assert inbound.was_mentioned is True
    assert inbound.mentioned_users == [{"id": "usr_agent", "display": "Agent"}]
    assert inbound.text == "@Agent hi"
