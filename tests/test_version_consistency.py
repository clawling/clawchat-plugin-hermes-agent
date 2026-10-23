"""plugin.yaml's manifest version must match clawchat_gateway.__version__.

``__version__`` is the single source of truth (pyproject.toml derives the wheel
version from it and the adapter reports it to the backend); Hermes reads the
manifest's ``version``. A release that bumps only one of them would ship a
plugin that describes itself two different ways.
"""

from pathlib import Path

import yaml

import clawchat_gateway

ROOT = Path(__file__).resolve().parent.parent


def test_plugin_manifest_version_matches_package_version():
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert str(manifest["version"]) == clawchat_gateway.__version__


def test_pyproject_derives_its_version_from_the_package():
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = { attr = "clawchat_gateway.__version__" }' in pyproject
