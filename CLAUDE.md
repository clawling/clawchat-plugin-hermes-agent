# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keep docs in sync with code

Always read the relevant doc before changing a feature, and update it after. Whenever you add, remove, or change a Hermes tool, CLI subcommand, env var, anchor patch, config field, or wire-protocol behavior, update the matching file in the same change set:

- `README.md`, `install.md`, `.e2e/dev_install.md` — install/quick-start, env vars, user-visible flows
- `plugin.yaml` — manifest (`requires_env`, `provides_tools`, `provides_hooks`); must match what `register(ctx)` actually registers
- `docs/clawchat-protocol.md` — wire protocol reference (the spec; `docs/protocol.md` documents the Python builder API)
- `docs/` per-module references — keep one doc per `src/clawchat_gateway/*.py` module
- `skills/clawchat/SKILL.md` — activation/profile/avatar flows surfaced to the LLM (must stay consistent with the tool `description` fields in `__init__.py`)
- This `CLAUDE.md` — architecture, commands, env-var lists below

Code and docs must not drift.

## What this repo is

A **Hermes Agent plugin** that integrates the ClawChat messaging platform. It is not a standalone application — at runtime it is loaded into a running hermes-agent process. On Hermes v0.12.0+ it registers the `clawchat` gateway platform directly through `ctx.register_platform(...)`; the legacy anchor-patch installer in `install.py` is only used as a fallback for older Hermes builds that do not expose the platform registry API.

The root `__init__.py` is the Hermes plugin entrypoint (called by hermes-agent via `register(ctx)`). `plugin.yaml` declares the plugin to Hermes. The `src/clawchat_gateway/` package is the gateway adapter, installer, restart helper, and tool implementations; it is also a pip-installable distribution (`pyproject.toml`, name: `clawchat-gateway`).

## Common commands

All runtime commands must use the **Hermes Python venv**, not the system Python, because the adapter imports `gateway.platforms.base` / `gateway.config` from hermes-agent at runtime. Tests stub these (see "Testing" below), so the system Python is fine for tests only.

```bash
# Run the full test suite (pyproject.toml configures pytest-asyncio auto mode)
pytest

# Single test / single file
pytest tests/test_adapter.py
pytest tests/test_install.py::test_apply_and_remove_patch_with_indentation

# Activate a ClawChat account against the API
# (writes secrets to ~/.hermes/.env, non-secret config to ~/.hermes/config.yaml,
#  and dispatches a detached `hermes gateway restart` unless --no-restart is passed)
python -m clawchat_gateway.activate CODE
python -m clawchat_gateway.activate CODE --base-url http://host:port
python -m clawchat_gateway.activate CODE --no-restart

# Inspect / update profile (requires activation first)
python -m clawchat_gateway.profile get
python -m clawchat_gateway.profile get-user <USER_ID>
python -m clawchat_gateway.profile friends [--page N] [--page-size N]
python -m clawchat_gateway.profile update [--nickname X] [--avatar-url URL] [--bio X]
python -m clawchat_gateway.profile upload-avatar /absolute/path/to/image.png
python -m clawchat_gateway.profile upload-media /absolute/path/to/file

# Legacy fallback only — apply patches to an older hermes-agent checkout (idempotent)
python -m clawchat_gateway.install --hermes-dir "$HERMES_DIR"
python -m clawchat_gateway.install --hermes-dir "$HERMES_DIR" --check    # status only
python -m clawchat_gateway.install --hermes-dir "$HERMES_DIR" --dry-run
python -m clawchat_gateway.install --hermes-dir "$HERMES_DIR" --uninstall
```

On Hermes v0.12.0+ the normal install flow is `hermes plugins install clawling/hermes-clawchat && hermes plugins enable clawchat`; the `clawchat_gateway.install` CLI is only needed when supporting an older Hermes checkout without `ctx.register_platform`.

## Architecture

### Boot sequence

`register(ctx)` in `__init__.py` runs in this order:

1. **Python path** — `_register_python_path(_plugin_dir() / "src")` inserts `src/` onto `sys.path` and writes `clawchat_gateway_src.pth` into the first writable site-packages so subprocess Pythons (e.g., `python -m clawchat_gateway.activate`) also find the package.
2. **Platform registration (v0.12+)** — `_register_platform(ctx)` calls `ctx.register_platform(...)` with the ClawChat adapter factory, dependency check, config validator, allowlist env vars, max message length, emoji, and a platform prompt hint. Returns `True` on v0.12+.
3. **Runtime defaults** — when platform registration succeeds, `_configure_runtime_defaults()` calls `configure_clawchat_allow_all()` and `configure_clawchat_streaming()` from `install.py` to seed `~/.hermes/.env` (`CLAWCHAT_ALLOW_ALL_USERS=true`) and `~/.hermes/config.yaml` (streaming defaults, display flags). No source patching.
4. **Legacy fallback** — when `ctx.register_platform` is unavailable, `_install_gateway()` runs `clawchat_gateway.install.main(["--hermes-dir", ...])` which anchor-patches hermes-agent's own files. After patching, `_refresh_gateway_module_cache()` invalidates `gateway.config`, `gateway.run`, and `clawchat_gateway.adapter` so any modules already imported pick up the fresh `Platform.CLAWCHAT` enum.
5. **Tool registration** — `_register_tools(ctx)` registers seven tools: `clawchat_activate`, `clawchat_get_account_profile`, `clawchat_get_user_profile`, `clawchat_list_account_friends`, `clawchat_update_account_profile`, `clawchat_upload_avatar_image`, `clawchat_upload_media_file`.
6. **Hook registration** — `ctx.register_hook("pre_gateway_dispatch", _clawchat_pre_gateway_dispatch)`. The hook drops inbound frames whose sender is the bot's own ClawChat user_id; without it the WS-echo of the bot's outbound chunks is treated as a fresh user message and cancels the in-flight turn (interrupt loop).
7. **Skill registration** — `ctx.register_skill("clawchat", skills/clawchat/SKILL.md, description=...)`.

After `clawchat_activate` succeeds, the handler calls `clawchat_gateway.restart.schedule_gateway_restart(delay_seconds=2)`, which spawns a detached `sh -lc 'sleep 2; HERMES_HOME=… HERMES_DIR=… <hermes-bin> gateway restart'`. The 2-second delay lets the activation tool response reach the user before the gateway is torn down.

### Anchor-patch installer (`install.py`, legacy only)

Modern Hermes registers platforms via `ctx.register_platform(...)`. The patch installer is retained for older Hermes builds that lack the platform registry. Patches insert code blocks into hermes-agent's own files, wrapped with `# clawchat-gateway:<id>:start` / `:end` markers; each `Patch` is idempotent — re-running detects the start marker and skips. Files touched in legacy mode:

- `gateway/config.py` — add `Platform.CLAWCHAT`, env-var overrides, `connected_platforms` check
- `gateway/run.py` — adapter factory case, auth-map entries, post-stream `on_run_complete` hooks, startup allowlist
- `agent/prompt_builder.py` — per-platform prompt hint for ClawChat
- `tools/send_message_tool.py` — map the `"clawchat"` string to `Platform.CLAWCHAT`
- `hermes_cli/platforms.py` — CLI platform registry entry

`configure_clawchat_allow_all()` and `configure_clawchat_streaming()` are shared by both paths — modern v0.12+ calls them through `_configure_runtime_defaults()`, legacy installs call them from `install.main()`. Installed state is tracked in `<hermes-dir>/.clawchat_gateway_install_state.json`.

When editing `build_patches()`, if you change an `anchor` string, pick something still present in the current hermes-agent source; if you change a `payload`, also bump or re-scope the `id` so existing installs don't skip your new payload (the old start marker would still be present).

### Gateway runtime (`src/clawchat_gateway/`)

- **`adapter.py`** — `ClawChatAdapter(BasePlatformAdapter)`. Implements `connect/disconnect`, `send`, `edit_message`, `on_run_complete`, `send_typing`, `send_image[_file]`. Holds a map of `_ActiveRun` per in-flight streaming message and uses the `stream` reply mode when available (falls back to a single `message.reply` for media-only or non-stream configs). Filters `<think>` and tool-call blocks out of visible content based on `show_think_output`/`show_tools_output`. Attaches the `clawchat` skill to inbound messages whose text matches `_ACTIVATION_INTENT_RE` so Hermes proactively picks up activation phrases.
- **`connection.py`** — `ClawChatConnection` owns the WebSocket lifecycle: a supervisor task with exponential backoff + jitter reconnect, challenge/`hello-ok` handshake (path `/v1/ws` uses the realtime subprotocol and skips the legacy handshake), an outbound send queue that flushes once `READY`, and a read loop that dispatches `message.send` / `message.reply` / `interaction.submit` frames to the adapter.
- **`protocol.py`** — pure frame builders (`message.created`, `message.add`, `message.done`, `message.reply`, `message.failed`, `typing.update`, `connect`) plus `compute_client_sign` (HMAC-SHA256 of `client_id|nonce` keyed by token), `extract_nonce`, `is_hello_ok`, and `encode_frame`/`decode_frame`. The wire-protocol reference is `docs/clawchat-protocol.md`; the Python module reference is `docs/protocol.md`.
- **`inbound.py`** — parses a `message.send` envelope into `InboundMessage`. In `group` chat + `group_mode=mention`, drops frames where the bot's `user_id` is not in `context.mentions`.
- **`stream_buffer.py`** — `compute_delta(last_text, new_text)` for streaming appends.
- **`media_runtime.py`** — outbound uploads via `/media/upload`, inbound downloads into `media_download_dir` (default `/tmp/clawchat-media`). Local outbound paths must be under `media_local_roots` (`ensure_allowed_local_path`).
- **`api_client.py`** — thin HTTP client using `urllib.request` in a thread (no `requests`/`httpx` dep). Default base URL is `http://company.newbaselab.com:10086`; responses must have envelope `{code: 0, data: {...}}` or `ClawChatApiError` is raised. Handles `/v1/agents/connect`, `/v1/users/me`, `/v1/users/{id}`, `/v1/friends`, `/media/upload`, `/v1/files/upload-url`.
- **`config.py`** — `ClawChatConfig` is a frozen dataclass built from `platform_config.extra`. Supports both snake_case and camelCase keys (`_get_alias`) and resolves connectivity values from `CLAWCHAT_*` env vars first via `_get_env` (process env → Hermes-managed env file → `$HERMES_HOME/.env` → `extra` → default). Do **not** add required fields without also updating `from_platform_config` and the activation writer.
- **`activate.py`** — calls `/v1/agents/connect` with `platform=hermes`, `type=clawbot`, writes `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` into `~/.hermes/.env`, writes `user_id`/`base_url`/`websocket_url` and streaming defaults into `~/.hermes/config.yaml` under `platforms.clawchat.extra`. The websocket URL is derived from the base URL unless the base is a known NewBase host (which uses `DEFAULT_WEBSOCKET_URL` verbatim). The CLI dispatches a detached `hermes gateway restart` via `restart.schedule_gateway_restart` unless `--no-restart` is passed.
- **`restart.py`** — `schedule_gateway_restart(delay_seconds=2)` builds an env-aware shell command (`HERMES_HOME=…  HERMES_DIR=…  <hermes-bin> gateway restart`), spawns it detached via `subprocess.Popen(..., start_new_session=True)`, and returns the command string for logging.
- **`tools.py`** — async tool handlers for the six new account/profile/media tools. Single source of truth shared by Hermes tool registration in `__init__.py` and the `profile.py` CLI. Returns result dicts; never raises (errors are returned as `{"error": "kind", "message": "..."}` envelopes).
- **`profile.py`** — CLI subcommands (`get`, `get-user`, `friends`, `update`, `upload-avatar`, `upload-media`) that mirror the `clawchat_*` tool surface. Each subcommand calls the matching handler in `tools.py`. Also exports `load_profile_config` / `ProfileConfig` used by `tools.py` for credential lookup.
- **`device_id.py`** — stable device ID for the `X-Device-Id` header.

### Skill (`skills/clawchat/SKILL.md`)

Registered via `ctx.register_skill("clawchat", ...)`. Legacy patch installs additionally copy it into `$HERMES_HOME/skills/clawchat/` via `install_skill()`. It encodes the activation / profile / media flows as instructions for the Hermes LLM. Keep it and the tool `description` fields in `__init__.py` consistent — both are surfaced to the model and divergent phrasing causes the activation tool to be skipped.

### Self-echo guard (`pre_gateway_dispatch` hook)

`_clawchat_pre_gateway_dispatch` (in `__init__.py`) inspects every gateway-bound event, looks up the bot's own ClawChat `user_id` from the loaded gateway config (re-resolved on every call so it picks up fresh activation), and returns `{"action": "skip", "reason": "clawchat-self-echo"}` when the sender matches. Without this guard, hermes-agent's interrupt-on-new-message logic treats the WebSocket echo of the bot's own outbound chunks as fresh user input and restarts the turn forever.

## Testing

`tests/conftest.py` inserts `src/` onto `sys.path` and then `tests/fake_hermes.py` injects stub modules for `gateway`, `gateway.config`, `gateway.platforms`, and `gateway.platforms.base` so the adapter can be imported without a real hermes-agent checkout. When adding imports from `gateway.*` in production code, extend `fake_hermes.py` or the test will fail at import time.

`tests/fake_ws.py` provides an in-memory WebSocket for connection-layer tests. Pytest runs in `asyncio_mode = "auto"` so async tests don't need `@pytest.mark.asyncio`.

## Environment variables

Runtime (read by Hermes v0.12+ platform registration helpers, the Python modules here, and by legacy installed patches):

- `HERMES_HOME` — Hermes data dir (default `~/.hermes`). Controls where `config.yaml`, `.env`, `skills/`, and `plugins/` live.
- `HERMES_DIR` / `HERMES_AGENT_DIR` — hermes-agent install dir (default `$HERMES_HOME/hermes-agent`, or `/opt/hermes` if present).
- `CLAWCHAT_WEBSOCKET_URL` / `CLAWCHAT_WS_URL`, `CLAWCHAT_BASE_URL`, `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, `CLAWCHAT_USER_ID`, `CLAWCHAT_REPLY_MODE`, `CLAWCHAT_GROUP_MODE`, `CLAWCHAT_MEDIA_LOCAL_ROOTS` — override values in `platforms.clawchat.extra` at hermes-agent startup (resolved by `_get_env` in `config.py`; legacy patches inject the same values via the `env_overrides` blocks).
- `CLAWCHAT_ALLOWED_USERS`, `CLAWCHAT_ALLOW_ALL_USERS` — auth allowlist, read by hermes-agent.
- `CLAWCHAT_DEVICE_ID` — override the auto-derived device id.
