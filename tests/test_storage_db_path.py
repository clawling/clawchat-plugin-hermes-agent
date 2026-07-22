from __future__ import annotations

import clawchat_gateway.storage as storage


def test_default_profile_db_lives_in_clawchat_dir(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert storage.clawchat_data_dir() == home / "clawchat"
    assert storage.default_db_path() == home / "clawchat" / "clawchat.sqlite"


def test_named_profile_db_uses_profile_suffix(monkeypatch, tmp_path):
    # A named profile's HERMES_HOME is ~/.hermes/profiles/<name>. With hermes_cli
    # absent in the test venv, the profile name is derived from this layout.
    home = tmp_path / ".hermes" / "profiles" / "coder"
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert storage.default_db_path() == home / "clawchat" / "clawchat-coder.sqlite"


def test_plain_hermes_home_is_default_profile(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert storage._active_profile_name() == "default"


def test_db_filename_default_is_unsuffixed():
    assert storage._db_filename("default") == "clawchat.sqlite"


def test_db_filename_named_profile():
    assert storage._db_filename("coder") == "clawchat-coder.sqlite"


def test_profile_name_sanitized_for_filesystem():
    assert storage._db_filename("we ird/../x") == "clawchat-we-ird-x.sqlite"
    assert storage._sanitize_profile("///") == "default"
