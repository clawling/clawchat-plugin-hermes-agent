from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

# activate.py imports hermes_cli.config at module load (hard requirement to go
# through the official Hermes API). hermes_cli is not installed in the test env,
# so install a lightweight stub JUST long enough to import activate, then remove
# it so the stub does not leak into sys.modules and perturb other tests (e.g.
# output_visibility, which reads hermes_cli.config.read_raw_config). All migration
# tests monkeypatch activate's own helpers, so the stub bodies are never run.
_stub_installed = "hermes_cli" not in sys.modules
if _stub_installed:
    _hermes_cli = ModuleType("hermes_cli")
    _hermes_config = ModuleType("hermes_cli.config")
    _hermes_config.get_config_path = lambda: "/tmp/config.yaml"
    _hermes_config.get_env_path = lambda: "/tmp/.env"
    _hermes_config.read_raw_config = lambda: {}
    _hermes_config.remove_env_value = lambda key: None
    _hermes_config.save_config = lambda config: None
    _hermes_config.save_env_value = lambda key, value: None
    _hermes_cli.config = _hermes_config
    sys.modules["hermes_cli"] = _hermes_cli
    sys.modules["hermes_cli.config"] = _hermes_config

from clawchat_gateway import activate  # noqa: E402
from clawchat_gateway.config import ClawChatConfig  # noqa: E402

if _stub_installed:
    sys.modules.pop("hermes_cli", None)
    sys.modules.pop("hermes_cli.config", None)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeConfigStore:
    """In-memory stand-in for the hermes_cli config + env helpers."""

    def __init__(self, config: dict):
        self.config = config
        self.env: dict[str, str | None] = {}
        self.config_writes = 0
        self.env_write_calls = 0

    def load(self):
        return Path("/tmp/config.yaml"), self.config

    def write_config(self, _path, config):
        self.config_writes += 1
        self.config = config

    def write_env(self, values):
        self.env_write_calls += 1
        for key, value in values.items():
            self.env[key] = value
        return Path("/tmp/.env")


def _install(monkeypatch, store: _FakeConfigStore, env_lookup=None):
    monkeypatch.setattr(activate, "_load_config", store.load)
    monkeypatch.setattr(activate, "_write_config", store.write_config)
    monkeypatch.setattr(activate, "_write_env_values", store.write_env)
    if env_lookup is None:
        env_lookup = lambda *names: ""  # noqa: E731
    monkeypatch.setattr(activate, "_get_env", env_lookup)


def _extra(config: dict) -> dict:
    return config["platforms"]["clawchat"]["extra"]


# ---------------------------------------------------------------------------
# migrate_legacy_config_tokens
# ---------------------------------------------------------------------------


def test_migrate_token_when_env_empty(monkeypatch):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {
                    "token": "legacy-access-tok",
                    "user_id": "usr_1",
                    "owner_user_id": "usr_owner",
                }
            }
        }
    }
    store = _FakeConfigStore(config)
    _install(monkeypatch, store, env_lookup=lambda *names: "")

    summary = activate.migrate_legacy_config_tokens()

    assert summary["migrated_token"] is True
    assert summary["stripped"] is True
    # token written to .env
    assert store.env["CLAWCHAT_TOKEN"] == "legacy-access-tok"
    # stripped from config extra
    assert "token" not in _extra(store.config)
    # identity untouched
    assert _extra(store.config)["user_id"] == "usr_1"
    assert _extra(store.config)["owner_user_id"] == "usr_owner"
    assert store.config_writes == 1


def test_migrate_refresh_token(monkeypatch):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {
                    "token": "legacy-access-tok",
                    "refresh_token": "legacy-refresh-tok",
                }
            }
        }
    }
    store = _FakeConfigStore(config)
    _install(monkeypatch, store, env_lookup=lambda *names: "")

    summary = activate.migrate_legacy_config_tokens()

    assert summary["migrated_token"] is True
    assert summary["migrated_refresh"] is True
    assert store.env["CLAWCHAT_TOKEN"] == "legacy-access-tok"
    assert store.env["CLAWCHAT_REFRESH_TOKEN"] == "legacy-refresh-tok"
    assert "token" not in _extra(store.config)
    assert "refresh_token" not in _extra(store.config)


def test_env_already_set_does_not_overwrite_but_strips(monkeypatch):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {"token": "legacy-access-tok"},
            }
        }
    }
    store = _FakeConfigStore(config)

    def env_lookup(*names):
        if "CLAWCHAT_TOKEN" in names:
            return "env-wins-tok"
        return ""

    _install(monkeypatch, store, env_lookup=env_lookup)

    summary = activate.migrate_legacy_config_tokens()

    # env wins → no .env write for the token, but config is still stripped
    assert summary["migrated_token"] is False
    assert summary["stripped"] is True
    assert store.env.get("CLAWCHAT_TOKEN") is None
    assert "token" not in _extra(store.config)


def test_no_extra_tokens_is_noop(monkeypatch):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {"user_id": "usr_1"},
            }
        }
    }
    store = _FakeConfigStore(config)
    _install(monkeypatch, store, env_lookup=lambda *names: "")

    summary = activate.migrate_legacy_config_tokens()

    assert summary["migrated_token"] is False
    assert summary["migrated_refresh"] is False
    assert summary["stripped"] is False
    # No file writes at all.
    assert store.config_writes == 0
    assert store.env_write_calls == 0
    assert _extra(store.config)["user_id"] == "usr_1"


def test_migration_is_idempotent(monkeypatch):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {"token": "legacy-access-tok"},
            }
        }
    }
    store = _FakeConfigStore(config)
    _install(monkeypatch, store, env_lookup=lambda *names: "")

    first = activate.migrate_legacy_config_tokens()
    assert first["stripped"] is True
    assert store.config_writes == 1

    second = activate.migrate_legacy_config_tokens()
    assert second["migrated_token"] is False
    assert second["stripped"] is False
    # No additional writes on the second run.
    assert store.config_writes == 1


def test_migration_does_not_log_raw_token(monkeypatch, caplog):
    config = {
        "platforms": {
            "clawchat": {
                "extra": {"token": "super-secret-token-value"},
            }
        }
    }
    store = _FakeConfigStore(config)
    _install(monkeypatch, store, env_lookup=lambda *names: "")

    with caplog.at_level("INFO"):
        activate.migrate_legacy_config_tokens()

    assert "super-secret-token-value" not in caplog.text


def test_migration_failopen_on_load_error(monkeypatch):
    def boom():
        raise RuntimeError("cannot read config")

    monkeypatch.setattr(activate, "_load_config", boom)

    # Must not raise.
    summary = activate.migrate_legacy_config_tokens()
    assert summary["migrated_token"] is False
    assert summary["stripped"] is False


def test_migration_malformed_config_no_crash(monkeypatch):
    for bad in (
        {"platforms": "not-a-dict"},
        {"platforms": {"clawchat": "not-a-dict"}},
        {"platforms": {"clawchat": {"extra": "not-a-dict"}}},
        {},
    ):
        store = _FakeConfigStore(dict(bad))
        _install(monkeypatch, store, env_lookup=lambda *names: "")
        summary = activate.migrate_legacy_config_tokens()
        assert summary["stripped"] is False
        assert store.config_writes == 0
        assert store.env_write_calls == 0


# ---------------------------------------------------------------------------
# config.from_platform_config fallback
# ---------------------------------------------------------------------------


def _clear_config_env(monkeypatch):
    import clawchat_gateway.config as cfg

    monkeypatch.setattr(cfg, "_read_hermes_env_value", lambda name: "")
    monkeypatch.setattr(cfg, "_read_env_file_value", lambda name: "")
    for name in ("CLAWCHAT_TOKEN", "CLAWCHAT_REFRESH_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_config_token_falls_back_to_extra(monkeypatch):
    _clear_config_env(monkeypatch)
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(
            extra={
                "websocket_url": "wss://ws.test/ws",
                "token": "extra-tok",
                "refresh_token": "extra-refresh",
            }
        )
    )
    assert config.token == "extra-tok"
    assert config.refresh_token == "extra-refresh"


def test_config_env_token_wins_over_extra(monkeypatch):
    _clear_config_env(monkeypatch)
    monkeypatch.setenv("CLAWCHAT_TOKEN", "env-tok")
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(
            extra={"websocket_url": "wss://ws.test/ws", "token": "extra-tok"}
        )
    )
    assert config.token == "env-tok"
