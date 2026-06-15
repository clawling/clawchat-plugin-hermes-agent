from __future__ import annotations

from pathlib import Path

import yaml

import clawchat_gateway

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _plugin_yaml_version() -> str:
    data = yaml.safe_load((_REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    return str(data["version"])


def test_plugin_yaml_version_matches_package_version() -> None:
    # clawchat_gateway.__version__ is the single source of truth (pyproject derives
    # its build version from it, and the adapter reports it to member-backend). The
    # Hermes manifest in plugin.yaml is the one remaining hand-maintained copy, so
    # guard it here: a release that bumps one but not the other fails CI instead of
    # silently reporting a stale version (the 0.14.0-21 vs 0.14.0-24 drift bug).
    assert _plugin_yaml_version() == clawchat_gateway.__version__
