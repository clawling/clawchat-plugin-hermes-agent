from __future__ import annotations

from types import SimpleNamespace

from clawchat_gateway.config import ClawChatConfig, effective_group_sessions_per_user


def test_group_sessions_per_user_resolves_with_group_overrides(monkeypatch):
    monkeypatch.delenv("CLAWCHAT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWCHAT_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("clawchat_gateway.config._read_hermes_env_value", lambda name: "")
    monkeypatch.setattr("clawchat_gateway.config._read_env_file_value", lambda name: "")
    platform_config = SimpleNamespace(
        extra={
            "websocket_url": "wss://example.test/ws",
            "group_sessions_per_user": True,
            "groups": {
                "*": {"group_sessions_per_user": False},
                "group-isolated": {"group_sessions_per_user": True},
            },
        }
    )

    config = ClawChatConfig.from_platform_config(platform_config)

    assert effective_group_sessions_per_user(config, "group-shared") is False
    assert effective_group_sessions_per_user(config, "group-isolated") is True


def test_group_sessions_per_user_defaults_to_hermes_isolated_groups(monkeypatch):
    monkeypatch.delenv("CLAWCHAT_TOKEN", raising=False)
    monkeypatch.delenv("CLAWCHAT_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr("clawchat_gateway.config._read_hermes_env_value", lambda name: "")
    monkeypatch.setattr("clawchat_gateway.config._read_env_file_value", lambda name: "")

    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://example.test/ws"})
    )

    assert effective_group_sessions_per_user(config, "group-default") is True
