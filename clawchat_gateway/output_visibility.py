from __future__ import annotations

import os
from copy import deepcopy
from typing import Any

OUTPUT_VISIBILITY_MODES = {"minimal", "normal", "full"}

DISPLAY_PRESETS: dict[str, dict[str, Any]] = {
    "minimal": {
        "tool_progress": "off",
        "show_reasoning": False,
        "streaming": False,
        "interim_assistant_messages": False,
        "long_running_notifications": False,
        "busy_ack_detail": False,
        "cleanup_progress": False,
    },
    "normal": {
        "tool_progress": "off",
        "show_reasoning": False,
        "streaming": False,
        "interim_assistant_messages": True,
        "long_running_notifications": False,
        "busy_ack_detail": False,
        "cleanup_progress": False,
    },
    "full": {
        "tool_progress": "verbose",
        "show_reasoning": True,
        "streaming": False,
        "interim_assistant_messages": True,
        "long_running_notifications": True,
        "busy_ack_detail": True,
        "cleanup_progress": False,
    },
}

AGENT_PRESETS: dict[str, dict[str, int]] = {
    "minimal": {
        "gateway_notify_interval": 0,
        "gateway_timeout_warning": 0,
    },
    "normal": {
        "gateway_notify_interval": 0,
        "gateway_timeout_warning": 0,
    },
    "full": {
        "gateway_notify_interval": 180,
        "gateway_timeout_warning": 900,
    },
}


def normalize_output_visibility(value: Any, *, default: str = "normal") -> str:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text not in OUTPUT_VISIBILITY_MODES:
        raise ValueError(
            "Unsupported ClawChat output visibility "
            f"{value!r}; expected minimal, normal, or full."
        )
    return text


def runtime_status_messages_for_visibility(mode: str) -> bool:
    return normalize_output_visibility(mode) == "full"


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


def _dict_child(parent: dict[str, Any], key: str) -> dict[str, Any]:
    child = parent.setdefault(key, {})
    if not isinstance(child, dict):
        child = {}
        parent[key] = child
    return child


def _clawchat_extra(config: dict[str, Any]) -> dict[str, Any]:
    platforms = _dict_child(config, "platforms")
    clawchat = _dict_child(platforms, "clawchat")
    return _dict_child(clawchat, "extra")


def _clawchat_display(config: dict[str, Any]) -> dict[str, Any]:
    display = _dict_child(config, "display")
    platforms = _dict_child(display, "platforms")
    return _dict_child(platforms, "clawchat")


def _agent(config: dict[str, Any]) -> dict[str, Any]:
    return _dict_child(config, "agent")


def _config_helpers():
    from hermes_cli.config import read_raw_config, save_config

    return read_raw_config, save_config


def apply_output_visibility(mode: str) -> dict[str, Any]:
    resolved_mode = normalize_output_visibility(mode)
    read_raw_config, save_config = _config_helpers()
    config = read_raw_config() or {}
    if not isinstance(config, dict):
        config = {}

    extra = _clawchat_extra(config)
    runtime_status_messages = runtime_status_messages_for_visibility(resolved_mode)
    extra["output_visibility"] = resolved_mode
    extra["runtime_status_messages"] = runtime_status_messages

    _clawchat_display(config).update(deepcopy(DISPLAY_PRESETS[resolved_mode]))
    agent_preset = AGENT_PRESETS[resolved_mode]
    _agent(config).update(agent_preset)
    os.environ["HERMES_AGENT_NOTIFY_INTERVAL"] = str(
        agent_preset["gateway_notify_interval"]
    )
    os.environ["HERMES_AGENT_TIMEOUT_WARNING"] = str(
        agent_preset["gateway_timeout_warning"]
    )

    save_config(config)
    return {
        "mode": resolved_mode,
        "runtime_status_messages": runtime_status_messages,
    }


def resolve_output_visibility_from_config(config: dict[str, Any] | None) -> str:
    if not isinstance(config, dict):
        return "normal"
    platforms = config.get("platforms") if isinstance(config.get("platforms"), dict) else {}
    clawchat = platforms.get("clawchat") if isinstance(platforms.get("clawchat"), dict) else {}
    extra = clawchat.get("extra") if isinstance(clawchat.get("extra"), dict) else {}

    raw_visibility = extra.get("output_visibility")
    if raw_visibility:
        return normalize_output_visibility(raw_visibility)

    legacy_runtime_status = _read_optional_bool(extra.get("runtime_status_messages"))
    if legacy_runtime_status is True:
        return "full"
    return "normal"


def runtime_status_messages_enabled(default: bool = False) -> bool:
    try:
        read_raw_config, _save_config = _config_helpers()
        config = read_raw_config() or {}
        return runtime_status_messages_for_visibility(
            resolve_output_visibility_from_config(config)
        )
    except Exception:
        return default
