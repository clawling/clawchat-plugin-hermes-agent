from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse, urlunparse

import yaml

from clawchat_gateway.api_client import DEFAULT_BASE_URL, DEFAULT_WEBSOCKET_URL, ClawChatApiClient
from clawchat_gateway.restart import schedule_gateway_restart


def _hermes_config_api() -> dict[str, Callable[..., Any]] | None:
    try:
        from hermes_cli.config import (
            get_config_path,
            get_env_path,
            read_raw_config,
            remove_env_value,
            save_config,
            save_env_value,
        )
    except Exception:
        return None
    return {
        "get_config_path": get_config_path,
        "get_env_path": get_env_path,
        "read_raw_config": read_raw_config,
        "remove_env_value": remove_env_value,
        "save_config": save_config,
        "save_env_value": save_env_value,
    }


def _hermes_home() -> Path:
    import os

    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _load_config() -> tuple[Path, dict[str, Any]]:
    api = _hermes_config_api()
    if api is not None:
        config_path = Path(api["get_config_path"]())
        try:
            config = api["read_raw_config"]() or {}
        except Exception:
            config = {}
        return config_path, config

    config_path = _hermes_home() / "config.yaml"
    if not config_path.exists():
        return config_path, {}
    try:
        return config_path, yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return config_path, {}


def _write_config(config_path: Path, config: dict[str, Any]) -> None:
    api = _hermes_config_api()
    if api is not None:
        api["save_config"](config)
        return

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=False, sort_keys=False),
        encoding="utf-8",
    )


def _env_path() -> Path:
    api = _hermes_config_api()
    if api is not None:
        return Path(api["get_env_path"]())
    return _hermes_home() / ".env"


def _validate_env_value(key: str, value: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot contain newlines")
    return value


def _write_env_values(values: dict[str, str | None]) -> Path:
    api = _hermes_config_api()
    if api is not None:
        for key, value in values.items():
            if value is None:
                api["remove_env_value"](key)
            else:
                api["save_env_value"](key, str(value))
        return Path(api["get_env_path"]())

    path = _env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = {
        key: None if value is None else _validate_env_value(key, str(value))
        for key, value in values.items()
    }
    emitted: set[str] = set()
    next_lines: list[str] = []

    for line in lines:
        key = line.split("=", 1)[0] if "=" in line else ""
        if key not in pending:
            next_lines.append(line)
            continue
        value = pending[key]
        if value is not None and key not in emitted:
            next_lines.append(f"{key}={value}")
            emitted.add(key)

    for key, value in pending.items():
        if value is not None and key not in emitted:
            next_lines.append(f"{key}={value}")

    path.write_text("\n".join(next_lines) + ("\n" if next_lines else ""), encoding="utf-8")
    return path


def _derive_websocket_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.netloc in {"company.newbaselab.com:19001", "company.newbaselab.com:10086"}:
        return DEFAULT_WEBSOCKET_URL
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunparse((scheme, parsed.netloc, "/v1/ws", "", "", ""))


def persist_activation(
    *,
    access_token: str,
    user_id: str,
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
    return persist_activation(
        access_token=str(result["access_token"]),
        user_id=str(result["agent"]["user_id"]),
        refresh_token=result.get("refresh_token"),
        base_url=base_url,
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m clawchat_gateway.activate")
    parser.add_argument("code", help="ClawChat activation code")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip the detached `hermes gateway restart` dispatched after activation.",
    )
    args = parser.parse_args(argv)
    payload = asyncio.run(
        activate_and_maybe_restart(
            args.code,
            base_url=args.base_url,
            restart=not args.no_restart,
        )
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
