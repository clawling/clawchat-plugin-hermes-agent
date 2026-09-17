from __future__ import annotations

import logging
from dataclasses import dataclass
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
    AGENT_NOT_FOUND_CODE,
    DEFAULT_BASE_URL,
    DEFAULT_WEBSOCKET_URL,
    ClawChatApiClient,
    ClawChatApiError,
    agents_connect_with_retry,
)
from clawchat_gateway.config import _get_env
from clawchat_gateway.device_id import get_device_id, resolve_paired_device_id
from clawchat_gateway.onboarding import RECONNECT_GUIDE_URL, onboarding_context
from clawchat_gateway.output_visibility import (
    normalize_output_visibility,
    runtime_status_messages_for_visibility,
)
from clawchat_gateway.restart import schedule_gateway_restart
from clawchat_gateway.storage import _active_profile_name, get_clawchat_store

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


def _read_existing_user_id(config: dict[str, Any], *, base_url: str = "") -> str:
    """Return the stored shadow ``user_id`` to replay as a re-pair hint.

    Returns "" when the stored identity did not originate here:

    * a *different backend* (``extra.base_url`` differs from the one being
      activated against) — agent ids are per-deployment, so replaying one
      across environments can only produce ``16001 agent not found``;
    * a *different profile* (``extra.profile`` differs from the active Hermes
      profile) — ``hermes profile create --clone`` copies ``config.yaml``
      wholesale, so a brand-new profile can carry the source profile's identity
      without ever having paired. Replaying it spends the connect code
      re-pairing the SOURCE agent instead of creating this profile's own.
    """
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
    if not isinstance(user_id, str) or not user_id.strip():
        return ""
    stored_base_url = extra.get("base_url")
    if base_url and isinstance(stored_base_url, str) and stored_base_url.strip():
        if stored_base_url.strip().rstrip("/") != base_url.strip().rstrip("/"):
            logger.info(
                "clawchat activation ignoring stored user_id from a different backend "
                "(stored=%s current=%s)",
                stored_base_url,
                base_url,
            )
            return ""
    stored_profile = extra.get("profile")
    if isinstance(stored_profile, str) and stored_profile.strip():
        current_profile = _active_profile_name()
        if stored_profile.strip() != current_profile:
            logger.info(
                "clawchat activation ignoring stored user_id minted by another Hermes "
                "profile (stored=%s current=%s); pairing this profile fresh",
                stored_profile,
                current_profile,
            )
            return ""
    return user_id.strip()


def _clawchat_extra(config: dict[str, Any]) -> dict[str, Any]:
    platforms = config.get("platforms")
    if not isinstance(platforms, dict):
        return {}
    clawchat = platforms.get("clawchat")
    if not isinstance(clawchat, dict):
        return {}
    extra = clawchat.get("extra")
    return extra if isinstance(extra, dict) else {}


@dataclass(frozen=True)
class PrecheckOutcome:
    pairable: bool
    bound_agent: bool
    refusal: str


def evaluate_precheck(result: dict[str, Any] | None) -> PrecheckOutcome:
    """Turn a ``/connect/check`` response into a decision.

    ``None`` (endpoint unreachable, older backend, rate limited) means "no
    pre-check": proceed exactly as before. Only an explicit ``pairable: false``
    refuses, and the refusal never names a flag, a minute count, or a second
    URL — the owner's ClawChat app and the reconnect page are the only exits.
    """
    if not result:
        return PrecheckOutcome(pairable=True, bound_agent=False, refusal="")
    bound = result.get("bound_agent") is True
    if result.get("pairable") is True:
        return PrecheckOutcome(pairable=True, bound_agent=bound, refusal="")
    status = str(result.get("status") or "unknown")
    if result.get("user_id_status") == "owner_mismatch":
        refusal = (
            "this connect code belongs to a different ClawChat account than the identity "
            "stored in this profile. Ask the owner of THIS agent for a code, or activate as "
            "a brand-new agent with --new-account."
        )
    elif status == "paired":
        refusal = (
            "this connect code was already redeemed. If this agent lost its connection, ask "
            "your owner to send you the reconnect prompt from the ClawChat app and follow "
            f"{RECONNECT_GUIDE_URL}; otherwise ask for a fresh code."
        )
    elif status in ("expired", "invalid"):
        refusal = f"this connect code is {status}. Ask your owner for a fresh code from the ClawChat app."
    else:
        refusal = (
            f"this connect code is not pairable (status={status}). "
            "Ask your owner for a fresh code from the ClawChat app."
        )
    return PrecheckOutcome(pairable=False, bound_agent=bound, refusal=refusal)


class ExistingActivationError(RuntimeError):
    """Raised when activating would silently replace this profile's identity.

    A Hermes profile holds exactly one ClawChat identity (the plugin's
    ``account_id`` is the constant ``"default"`` in config, .env and the
    activations table alike). Replaying the stored ``user_id`` against a *new*
    connect code therefore does not add a second agent — the server treats it
    as a re-pair of the existing one, consumes the code, creates no second
    agent and no second contact entry, and the local credentials are
    overwritten in place. Refuse instead, and name the two real intents.
    """

    def __init__(self, user_id: str, agent_id: str = "") -> None:
        self.user_id = user_id
        self.agent_id = agent_id
        who = f"agent {agent_id} (shadow user {user_id})" if agent_id else f"agent {user_id}"
        super().__init__(
            f"this Hermes profile is already paired to ClawChat {who}. "
            "Redeeming another connect code here would re-bind that code to the SAME "
            "agent — no second agent is created, and this profile's credentials are "
            "overwritten.\n"
            "  - Want a NEW agent for this profile — including when the identity above "
            "was inherited from a cloned config: re-run with --new-account\n"
            "  - Want a SECOND agent alongside this one: give it its own profile:\n"
            "      hermes profile create <name>\n"
            "      hermes -p <name> plugins install clawling/clawchat-plugin-hermes-agent --enable\n"
            "      hermes -p <name> clawchat activate <CODE>\n"
            f"  - Want to RESTORE {who} (it only lost its token): do not spend a fresh "
            "code on it. Ask your owner to send you the reconnect prompt from the "
            "ClawChat app — its code is bound to this identity and usually restores it "
            "on its own; if activation still reports it as already paired, run it "
            f"again with --repair — and follow {RECONNECT_GUIDE_URL}"
        )


class UnprovenRepairError(ExistingActivationError):
    """Raised when ``--repair`` would replay an identity of unknown provenance.

    ``--repair`` is for one situation: this profile paired its own agent and
    later lost the token. It keeps ``extra.user_id`` in the replay, so the
    server re-pairs *that* agent and spends the code on it.

    An inherited identity (``hermes profile create <name> --clone``, the
    dashboard's clone, a hand-copied ``config.yaml``) is indistinguishable at
    the flag level, and a fresh install has no token by construction — so
    "lost its token" reads as a match, and the code lands on the SOURCE
    profile's agent while this profile creates none. Refuse instead, and name
    the flag that does what the caller meant.
    """

    def __init__(
        self,
        user_id: str,
        agent_id: str = "",
        *,
        profile: str = "",
        config_path: str = "",
    ) -> None:
        RuntimeError.__init__(  # noqa: PLC2801 - bypass the base guard's text
            self,
            f"--repair would redeem this code onto ClawChat "
            f"{f'agent {agent_id} (shadow user {user_id})' if agent_id else f'agent {user_id}'}"
            f", an identity this Hermes profile ({profile or 'unknown'!r}) cannot prove it "
            "owns: config.yaml carries no platforms.clawchat.extra.profile stamp for this "
            "profile, and this profile's own database records no pairing for that agent. "
            "That is what a cloned or copied config looks like, and re-pairing it binds "
            "the code to the SOURCE profile's agent — this profile still ends up with no "
            "agent of its own.\n"
            "  - To give THIS profile its own new agent: re-run with --new-account\n"
            "  - To restore that agent instead: ask its owner to send you the reconnect "
            "prompt from the ClawChat app — that code is bound to the agent, so the "
            f"server, not this config, proves ownership — and follow {RECONNECT_GUIDE_URL}"
        )
        self.user_id = user_id
        self.agent_id = agent_id
        self.profile = profile


def _identity_is_this_profiles_own(user_id: str) -> bool:
    """True when ``user_id`` is provably an identity this profile itself paired.

    Two proofs, in order:

    * ``extra.profile`` names the active profile. Activation stamps it, so any
      identity minted here since the multi-profile change carries it. (A stamp
      naming a *different* profile never reaches this check — such an identity
      is already dropped from the replay by ``_read_existing_user_id``.)
    * this profile's own SQLite row records the same ``user_id``. Pre-stamp
      installs have no stamp to check, and the database is the one piece of
      per-profile state ``hermes profile create --clone`` does not copy.

    Fails **open** when the local store cannot answer (older store object, or
    an unreadable database): a broken database must not strand an operator
    whose profile genuinely owns the agent.
    """
    _config_path, config = _load_config()
    stamped = _clawchat_extra(config).get("profile")
    if isinstance(stamped, str) and stamped.strip():
        return stamped.strip() == _active_profile_name()
    try:
        reader = getattr(get_clawchat_store(), "get_activation_credentials", None)
        if reader is None:
            return True
        credentials = reader(platform="hermes", account_id="default")
    except Exception:  # noqa: BLE001
        logger.warning(
            "clawchat activation: repair provenance check could not read the local "
            "activation row; allowing the re-pair",
            exc_info=True,
        )
        return True
    stored = str(getattr(credentials, "user_id", "") or "").strip()
    return bool(stored) and stored == user_id


def _resolve_activation_device_id(*, existing_user_id: str, new_account: bool) -> str:
    """Device id sent as ``x-device-id`` on activation's HTTP calls, and later
    persisted onto the activations row for that connect.

    A fresh pairing — no identity to replay at all, or an explicit
    ``--new-account`` — gets the new agent-scoped ``get_device_id()`` id: this
    IS the "brand-new activation" rule 5 talks about. Reusing an EXISTING
    identity (``existing_user_id`` truthy: covers ``--repair``, the
    server-confirmed bound-agent auto-repair, and even the about-to-be-refused
    ``ExistingActivationError`` / ``UnprovenRepairError`` paths, since all of
    them replay that user_id in the very ``/connect/check`` call this device id
    heads) must keep presenting whatever id that identity paired with — via
    the same row → token ``did`` → legacy-host-id resolution
    ``connection.py::_resolve_device_id`` uses — or the backend's redeem
    safety gate (``paired_device_id``, keyed on device id alone) sees a
    different device, and a successful repair anyway orphans the backend's
    per-device delivery cursor for the id it used to present.
    """
    if not existing_user_id or new_account:
        return get_device_id()
    try:
        credentials = get_clawchat_store().get_activation_credentials(
            platform="hermes", account_id="default"
        )
    except Exception:  # noqa: BLE001
        credentials = None
    stored = getattr(credentials, "device_id", None) if credentials else None
    token = _get_env("CLAWCHAT_TOKEN")
    return resolve_paired_device_id(stored=stored, token=token) or get_device_id()


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
    # Stamp the minting profile so a later `hermes profile create --clone` of
    # this config is recognisable as someone else's identity rather than
    # replayed as this profile's own.
    extra["profile"] = _active_profile_name()
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


def _mask_token(token: str) -> str:
    """Mask a token for logging — never emit the raw secret."""
    if not token:
        return "<empty>"
    return "***" if len(token) <= 4 else f"***{token[-4:]}"


def migrate_legacy_config_tokens() -> dict[str, Any]:
    """Migrate legacy ``extra.token`` / ``extra.refresh_token`` out of config.yaml.

    Old ClawChat plugin configs stored the auth token under
    ``platforms.clawchat.extra.token`` (and ``extra.refresh_token``). The current
    plugin reads tokens ONLY from env / ``.env`` / SQLite, and activation
    deliberately strips them from config.yaml — so a user upgrading from an old
    config would silently never connect.

    This one-shot, idempotent, fail-open migration:

    * copies a non-empty ``extra.token`` to ``CLAWCHAT_TOKEN`` in ``.env`` only
      when the env does not already provide one (env wins — never overwrite);
    * does the same for ``extra.refresh_token`` → ``CLAWCHAT_REFRESH_TOKEN``
      (only when non-empty);
    * always strips ``token`` / ``refresh_token`` from the config's ``extra``;
    * makes NO writes when there is nothing to migrate.

    Returns a small summary dict and never raises (a failure must not crash
    plugin registration).
    """
    summary = {"migrated_token": False, "migrated_refresh": False, "stripped": False}
    try:
        config_path, config = _load_config()
    except Exception:  # noqa: BLE001
        logger.warning("clawchat legacy-token migration: failed to load config", exc_info=True)
        return summary

    try:
        platforms = config.get("platforms") if isinstance(config, dict) else None
        if not isinstance(platforms, dict):
            return summary
        clawchat = platforms.get("clawchat")
        if not isinstance(clawchat, dict):
            return summary
        extra = clawchat.get("extra")
        if not isinstance(extra, dict):
            return summary

        raw_token = extra.get("token")
        raw_refresh = extra.get("refresh_token")
        token = raw_token.strip() if isinstance(raw_token, str) else ""
        refresh = raw_refresh.strip() if isinstance(raw_refresh, str) else ""

        has_token_key = "token" in extra
        has_refresh_key = "refresh_token" in extra
        if not token and not refresh and not has_token_key and not has_refresh_key:
            return summary

        env_values: dict[str, str | None] = {}
        if token:
            if _get_env("CLAWCHAT_TOKEN"):
                logger.info(
                    "clawchat legacy-token migration: env CLAWCHAT_TOKEN already set, "
                    "stripping config token without overwrite"
                )
            else:
                env_values["CLAWCHAT_TOKEN"] = token
                summary["migrated_token"] = True
        if refresh:
            if _get_env("CLAWCHAT_REFRESH_TOKEN"):
                logger.info(
                    "clawchat legacy-token migration: env CLAWCHAT_REFRESH_TOKEN already "
                    "set, stripping config refresh token without overwrite"
                )
            else:
                env_values["CLAWCHAT_REFRESH_TOKEN"] = refresh
                summary["migrated_refresh"] = True

        # Nothing actually present to strip (keys absent) → no-op.
        if not has_token_key and not has_refresh_key:
            return summary

        extra.pop("token", None)
        extra.pop("refresh_token", None)
        summary["stripped"] = True

        if env_values:
            _write_env_values(env_values)
        _write_config(config_path, config)

        logger.info(
            "clawchat legacy-token migration: stripped config tokens "
            "(token=%s migrated=%s, refresh=%s migrated=%s)",
            _mask_token(token),
            summary["migrated_token"],
            _mask_token(refresh),
            summary["migrated_refresh"],
        )
    except Exception:  # noqa: BLE001
        logger.warning("clawchat legacy-token migration failed", exc_info=True)
    return summary


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


async def activate(
    code: str,
    *,
    base_url: str,
    new_account: bool = False,
    repair: bool = False,
) -> dict[str, Any]:
    config_path, config = _load_config()
    existing_user_id = _read_existing_user_id(config, base_url=base_url)
    device_id = _resolve_activation_device_id(
        existing_user_id=existing_user_id, new_account=new_account
    )
    client = ClawChatApiClient(
        base_url=base_url.rstrip("/"),
        token="",
        user_id="",
        device_id=device_id,
        timeout=ACTIVATION_TIMEOUT_SECONDS,
    )
    context = onboarding_context()
    try:
        raw = await client.agents_connect_check(
            code=code, user_id=existing_user_id or None, context=context
        )
    except Exception as exc:  # noqa: BLE001 — pre-check is telemetry + courtesy, never a gate
        logger.info("clawchat activation pre-check unavailable (%s); continuing", type(exc).__name__)
        raw = None
    precheck = evaluate_precheck(raw)
    if not precheck.pairable:
        raise ClawChatApiError("validation", precheck.refusal)
    # A bound code is the owner's reconnect prompt: it can only restore the
    # incumbent identity, so it settles the new-vs-restore question and needs
    # no local provenance proof — the server enforces the binding.
    if precheck.bound_agent and existing_user_id:
        repair = True
    # Deliberately NOT gated on "does a live token exist". A config written
    # before `extra.profile` existed is indistinguishable from a clone, and a
    # clone's inherited token is routinely stale — so "identity present, token
    # absent" cannot be read as "this profile auto-logged out". Anything that
    # survives _read_existing_user_id is this profile's own identity, and
    # spending a single-use code on it must be an explicit choice.
    if existing_user_id and not new_account and not repair:
        extra = _clawchat_extra(config)
        agent_id = extra.get("agent_id")
        raise ExistingActivationError(
            existing_user_id,
            agent_id.strip() if isinstance(agent_id, str) else "",
        )
    # --repair keeps the replay, so it is only safe on an identity this profile
    # can prove it paired. An inherited one re-pairs the SOURCE agent and
    # leaves this profile with none — the failure the flag looks most like.
    if (
        existing_user_id
        and repair
        and not precheck.bound_agent
        and not _identity_is_this_profiles_own(existing_user_id)
    ):
        extra = _clawchat_extra(config)
        agent_id = extra.get("agent_id")
        raise UnprovenRepairError(
            existing_user_id,
            agent_id.strip() if isinstance(agent_id, str) else "",
            profile=_active_profile_name(),
            config_path=str(config_path or ""),
        )
    if new_account:
        # A brand-new identity is precisely "do not replay" — the replay is the
        # only thing that would bind this code to the incumbent agent.
        existing_user_id = ""
    try:
        result = await agents_connect_with_retry(
            client, code=code, user_id=existing_user_id or None, context=context
        )
    except ClawChatApiError as exc:
        # AGENT_NOT_FOUND means the replayed user_id has no agent row on this
        # backend — stale local state (the owner deleted their account, or the
        # config outlived its deployment). The connect code itself is untouched
        # and still pending in that case, so shed the id and pair fresh. Any
        # other failure (including a genuine owner mismatch) propagates: only a
        # provably meaningless user_id is worth a second attempt at a
        # single-use code.
        if not existing_user_id or exc.code != AGENT_NOT_FOUND_CODE:
            raise
        logger.warning(
            "clawchat activation: stored user_id %s no longer exists on %s; "
            "retrying as a fresh pairing",
            existing_user_id,
            base_url,
        )
        # The replayed identity doesn't exist on this backend: this IS a
        # brand-new agent (rule 5), so it must get its own per-profile
        # get_device_id() id — never the stale identity's resolved
        # legacy/stored id the client above was built with, which could
        # otherwise collide with another agent on this host (e.g. the default
        # profile's agent, if that one paired under the legacy host id).
        device_id = get_device_id()
        client = ClawChatApiClient(
            base_url=base_url.rstrip("/"),
            token="",
            user_id="",
            device_id=device_id,
            timeout=ACTIVATION_TIMEOUT_SECONDS,
        )
        result = await agents_connect_with_retry(client, code=code, user_id=None, context=context)
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
            # `device_id` is the same value the client above sent as
            # `x-device-id` — the resolved legacy/stored id on a repair, the new
            # per-profile id only for a genuinely fresh pairing (rule 5).
            device_id=device_id,
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
    new_account: bool = False,
    repair: bool = False,
) -> dict[str, Any]:
    payload = await activate(
        code.strip(), base_url=base_url, new_account=new_account, repair=repair
    )
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
