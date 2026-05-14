# Restart — `clawchat_gateway/restart.py`

Schedules a detached `hermes gateway restart` after activation so the gateway picks up freshly written `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` and any new `platforms.clawchat.extra` values without blocking the activation tool response.

Used by:

- `clawchat_gateway.activate.main` — the CLI dispatches a restart unless `--no-restart` is passed.
- `clawchat_gateway.cli.handle_clawchat_cli` — the native `hermes clawchat activate CODE` command dispatches a restart unless `--no-restart` is passed.
- `_handle_clawchat_activate` in the repo-root `__init__.py` — the Hermes tool handler dispatches a restart through `activate_and_maybe_restart(..., restart=True)`.

Not used by `clawchat_gateway.setup.setup_clawchat_platform`, which calls activation with `restart=False` so Hermes gateway setup can decide the final service action after the full setup flow: restart a running gateway, start an installed stopped gateway, or install/start a service when needed.

## Helpers

| Function | Signature | Purpose |
|---|---|---|
| `_hermes_home` | `() -> Path` | `$HERMES_HOME` or `~/.hermes`. |
| `_hermes_dir` | `() -> Path` | `$HERMES_DIR` / `$HERMES_AGENT_DIR`; else `/opt/hermes` if `/opt/hermes/gateway` exists; else `$HERMES_HOME/hermes-agent`. |
| `_hermes_binary` | `(hermes_dir: Path) -> Path` | First existing candidate from `<hermes_dir>/.venv/bin/hermes`, `~/.hermes/hermes-agent/.venv/bin/hermes`, `/opt/hermes/.venv/bin/hermes`; falls back to bare `hermes` (relies on `$PATH`). |

## `schedule_gateway_restart`

```python
schedule_gateway_restart(delay_seconds: int = 2) -> str
```

1. Resolve `HERMES_HOME`, `HERMES_DIR`, and the hermes binary.
2. Build a single shell command (with the resolved env vars and binary path quoted via `repr`):

   ```
   sleep <delay_seconds>; HERMES_HOME='<home>' HERMES_DIR='<dir>' '<hermes-bin>' gateway restart
   ```

3. Spawn it via `subprocess.Popen(["sh", "-lc", command], stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)`. `start_new_session=True` detaches the child from the parent process group so the restart survives the parent (the Hermes worker) being torn down by the restart itself.
4. Return the command string. Callers log it but do not wait on it.

## Why a separate module

- The same restart logic is needed by both the CLI (`activate.main`) and the Hermes tool handler (`_handle_clawchat_activate`); putting it in one place keeps the env resolution consistent.
- Exposing the resolved command string as a return value makes the dispatch testable (callers can log it) and makes log-driven debugging possible without parsing `ps`.
- Detaching with `start_new_session=True` is required: the parent `hermes` worker is the thing being restarted, so any non-detached child would be killed mid-restart.
