# Plugin Entrypoint

Covers the repo-root `__init__.py`, `plugin.yaml`, `clawchat_gateway/plugin_tools.py`, and `clawchat_gateway/__init__.py`.

## `plugin.yaml`

Hermes plugin manifest.

```yaml
manifest_version: 1
name: clawchat
kind: platform
version: 0.4.0
description: "ClawChat gateway integration for Hermes Agent."
author: NewBase
requires_env:
  - CLAWCHAT_TOKEN
  - CLAWCHAT_REFRESH_TOKEN
provides_hooks:
  - pre_gateway_dispatch
provides_tools:
  - clawchat_get_account_profile
  - clawchat_get_user_profile
  - clawchat_list_account_friends
  - clawchat_update_account_profile
  - clawchat_upload_avatar_image
  - clawchat_upload_media_file
```

The manifest declares that this is a gateway platform plugin and lists the tools surfaced by `register()`. The actual platform and tool handlers are still registered dynamically through the plugin context.

## Repo-root `__init__.py`

### Path helpers

| Function | Signature | Purpose |
|---|---|---|
| `_plugin_dir` | `() -> Path` | Absolute path of this file's parent directory. |

### Platform registration

Hermes v0.12.0+ loads ClawChat through the platform registry. Older Hermes
builds without `ctx.register_platform` are not supported.

| Function | Signature | Purpose |
|---|---|---|
| `_clawchat_platform_config_with_home_extra` | `(config) -> PlatformConfig-like` | Merge sparse runtime config with `platforms.clawchat.extra` from `$HERMES_HOME/config.yaml`. |
| `_check_clawchat_platform_requirements` | `() -> bool` | Check optional runtime dependencies such as `websockets`. |
| `_validate_clawchat_platform_config` | `(config) -> bool` | Validate the merged ClawChat config has `websocket_url` and `token`. |
| `_create_clawchat_adapter` | `(config) -> ClawChatAdapter` | Build `ClawChatAdapter` with merged config. |
| `_setup_clawchat_platform` | `() -> None` | Lazy import wrapper for `clawchat_gateway.setup.setup_clawchat_platform`, passed to Hermes as the platform `setup_fn`. |
| `_register_platform` | `(ctx) -> bool` | Register the `clawchat` platform with adapter factory, `setup_fn`, validation hooks, auth env vars, max message length, emoji, and platform hint. Raises `RuntimeError` if the host lacks `ctx.register_platform`. |
| `_configure_runtime_defaults` | `() -> None` | Configure allow-all and streaming defaults via `clawchat_gateway.runtime_defaults`. |

### Self-echo guard (`pre_gateway_dispatch` hook)

| Function | Signature | Purpose |
|---|---|---|
| `_platform_value` / `_is_clawchat_platform` | `(platform) -> str` / `(platform) -> bool` | Normalize enum, dynamic enum, and string platform values so hook checks keep working across Hermes registry modes. |
| `_resolve_clawchat_bot_user_id` | `(gateway) -> str \| None` | Look up the bot's own ClawChat `user_id` from the loaded `gateway.config.platforms` via `ClawChatConfig.from_platform_config(...)`. Accepts `Platform.CLAWCHAT`, string `"clawchat"`, and dynamic enum-like keys. Re-resolved on every hook call (not cached) so freshly activated values are picked up immediately. Returns `None` when `gateway.config` / the platform entry / `user_id` is missing. |
| `_clawchat_pre_gateway_dispatch` | `(*, event, gateway, session_store=None, **_) -> dict \| None` | Hook handler. Returns `{"action": "skip", "reason": "clawchat-self-echo"}` when the event source normalizes to the ClawChat platform **and** the sender's `user_id` matches the resolved bot user_id. Returns `None` to let the dispatch proceed in every other case (other platforms, missing sender, no configured bot user_id). |

Without this hook, hermes-agent's interrupt-on-new-message logic treats the WebSocket echo of the bot's own outbound chunks as a fresh user message, which cancels the in-flight turn and produces an infinite "Operation interrupted: waiting for model response" cascade.

### Tool registration

Tool schemas, descriptions, JSON-string result shaping, and handlers live in `clawchat_gateway/plugin_tools.py`; see [plugin-tools.md](./plugin-tools.md). The repo-root `register(ctx)` imports `register_tools` lazily and calls it after platform registration and runtime defaults.

### Native CLI registration

`_register_cli_commands(ctx)` is a no-op on older Hermes builds. When `ctx.register_cli_command` is available, it imports `clawchat_gateway.cli` and registers:

- command name: `clawchat`
- help: `Manage ClawChat integration`
- setup function: `setup_clawchat_cli`
- handler function: `handle_clawchat_cli`
- description: `Activate and manage the ClawChat Hermes gateway integration.`

This exposes `hermes clawchat activate CODE [--base-url URL] [--no-restart]`. The command shares the same activation helper as the slash command and prints concise CLI status lines.

Hermes Agent v0.12.0 stores this registration but does not add general plugin CLI commands to the top-level `hermes` argparse tree. For that host version, use the repo-root compatibility script instead:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate CODE
```

That script reuses `clawchat_gateway.cli.setup_clawchat_cli` and `handle_clawchat_cli`, so persistence and restart behavior stay identical.

### Slash command registration

`_register_commands(ctx)` is a no-op on older Hermes builds. When `ctx.register_command` is available, it imports `clawchat_gateway.commands` and registers:

- command name: `clawchat-activate`
- description: `Activate ClawChat with an activation code.`
- args hint: `CODE [--base-url URL] [--no-restart]`
- handler: `handle_clawchat_activate_command`

This exposes `/clawchat-activate CODE [--base-url URL] [--no-restart]` inside Hermes sessions. The command uses the same activation helper as the native CLI.

### `register(ctx)` — plugin entrypoint

Module-level setup (runs once when Hermes imports the plugin): the repo root is prepended to `sys.path` so absolute imports of `clawchat_gateway.*` succeed inside the plugin process.

Order of operations inside `register(ctx)`:

1. `_register_platform(ctx)` registers `clawchat` through Hermes' platform registry, including `setup_fn=_setup_clawchat_platform`. If the host does not expose `ctx.register_platform`, registration fails with a clear `RuntimeError`.
2. `_configure_runtime_defaults()` seeds ClawChat defaults in `$HERMES_HOME`.
3. `clawchat_gateway.plugin_tools.register_tools(ctx)` registers the six account/profile/media `clawchat_*` tools.
4. `_register_cli_commands(ctx)` registers the native `hermes clawchat` CLI command when supported.
5. `_register_commands(ctx)` registers `/clawchat-activate` when supported.
6. `ctx.register_hook("pre_gateway_dispatch", _clawchat_pre_gateway_dispatch)` installs the self-echo guard.

## `clawchat_gateway/__init__.py`

Public package surface:

The package `__init__` intentionally exposes only `__version__`. It does not eagerly import `adapter.py`, because that module imports Hermes' `gateway.config.Platform` at module scope. Keeping the adapter import lazy prevents plugin discovery from importing gateway modules before Hermes is ready.
