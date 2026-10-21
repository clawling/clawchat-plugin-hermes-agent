"""Default path resolution must never reach a developer's Hermes installation."""

import os
from pathlib import Path

import pytest

from clawchat_gateway.hermes_home import hermes_home, platform_default_hermes_home
from clawchat_gateway.profile_collision import hermes_root


def test_default_homes_are_isolated(_isolate_hermes_home, original_os_home, tmp_path):
    home = _isolate_hermes_home
    assert hermes_home() == home
    assert hermes_root() == home
    assert Path.home() == home.parent
    assert Path(os.environ["USERPROFILE"]) == home.parent
    assert Path(os.environ["LOCALAPPDATA"]).is_relative_to(home.parent)
    for path in (hermes_home(), hermes_root(), Path.home(), platform_default_hermes_home()):
        assert path.is_relative_to(home.parent)
        assert not path.resolve().is_relative_to(original_os_home / ".hermes")
    assert not any(name.startswith("CLAWCHAT_") for name in os.environ)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("local_appdata", [True, False])
def test_unset_hermes_home_uses_fake_platform_default(
    monkeypatch, _isolate_hermes_home, original_os_home, platform, local_appdata,
):
    import clawchat_gateway.hermes_home as home_module

    monkeypatch.delenv("HERMES_HOME")
    monkeypatch.setattr(home_module.sys, "platform", platform)
    if not local_appdata:
        monkeypatch.delenv("LOCALAPPDATA")
    os_home = _isolate_hermes_home.parent
    expected = (
        os_home / "AppData" / "Local" / "hermes"
        if platform == "win32" else os_home / ".hermes"
    )
    assert Path.home() == os_home
    assert hermes_home() == platform_default_hermes_home() == hermes_root() == expected
    assert not expected.resolve().is_relative_to(original_os_home / ".hermes")


def test_test_can_override_isolated_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert hermes_home() == hermes_root() == tmp_path

