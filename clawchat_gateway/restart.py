from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from clawchat_gateway.hermes_home import hermes_home


def _hermes_home() -> Path:
    return hermes_home()


def _hermes_dir() -> Path:
    for key in ("HERMES_DIR", "HERMES_AGENT_DIR"):
        value = os.environ.get(key)
        if value:
            return Path(value)
    if Path("/opt/hermes/gateway").is_dir():
        return Path("/opt/hermes")
    return _hermes_home() / "hermes-agent"


def _venv_hermes(venv: Path) -> list[Path]:
    """Console-script locations inside a venv, in probe order.

    Windows venvs put console scripts in ``Scripts\\`` with an ``.exe`` shim, not
    ``bin/``. Probing only the POSIX layout meant every Windows lookup missed and
    fell through to the bare ``Path("hermes")``, so a restart worked only when
    hermes happened to be on PATH already.
    """
    if sys.platform == "win32":
        return [venv / "Scripts" / "hermes.exe", venv / "Scripts" / "hermes"]
    return [venv / "bin" / "hermes"]


def _hermes_binary(hermes_dir: Path) -> Path:
    roots = [hermes_dir, Path.home() / ".hermes" / "hermes-agent"]
    if sys.platform != "win32":
        # Container image layout; there is no Windows equivalent to probe.
        roots.append(Path("/opt/hermes"))
    candidates = [exe for root in roots for exe in _venv_hermes(root / ".venv")]
    # Bare "hermes" is the last resort: os.execvpe resolves it through PATH
    # (and through PATHEXT on Windows), which is correct whenever the venv is
    # already active.
    return next((path for path in candidates if path.exists()), Path("hermes"))


# Where the detached restart writes its output and exit code. Lives in the
# plugin's own data directory (``$HERMES_HOME/clawchat/``, next to the SQLite
# store) — never inside the plugin checkout, which Hermes may replace on update.
RESTART_LOG_FILENAME = "gateway-restart.log"

# The adapter logs this on every transition into READY
# (``clawchat_gateway/adapter.py``); it is the proof the restart took effect.
READY_LOG_LINE = "clawchat state -> ready"

_TRUTHY = {"1", "true", "yes", "on", "y"}
_FALSY = {"0", "false", "no", "off", "n"}


def restart_log_path() -> Path:
    from clawchat_gateway.storage import clawchat_data_dir

    return clawchat_data_dir() / RESTART_LOG_FILENAME


@dataclass(frozen=True)
class RestartPlan:
    """What the pre-restart check found for the active profile.

    ``action`` is one of:

    * ``"schedule"`` — a gateway has run for this profile; schedule the restart.
    * ``"no_gateway"`` — no gateway has ever run for this profile, so there is
      nothing to restart (and a bare ``hermes gateway restart`` with no service
      installed would start an unmanaged foreground gateway instead).
    * ``"multiplexed"`` — this named profile is served by the default profile's
      multiplexing gateway, which refuses a per-profile restart.
    """

    action: str
    profile: str
    hermes_home: Path
    multiplex_owner: bool = False


def _bool_token(value: Any) -> bool | None:
    token = str(value).strip().lower()
    if token in _TRUTHY:
        return True
    if token in _FALSY:
        return False
    return None


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - unreadable config means "unknown", never fatal
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _multiplex_serves(profile: str) -> bool:
    """Whether the default profile's config multiplexes ``profile`` into its gateway.

    Mirrors the host's documented switches: the ``GATEWAY_MULTIPLEX_PROFILES``
    env override, else ``gateway.multiplex_profiles`` in the *default* profile's
    ``config.yaml``, narrowed by ``gateway.multiplex_profile_allowlist`` when set
    (a non-list allowlist serves the default profile only).
    """
    from clawchat_gateway.profile_collision import CONFIG_FILENAME, hermes_root

    cfg = _read_yaml(hermes_root() / CONFIG_FILENAME)
    gateway_cfg = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
    env_raw = os.environ.get("GATEWAY_MULTIPLEX_PROFILES", "")
    enabled = _bool_token(env_raw) if env_raw.strip() else None
    if enabled is None:
        enabled = bool(cfg.get("multiplex_profiles") or gateway_cfg.get("multiplex_profiles"))
    if not enabled:
        return False
    if profile == "default":
        return True
    if "multiplex_profile_allowlist" in cfg:
        allowlist = cfg.get("multiplex_profile_allowlist")
    else:
        allowlist = gateway_cfg.get("multiplex_profile_allowlist")
    if allowlist is None:
        return True
    if not isinstance(allowlist, list):
        return False
    wanted = profile.strip().lower()
    return any(isinstance(entry, str) and entry.strip().lower() == wanted for entry in allowlist)


def _gateway_has_run(home: Path) -> bool:
    """True when the profile home holds a gateway runtime file.

    Every Hermes gateway (service-managed or foreground) writes ``gateway.pid``
    into its ``HERMES_HOME``; newer hosts also keep ``gateway_state.json``.
    Neither present means no gateway has ever run for this profile.
    """
    return (home / "gateway.pid").exists() or (home / "gateway_state.json").exists()


def plan_gateway_restart() -> RestartPlan:
    from clawchat_gateway.storage import _active_profile_name

    profile = _active_profile_name()
    home = _hermes_home()
    try:
        multiplexed = _multiplex_serves(profile)
    except Exception:  # noqa: BLE001 - detection is best-effort
        multiplexed = False
    if multiplexed and profile != "default":
        return RestartPlan("multiplexed", profile, home)
    try:
        has_run = _gateway_has_run(home)
    except OSError:
        has_run = True
    if not has_run:
        return RestartPlan("no_gateway", profile, home)
    return RestartPlan("schedule", profile, home, multiplex_owner=multiplexed)


def _hermes_cmd(profile: str) -> str:
    return "hermes" if profile == "default" else f"hermes -p {profile}"


def restart_next_steps(plan: RestartPlan) -> list[str]:
    """Operator guidance for a restart that was NOT scheduled."""
    if plan.action == "no_gateway":
        cmd = _hermes_cmd(plan.profile)
        return [
            f"no Hermes gateway has run for profile '{plan.profile}' yet "
            f"(no gateway.pid or gateway_state.json in {plan.hermes_home}), "
            "so there is nothing to restart. Install and start one:",
            f"  {cmd} gateway install",
            f"  {cmd} gateway start",
        ]
    if plan.action == "multiplexed":
        return [
            f"profile '{plan.profile}' is served by the shared multiplexed gateway "
            "owned by the default profile (gateway.multiplex_profiles), which refuses "
            "a per-profile restart. Restart the shared gateway from the default profile:",
            "  hermes -p default gateway restart",
            "This restarts every profile that gateway serves, not only this one.",
        ]
    return []


def schedule_gateway_restart(delay_seconds: int = 2) -> str:
    hermes_dir = _hermes_dir()
    hermes_home = _hermes_home()
    hermes_bin = _hermes_binary(hermes_dir)
    log_path = restart_log_path()
    # Run from the Hermes home, never from the caller's cwd: a process whose cwd
    # sits inside plugins/clawchat keeps that folder locked on Windows, which
    # breaks a later plugin update or removal.
    cwd = hermes_home if hermes_home.is_dir() else Path.home()

    env = {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        "HERMES_DIR": str(hermes_dir),
        "CLAWCHAT_HERMES_BIN": str(hermes_bin),
        "CLAWCHAT_GATEWAY_RESTART_DELAY": str(int(delay_seconds)),
    }
    # The launcher's stdout/stderr ARE the log file, so the restart's own output
    # lands there, followed by an explicit exit_code line once it returns.
    launcher = (
        "import os, subprocess, sys, time, datetime; "
        "delay=int(os.environ.get('CLAWCHAT_GATEWAY_RESTART_DELAY', '2')); "
        "time.sleep(delay); "
        "now=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'); "
        "argv=[os.environ['CLAWCHAT_HERMES_BIN'], 'gateway', 'restart']; "
        "print(f'[{now()}] running: {argv}', flush=True); "
        "rc=127\n"
        "try:\n"
        "    rc=subprocess.call(argv, env=os.environ, stdin=subprocess.DEVNULL)\n"
        "except OSError as exc:\n"
        "    print(f'[{now()}] could not start: {exc}', flush=True)\n"
        "print(f'[{now()}] exit_code={rc}', flush=True)"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 - handed to the child
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log_file.write(
            f"[{stamp}] clawchat: gateway restart requested "
            f"(delay={int(delay_seconds)}s, HERMES_HOME={hermes_home})\n"
        )
        log_file.flush()
        popen_kwargs: dict[str, Any] = {
            "env": env,
            "cwd": str(cwd),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
        }
        if sys.platform == "win32":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
        else:
            popen_kwargs["start_new_session"] = True

        subprocess.Popen(
            [sys.executable, "-c", launcher],
            **popen_kwargs,
        )
    finally:
        log_file.close()
    return (
        f"sleep {int(delay_seconds)}; "
        f"HERMES_HOME={str(hermes_home)!r} "
        f"HERMES_DIR={str(hermes_dir)!r} "
        f"{str(hermes_bin)!r} gateway restart"
    )


def format_restart_lines(payload: dict[str, Any]) -> list[str]:
    """Operator-facing lines about the restart, shared by the CLI and slash command."""
    lines: list[str] = []
    if payload.get("restart_scheduled"):
        log_path = payload.get("restart_log")
        lines.append(
            "clawchat: Hermes gateway restart requested; it runs in the background in "
            f"{payload.get('restart_delay_seconds')}s and is not confirmed yet."
        )
        if payload.get("restart_multiplex_owner"):
            lines.append(
                "clawchat: this gateway multiplexes several profiles; the restart "
                "affects all of them."
            )
        lines.append(
            f"clawchat: verify: {log_path} should end with 'exit_code=0', then the "
            f"Hermes log (logs/ under the Hermes home) shows '{READY_LOG_LINE}'."
        )
        lines.append(
            "clawchat: no exit_code line after a few minutes means the restart is "
            "still waiting (e.g. for open sessions to finish) or was stopped together "
            "with the old gateway; the ready line is what confirms the restart."
        )
    for step in payload.get("restart_next_steps") or []:
        lines.append(f"clawchat: {step}" if not step.startswith("  ") else step)
    return lines
