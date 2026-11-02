"""Read-only compression diagnostics; model-dependent ratio points stay with Hermes."""

from __future__ import annotations

from collections.abc import Mapping

from clawchat_gateway.config import SESSION_INT_KEYS, _read_clamped_int
from clawchat_gateway.host_version import parse_version


def host_version_display(version: str | None) -> str:
    try:
        parsed = parse_version(version)
        if parsed is not None and parsed != (0, 0, 0):
            return version.strip().removeprefix("v")
    except Exception:  # noqa: BLE001 — malformed version metadata is diagnostic only
        pass
    return "unknown (local build)"


def read_host_config() -> dict:
    """Read the raw profile config without merging defaults or writing it."""
    try:
        from hermes_cli.config import read_raw_config

        config = read_raw_config()
        return config if isinstance(config, dict) else {}
    except Exception:  # noqa: BLE001 — status stays available without Hermes config
        return {}


def _plugin_int(extra: Mapping, key: str) -> int:
    _field, default, minimum, maximum = SESSION_INT_KEYS[key]
    try:
        return _read_clamped_int(extra.get(key, default), default, minimum, maximum)
    except Exception:  # noqa: BLE001 — includes non-finite numeric configuration
        return default


def _host_cap(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None


def describe_compression(
    host_config: Mapping | None,
    extra: Mapping | None,
    *,
    host_version: str | None,
) -> dict:
    """Describe raw settings without changing them or predicting host-version behavior."""
    host_config = host_config if isinstance(host_config, Mapping) else {}
    extra = extra if isinstance(extra, Mapping) else {}
    compression = host_config.get("compression")
    compression = compression if isinstance(compression, Mapping) else {}
    enabled = compression.get("enabled") is not False
    plugin_cap = _plugin_int(extra, "session-cap-tokens")
    plugin_cap_set = "session-cap-tokens" in extra
    host_cap = _host_cap(compression.get("threshold_tokens"))
    if host_cap is not None:
        source, threshold = "host", host_cap
    else:
        source = "plugin_fallback" if "threshold_tokens" in compression else "plugin_fill"
        threshold = plugin_cap
    ratio = compression.get("threshold")
    ratio = (
        float(ratio)
        if not isinstance(ratio, bool) and isinstance(ratio, (int, float)) and 0 < ratio <= 1
        else None
    )
    margin = _plugin_int(extra, "sediment-margin-tokens")
    notes = []
    if source == "host" and plugin_cap_set and host_cap != plugin_cap:
        notes.append(
            f"Host compression.threshold_tokens ({host_cap}) overrides "
            f"platforms.clawchat.extra.session-cap-tokens ({plugin_cap}). "
            "The host key is global: it applies to every Hermes platform, not just ClawChat."
        )
    if source == "plugin_fallback":
        notes.append(
            "Host compression.threshold_tokens is invalid; the plugin uses its own "
            "session-cap-tokens to calculate the sediment point, while the host's own "
            "compression behavior depends on Hermes."
        )
    if not enabled:
        notes.append(
            "Host context compression is disabled (compression.enabled: false); "
            "the compression point is not used."
        )
    return {
        "host_version": host_version_display(host_version),
        "host_compression_enabled": enabled,
        "threshold_tokens": threshold,
        "threshold_tokens_source": source,
        "host_threshold_tokens": host_cap,
        "plugin_session_cap_tokens": plugin_cap,
        "plugin_session_cap_set": plugin_cap_set,
        "host_threshold_ratio": ratio,
        "ratio_applies": (
            "Hermes compresses at the lower of threshold_tokens and the ratio threshold "
            "of the active model's window (default ratio 0.5, at least 0.75 below a 512K "
            "window); the ratio point depends on the model and is not computed here."
        ),
        "sediment_margin_tokens": margin,
        "sediment_point_tokens": max(1, threshold - margin),
        "notes": notes,
    }
