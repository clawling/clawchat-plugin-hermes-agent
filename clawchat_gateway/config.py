import base64
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from clawchat_gateway.api_client import DEFAULT_BASE_URL, DEFAULT_WEBSOCKET_URL


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


def resolve_activation_base_url() -> str:
    """Base URL for `clawchat activate`.

    The installer writes the deployment's CLAWCHAT_BASE_URL into the Hermes
    .env, so activation must honor it (env -> hermes env -> .env file) instead
    of always hitting the public default — otherwise a connect code minted on a
    custom backend is sent to app.clawling.com and rejected as invalid.
    """
    return _get_env("CLAWCHAT_BASE_URL") or DEFAULT_BASE_URL


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


def _jwt_exp(token: str) -> int | None:
    """Decode the access token's ``exp`` claim (epoch seconds).

    Returns ``None`` when the token is empty / malformed / has no numeric
    ``exp`` — callers fall back to ``activated_at + 24h`` (spec A.0). No expiry
    column is persisted: the value is derived from the live token on each load.
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    exp = data.get("exp")
    if isinstance(exp, bool):
        return None
    if isinstance(exp, int):
        return exp
    if isinstance(exp, float):
        return int(exp)
    if isinstance(exp, str):
        try:
            return int(float(exp.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _jwt_iat(token: str) -> int | None:
    """Decode the access token's ``iat`` claim (epoch seconds) or ``None``."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    iat = data.get("iat")
    if isinstance(iat, bool):
        return None
    if isinstance(iat, (int, float)):
        return int(iat)
    if isinstance(iat, str):
        try:
            return int(float(iat.strip()))
        except (TypeError, ValueError):
            return None
    return None


def _read_group_mode(value: Any) -> str:
    return "mention" if value == "mention" else "all"


def _read_group_command_mode(value: Any) -> str:
    return value if value in {"owner", "all", "off"} else "owner"


def _read_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _read_group_sessions_per_user(value: Any) -> bool:
    parsed = _read_optional_bool(value)
    return True if parsed is None else parsed


def _read_groups(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    groups: dict[str, dict[str, Any]] = {}
    for chat_id, raw_group in value.items():
        if not isinstance(chat_id, str) or not chat_id:
            continue
        group = raw_group if isinstance(raw_group, dict) else {}
        parsed_group: dict[str, Any] = {}
        if "group_mode" in group:
            parsed_group["group_mode"] = _read_group_mode(group.get("group_mode"))
        if "group_command_mode" in group:
            parsed_group["group_command_mode"] = _read_group_command_mode(
                group.get("group_command_mode")
            )
        if "group_sessions_per_user" in group:
            parsed = _read_optional_bool(group.get("group_sessions_per_user"))
            if parsed is not None:
                parsed_group["group_sessions_per_user"] = parsed
        groups[chat_id] = parsed_group
    return groups


@dataclass(frozen=True)
class ClawChatConfig:
    websocket_url: str
    base_url: str = ""
    media_base_url: str = ""
    token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    agent_id: str = ""
    owner_user_id: str = ""
    memory_root: str = ""
    group_mode: str = "all"
    group_command_mode: str = "owner"
    group_sessions_per_user: bool = True
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
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
    runtime_status_messages: bool = False

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
            or _get_config_value(extra, "websocket_url", "")
            or DEFAULT_WEBSOCKET_URL,
            base_url=_get_env("CLAWCHAT_BASE_URL")
            or _get_config_value(extra, "base_url", "")
            or DEFAULT_BASE_URL,
            media_base_url=_get_env("CLAWCHAT_MEDIA_BASE_URL")
            or _get_config_value(extra, "media_base_url", ""),
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
            group_sessions_per_user=_read_group_sessions_per_user(
                _get_config_value(extra, "group_sessions_per_user", True)
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
            runtime_status_messages=_read_optional_bool(
                _get_config_value(extra, "runtime_status_messages", False)
            )
            is True,
        )


def effective_group_mode(config: ClawChatConfig, chat_id: str) -> str:
    exact = config.groups.get(chat_id)
    if exact is not None and "group_mode" in exact:
        return _read_group_mode(exact.get("group_mode"))
    wildcard = config.groups.get("*")
    if wildcard is not None and "group_mode" in wildcard:
        return _read_group_mode(wildcard.get("group_mode"))
    return _read_group_mode(config.group_mode)


def effective_group_command_mode(config: ClawChatConfig, chat_id: str) -> str:
    exact = config.groups.get(chat_id)
    if exact is not None and "group_command_mode" in exact:
        return _read_group_command_mode(exact.get("group_command_mode"))
    wildcard = config.groups.get("*")
    if wildcard is not None and "group_command_mode" in wildcard:
        return _read_group_command_mode(wildcard.get("group_command_mode"))
    return _read_group_command_mode(config.group_command_mode)


def effective_group_sessions_per_user(config: ClawChatConfig, chat_id: str) -> bool:
    exact = config.groups.get(chat_id)
    if exact is not None and "group_sessions_per_user" in exact:
        return bool(exact.get("group_sessions_per_user"))
    wildcard = config.groups.get("*")
    if wildcard is not None and "group_sessions_per_user" in wildcard:
        return bool(wildcard.get("group_sessions_per_user"))
    return bool(config.group_sessions_per_user)
