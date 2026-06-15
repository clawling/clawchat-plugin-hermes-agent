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

from clawchat_gateway.api_client import (
    ACTIVATION_TIMEOUT_SECONDS,
    DEFAULT_BASE_URL,
    DEFAULT_WEBSOCKET_URL,
    ClawChatApiClient,
    agents_connect_with_retry,
)
from clawchat_gateway.device_id import get_device_id
from clawchat_gateway.output_visibility import (
    normalize_output_visibility,
    runtime_status_messages_for_visibility,
)
from clawchat_gateway.restart import schedule_gateway_restart
from clawchat_gateway.storage import get_clawchat_store

logger = logging.getLogger(__name__)

CLAWCHAT_GLOBAL_DISPLAY_DEFAULTS = {
    "busy_input_mode": "queue",
    "busy_ack_enabled": False,
    "background_process_notifications": "off",
    "tool_progress_command": False,
}

CLAWCHAT_AGENT_DEFAULTS = {
    "gateway_notify_interval": 0,
    "gateway_timeout_warning": 0,
}

CLAWCHAT_DISPLAY_DEFAULTS = {
    "tool_progress": "off",
    "show_reasoning": False,
    "streaming": False,
    "interim_assistant_messages": True,
    "long_running_notifications": False,
    "busy_ack_detail": False,
    "cleanup_progress": False,
}


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


def _read_existing_user_id(config: dict[str, Any]) -> str:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return ""
    clawchat = platforms.get("clawchat")
    if not isinstance(clawchat, dict):
        return ""
    extra = clawchat.get("extra")
    if not isinstance(extra, dict):
        return ""
    user_id = extra.get("user_id")
    return user_id.strip() if isinstance(user_id, str) else ""


def _derive_websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.netloc == "app.clawling.com":
        return DEFAULT_WEBSOCKET_URL
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/ws", "", "", ""))


def _ensure_clawchat_display_defaults(config: dict[str, Any]) -> None:
    display = config.setdefault("display", {})
    if not isinstance(display, dict):
        display = {}
        config["display"] = display
    for key, value in CLAWCHAT_GLOBAL_DISPLAY_DEFAULTS.items():
        display[key] = value
    display_platforms = display.setdefault("platforms", {})
    if not isinstance(display_platforms, dict):
        display_platforms = {}
        display["platforms"] = display_platforms
    clawchat_display = display_platforms.setdefault("clawchat", {})
    if not isinstance(clawchat_display, dict):
        clawchat_display = {}
        display_platforms["clawchat"] = clawchat_display
    for key, value in CLAWCHAT_DISPLAY_DEFAULTS.items():
        clawchat_display.setdefault(key, value)


def _ensure_clawchat_agent_defaults(config: dict[str, Any]) -> None:
    agent = config.setdefault("agent", {})
    if not isinstance(agent, dict):
        agent = {}
        config["agent"] = agent
    for key, value in CLAWCHAT_AGENT_DEFAULTS.items():
        agent[key] = value


def _read_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _ensure_output_visibility_defaults(extra: dict[str, Any]) -> None:
    raw_visibility = extra.get("output_visibility")
    try:
        mode = normalize_output_visibility(raw_visibility, default="")
    except ValueError:
        mode = ""
    if not mode:
        mode = (
            "full"
            if _read_optional_bool(extra.get("runtime_status_messages")) is True
            else "normal"
        )
    extra["output_visibility"] = mode
    extra["runtime_status_messages"] = runtime_status_messages_for_visibility(mode)


def persist_activation(
    *,
    access_token: str,
    user_id: str,
    owner_user_id: str,
    agent_id: str = "",
    refresh_token: str | None,
    base_url: str,
    home_channel_id: str | None = None,
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
    _ensure_output_visibility_defaults(extra)
    _ensure_clawchat_agent_defaults(config)
    _ensure_clawchat_display_defaults(config)
    env_values = {
        "CLAWCHAT_TOKEN": access_token,
        "CLAWCHAT_REFRESH_TOKEN": refresh_token or None,
    }
    if home_channel_id:
        env_values.update(
            {
                "CLAWCHAT_HOME_CHANNEL": home_channel_id,
                "CLAWCHAT_HOME_CHANNEL_THREAD_ID": "",
                "CLAWCHAT_HOME_CHANNEL_NAME": "ClawChat",
            }
        )
    env_path = _write_env_values(env_values)
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
        "home_channel_id": home_channel_id or None,
        "restart_required": True,
        "restart_message": "Restart Hermes gateway so ClawChat reloads the new credentials.",
    }


def persist_rotated_tokens(
    *,
    access_token: str,
    refresh_token: str,
    device_id: str | None = None,
    account_id: str = "default",
    user_id: str | None = None,
    owner_user_id: str | None = None,
    conversation_id: str | None = None,
) -> bool:
    """Durably write a refresh-rotated token pair to BOTH .env and SQLite.

    Token-refresh spec §0 + §C.2: this is the persist step that MUST complete
    before the in-memory token is swapped. It writes the rotated pair to the
    Hermes ``.env`` (so an env-booted process recovers) AND the SQLite
    ``activations`` row (so the wait-for-activation loop / a future restart pick
    it up), without scheduling a gateway restart. Returns True only when BOTH
    writes succeed.

    For an **env-only deployment** (CLAWCHAT_TOKEN/CLAWCHAT_REFRESH_TOKEN preset
    in ``.env``, never activated in-pod) there is NO activations row yet, so the
    SQLite write seeds one from the supplied identity (``user_id`` /
    ``owner_user_id`` / ``conversation_id`` threaded down from the connection's
    in-memory config) instead of failing — otherwise the very first refresh would
    brick the agent (§C.2). The seed never resets bootstrap/activation flags.
    """
    env_ok = False
    try:
        _write_env_values(
            {
                "CLAWCHAT_TOKEN": access_token,
                "CLAWCHAT_REFRESH_TOKEN": refresh_token or None,
            }
        )
        env_ok = True
    except Exception:  # noqa: BLE001
        logger.warning("clawchat rotated-token .env persistence failed", exc_info=True)
    db_ok = False
    try:
        result = get_clawchat_store().update_activation_tokens(
            platform="hermes",
            account_id=account_id,
            access_token=access_token,
            refresh_token=refresh_token,
            device_id=device_id,
            seed_user_id=user_id,
            seed_owner_user_id=owner_user_id,
            seed_conversation_id=conversation_id,
        )
        db_ok = bool(result)
    except Exception:  # noqa: BLE001
        logger.warning("clawchat rotated-token database persistence failed", exc_info=True)
    return env_ok and db_ok


def clear_persisted_credentials(*, account_id: str = "default") -> None:
    """Remove ClawChat credentials from BOTH .env and SQLite, keeping identity.

    Token-refresh spec §C.1: auto-logout on permanent refresh failure removes
    ``CLAWCHAT_TOKEN`` / ``CLAWCHAT_REFRESH_TOKEN`` from .env and blanks the
    token columns of the activations row, while preserving user_id /
    owner_user_id / conversation_id so re-pair reuses the same identity.
    """
    try:
        _write_env_values(
            {
                "CLAWCHAT_TOKEN": None,
                "CLAWCHAT_REFRESH_TOKEN": None,
            }
        )
    except Exception:  # noqa: BLE001
        logger.warning("clawchat logout .env clear failed", exc_info=True)
    try:
        get_clawchat_store().clear_activation_credentials(
            platform="hermes",
            account_id=account_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning("clawchat logout database clear failed", exc_info=True)


async def activate(code: str, *, base_url: str) -> dict[str, Any]:
    client = ClawChatApiClient(
        base_url=base_url.rstrip("/"),
        token="",
        user_id="",
        timeout=ACTIVATION_TIMEOUT_SECONDS,
    )
    _config_path, config = _load_config()
    existing_user_id = _read_existing_user_id(config)
    result = await agents_connect_with_retry(
        client, code=code, user_id=existing_user_id or None
    )
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
        home_channel_id=conversation_id,
    )
    try:
        get_clawchat_store().upsert_activation(
            platform="hermes",
            account_id="default",
            user_id=user_id,
            conversation_id=conversation_id,
            owner_user_id=owner_id,
            access_token=str(result["access_token"]),
            refresh_token=result.get("refresh_token"),
            # Token-refresh spec §E: persist the EXACT device id presented on
            # connect (this is the x-device-id baked into the session), so the
            # later /v1/auth/refresh sends it verbatim and avoids a 10003
            # device-mismatch on pod reschedule when CLAWCHAT_DEVICE_ID is pinned.
            device_id=get_device_id(),
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
