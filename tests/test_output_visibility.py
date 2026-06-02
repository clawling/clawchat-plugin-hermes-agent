from __future__ import annotations

import copy
import importlib
import os
import sys
from types import ModuleType

import pytest


def _load_output_visibility(monkeypatch, tmp_path, raw_config):
    saved_config = {}

    hermes_cli = ModuleType("hermes_cli")
    hermes_config = ModuleType("hermes_cli.config")
    hermes_config.get_config_path = lambda: str(tmp_path / "config.yaml")
    hermes_config.read_raw_config = lambda: copy.deepcopy(raw_config)
    hermes_config.save_config = lambda config: saved_config.update(copy.deepcopy(config))

    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", hermes_config)

    import clawchat_gateway.output_visibility as output_visibility

    return importlib.reload(output_visibility), saved_config


def _load_commands(monkeypatch, tmp_path, raw_config):
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

    import clawchat_gateway.commands as commands

    return importlib.reload(commands), saved_config


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (
            "minimal",
            {
                "runtime_status_messages": False,
                "tool_progress": "off",
                "show_reasoning": False,
                "streaming": False,
                "interim_assistant_messages": False,
                "long_running_notifications": False,
                "busy_ack_detail": False,
                "cleanup_progress": False,
                "gateway_notify_interval": 0,
                "gateway_timeout_warning": 0,
            },
        ),
        (
            "normal",
            {
                "runtime_status_messages": False,
                "tool_progress": "off",
                "show_reasoning": False,
                "streaming": False,
                "interim_assistant_messages": True,
                "long_running_notifications": False,
                "busy_ack_detail": False,
                "cleanup_progress": False,
                "gateway_notify_interval": 0,
                "gateway_timeout_warning": 0,
            },
        ),
        (
            "full",
            {
                "runtime_status_messages": True,
                "tool_progress": "verbose",
                "show_reasoning": True,
                "streaming": False,
                "interim_assistant_messages": True,
                "long_running_notifications": True,
                "busy_ack_detail": True,
                "cleanup_progress": False,
                "gateway_notify_interval": 180,
                "gateway_timeout_warning": 900,
            },
        ),
    ],
)
def test_apply_output_visibility_writes_clawchat_preset(monkeypatch, tmp_path, mode, expected):
    output_visibility, saved_config = _load_output_visibility(
        monkeypatch,
        tmp_path,
        {
            "platforms": {
                "clawchat": {
                    "enabled": True,
                    "extra": {
                        "base_url": "https://app.clawling.com",
                    },
                }
            },
            "display": {
                "platforms": {
                    "telegram": {
                        "tool_progress": "all",
                    }
                }
            },
            "agent": {
                "max_turns": 60,
            },
        },
    )

    result = output_visibility.apply_output_visibility(mode)

    extra = saved_config["platforms"]["clawchat"]["extra"]
    assert extra["base_url"] == "https://app.clawling.com"
    assert extra["output_visibility"] == mode
    assert extra["runtime_status_messages"] is expected["runtime_status_messages"]
    assert saved_config["display"]["platforms"]["telegram"] == {"tool_progress": "all"}
    assert saved_config["display"]["platforms"]["clawchat"] == {
        "tool_progress": expected["tool_progress"],
        "show_reasoning": expected["show_reasoning"],
        "streaming": expected["streaming"],
        "interim_assistant_messages": expected["interim_assistant_messages"],
        "long_running_notifications": expected["long_running_notifications"],
        "busy_ack_detail": expected["busy_ack_detail"],
        "cleanup_progress": expected["cleanup_progress"],
    }
    assert saved_config["agent"]["max_turns"] == 60
    assert saved_config["agent"]["gateway_notify_interval"] == expected["gateway_notify_interval"]
    assert saved_config["agent"]["gateway_timeout_warning"] == expected["gateway_timeout_warning"]
    assert result["mode"] == mode
    assert result["runtime_status_messages"] is expected["runtime_status_messages"]


def test_apply_output_visibility_rejects_unknown_mode(monkeypatch, tmp_path):
    output_visibility, _saved_config = _load_output_visibility(monkeypatch, tmp_path, {})

    with pytest.raises(ValueError, match="Unsupported ClawChat output visibility"):
        output_visibility.apply_output_visibility("verbose")


def test_apply_output_visibility_updates_current_process_agent_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_AGENT_NOTIFY_INTERVAL", "0")
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT_WARNING", "0")
    output_visibility, _saved_config = _load_output_visibility(monkeypatch, tmp_path, {})

    output_visibility.apply_output_visibility("full")

    assert os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] == "180"
    assert os.environ["HERMES_AGENT_TIMEOUT_WARNING"] == "900"

    output_visibility.apply_output_visibility("minimal")

    assert os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] == "0"
    assert os.environ["HERMES_AGENT_TIMEOUT_WARNING"] == "0"


@pytest.mark.asyncio
async def test_clawchat_output_command_sets_visibility(monkeypatch, tmp_path):
    commands, saved_config = _load_commands(monkeypatch, tmp_path, {})

    response = await commands.handle_clawchat_output_command("full")

    assert response == (
        "**ClawChat output updated**\n\n"
        "- visibility: `full`\n"
        "- runtime status: `on`\n"
        "- detail level: `verbose`\n\n"
        "Applies to new ClawChat messages."
    )
    extra = saved_config["platforms"]["clawchat"]["extra"]
    assert extra["output_visibility"] == "full"
    assert extra["runtime_status_messages"] is True
