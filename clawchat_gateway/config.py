import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _read_env_file_value(name: str) -> str:
    home = os.getenv("HERMES_HOME", "").strip()
    env_path = Path(home).expanduser() / ".env" if home else Path.home() / ".hermes" / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, sep, value = stripped.partition("=")
        if sep and key.strip() == name:
            return value.strip().strip("\"'")
    return ""


def _read_hermes_env_value(name: str) -> str:
    try:
        from hermes_cli.config import get_env_value
    except Exception:
        return ""

    try:
        return (get_env_value(name) or "").strip()
    except Exception:
        return ""


def _get_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    for name in names:
        value = _read_hermes_env_value(name)
        if value:
            return value
    for name in names:
        value = _read_env_file_value(name).strip()
        if value:
            return value
    return ""


def _get_config_value(data: dict[str, Any], key: str, default: Any = None) -> Any:
    if key in data:
        return data[key]
    return default


def _path_string(value: Any) -> str:
    if value is None:
        return ""
    try:
        text = os.fspath(value).strip()
    except TypeError:
        return ""
    if not text:
        return ""
    try:
        return str(Path(text).expanduser())
    except Exception:
        return ""


def _memory_root_from_home(home: str) -> str:
    return str(Path(home) / "memories") if home else ""


def _resolve_memory_root() -> str:
    try:
        import hermes_constants
    except Exception:
        hermes_constants = None
    if hermes_constants is not None:
        get_hermes_home = getattr(hermes_constants, "get_hermes_home", None)
        if callable(get_hermes_home):
            try:
                home = _path_string(get_hermes_home())
            except Exception:
                home = ""
            if home:
                return _memory_root_from_home(home)

    return _memory_root_from_home(_path_string(os.environ.get("HERMES_HOME", "")))


def _jwt_claim(token: str, claim: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return ""
    value = data.get(claim) if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) else ""


def _read_group_mode(value: Any) -> str:
    return "mention" if value == "mention" else "all"


def _read_group_command_mode(value: Any) -> str:
    return value if value in {"owner", "all", "off"} else "owner"


def _read_groups(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    groups: dict[str, dict[str, str]] = {}
    for chat_id, raw_group in value.items():
        if not isinstance(chat_id, str) or not chat_id:
            continue
        group = raw_group if isinstance(raw_group, dict) else {}
        groups[chat_id] = {
            "group_mode": _read_group_mode(group.get("group_mode")),
            "group_command_mode": _read_group_command_mode(
                group.get("group_command_mode")
            ),
        }
    return groups


@dataclass(frozen=True)
class ClawChatConfig:
    websocket_url: str
    base_url: str = ""
    token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    agent_id: str = ""
    owner_user_id: str = ""
    memory_root: str = ""
    group_mode: str = "all"
    group_command_mode: str = "owner"
    groups: dict[str, dict[str, str]] = field(default_factory=dict)
    reconnect_initial_delay_ms: int = 500
    reconnect_max_delay_ms: int = 15000
    reconnect_jitter_ratio: float = 0.3
    reconnect_max_retries: float = float("inf")
    heartbeat_interval_ms: int = 20000
    heartbeat_timeout_ms: int = 10000
    ack_timeout_ms: int = 15000
    ack_auto_resend_on_timeout: bool = False
    media_local_roots: tuple[str, ...] = field(default_factory=tuple)
    media_download_dir: str = "/tmp/clawchat-media"
    enable_rich_interactions: bool = False

    @classmethod
    def from_platform_config(cls, platform_config: Any) -> "ClawChatConfig":
        extra = getattr(platform_config, "extra", None) or {}
        media_roots_env = _get_env("CLAWCHAT_MEDIA_LOCAL_ROOTS")
        media_local_roots = (
            tuple(p.strip() for p in media_roots_env.split(os.pathsep) if p.strip())
            if media_roots_env
            else _get_config_value(extra, "media_local_roots", ())
        )
        token = _get_env("CLAWCHAT_TOKEN")
        return cls(
            websocket_url=_get_env("CLAWCHAT_WEBSOCKET_URL", "CLAWCHAT_WS_URL")
            or _get_config_value(extra, "websocket_url", ""),
            base_url=_get_env("CLAWCHAT_BASE_URL")
            or _get_config_value(extra, "base_url", ""),
            token=token,
            refresh_token=_get_env("CLAWCHAT_REFRESH_TOKEN"),
            user_id=_get_env("CLAWCHAT_USER_ID")
            or _get_config_value(extra, "user_id", ""),
            agent_id=_get_env("CLAWCHAT_AGENT_ID")
            or _get_config_value(extra, "agent_id", "")
            or _jwt_claim(token, "aid"),
            owner_user_id=_get_env("CLAWCHAT_OWNER_USER_ID")
            or _get_config_value(extra, "owner_user_id", ""),
            memory_root=_resolve_memory_root(),
            group_mode=_read_group_mode(
                _get_env("CLAWCHAT_GROUP_MODE")
                or _get_config_value(extra, "group_mode", "all")
            ),
            group_command_mode=_read_group_command_mode(
                _get_env("CLAWCHAT_GROUP_COMMAND_MODE")
                or _get_config_value(extra, "group_command_mode", "owner")
            ),
            groups=_read_groups(_get_config_value(extra, "groups", {})),
            reconnect_initial_delay_ms=_get_config_value(
                extra, "reconnect_initial_delay_ms", 500
            ),
            reconnect_max_delay_ms=_get_config_value(
                extra, "reconnect_max_delay_ms", 15000
            ),
            reconnect_jitter_ratio=_get_config_value(
                extra, "reconnect_jitter_ratio", 0.3
            ),
            reconnect_max_retries=_get_config_value(
                extra, "reconnect_max_retries", float("inf")
            ),
            heartbeat_interval_ms=_get_config_value(
                extra, "heartbeat_interval_ms", 20000
            ),
            heartbeat_timeout_ms=_get_config_value(
                extra, "heartbeat_timeout_ms", 10000
            ),
            ack_timeout_ms=_get_config_value(extra, "ack_timeout_ms", 15000),
            ack_auto_resend_on_timeout=_get_config_value(
                extra, "ack_auto_resend_on_timeout", False
            ),
            media_local_roots=tuple(media_local_roots),
            media_download_dir=_get_config_value(
                extra, "media_download_dir", "/tmp/clawchat-media"
            ),
            enable_rich_interactions=bool(
                _get_config_value(
                    extra,
                    "enable_rich_interactions",
                    False,
                )
            ),
        )


def effective_group_mode(config: ClawChatConfig, chat_id: str) -> str:
    exact = config.groups.get(chat_id)
    if exact is not None:
        return _read_group_mode(exact.get("group_mode"))
    wildcard = config.groups.get("*")
    if wildcard is not None:
        return _read_group_mode(wildcard.get("group_mode"))
    return _read_group_mode(config.group_mode)


def effective_group_command_mode(config: ClawChatConfig, chat_id: str) -> str:
    exact = config.groups.get(chat_id)
    if exact is not None:
        return _read_group_command_mode(exact.get("group_command_mode"))
    wildcard = config.groups.get("*")
    if wildcard is not None:
        return _read_group_command_mode(wildcard.get("group_command_mode"))
    return _read_group_command_mode(config.group_command_mode)
