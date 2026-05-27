from __future__ import annotations

import copy
import importlib
import sys
from types import ModuleType, SimpleNamespace

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


def test_persist_activation_preserves_existing_streaming(monkeypatch, tmp_path):
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
    assert "reply_mode" not in extra
    assert saved_config["streaming"] == {
        "enabled": False,
        "transport": "none",
        "edit_interval": 9,
        "buffer_threshold": 99,
    }


def test_runtime_defaults_do_not_write_reply_mode_or_streaming(monkeypatch, tmp_path):
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
    assert "reply_mode" not in extra
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
