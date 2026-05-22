from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    from hermes_cli.config import (
        get_config_path,
        get_env_path,
        read_raw_config,
        remove_env_value,
        save_config,
        save_env_value,
    )
except Exception as exc:
    raise RuntimeError(
        "ClawChat activation requires hermes_cli.config helpers; "
        "run activation through Hermes so config writes use the official API."
    ) from exc

from clawchat_gateway.api_client import DEFAULT_BASE_URL, DEFAULT_WEBSOCKET_URL, ClawChatApiClient
from clawchat_gateway.restart import schedule_gateway_restart
from clawchat_gateway.storage import get_clawchat_store

logger = logging.getLogger(__name__)


def _load_config() -> tuple[Path, dict[str, Any]]:
    config_path = Path(get_config_path())
    return config_path, read_raw_config() or {}


def _write_config(_config_path: Path, config: dict[str, Any]) -> None:
    save_config(config)


def _write_env_values(values: dict[str, str | None]) -> Path:
    for key, value in values.items():
        if value is None:
            remove_env_value(key)
        else:
            save_env_value(key, str(value))
    return Path(get_env_path())


def _derive_websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.netloc == "app.clawling.com":
        return DEFAULT_WEBSOCKET_URL
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/ws", "", "", ""))


def persist_activation(
    *,
    access_token: str,
    user_id: str,
    owner_user_id: str,
    agent_id: str = "",
    refresh_token: str | None,
    base_url: str,
) -> dict[str, Any]:
    config_path, config = _load_config()
    platforms = config.setdefault("platforms", {})
    clawchat = platforms.setdefault("clawchat", {})
    clawchat["enabled"] = True
    extra = clawchat.setdefault("extra", {})
    extra["base_url"] = base_url.rstrip("/")
    extra["websocket_url"] = _derive_websocket_url(extra["base_url"])
    extra.pop("token", None)
    extra.pop("refresh_token", None)
    extra["user_id"] = user_id
    if agent_id:
        extra["agent_id"] = agent_id
    else:
        extra.pop("agent_id", None)
    extra["owner_user_id"] = owner_user_id
    extra["reply_mode"] = "stream"
    extra["show_tools_output"] = False
    extra["show_think_output"] = False

    streaming = config.setdefault("streaming", {})
    streaming["enabled"] = True
    streaming.setdefault("transport", "edit")
    streaming.setdefault("edit_interval", 0.25)
    streaming.setdefault("buffer_threshold", 16)

    display = config.setdefault("display", {})
    display_platforms = display.setdefault("platforms", {})
    clawchat_display = display_platforms.setdefault("clawchat", {})
    clawchat_display["tool_progress"] = "off"
    clawchat_display["show_reasoning"] = False

    env_path = _write_env_values(
        {
            "CLAWCHAT_TOKEN": access_token,
            "CLAWCHAT_REFRESH_TOKEN": refresh_token or None,
        }
    )
    _write_config(config_path, config)
    return {
        "config_path": str(config_path),
        "env_path": str(env_path),
        "user_id": user_id,
        "agent_id": agent_id,
        "owner_user_id": owner_user_id,
        "base_url": extra["base_url"],
        "websocket_url": extra["websocket_url"],
        "token": "***",
        "refresh_token": "***" if refresh_token else None,
        "restart_required": True,
        "restart_message": "Restart Hermes gateway so ClawChat reloads the new credentials.",
    }


async def activate(code: str, *, base_url: str) -> dict[str, Any]:
    client = ClawChatApiClient(base_url=base_url.rstrip("/"), token="", user_id="")
    result = await client.agents_connect(code=code)
    agent = result["agent"]
    agent_id = str(agent.get("id") or "")
    user_id = str(agent["user_id"])
    owner_id = str(agent["owner_id"])
    conversation_id = str(result["conversation"]["id"])
    payload = persist_activation(
        access_token=str(result["access_token"]),
        user_id=user_id,
        agent_id=agent_id,
        owner_user_id=owner_id,
        refresh_token=result.get("refresh_token"),
        base_url=base_url,
    )
    try:
        get_clawchat_store().upsert_activation(
            platform="hermes",
            account_id="default",
            user_id=user_id,
            conversation_id=conversation_id,
            owner_user_id=owner_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("clawchat activation database persistence failed")
    return payload


async def activate_and_maybe_restart(
    code: str,
    *,
    base_url: str,
    restart: bool,
    restart_delay_seconds: int = 2,
) -> dict[str, Any]:
    payload = await activate(code.strip(), base_url=base_url)
    payload["ok"] = True
    if restart:
        payload["restart_scheduled"] = True
        payload["restart_delay_seconds"] = restart_delay_seconds
        payload["restart_command"] = schedule_gateway_restart(
            delay_seconds=restart_delay_seconds
        )
        payload["restart_message"] = (
            "ClawChat activation is saved. Hermes restart has been scheduled in the background."
        )
    return payload
