# Plugin Entrypoint

Covers the repo-root `__init__.py`, `plugin.yaml`, and `clawchat_gateway/__init__.py`.

## `plugin.yaml`

Hermes plugin manifest.

```yaml
manifest_version: 1
name: clawchat
kind: platform
version: 0.1.0
description: "ClawChat gateway integration for Hermes Agent."
author: NewBase
requires_env:
  - CLAWCHAT_TOKEN
  - CLAWCHAT_REFRESH_TOKEN
provides_hooks:
  - pre_gateway_dispatch
provides_tools:
  - clawchat_activate
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
| `_hermes_dir` | `() -> Path` | Resolve hermes-agent dir from `HERMES_DIR` / `HERMES_AGENT_DIR`; fall back to `/opt/hermes` if `/opt/hermes/gateway` exists, else `$HERMES_HOME/hermes-agent`. |
| `_install_gateway` | `() -> None` | Legacy fallback for Hermes builds without `ctx.register_platform`: call `clawchat_gateway.install.main(["--hermes-dir", ...])`; raise `RuntimeError` on non-zero exit. Calls `_refresh_gateway_module_cache()` afterwards. |
| `_refresh_gateway_module_cache` | `() -> None` | After legacy patching, `importlib.invalidate_caches()` + reload `gateway.config`, `gateway.run`, and `clawchat_gateway.adapter`. Necessary because hermes-agent may have already imported `gateway.config` (binding the pre-patch `Platform` enum) before plugin discovery; without this reload, `gateway.run` resolves `Platform.CLAWCHAT` against a stale enum. Reload failures are logged but do not raise. |

### Platform registration

Hermes v0.12.0+ loads ClawChat through the platform registry instead of source patches.

| Function | Signature | Purpose |
|---|---|---|
| `_clawchat_platform_config_with_home_extra` | `(config) -> PlatformConfig-like` | Merge sparse runtime config with `platforms.clawchat.extra` from `$HERMES_HOME/config.yaml`. |
| `_check_clawchat_platform_requirements` | `() -> bool` | Check optional runtime dependencies such as `websockets`. |
| `_validate_clawchat_platform_config` | `(config) -> bool` | Validate the merged ClawChat config has `websocket_url` and `token`. |
| `_create_clawchat_adapter` | `(config) -> ClawChatAdapter` | Build `ClawChatAdapter` with merged config. |
| `_register_platform` | `(ctx) -> bool` | If `ctx.register_platform` exists, register the `clawchat` platform with adapter factory, validation hooks, auth env vars, max message length, emoji, and platform hint. Returns `False` on older Hermes builds. |
| `_configure_runtime_defaults` | `() -> None` | Configure allow-all and streaming defaults without patching Hermes source. |

### Tool error shaping

| Function | Signature | Purpose |
|---|---|---|
| `_tool_error` | `(exc: Exception) -> dict` | Return `{"ok": False, "error": str(exc), "kind": exc.__class__.__name__}`. |

### Tool handlers

Each handler is `async`, takes `(args: dict, **kw)`, logs `task_id`, and returns a dict. `clawchat_activate` still uses `_tool_error(exc)` for legacy activation failures. The six account/media handlers delegate to `clawchat_gateway.tools` and return that module's result envelope directly.

| Handler | Args | Backing |
|---|---|---|
| `_handle_clawchat_activate` | `code`, optional `baseUrl` | `clawchat_gateway.activate.activate` |
| `_handle_clawchat_get_account_profile` | — | `clawchat_gateway.tools.get_account_profile` |
| `_handle_clawchat_get_user_profile` | `userId` | `clawchat_gateway.tools.get_user_profile` |
| `_handle_clawchat_list_account_friends` | optional `page`, optional `pageSize` | `clawchat_gateway.tools.list_account_friends` |
| `_handle_clawchat_update_account_profile` | optional `nickname`, optional `avatar_url`, optional `bio` (>=1) | `clawchat_gateway.tools.update_account_profile` |
| `_handle_clawchat_upload_avatar_image` | `filePath` | `clawchat_gateway.tools.upload_avatar_image` |
| `_handle_clawchat_upload_media_file` | `filePath` | `clawchat_gateway.tools.upload_media_file` |

### Self-echo guard (`pre_gateway_dispatch` hook)

| Function | Signature | Purpose |
|---|---|---|
| `_platform_value` / `_is_clawchat_platform` | `(platform) -> str` / `(platform) -> bool` | Normalize enum, dynamic enum, and string platform values so hook checks keep working across Hermes registry modes. |
| `_resolve_clawchat_bot_user_id` | `(gateway) -> str \| None` | Look up the bot's own ClawChat `user_id` from the loaded `gateway.config.platforms` via `ClawChatConfig.from_platform_config(...)`. Accepts `Platform.CLAWCHAT`, string `"clawchat"`, and dynamic enum-like keys. Re-resolved on every hook call (not cached) so freshly activated values are picked up immediately. Returns `None` when `gateway.config` / the platform entry / `user_id` is missing. |
| `_clawchat_pre_gateway_dispatch` | `(*, event, gateway, session_store=None, **_) -> dict \| None` | Hook handler. Returns `{"action": "skip", "reason": "clawchat-self-echo"}` when the event source normalizes to the ClawChat platform **and** the sender's `user_id` matches the resolved bot user_id. Returns `None` to let the dispatch proceed in every other case (other platforms, missing sender, no configured bot user_id). |

Without this hook, hermes-agent's interrupt-on-new-message logic treats the WebSocket echo of the bot's own outbound chunks as a fresh user message, which cancels the in-flight turn and produces an infinite "Operation interrupted: waiting for model response" cascade.

### Tool registration

`_register_tools(ctx)` registers seven tools with fixed JSON schemas. The `name` inside each schema matches the registration key. Description text is intentionally prescriptive because it is surfaced to the LLM:

- `clawchat_activate` (🔑) — exchange an activation code for credentials.
- `clawchat_get_account_profile` (👤) — fetch the configured account profile.
- `clawchat_get_user_profile` (🧑) — fetch a public profile by explicit `userId`.
- `clawchat_list_account_friends` (👥) — list account friends with pagination.
- `clawchat_update_account_profile` (✏️) — update nickname, avatar URL, and/or bio.
- `clawchat_upload_avatar_image` (🖼️) — upload a local avatar image and return its URL.
- `clawchat_upload_media_file` (📎) — upload a local file/media attachment and return its URL.

### `register(ctx)` — plugin entrypoint

Module-level setup (runs once when Hermes imports the plugin): the repo root is prepended to `sys.path` so absolute imports of `clawchat_gateway.*` succeed both inside the plugin process and inside the `python -m clawchat_gateway.activate` subprocess.

Order of operations inside `register(ctx)`:

1. `_register_platform(ctx)` registers `clawchat` through Hermes' platform registry on v0.12.0+.
2. If platform registration succeeds, `_configure_runtime_defaults()` seeds ClawChat defaults in `$HERMES_HOME`.
3. If platform registration is unavailable, `_install_gateway()` applies legacy hermes-agent patches and refreshes the gateway module cache. A `RuntimeError` here is re-raised; tools/skill registration is **skipped** so hermes-agent is not left in a partially patched state.
4. `_register_tools(ctx)` registers the seven `clawchat_*` tools.
5. `ctx.register_hook("pre_gateway_dispatch", _clawchat_pre_gateway_dispatch)` installs the self-echo guard.
6. If `skills/clawchat/SKILL.md` exists, `ctx.register_skill("clawchat", skill, description=...)`.

## `clawchat_gateway/__init__.py`

Public package surface:

The package `__init__` intentionally exposes only `__version__`. It does not eagerly import `adapter.py`, because that module imports Hermes' `gateway.config.Platform` at module scope. Keeping the adapter import lazy avoids stale enum references in legacy patched installs and keeps modern platform registration from importing gateway modules before Hermes is ready.
