"""Activation records the WebSocket URL the installer chose, not a derived one.

The installer writes ``CLAWCHAT_WEBSOCKET_URL`` next to ``CLAWCHAT_BASE_URL``.
At runtime that env value already wins over ``extra.websocket_url``; activation
must write the same value into ``extra.websocket_url`` (and report it), and
only derive one from the base URL when no explicit URL is set.
"""

from __future__ import annotations

import pytest

from clawchat_gateway import activate


@pytest.fixture
def fake_config(monkeypatch, tmp_path):
    state: dict = {"config": {}}

    def write(_path, config):
        state["config"] = config

    monkeypatch.setattr(activate, "_load_config", lambda: (tmp_path / "config.yaml", state["config"]))
    monkeypatch.setattr(activate, "_write_config", write)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: tmp_path / ".env")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("CLAWCHAT_WEBSOCKET_URL", "CLAWCHAT_WS_URL"):
        monkeypatch.delenv(name, raising=False)
    return state


def _activate():
    return activate.persist_activation(
        access_token="tok",
        user_id="usr_agent",
        owner_user_id="usr_owner",
        refresh_token=None,
        base_url="https://api.example.invalid/",
    )


def test_explicit_websocket_url_wins(fake_config, monkeypatch):
    monkeypatch.setenv("CLAWCHAT_WEBSOCKET_URL", "wss://ws.example.invalid/socket")
    payload = _activate()
    extra = fake_config["config"]["platforms"]["clawchat"]["extra"]
    assert extra["websocket_url"] == "wss://ws.example.invalid/socket"
    assert payload["websocket_url"] == "wss://ws.example.invalid/socket"


def test_legacy_ws_url_name_is_honoured(fake_config, monkeypatch):
    monkeypatch.setenv("CLAWCHAT_WS_URL", "wss://legacy.example.invalid/ws")
    _activate()
    assert fake_config["config"]["platforms"]["clawchat"]["extra"]["websocket_url"] == "wss://legacy.example.invalid/ws"


def test_derived_from_base_url_without_an_explicit_one(fake_config):
    _activate()
    assert fake_config["config"]["platforms"]["clawchat"]["extra"]["websocket_url"] == "wss://api.example.invalid/ws"
