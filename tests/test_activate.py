import asyncio
import sys
import types
from pathlib import Path

import yaml

from clawchat_gateway.activate import persist_activation


def _read_env(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_persist_activation_writes_secrets_to_env_and_config_without_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    result = persist_activation(
        access_token="tk",
        refresh_token="rt",
        user_id="agent-1",
        base_url="http://company.newbaselab.com:10086",
    )

    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    extra = config["platforms"]["clawchat"]["extra"]
    env = _read_env(hermes_home / ".env")
    assert env["CLAWCHAT_TOKEN"] == "tk"
    assert env["CLAWCHAT_REFRESH_TOKEN"] == "rt"
    assert "token" not in extra
    assert "refresh_token" not in extra
    assert extra["user_id"] == "agent-1"
    assert extra["websocket_url"] == "ws://company.newbaselab.com:10086/ws"
    assert config["display"]["platforms"]["clawchat"]["tool_progress"] == "off"
    assert result["restart_required"] is True


def test_persist_activation_removes_stale_config_secrets_and_refresh_env(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(
        "KIMI_BASE_URL=https://api.kimi.com/coding/v1\n"
        "CLAWCHAT_TOKEN=old\n"
        "CLAWCHAT_REFRESH_TOKEN=stale\n"
        "OTHER=value\n"
    )
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "platforms": {
                    "clawchat": {
                        "enabled": True,
                        "extra": {
                            "token": "old-yaml",
                            "refresh_token": "old-refresh",
                            "user_id": "old-user",
                        },
                    }
                }
            }
        )
    )

    persist_activation(
        access_token="new-token",
        refresh_token=None,
        user_id="agent-2",
        base_url="https://chat.example",
    )

    config = yaml.safe_load((hermes_home / "config.yaml").read_text())
    extra = config["platforms"]["clawchat"]["extra"]
    env = _read_env(hermes_home / ".env")
    assert env["CLAWCHAT_TOKEN"] == "new-token"
    assert "CLAWCHAT_REFRESH_TOKEN" not in env
    assert env["KIMI_BASE_URL"] == "https://api.kimi.com/coding/v1"
    assert env["OTHER"] == "value"
    assert "token" not in extra
    assert "refresh_token" not in extra
    assert extra["user_id"] == "agent-2"
    assert extra["websocket_url"] == "wss://chat.example/v1/ws"


def test_persist_activation_uses_hermes_config_helpers_when_available(
    tmp_path: Path, monkeypatch
) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    saved_env: dict[str, str] = {}
    removed_env: list[str] = []
    saved_configs: list[dict] = []
    config_module = types.ModuleType("hermes_cli.config")
    config_module.get_config_path = lambda: hermes_home / "config.yaml"
    config_module.get_env_path = lambda: hermes_home / ".env"
    config_module.read_raw_config = lambda: {
        "platforms": {
            "clawchat": {
                "enabled": True,
                "extra": {
                    "token": "old-yaml",
                    "refresh_token": "old-refresh",
                },
            }
        }
    }
    config_module.save_config = lambda config: saved_configs.append(config)
    config_module.save_env_value = lambda key, value: saved_env.__setitem__(key, value)
    config_module.remove_env_value = lambda key: removed_env.append(key) or True
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config_module)
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))

    result = persist_activation(
        access_token="new-token",
        refresh_token=None,
        user_id="agent-3",
        base_url="https://chat.example",
    )

    assert saved_env == {"CLAWCHAT_TOKEN": "new-token"}
    assert removed_env == ["CLAWCHAT_REFRESH_TOKEN"]
    assert len(saved_configs) == 1
    extra = saved_configs[0]["platforms"]["clawchat"]["extra"]
    assert "token" not in extra
    assert "refresh_token" not in extra
    assert extra["user_id"] == "agent-3"
    assert result["config_path"] == str(hermes_home / "config.yaml")
    assert result["env_path"] == str(hermes_home / ".env")


def test_activate_and_maybe_restart_schedules_restart(monkeypatch, tmp_path: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from clawchat_gateway import activate as activate_mod

    async def fake_activate(code: str, *, base_url: str):
        assert code == "ABC123"
        assert base_url == "https://chat.example"
        return {
            "config_path": str(hermes_home / "config.yaml"),
            "env_path": str(hermes_home / ".env"),
            "user_id": "agent-1",
            "base_url": base_url,
            "websocket_url": "wss://chat.example/v1/ws",
            "token": "***",
            "refresh_token": "***",
            "restart_required": True,
            "restart_message": "Restart Hermes gateway so ClawChat reloads the new credentials.",
        }

    monkeypatch.setattr(activate_mod, "activate", fake_activate)
    monkeypatch.setattr(
        activate_mod,
        "schedule_gateway_restart",
        lambda delay_seconds=2: f"restart after {delay_seconds}",
        raising=False,
    )

    result = asyncio.run(
        activate_mod.activate_and_maybe_restart(
            "ABC123",
            base_url="https://chat.example",
            restart=True,
        )
    )

    assert result["ok"] is True
    assert result["restart_scheduled"] is True
    assert result["restart_delay_seconds"] == 2
    assert result["restart_command"] == "restart after 2"
    assert result["restart_message"] == (
        "ClawChat activation is saved. Hermes restart has been scheduled in the background."
    )


def test_activate_and_maybe_restart_can_skip_restart(monkeypatch, tmp_path: Path) -> None:
    hermes_home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from clawchat_gateway import activate as activate_mod

    async def fake_activate(code: str, *, base_url: str):
        return {
            "config_path": str(hermes_home / "config.yaml"),
            "env_path": str(hermes_home / ".env"),
            "user_id": "agent-1",
            "base_url": base_url,
            "websocket_url": "wss://chat.example/v1/ws",
            "token": "***",
            "refresh_token": None,
            "restart_required": True,
            "restart_message": "Restart Hermes gateway so ClawChat reloads the new credentials.",
        }

    monkeypatch.setattr(activate_mod, "activate", fake_activate)

    def fail_restart(delay_seconds=2):
        raise AssertionError("restart should not be scheduled")

    monkeypatch.setattr(activate_mod, "schedule_gateway_restart", fail_restart, raising=False)

    result = asyncio.run(
        activate_mod.activate_and_maybe_restart(
            "ABC123",
            base_url="https://chat.example",
            restart=False,
        )
    )

    assert result["ok"] is True
    assert "restart_scheduled" not in result
    assert result["restart_required"] is True
