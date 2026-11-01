"""Post-activation gateway restart: pre-check, cwd, and captured outcome.

The restart used to be fire-and-forget (output discarded, result never checked)
while the CLI printed "restart scheduled" regardless. These tests pin:

* no gateway has run for the default profile -> nothing is spawned and the
  operator is told to install + start one;
* no gateway has run for a named profile -> nothing is spawned and the operator
  is pointed at the default profile's multiplexed gateway first, with a
  separate per-profile gateway only as the fallback;
* a named profile under a multiplexing default gateway -> nothing is spawned
  and the operator is told to restart the shared gateway;
* a scheduled restart runs with cwd = HERMES_HOME (never the plugin folder) and
  writes the command's output plus an ``exit_code=`` line to the restart log.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
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


@pytest.mark.parametrize("restart", [True, False], ids=["restart", "no-restart"])
@pytest.mark.parametrize(
    ("action", "profile", "owner_command"),
    [
        ("schedule", "default", "hermes gateway restart"),
        ("schedule", "coder", "hermes -p coder gateway restart"),
        ("multiplexed", "coder", "hermes -p default gateway restart"),
        ("no_gateway", "default", None),
        ("no_gateway", "coder", None),
    ],
)
async def test_activation_restart_payload_and_owner_lines(
    profile_home: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
    restart: bool,
    action: str,
    profile: str,
    owner_command: str | None,
) -> None:
    plan = restart_mod.RestartPlan(action, profile, profile_home)
    planned: list[bool] = []
    scheduled: list[int] = []
    log_path = profile_home / "clawchat" / restart_mod.RESTART_LOG_FILENAME

    def _plan() -> restart_mod.RestartPlan:
        planned.append(True)
        return plan

    def _schedule(*, delay_seconds: int) -> str:
        scheduled.append(delay_seconds)
        return "fake detached restart"

    monkeypatch.setattr(activate_mod, "plan_gateway_restart", _plan)
    monkeypatch.setattr(activate_mod, "schedule_gateway_restart", _schedule)
    monkeypatch.setattr(activate_mod, "restart_log_path", lambda: log_path)

    payload = await activate_mod.activate_and_maybe_restart(
        " CODE ", base_url="https://example.com", restart=restart, restart_delay_seconds=5
    )

    assert planned == [True]
    assert payload["ok"] is True
    assert payload["user_id"] == "usr_test"
    assert payload["restart_required"] is True
    assert payload["gateway_restart_required"] is (action == "schedule")
    if owner_command is None:
        assert "owner_restart_command" not in payload
    else:
        assert payload["owner_restart_command"] == owner_command
    assert scheduled == ([5] if restart and action == "schedule" else [])
    if not restart:
        assert "restart_scheduled" not in payload
        assert "restart_next_steps" not in payload
        assert "restart_message" not in payload
    elif action == "schedule":
        assert payload["restart_scheduled"] is True
        assert payload["restart_delay_seconds"] == 5
        assert payload["restart_command"] == "fake detached restart"
        assert payload["restart_log"] == str(log_path)
        assert payload["restart_multiplex_owner"] is False
    else:
        assert payload["restart_scheduled"] is False
        assert payload["restart_skipped_reason"] == action
        assert payload["restart_next_steps"] == restart_mod.restart_next_steps(plan)

    lines = restart_mod.format_restart_lines(payload)
    if action != "schedule":
        assert restart_mod.owner_restart_lines(payload) == []
        assert not any("message for the owner" in line for line in lines)
        assert not any("reloading plugins does not start" in line for line in lines)
        assert not any("after your current reply ends" in line for line in lines)
        return
    expected = [
        "clawchat: reloading plugins does not start the ClawChat connection; unless "
        "the gateway already had ClawChat enabled when it started, it connects only "
        "after the Hermes gateway restarts.",
        f"clawchat: first check the Hermes log for a '{restart_mod.READY_LOG_LINE}' "
        "line after this activation. The background restarter waits up to 30 minutes "
        "for idle. If still offline, ask the owner - after your current reply ends "
        "- to run this in a terminal "
        f"outside Hermes: {owner_command}",
        "clawchat: message for the owner if a restart is needed (en): ClawChat is activated. Once my reply "
        f"ends, please run `{owner_command}` in a terminal (not in this chat). "
        "After the restart I will greet you in ClawChat.",
        "clawchat: message for the owner if a restart is needed (zh): ClawChat 已激活。请在我这条回复结束后，"
        f"在终端里(不是在这个对话里)执行 `{owner_command}`,重启后我会在 ClawChat 里跟你打招呼。",
    ]
    assert restart_mod.owner_restart_lines(payload) == expected
    assert lines[-4:] == expected


async def test_no_restart_plan_failure_still_reports_activation(
    profile_home: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom() -> restart_mod.RestartPlan:
        raise RuntimeError("profile detection broke")

    monkeypatch.setattr(activate_mod, "plan_gateway_restart", _boom)

    payload = await activate_mod.activate_and_maybe_restart(
        "CODE", base_url="https://example.com", restart=False
    )

    assert payload["ok"] is True
    assert payload["gateway_restart_required"] is True
    assert payload["owner_restart_command"].endswith("gateway restart")
    assert "restart_scheduled" not in payload


def test_no_gateway_named_profile_points_at_multiplexing_not_own_gateway(
    profile_home: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = _run_cli(monkeypatch, capsys)

    assert "activation complete for usr_test" in out
    assert "gateway.multiplex_profiles: true" in out
    assert "multiplex_profile_allowlist" in out
    assert "hermes -p default gateway restart" in out
    # A per-profile gateway stays available, but only as the fallback after multiplexing.
    assert "hermes -p coder gateway install" in out
    assert out.index("multiplex_profiles") < out.index("hermes -p coder gateway install")
    assert "restart requested" not in out
    assert "scheduled" not in out


def test_no_gateway_default_profile_prints_install_and_start(
    tmp_path: Path,
    fake_activate: None,
    forbid_spawn: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("GATEWAY_MULTIPLEX_PROFILES", raising=False)

    out = _run_cli(monkeypatch, capsys)

    assert "hermes gateway install" in out
    assert "hermes gateway start" in out
    assert "restart requested" not in out


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
        kwargs["env"] = {
            **kwargs["env"], "CLAWCHAT_GATEWAY_RESTART_DELAY": "0",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_INITIAL": "0.01",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_MAX": "0.02",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_TIMEOUT": "0.2",
        }
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
        kwargs["env"] = {
            **kwargs["env"], "CLAWCHAT_GATEWAY_RESTART_DELAY": "0",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_INITIAL": "0.01",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_MAX": "0.02",
            "CLAWCHAT_GATEWAY_RESTART_RETRY_TIMEOUT": "0.2",
        }
        proc = real_popen(argv, **kwargs)
        proc.wait(timeout=30)
        return proc

    monkeypatch.setattr(restart_mod.subprocess, "Popen", _popen)

    restart_mod.schedule_gateway_restart(delay_seconds=0)

    log = restart_mod.restart_log_path().read_text(encoding="utf-8")
    assert "could not start" in log
    assert "exit_code=127" in log


@pytest.mark.parametrize('change,expected_calls,reason', [
    ('token-after-failure', 1, 'superseded by a newer restart request; stopping'),
    ('missing-token', 1, 'superseded by a newer restart request; stopping'),
    ('unreadable-token', 1, 'superseded by a newer restart request; stopping'),
    ('token-before-first', 0, 'superseded by a newer restart request; stopping'),
    ('token-during-identity-read', 0, 'superseded by a newer restart request; stopping'),
    ('pid', 1, 'gateway already restarted; stopping'),
    ('start-time', 1, 'gateway already restarted; stopping'),
    ('started-after-request', 1, 'gateway already restarted; stopping'),
    ('unchanged', 3, None),
    ('unknown-identity', 3, None),
])
def test_launcher_stops_obsolete_waiters(
    profile_home, monkeypatch, capsys, change, expected_calls, reason,
):
    pid, start, created_at = [None if change == 'started-after-request' else 11001], [100], [1.0]
    reads = []
    def live_pid(home):
        assert home == profile_home
        reads.append(home)
        if change == 'token-during-identity-read' and len(reads) == 2:
            token_path.write_text('replacement', encoding='utf-8')
        if change == 'unknown-identity':
            raise OSError('identity unavailable')
        return pid[0]
    monkeypatch.setitem(sys.modules, 'gateway.status', SimpleNamespace(
        live_gateway_pid_for_home=live_pid, get_process_start_time=lambda _: start[0]))
    monkeypatch.setitem(sys.modules, 'psutil', SimpleNamespace(
        Process=lambda _: SimpleNamespace(create_time=lambda: created_at[0])))
    spawned = []
    monkeypatch.setattr(restart_mod.subprocess, 'Popen', lambda argv, **kw: spawned.append((argv, kw)))
    restart_mod.schedule_gateway_restart(delay_seconds=0)
    argv, kwargs = spawned[0]
    for key, value in kwargs['env'].items():
        monkeypatch.setenv(key, value)
    token_path = restart_mod.restart_log_path().with_name('gateway-restart.token')
    if change == 'token-before-first':
        token_path.write_text('replacement', encoding='utf-8')
    elapsed, calls, sleeps = [0.0], [], []
    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds
    def call(args, **kw):
        assert args[-2:] == ['gateway', 'restart']
        assert '--yes' not in args and '--interrupt' not in args and 'timeout' not in kw
        calls.append(args)
        return 0 if len(calls) == 3 else 1
    def wake(seconds):
        sleep(seconds)
        if not calls:
            return
        # Change runtime state during backoff, after the first refusal.
        if change == 'token-after-failure':
            token_path.write_text('replacement', encoding='utf-8')
        elif change == 'missing-token':
            token_path.unlink()
        elif change == 'unreadable-token':
            token_path.write_bytes(b'\xff')
        elif change == 'pid':
            pid[0] += 1
        elif change == 'start-time':
            start[0] += 100
        elif change == 'started-after-request':
            pid[0] = 11002
            created_at[0] = float(kwargs['env']['CLAWCHAT_GATEWAY_RESTART_REQUESTED_AT']) + 1
    monkeypatch.setattr(time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(time, 'sleep', wake)
    monkeypatch.setattr(subprocess, 'call', call)
    exec(argv[2], {})
    output = capsys.readouterr().out
    assert len(calls) == expected_calls
    if reason:
        assert output.rstrip().endswith(reason)
        assert sleeps == ([0] if expected_calls == 0 else [0, 5])
    else:
        assert sleeps == [0, 5, 10]
        assert output.rstrip().endswith('attempt=3 exit_code=0')


def test_new_restart_request_replaces_previous_token(profile_home, monkeypatch):
    spawned = []
    monkeypatch.setattr(restart_mod.subprocess, 'Popen', lambda argv, **kw: spawned.append(kw))
    restart_mod.schedule_gateway_restart()
    token_path = restart_mod.restart_log_path().with_name('gateway-restart.token')
    first = token_path.read_text(encoding='utf-8')
    restart_mod.schedule_gateway_restart()
    second = token_path.read_text(encoding='utf-8')
    assert first != second
    assert [kw['env']['CLAWCHAT_GATEWAY_RESTART_TOKEN'] for kw in spawned] == [first, second]
    assert list(token_path.parent.glob('.gateway-restart-*')) == []


@pytest.mark.skipif(sys.platform == 'win32', reason='fake hermes uses a POSIX shebang')
def test_detached_launcher_reads_host_identity_outside_checkout(profile_home, tmp_path, monkeypatch):
    source = tmp_path / 'host-source'
    package = source / 'gateway'
    package.mkdir(parents=True)
    (package / '__init__.py').write_text('', encoding='utf-8')
    status_file = package / 'status.py'
    status_file.write_text(
        "def live_gateway_pid_for_home(home):\n"
        "    return int((home / 'current-pid').read_text())\n"
        "def get_process_start_time(pid):\n"
        "    return 100\n", encoding='utf-8')
    (profile_home / 'current-pid').write_text('11001', encoding='utf-8')
    monkeypatch.setitem(sys.modules, 'gateway.status', SimpleNamespace(
        __file__=str(status_file), live_gateway_pid_for_home=lambda _: 11001,
        get_process_start_time=lambda _: 100))
    fake_bin = tmp_path / 'hermes'
    fake_bin.write_text(
        f'#!{sys.executable}\n'
        'from pathlib import Path\n'
        "print('manual restart simulated', flush=True)\n"
        "Path('current-pid').write_text('11002')\n"
        'raise SystemExit(1)\n', encoding='utf-8')
    fake_bin.chmod(0o755)
    monkeypatch.setattr(restart_mod, '_hermes_binary', lambda _: fake_bin)
    # A launcher's injected sys.path need not match the configured fallback.
    monkeypatch.setenv('HERMES_DIR', str(tmp_path / 'absent-source'))
    real_popen = subprocess.Popen
    def popen(argv, **kwargs):
        proc = real_popen(argv, **kwargs)
        proc.wait(timeout=30)
        return proc
    monkeypatch.setattr(restart_mod.subprocess, 'Popen', popen)
    restart_mod.schedule_gateway_restart(delay_seconds=0, retry_initial_seconds=0.01)
    log = restart_mod.restart_log_path().read_text(encoding='utf-8')
    assert log.count('manual restart simulated') == 1
    assert 'attempt=1 exit_code=1' in log
    assert log.rstrip().endswith('gateway already restarted; stopping')


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_token_publish_failure_does_not_launch_or_damage_previous_token(
    profile_home, monkeypatch, forbid_spawn, failure,
):
    token_path = restart_mod.restart_log_path().with_name('gateway-restart.token')
    token_path.parent.mkdir(parents=True)
    token_path.write_text('previous', encoding='utf-8')
    def fail(*_args):
        raise OSError('token publish failed')
    monkeypatch.setattr(restart_mod.os, failure, fail)
    with pytest.raises(OSError, match='token publish failed'):
        restart_mod.schedule_gateway_restart()
    assert token_path.read_text(encoding='utf-8') == 'previous'
    assert list(token_path.parent.glob('.gateway-restart-*')) == []
