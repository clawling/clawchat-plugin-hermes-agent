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
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 4 else None


def read_onboarding_report(home_dir: Path | None = None) -> dict[str, Any] | None:
    base = home_dir if home_dir is not None else Path.home()
    try:
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
