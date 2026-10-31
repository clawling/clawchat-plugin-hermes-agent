"""Hermes compatibility hints and shared real-source lookup diagnostics."""

import importlib.util
import json
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hermes_source
from clawchat_gateway import host_version


@pytest.mark.parametrize(("text", "expected"), [
    ("0.20.4", (0, 20, 4)),
    ("v0.20.5", (0, 20, 5)),
    ("0.20.4+canary.x", (0, 20, 4)),
    ("0.20.4rc1", (0, 20, 4)),
    ("0.22.5+canary.x", (0, 22, 5)),
    (" 0.21.0 ", (0, 21, 0)),
    ("", None),
    ("garbage", None),
    ("0.20", None),
])
def test_parse_version(text, expected):
    assert host_version.parse_version(text) == expected


@pytest.mark.parametrize("version", [
    "0.20.3", "0.20.4", "0.20.6", "v0.20.5", "0.20.4+canary.x", "0.20.9",
])
def test_unsupported_version_message(version):
    message = host_version.unsupported_host_message(version)
    assert message == (
        f"Hermes Agent v{version.removeprefix('v')} is not supported by the ClawChat plugin: "
        "Hermes v0.20.3–v0.20.6 block this plugin at install time, so it cannot "
        "be installed or updated there. Upgrade Hermes (hermes update) to "
        "v0.21.6 or newer."
    )


@pytest.mark.parametrize("version", ["0.20.2", "0.21.0", "0.12.0", "", "garbage"])
def test_other_versions_have_no_hint(version):
    assert host_version.unsupported_host_message(version) is None


def test_version_hint_survives_resolution_failure(monkeypatch):
    def broken():
        raise RuntimeError("version unavailable")

    monkeypatch.setattr(host_version, "resolve_host_agent_version", broken)
    assert host_version.unsupported_host_message() is None


@pytest.fixture
def plugin(monkeypatch):
    entry = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_version_entry_test", entry)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    for name in (
        "_migrate_legacy_config_tokens", "_ensure_host_defaults", "_warn_on_shared_identity",
        "_patch_send_message_target_parser", "_patch_send_message_media_delivery",
    ):
        monkeypatch.setattr(module, name, lambda: None)
    return module


def test_registration_warns_and_succeeds(plugin, monkeypatch, caplog):
    monkeypatch.setattr(host_version, "resolve_host_agent_version", lambda: "0.20.4")
    ctx = SimpleNamespace(register_platform=Mock())
    with caplog.at_level(logging.WARNING, logger=plugin.__name__):
        assert plugin._register_platform(ctx) is True
    ctx.register_platform.assert_called_once()
    assert any(
        record.levelno == logging.WARNING
        and record.getMessage() == host_version.unsupported_host_message("0.20.4")
        for record in caplog.records
    )


def test_registration_survives_hint_failure(plugin, monkeypatch, caplog):
    def broken():
        raise RuntimeError("hint unavailable")

    monkeypatch.setattr(host_version, "unsupported_host_message", broken)
    ctx = SimpleNamespace(register_platform=Mock())
    with caplog.at_level(logging.DEBUG, logger=plugin.__name__):
        assert plugin._register_platform(ctx) is True
    ctx.register_platform.assert_called_once()
    assert "ClawChat Hermes version check skipped: hint unavailable" in caplog.text


@pytest.mark.parametrize("version", ["0.20.4", "0.21.0"])
def test_status_hint_preserves_stdout_json(version, monkeypatch, capsys):
    from clawchat_cli import main
    from clawchat_gateway import cli, install_identity

    payload = {"install_id": None, "is_superseded": False, "instance_lock": {"state": "unknown"}}
    monkeypatch.setattr(host_version, "resolve_host_agent_version", lambda: version)
    monkeypatch.setattr(cli, "_status_user_id", lambda: None)
    monkeypatch.setattr(install_identity, "install_status", lambda *_args: payload)

    assert main(["status"]) == 0
    captured = capsys.readouterr()
    assert captured.out == json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    message = host_version.unsupported_host_message(version)
    assert captured.err == (message + "\n" if message else "")


def test_source_candidates_order(monkeypatch, tmp_path):
    repo, main, override = (tmp_path / name for name in ("repo", "main", "override"))
    monkeypatch.setattr(hermes_source, "REPO_ROOT", repo)
    monkeypatch.setattr(hermes_source, "main_checkout", lambda: main)
    monkeypatch.setenv("HERMES_SOURCE_DIR", str(override))
    assert hermes_source.candidates() == [
        override, repo / "tmp/hermes", repo / "tmp/hermes-agent",
        main / "tmp/hermes", main / "tmp/hermes-agent",
    ]
    monkeypatch.delenv("HERMES_SOURCE_DIR")
    monkeypatch.setattr(hermes_source, "main_checkout", lambda: repo)
    assert hermes_source.candidates() == [repo / "tmp/hermes", repo / "tmp/hermes-agent"]


def test_source_lookup_uses_first_file_marker(monkeypatch, tmp_path):
    paths = [tmp_path / name for name in ("directory", "first", "second")]
    marker = "gateway/platforms/base.py"
    for path in paths:
        (path / marker).parent.mkdir(parents=True)
    (paths[0] / marker).mkdir()
    for path in paths[1:]:
        (path / marker).touch()
    monkeypatch.setattr(hermes_source, "candidates", lambda: paths)
    assert hermes_source.find_hermes_source(marker) == paths[1].resolve()
    assert hermes_source.require_hermes_source(marker) == paths[1].resolve()


@pytest.mark.parametrize("required", [False, True])
def test_missing_source_diagnostic(monkeypatch, tmp_path, required):
    paths = [tmp_path / "override", tmp_path / "fallback"]
    marker = "gateway/platforms/base.py"
    monkeypatch.setattr(hermes_source, "candidates", lambda: paths)
    monkeypatch.setenv("HERMES_SOURCE_DIR", str(paths[0]))
    monkeypatch.setenv("HERMES_SOURCE_REQUIRED", "1" if required else "0")
    outcome = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(outcome) as caught:
        hermes_source.require_hermes_source(marker)
    reason = str(caught.value)
    assert marker in reason
    assert all(str(path) in reason for path in paths)
    assert "HERMES_SOURCE_DIR was set" in reason
    assert "but lacks" in reason
