from __future__ import annotations

import logging
import os
import sys
from copy import copy
from pathlib import Path
from types import SimpleNamespace

logger = logging.getLogger(__name__)


def _plugin_dir() -> Path:
    return Path(__file__).resolve().parent


# Hermes loads this plugin as ``hermes_plugins.clawchat`` and only sets up
# its ``__path__`` for relative submodule imports. The plugin's own helpers
# reach for the package via absolute imports, so the plugin root must be on
# ``sys.path``.
_PLUGIN_ROOT = str(_plugin_dir())
if _PLUGIN_ROOT not in sys.path:
    sys.path.insert(0, _PLUGIN_ROOT)


def _setup_clawchat_platform() -> None:
    from clawchat_gateway.setup import setup_clawchat_platform

    setup_clawchat_platform()


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _clawchat_home_extra() -> dict:
    config_path = _hermes_home() / "config.yaml"
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.debug(
            "ClawChat could not read Hermes config.yaml for registry check: %s",
            exc,
        )
        return {}

    platform_block = (data.get("platforms") or {}).get("clawchat") or {}
    if not isinstance(platform_block, dict):
        return {}
    extra = platform_block.get("extra") or {}
    return extra if isinstance(extra, dict) else {}


def _clawchat_platform_config_with_home_extra(config):
    """Merge config.yaml ClawChat extra into sparse plugin PlatformConfig values.

    Hermes v0.12 can load gateway config before user plugin platform names are
    registered. In that path the dynamic platform may be enabled but its
    ``extra`` block is empty. Once the plugin is registered, use the canonical
    config.yaml data as a fallback while letting explicit runtime config win.
    """
    home_extra = _clawchat_home_extra()
    current_extra = getattr(config, "extra", None) or {}
    if not home_extra:
        return config
    if not isinstance(current_extra, dict):
        current_extra = {}

    merged_extra = dict(home_extra)
    for key, value in current_extra.items():
        if value is None or value == "":
            continue
        merged_extra[key] = value

    if merged_extra == current_extra:
        return config

    try:
        merged_config = copy(config)
        merged_config.extra = merged_extra
        return merged_config
    except Exception:
        return SimpleNamespace(extra=merged_extra)


def _clawchat_env_enablement() -> dict | None:
    from clawchat_gateway.api_client import DEFAULT_BASE_URL, DEFAULT_WEBSOCKET_URL

    seed = {
        "base_url": os.getenv("CLAWCHAT_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        "websocket_url": (
            os.getenv("CLAWCHAT_WEBSOCKET_URL", "").strip()
            or os.getenv("CLAWCHAT_WS_URL", "").strip()
            or DEFAULT_WEBSOCKET_URL
        ),
    }
    home_channel = os.getenv("CLAWCHAT_HOME_CHANNEL", "").strip()
    if not home_channel:
        return seed

    home = {
        "chat_id": home_channel,
        "name": os.getenv("CLAWCHAT_HOME_CHANNEL_NAME", "").strip() or "ClawChat",
    }
    thread_id = os.getenv("CLAWCHAT_HOME_CHANNEL_THREAD_ID", "").strip()
    if thread_id:
        home["thread_id"] = thread_id
    seed["home_channel"] = home
    return seed


def _clawchat_dependencies_available() -> bool:
    try:
        import websockets  # noqa: F401
    except ImportError:
        return False
    return True


def _clawchat_connection_configured(config=None) -> bool:
    from clawchat_gateway.config import ClawChatConfig

    platform_config = (
        _clawchat_platform_config_with_home_extra(config)
        if config is not None
        else SimpleNamespace(extra=_clawchat_home_extra())
    )
    clawchat_config = ClawChatConfig.from_platform_config(platform_config)
    return bool(clawchat_config.websocket_url and clawchat_config.token)


def _clawchat_can_start(config=None) -> bool:
    from clawchat_gateway.config import ClawChatConfig

    platform_config = (
        _clawchat_platform_config_with_home_extra(config)
        if config is not None
        else SimpleNamespace(extra=_clawchat_home_extra())
    )
    clawchat_config = ClawChatConfig.from_platform_config(platform_config)
    return bool(clawchat_config.websocket_url)


def _check_clawchat_platform_requirements() -> bool:
    return _clawchat_dependencies_available()


def _validate_clawchat_platform_config(config) -> bool:
    if not _clawchat_dependencies_available():
        return False

    from clawchat_gateway.config import ClawChatConfig

    merged_config = _clawchat_platform_config_with_home_extra(config)
    clawchat_config = ClawChatConfig.from_platform_config(merged_config)
    configured = bool(clawchat_config.websocket_url)
    if not configured:
        logger.warning(
            "ClawChat platform config incomplete: websocket_url=%s token=%s hermes_home=%s",
            bool(clawchat_config.websocket_url),
            bool(clawchat_config.token),
            _hermes_home(),
        )
    return configured


def _create_clawchat_adapter(config):
    from clawchat_gateway.adapter import ClawChatAdapter

    return ClawChatAdapter(_clawchat_platform_config_with_home_extra(config))


def _patch_send_message_target_parser() -> None:
    """Teach Hermes' built-in send_message tool ClawChat conversation ids.

    Hermes keeps target parsing inside ``tools.send_message_tool``. Plugin
    platforms can register adapters without changing Hermes source, but the
    built-in parser must still recognize platform-specific explicit ids before
    it reaches the registered adapter. Keep this patch narrowly scoped to
    ``clawchat:cnv_...`` and delegate every other target to Hermes' original
    parser.
    """
    try:
        from tools import send_message_tool
    except Exception as exc:
        logger.debug("ClawChat could not patch send_message target parser: %s", exc)
        return

    original = getattr(send_message_tool, "_parse_target_ref", None)
    if not callable(original) or getattr(original, "_clawchat_target_patch", False):
        return

    def _parse_target_ref_with_clawchat(platform_name: str, target_ref: str):
        platform = str(platform_name or "").strip().lower()
        target = str(target_ref or "").strip()
        if platform == "clawchat" and target.startswith("cnv_"):
            return target, None, True
        return original(platform_name, target_ref)

    _parse_target_ref_with_clawchat._clawchat_target_patch = True
    _parse_target_ref_with_clawchat._clawchat_original = original
    send_message_tool._parse_target_ref = _parse_target_ref_with_clawchat


async def _send_clawchat_media_via_live_adapter(
    platform,
    chat_id: str,
    message: str,
    *,
    thread_id=None,
    media_files=None,
):
    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
    except Exception:
        runner = None

    adapter = None
    if runner is not None:
        try:
            adapter = runner.adapters.get(platform)
        except Exception:
            adapter = None
    if adapter is None:
        return {
            "error": (
                "No live adapter for platform 'clawchat'. Is the gateway "
                "running with this platform connected?"
            )
        }

    metadata = {"_clawchat_immediate_media_send": True}
    if thread_id:
        metadata["thread_id"] = thread_id
    try:
        result = await adapter.send(
            chat_id=chat_id,
            content=message,
            metadata=metadata,
            media_files=media_files or [],
            _clawchat_media_files_validated=True,
        )
    except Exception as exc:
        return {"error": f"Plugin platform send failed: {exc}"}
    if result.success:
        return {"success": True, "message_id": result.message_id}
    return {"error": f"Adapter send failed: {result.error}"}


def _patch_send_message_media_delivery() -> None:
    """Let Hermes' built-in send_message deliver ClawChat MEDIA attachments.

    Hermes owns ``send_message`` and currently hard-codes native media branches
    for built-in platforms before the generic plugin adapter path. Keep this
    plugin patch scoped to ClawChat media sends so text-only delivery continues
    through Hermes' original implementation.
    """
    try:
        from tools import send_message_tool
    except Exception as exc:
        logger.debug("ClawChat could not patch send_message media delivery: %s", exc)
        return

    original = getattr(send_message_tool, "_send_to_platform", None)
    if not callable(original) or getattr(original, "_clawchat_media_patch", False):
        return

    async def _send_to_platform_with_clawchat_media(
        platform,
        pconfig,
        chat_id,
        message,
        thread_id=None,
        media_files=None,
        force_document=False,
    ):
        platform_name = getattr(platform, "value", str(platform))
        if platform_name == "clawchat" and media_files:
            return await _send_clawchat_media_via_live_adapter(
                platform,
                chat_id,
                message,
                thread_id=thread_id,
                media_files=media_files,
            )
        return await original(
            platform,
            pconfig,
            chat_id,
            message,
            thread_id=thread_id,
            media_files=media_files,
            force_document=force_document,
        )

    _send_to_platform_with_clawchat_media._clawchat_media_patch = True
    _send_to_platform_with_clawchat_media._clawchat_original = original
    send_message_tool._send_to_platform = _send_to_platform_with_clawchat_media


def _migrate_legacy_config_tokens() -> None:
    """Best-effort migration of legacy config.yaml tokens into env/.env.

    Old plugin configs stored the auth token under extra.token; the current
    plugin reads tokens only from env/.env/SQLite. Run once at plugin load so an
    upgraded config still connects. Never let a failure break registration.
    """
    try:
        from clawchat_gateway.activate import migrate_legacy_config_tokens

        migrate_legacy_config_tokens()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ClawChat legacy-token migration skipped: %s", exc)


def _register_platform(ctx) -> bool:
    from clawchat_gateway.plugin_prompts import platform_prompt

    _migrate_legacy_config_tokens()

    register_platform = getattr(ctx, "register_platform", None)
    if not callable(register_platform):
        raise RuntimeError(
            "ClawChat requires Hermes v0.12.0+ with ctx.register_platform support."
        )

    register_platform(
        name="clawchat",
        label="ClawChat",
        adapter_factory=_create_clawchat_adapter,
        setup_fn=_setup_clawchat_platform,
        check_fn=_check_clawchat_platform_requirements,
        validate_config=_validate_clawchat_platform_config,
        is_connected=_clawchat_can_start,
        required_env=[],
        install_hint=(
            "Activate ClawChat with hermes gateway setup, hermes clawchat activate CODE, "
            "or /clawchat-activate CODE."
        ),
        allowed_users_env="CLAWCHAT_ALLOWED_USERS",
        allow_all_env="CLAWCHAT_ALLOW_ALL_USERS",
        env_enablement_fn=_clawchat_env_enablement,
        max_message_length=0,
        emoji="💬",
        platform_hint=platform_prompt(),
    )
    _patch_send_message_target_parser()
    _patch_send_message_media_delivery()
    logger.info("ClawChat registered Hermes platform via plugin registry")
    return True


def _configure_runtime_defaults() -> None:
    try:
        from clawchat_gateway.runtime_defaults import (
            configure_clawchat_allow_all,
        )

        configure_clawchat_allow_all()
    except Exception as exc:
        logger.warning("ClawChat could not configure runtime defaults: %s", exc)


def _register_skill(ctx) -> None:
    register_skill = getattr(ctx, "register_skill", None)
    if not callable(register_skill):
        return

    skill = _plugin_dir() / "skills" / "clawchat" / "SKILL.md"
    if not skill.exists():
        return

    register_skill(
        "clawchat",
        skill,
        description="ClawChat profiles, friends, moments, and media.",
    )

    liveware_skill = _plugin_dir() / "skills" / "liveware-app" / "SKILL.md"
    if liveware_skill.exists():
        register_skill(
            "liveware-app",
            liveware_skill,
            description="Expose a local web service via liveware and register it to ClawChat.",
        )


def _platform_value(platform) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").lower()


def _is_clawchat_platform(platform) -> bool:
    return _platform_value(platform) == "clawchat"


def _resolve_clawchat_bot_user_id(gateway) -> str | None:
    """Look up the ClawChat bot's own user_id from the loaded gateway config.

    Re-resolved on every hook call rather than cached at register time —
    activation rewrites this value live and we don't want to keep a stale read
    from before activation.
    """
    try:
        from gateway.config import Platform
    except Exception:
        return None
    platforms = getattr(getattr(gateway, "config", None), "platforms", None)
    if not isinstance(platforms, dict):
        return None
    platform_config = platforms.get(getattr(Platform, "CLAWCHAT", None))
    if platform_config is None:
        platform_config = platforms.get("clawchat")
    if platform_config is None:
        for platform_key, config in platforms.items():
            if _is_clawchat_platform(platform_key):
                platform_config = config
                break
    if platform_config is None:
        return None
    try:
        from clawchat_gateway.config import ClawChatConfig
        cfg = ClawChatConfig.from_platform_config(platform_config)
    except Exception as exc:
        logger.debug("clawchat self-echo: ClawChatConfig load failed: %s", exc)
        return None
    user_id = cfg.user_id or None
    return user_id if isinstance(user_id, str) and user_id else None


def _clawchat_pre_gateway_dispatch(*, event, gateway, session_store=None, **_):
    """Drop frames where the sender is the bot's own ClawChat account.

    Without this, hermes-agent's interrupt-on-new-message logic treats the
    WS-echo of the bot's own outbound chunks as fresh user input, which
    cancels the in-flight turn and produces an "Operation interrupted:
    waiting for model response" cascade (iteration 1/N restarts forever).
    """
    source = getattr(event, "source", None)
    if source is None or not _is_clawchat_platform(
        getattr(source, "platform", None)
    ):
        return None
    sender_id = getattr(source, "user_id", None)
    if not sender_id:
        return None
    bot_user_id = _resolve_clawchat_bot_user_id(gateway)
    if bot_user_id and sender_id == bot_user_id:
        logger.warning(
            "clawchat pre_gateway_dispatch skip: self-echo chat_id=%s user_id=%s",
            getattr(source, "chat_id", None),
            sender_id,
        )
        return {"action": "skip", "reason": "clawchat-self-echo"}
    return None


def _register_cli_commands(ctx) -> None:
    register_cli_command = getattr(ctx, "register_cli_command", None)
    if not callable(register_cli_command):
        return

    from clawchat_gateway.cli import handle_clawchat_cli, setup_clawchat_cli

    register_cli_command(
        "clawchat",
        "Manage ClawChat integration",
        setup_clawchat_cli,
        handler_fn=handle_clawchat_cli,
        description="Activate and manage the ClawChat Hermes gateway integration.",
    )


def _register_commands(ctx) -> None:
    register_command = getattr(ctx, "register_command", None)
    if not callable(register_command):
        return

    from clawchat_gateway.commands import (
        handle_clawchat_activate_command,
        handle_clawchat_output_command,
    )

    register_command(
        "clawchat-activate",
        handle_clawchat_activate_command,
        description="Activate ClawChat with an activation code.",
        args_hint="CODE [--restart] [--no-restart]",
    )
    register_command(
        "clawchat-output",
        handle_clawchat_output_command,
        description="Set ClawChat output visibility.",
        args_hint="minimal|normal|full",
    )


def _register_llm_context_debug_hooks(ctx) -> None:
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return

    from clawchat_gateway.llm_context_hooks import _clawchat_pre_api_request

    register_hook("pre_api_request", _clawchat_pre_api_request)


def _start_liveware_cli_download() -> None:
    """Best-effort: download the liveware CLI on a daemon thread at load time."""
    try:
        from clawchat_gateway.liveware_cli import ensure_liveware_cli_background

        ensure_liveware_cli_background()
    except Exception as exc:  # noqa: BLE001
        logger.warning("ClawChat liveware CLI ensure skipped: %s", exc)


def register(ctx) -> None:
    _register_platform(ctx)
    _configure_runtime_defaults()
    _start_liveware_cli_download()

    from clawchat_gateway.plugin_tools import register_tools

    register_tools(ctx)
    _register_skill(ctx)
    _register_cli_commands(ctx)
    _register_commands(ctx)
    _register_llm_context_debug_hooks(ctx)
    ctx.register_hook("pre_gateway_dispatch", _clawchat_pre_gateway_dispatch)
