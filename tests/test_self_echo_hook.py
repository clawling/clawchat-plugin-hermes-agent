from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SessionSource


def _load_root_plugin():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_root_plugin", plugin_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@dataclass
class _PlatformConfig:
    extra: dict[str, Any]


@dataclass
class _Config:
    platforms: dict[Any, Any]


@dataclass
class _Gateway:
    config: _Config


def _build_gateway(bot_user_id: str | None) -> _Gateway:
    extra = {"user_id": bot_user_id} if bot_user_id is not None else {}
    return _Gateway(config=_Config(platforms={Platform.CLAWCHAT: _PlatformConfig(extra=extra)}))


def _build_event(*, platform, user_id: str, chat_id: str = "c1") -> MessageEvent:
    src = SessionSource(platform=platform, chat_id=chat_id, user_id=user_id)
    return MessageEvent(text="anything", source=src)


def test_self_echo_is_skipped():
    plugin = _load_root_plugin()
    gateway = _build_gateway(bot_user_id="bot-1")
    event = _build_event(platform=Platform.CLAWCHAT, user_id="bot-1")

    result = plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "clawchat-self-echo"}


def test_self_echo_is_skipped_with_string_platform_config():
    plugin = _load_root_plugin()
    gateway = _Gateway(
        config=_Config(
            platforms={"clawchat": _PlatformConfig(extra={"user_id": "bot-1"})}
        )
    )
    event = _build_event(platform="clawchat", user_id="bot-1")

    result = plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "clawchat-self-echo"}


def test_real_user_message_is_allowed():
    plugin = _load_root_plugin()
    gateway = _build_gateway(bot_user_id="bot-1")
    event = _build_event(platform=Platform.CLAWCHAT, user_id="user-99")

    assert plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway) is None


def test_other_platform_is_left_alone():
    plugin = _load_root_plugin()
    gateway = _build_gateway(bot_user_id="bot-1")
    event = _build_event(platform=Platform.QQBOT, user_id="bot-1")

    assert plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway) is None


def test_unconfigured_bot_user_id_does_not_skip():
    plugin = _load_root_plugin()
    gateway = _build_gateway(bot_user_id=None)
    event = _build_event(platform=Platform.CLAWCHAT, user_id="bot-1")

    assert plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway) is None


def test_event_without_user_id_is_left_alone():
    plugin = _load_root_plugin()
    gateway = _build_gateway(bot_user_id="bot-1")
    event = _build_event(platform=Platform.CLAWCHAT, user_id="")

    assert plugin._clawchat_pre_gateway_dispatch(event=event, gateway=gateway) is None
