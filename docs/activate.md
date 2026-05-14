# Activate — `clawchat_gateway/activate.py`

Exchanges a ClawChat activation (invite) code for a token via `/v1/agents/connect`, persists secrets into `$HERMES_HOME/.env`, and writes non-secret platform settings + streaming defaults into `$HERMES_HOME/config.yaml`. When running inside Hermes, persistence goes through `hermes_cli.config` helpers (`read_raw_config`, `save_config`, `save_env_value`, `remove_env_value`) so Hermes owns atomic writes, permissions, managed-mode handling, and env-cache invalidation. The module keeps direct file I/O only as a fallback for tests or standalone installs where `hermes_cli` is not importable.

Exposed as a Python API, a shared activation-and-restart helper, a native Hermes CLI command (`hermes clawchat activate CODE`), the interactive Hermes gateway setup flow (`hermes gateway setup`), the `clawchat_activate` tool handler, and a standalone module CLI (`python -m clawchat_gateway.activate CODE`).

## Helpers

| Function | Signature | Purpose |
|---|---|---|
| `_hermes_config_api` | `() -> dict[str, Callable] \| None` | Detect `hermes_cli.config` write helpers. Returns `None` outside Hermes. |
| `_hermes_home` | `() -> Path` | `$HERMES_HOME` or `~/.hermes`. |
| `_load_config` | `() -> tuple[Path, dict]` | Prefer `hermes_cli.config.get_config_path()` + `read_raw_config()`; fallback loads `$HERMES_HOME/config.yaml`; returns `(path, {})` if missing or malformed. Does not raise. |
| `_write_config` | `(config_path: Path, config: dict) -> None` | Prefer `hermes_cli.config.save_config(config)`; fallback serialises via `yaml.safe_dump(..., allow_unicode=False, sort_keys=False)` and creates parent dirs. |
| `_env_path` | `() -> Path` | Prefer `hermes_cli.config.get_env_path()`; fallback returns `$HERMES_HOME/.env`. |
| `_validate_env_value` | `(key: str, value: str) -> str` | Reject `\n` / `\r` in `.env` values to keep the line-based format intact. Raises `ValueError` when invalid; returns `value` otherwise. |
| `_write_env_values` | `(values: dict[str, str \| None]) -> Path` | Prefer `hermes_cli.config.save_env_value` / `remove_env_value`; fallback upserts selected `KEY=value` lines in `.env`, preserves unrelated lines, and removes keys whose value is `None`. Each fallback non-`None` value goes through `_validate_env_value`. |
| `_derive_websocket_url` | `(base_url: str) -> str` | For the two well-known NewBase hosts (`company.newbaselab.com:19001` and `:10086`), return `DEFAULT_WEBSOCKET_URL` verbatim. Otherwise swap `http→ws`/`https→wss` and append `/v1/ws`. |

## `persist_activation`

```python
persist_activation(*, access_token: str, user_id: str, refresh_token: str | None, base_url: str) -> dict
```

Writes into `~/.hermes/.env`:

- `CLAWCHAT_TOKEN = access_token`
- `CLAWCHAT_REFRESH_TOKEN = refresh_token` when present, or removes any stale `CLAWCHAT_REFRESH_TOKEN` when absent.
- Uses `hermes_cli.config.save_env_value` / `remove_env_value` when available.

Writes into `~/.hermes/config.yaml`:

- `platforms.clawchat.enabled = True`
- `platforms.clawchat.extra`:
  - `base_url` (rstripped)
  - `websocket_url` (`_derive_websocket_url`)
  - `user_id`
  - `reply_mode = "stream"`
  - `show_tools_output = False`
  - `show_think_output = False`
  - stale `token` / `refresh_token` keys are removed if they were written by an older version.
- `streaming`:
  - `enabled = True`
  - defaults for `transport = "edit"`, `edit_interval = 0.25`, `buffer_threshold = 16` (via `setdefault`, so not overwritten if already set).
- `display.platforms.clawchat`:
  - `tool_progress = "off"`
  - `show_reasoning = False`
- Uses `hermes_cli.config.read_raw_config` / `save_config` when available, so only the user's raw config is mutated rather than dumping Hermes defaults.

Returns a dict describing the result (tokens are redacted as `"***"`):

```python
{
  "config_path": str,
  "env_path": str,
  "user_id": str,
  "base_url": str,
  "websocket_url": str,
  "token": "***",
  "refresh_token": "***" | None,
  "restart_required": True,
  "restart_message": "Restart Hermes gateway so ClawChat reloads the new credentials.",
}
```

## `activate`

```python
async activate(code: str, *, base_url: str) -> dict
```

1. Build a `ClawChatApiClient(base_url=base_url.rstrip("/"), token="", user_id="")` — the connect call is unauthenticated.
2. `await client.agents_connect(code=code)` — expected payload: `{access_token, refresh_token?, agent: {user_id, ...}}`.
3. Hand off to `persist_activation`.

This function only performs the connect request and persistence. It does not add `ok` metadata or schedule a restart.

## `activate_and_maybe_restart`

```python
async activate_and_maybe_restart(
    code: str,
    *,
    base_url: str,
    restart: bool,
    restart_delay_seconds: int = 2,
) -> dict
```

Shared wrapper for every user-facing activation entrypoint. It trims the code, awaits `activate(...)`, appends `ok: True`, and, when `restart=True`, calls `schedule_gateway_restart(delay_seconds=restart_delay_seconds)` and adds:

- `restart_scheduled = True`
- `restart_delay_seconds`
- `restart_command`
- `restart_message = "ClawChat activation is saved. Hermes restart has been scheduled in the background."`

When `restart=False`, the persisted activation result still contains `restart_required: True`, but no restart is dispatched and no `restart_scheduled` key is added. `hermes gateway setup` uses this mode because Hermes owns the final service action after the full setup flow: restart a running gateway, start an installed stopped gateway, or install/start a service when needed.

## Entrypoints

| Entrypoint | Restart behavior | Notes |
|---|---|---|
| `clawchat_activate` tool (`_handle_clawchat_activate` in `__init__.py`) | Always calls `activate_and_maybe_restart(..., restart=True)`. | Returns a Hermes v0.12-compatible JSON string. Converts exceptions to `_tool_error`. |
| `hermes clawchat activate CODE` | Calls `activate_and_maybe_restart(..., restart=not --no-restart)`. | Preferred scriptable Hermes-native flow. Registered by `ctx.register_cli_command`. |
| `hermes gateway setup` | Calls `activate_and_maybe_restart(..., restart=False)`. | Preferred interactive flow. The setup hook tells the user that Hermes gateway setup will handle the final service step after finishing. |
| `python -m clawchat_gateway.activate CODE` | Calls `activate_and_maybe_restart(..., restart=not --no-restart)`. | Fallback when the native plugin CLI command is unavailable. |

## CLI — `main(argv=None) -> int`

```
usage: python -m clawchat_gateway.activate [--base-url URL] [--no-restart] CODE
```

- `code` — positional, required. Passed to `activate_and_maybe_restart(...)`, which strips surrounding whitespace before calling `activate(...)`.
- `--base-url` — default `DEFAULT_BASE_URL`.
- `--no-restart` — skip the detached `hermes gateway restart` that would otherwise be dispatched after activation succeeds. Useful when chaining the CLI with another orchestrator that controls restart timing.

Runs `asyncio.run(activate_and_maybe_restart(...))`. On success (and unless `--no-restart` is passed):

1. Imports `clawchat_gateway.restart.schedule_gateway_restart` and calls it with `delay_seconds=2`.
2. Augments the printed payload with `restart_scheduled: True`, `restart_delay_seconds: 2`, `restart_command: <resolved sh -lc string>`, and `restart_message: "ClawChat activation is saved. Hermes restart has been scheduled in the background."`.
3. Pretty-prints the payload (`ensure_ascii=False, indent=2`) and exits 0.

API errors bubble up as exceptions (no try/except wrapper in `main`), so the CLI crashes with a traceback on transport / auth failure. The Hermes tool handler `_handle_clawchat_activate` catches these and converts them to a `_tool_error` envelope.

## Native Hermes CLI — `hermes clawchat activate CODE`

Registered by the plugin via `ctx.register_cli_command("clawchat", ...)` when the host supports native plugin CLI commands.

```
usage: hermes clawchat activate [--base-url URL] [--no-restart] CODE
```

- `code` — positional, required activation code.
- `--base-url` — default `DEFAULT_BASE_URL`.
- `--no-restart` — pass `restart=False` to the shared activation helper.

The command calls `activate_and_maybe_restart(code, base_url=..., restart=not no_restart)`. On success it prints concise status lines:

```
clawchat: activation complete for <user_id>
clawchat: Hermes restart scheduled in <seconds>s
```

The restart line is omitted when no restart was scheduled.
