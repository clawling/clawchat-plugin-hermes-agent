# tests/test_media_base_url.py
from __future__ import annotations

from types import SimpleNamespace

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.media_runtime import derive_base_url


def _clear(monkeypatch):
    for name in ("CLAWCHAT_TOKEN", "CLAWCHAT_REFRESH_TOKEN", "CLAWCHAT_MEDIA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("clawchat_gateway.config._read_hermes_env_value", lambda name: "")
    monkeypatch.setattr("clawchat_gateway.config._read_env_file_value", lambda name: "")


def test_media_base_url_from_env(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAWCHAT_MEDIA_BASE_URL", "https://media.test:39003")
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://ws.test/ws"})
    )
    assert config.media_base_url == "https://media.test:39003"


def test_media_base_url_from_extra(monkeypatch):
    _clear(monkeypatch)
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://ws.test/ws", "media_base_url": "https://m.extra"})
    )
    assert config.media_base_url == "https://m.extra"


def test_media_base_url_defaults_empty(monkeypatch):
    _clear(monkeypatch)
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://ws.test/ws"})
    )
    assert config.media_base_url == ""


def test_media_base_url_env_wins_over_extra(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAWCHAT_MEDIA_BASE_URL", "https://env.test")
    config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://ws.test/ws", "media_base_url": "https://extra.test"})
    )
    assert config.media_base_url == "https://env.test"


def test_derive_base_url_prefers_explicit_media_base():
    assert (
        derive_base_url(
            websocket_url="wss://ws.test/ws",
            base_url="https://api.test",
            media_base_url="https://media.test:39003/",
        )
        == "https://media.test:39003"
    )


def test_derive_base_url_falls_back_to_ws_derivation():
    assert (
        derive_base_url(websocket_url="wss://ws.test:39002/ws", base_url="", media_base_url="")
        == "https://ws.test:39002"
    )


def test_derive_base_url_ignores_whitespace_media_base():
    assert (
        derive_base_url(websocket_url="wss://ws.test:39002/ws", base_url="", media_base_url="   ")
        == "https://ws.test:39002"
    )
