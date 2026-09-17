"""Agent-written onboarding facts relayed on the plugin-report call.

After following the connection wiki the agent may write
``~/clawchat/onboarding.json`` (same directory as ``greeting.md``) with the id
of the field report it filed there and its self-assessed capability tier. The
plugin validates and relays; it never files a wiki report itself, and no
ClawChat id ever flows the other way.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_REPORT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CAP_KEYS = ("headless", "mcp", "permission_hook", "session_line")


def _tier(value: Any) -> int | None:
    # Mirrors the TS side's `Number.isInteger`, which has no separate float
    # type: a JSON `4.0` must validate the same as `4` on both plugins. Bool
    # is excluded first since `isinstance(True, int)` is otherwise True.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 4 else None
    if isinstance(value, float) and value.is_integer():
        as_int = int(value)
        return as_int if 1 <= as_int <= 4 else None
    return None


def read_onboarding_report(home_dir: Path | None = None) -> dict[str, Any] | None:
    # Never throws: an unresolvable home directory, an absent/unreadable
    # file, or malformed JSON all fall through to `None`, same as a file with
    # no valid fields — this must be safe to call unconditionally from a
    # best-effort report path.
    try:
        base = home_dir if home_dir is not None else Path.home()
        raw = json.loads((base / "clawchat" / "onboarding.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent or unreadable == nothing to relay
        return None
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    rid = raw.get("wiki_report_id")
    if isinstance(rid, str) and _REPORT_ID_RE.match(rid):
        out["wiki_report_id"] = rid
    for key in ("capability_tier", "capability_ceiling"):
        tier = _tier(raw.get(key))
        if tier is not None:
            out[key] = tier
    caps = raw.get("capabilities")
    if isinstance(caps, dict):
        clean = {k: caps[k] for k in _CAP_KEYS if isinstance(caps.get(k), bool)}
        if clean:
            out["capabilities"] = clean
    return out or None
