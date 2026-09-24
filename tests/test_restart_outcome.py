"""Post-activation gateway restart: pre-check, cwd, and captured outcome.

The restart used to be fire-and-forget (output discarded, result never checked)
while the CLI printed "restart scheduled" regardless. These tests pin:

* no gateway has run for the profile -> nothing is spawned and the operator is
  told to install + start one;
* a named profile under a multiplexing default gateway -> nothing is spawned
  and the operator is told to restart the shared gateway;
* a scheduled restart runs with cwd = HERMES_HOME (never the plugin folder) and
  writes the command's output plus an ``exit_code=`` line to the restart log.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from clawchat_gateway import activate as activate_mod
from clawchat_gateway import cli as cli_mod
from clawchat_gateway import restart as restart_mod


@pytest.fixture
def profile_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "hermes-root"
    home = root / "profiles" / "coder"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)
    return home


@pytest.fixture
def fake_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _activate(code: str, **_kwargs: Any) -> dict[str, Any]:
        return {"user_id": "usr_test", "restart_required": True}

    monkeypatch.setattr(activate_mod, "activate", _activate)


@pytest.fixture
def forbid_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("no restart must be spawned")

    monkeypatch.setattr(restart_mod.subprocess, "Popen", _boom)


def _run_cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr(cli_mod, "activate_and_maybe_restart", None)
    parser = __import__("argparse").ArgumentParser(prog="hermes clawchat")
    cli_mod.setup_clawchat_cli(parser)
    args = parser.parse_args(["activate", "CODE"])
    assert cli_mod.handle_clawchat_cli(args) == 0
    return capsys.readouterr().out


def test_no_gateway_prints_install_and_start_instead_of_restarting(
    profile_home: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _run_cli(monkeypatch, capsys)

    assert "activation complete for usr_test" in out
    assert "hermes -p coder gateway install" in out
    assert "hermes -p coder gateway start" in out
    assert "restart requested" not in out
    assert "scheduled" not in out


def test_multiplexed_named_profile_points_at_shared_gateway(
    profile_home: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = profile_home.parent.parent
    (root / "config.yaml").write_text("gateway:\n  multiplex_profiles: true\n", encoding="utf-8")
    # Even with a gateway.pid present, a multiplexed profile is not restarted here.
    (profile_home / "gateway.pid").write_text("{}", encoding="utf-8")

    out = _run_cli(monkeypatch, capsys)

    assert "hermes -p default gateway restart" in out
    assert "every profile" in out
    assert "restart requested" not in out


def test_multiplex_allowlist_excluding_profile_is_not_multiplexed(
    profile_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = profile_home.parent.parent
    (root / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n  multiplex_profile_allowlist: [other]\n",
        encoding="utf-8",
    )
    (profile_home / "gateway.pid").write_text("{}", encoding="utf-8")

    assert restart_mod.plan_gateway_restart().action == "schedule"


def _write_fake_hermes(path: Path, exit_code: int) -> Path:
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "print('fake hermes', ' '.join(sys.argv[1:]))\n"
        "print('cwd=' + os.getcwd())\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.skipif(sys.platform == "win32", reason="fake hermes uses a POSIX shebang")
def test_scheduled_restart_sets_cwd_and_writes_outcome_log(
    profile_home: Path,
    tmp_path: Path,
    fake_activate: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (profile_home / "gateway.pid").write_text("{}", encoding="utf-8")
    fake_bin = _write_fake_hermes(tmp_path / "hermes", exit_code=3)
    monkeypatch.setattr(restart_mod, "_hermes_binary", lambda _dir: fake_bin)
    monkeypatch.chdir(tmp_path)  # the caller's cwd must NOT leak into the spawn

    real_popen = subprocess.Popen
    spawned: list[dict[str, Any]] = []

    def _popen(argv: list[str], **kwargs: Any) -> Any:
        spawned.append(kwargs)
        # Run the detached launcher to completion so its log can be inspected.
        kwargs["env"] = {**kwargs["env"], "CLAWCHAT_GATEWAY_RESTART_DELAY": "0"}
        proc = real_popen(argv, **kwargs)
        proc.wait(timeout=30)
        return proc

    monkeypatch.setattr(restart_mod.subprocess, "Popen", _popen)

    out = _run_cli(monkeypatch, capsys)

    assert len(spawned) == 1
    assert spawned[0]["cwd"] == str(profile_home)
    log_path = profile_home / "clawchat" / restart_mod.RESTART_LOG_FILENAME
    log = log_path.read_text(encoding="utf-8")
    assert "gateway restart requested" in log
    assert "fake hermes gateway restart" in log
    assert f"cwd={profile_home}" in log
    assert "exit_code=3" in log

    assert "restart requested" in out
    assert str(log_path) in out
    assert restart_mod.READY_LOG_LINE in out
    assert "scheduled in" not in out


def test_launcher_records_spawn_failure(
    profile_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart_mod, "_hermes_binary", lambda _dir: tmp_path / "missing-hermes")
    real_popen = subprocess.Popen

    def _popen(argv: list[str], **kwargs: Any) -> Any:
        kwargs["env"] = {**kwargs["env"], "CLAWCHAT_GATEWAY_RESTART_DELAY": "0"}
        proc = real_popen(argv, **kwargs)
        proc.wait(timeout=30)
        return proc

    monkeypatch.setattr(restart_mod.subprocess, "Popen", _popen)

    restart_mod.schedule_gateway_restart(delay_seconds=0)

    log = restart_mod.restart_log_path().read_text(encoding="utf-8")
    assert "could not start" in log
    assert "exit_code=127" in log
