# How the plugin plugs into Hermes

The plugin is a Python module loaded at runtime by a Hermes Agent
v0.12.0+ process. Its single public entrypoint is the top-level
`register(ctx)` function in `__init__.py:449-459`.

## Naming map

These names refer to different layers and are not interchangeable:

| Name                                                  | Where it appears                                     | What it identifies                          |
|-------------------------------------------------------|------------------------------------------------------|---------------------------------------------|
| `clawchat`                                            | Hermes plugin id, gateway platform name, slash-command prefix, install dir | The Hermes-side handle for the plugin       |
| `clawchat-gateway`                                    | `pyproject.toml`, wheel name                         | The Python distribution                     |
| `clawling/clawchat-plugin-hermes-agent`                            | `hermes plugins install <here>`                      | The GitHub source spec Hermes pulls from    |
| `clawchat:clawchat`                                   | Bundled-skill qualified name (`skill_view(...)`)     | The Hermes Plugin Bundle skill              |
| `$HERMES_HOME/plugins/clawchat/`                      | Disk path after install                              | The installed plugin tree                   |

## Registration surface

`register(ctx)` calls these Hermes ABI hooks in order:

| Call                                                         | Provided by              | Effect                                                                                       |
|--------------------------------------------------------------|--------------------------|----------------------------------------------------------------------------------------------|
| `ctx.register_platform(name="clawchat", ...)`                | `__init__._register_platform` | Registers the gateway platform. Requires Hermes v0.12.0+; raises otherwise.            |
| `ctx.register_tool(name, "clawchat", schema, handler, ...)`  | `clawchat_gateway.plugin_tools.register_tools` | Registers all thirty `clawchat_*` tools. List is also in `plugin.yaml`. |
| `ctx.register_skill("clawchat", path, description=...)`      | `__init__._register_skill` | Registers the bundled Plugin Bundle skill `clawchat:clawchat` (path `skills/clawchat/SKILL.md`). Skipped silently if the host does not implement `register_skill`. |
| `ctx.register_cli_command("clawchat", ...)`                  | `__init__._register_cli_commands` | Adds `hermes clawchat activate <CODE>` on Hermes builds that expose `register_cli_command`. |
| `ctx.register_command("clawchat-activate", ...)`             | `__init__._register_commands` | Adds the `/clawchat-activate <CODE>` slash command for in-session activation.        |
| `ctx.register_hook("pre_gateway_dispatch", ...)`             | `__init__._clawchat_pre_gateway_dispatch` | Drops frames whose sender matches the bot's own ClawChat `user_id` (self-echo). |

`adapter_factory`, `setup_fn`, `check_fn`, `validate_config`, and
`is_connected` are passed through `register_platform`. `setup_fn` runs
the interactive `hermes gateway setup` prompts (`clawchat_gateway.setup`);
`validate_config` returns true when a `websocket_url` is available. Tokens are
not required at validation time. When Hermes has already loaded the plugin and
started the ClawChat adapter, a missing token/user credential bundle puts the
adapter in the waiting-for-activation state; the background connection
supervisor can then connect after activation writes SQLite credentials. If the
plugin was only installed and the gateway has not loaded it yet, a normal Hermes
reload or restart is still required before that waiting state exists. If
activation cannot persist SQLite credentials, the default activation restart
lets the next gateway process load credentials from `.env` and `config.yaml`.

## Self-echo hook (`pre_gateway_dispatch`)

`__init__._clawchat_pre_gateway_dispatch` re-resolves the bot's own
`user_id` from the loaded gateway config on every call (never cached —
activation rewrites the value live) and returns
`{"action": "skip", "reason": "clawchat-self-echo"}` when the inbound
frame's `source.user_id` matches the bot. Without this, the
interrupt-on-new-message logic in Hermes treats the WebSocket echo of
the bot's own outbound chunks as fresh user input and produces an
`Operation interrupted` cascade.

## `send_message` target parser patch

`__init__._patch_send_message_target_parser` monkey-patches Hermes'
built-in `tools.send_message_tool._parse_target_ref` so that
`platform="clawchat"` targets starting with `cnv_` are recognized as
explicit ClawChat conversation ids without changing Hermes source. The
patch is narrowly scoped and idempotent (it tags itself with
`_clawchat_target_patch=True`).

## Adapter

`clawchat_gateway.adapter.ClawChatAdapter` extends Hermes'
`gateway.platforms.base.BasePlatformAdapter` and owns the WebSocket
lifecycle (`clawchat_gateway.connection`), inbound frame parsing
(`clawchat_gateway.inbound`), outbound frame construction
(`clawchat_gateway.protocol`), media handling
(`clawchat_gateway.media_runtime`), and per-turn channel-prompt
injection (`_compose_channel_prompt`).

## Wire protocol

This plugin and the sibling `openclaw-clawchat` plugin are **peer
Protocol-v2 clients**. The wire contract is documented in
[`./client-integration.md`](./client-integration.md) — the authoritative
Protocol v2 reference for this plugin (envelope, events, routing, replay,
streaming, and canonical wire examples).

When the wire shape changes:

1. Update [`./client-integration.md`](./client-integration.md) first.
2. Update `clawchat_gateway/protocol.py` (frame builders) and
   `clawchat_gateway/inbound.py` (frame parsing) here.
3. Mirror the same change in `clawchat-plugin-openclaw/src/`.

## Configuration loading

`clawchat_gateway.config.ClawChatConfig.from_platform_config` resolves
configuration in this priority order:

1. Process environment (`CLAWCHAT_*` vars).
2. `hermes_cli.config.get_env_value(...)` (if the helper is importable).
3. `$HERMES_HOME/.env` file lookup.
4. `platforms.clawchat.extra` from `config.yaml`.
5. Hard-coded defaults from the dataclass.

`__init__._clawchat_platform_config_with_home_extra` merges the on-disk
`config.yaml` extra block into sparse runtime `PlatformConfig` values so
Hermes v0.12 can load gateway config before user platform names are
registered. Explicit runtime values always win over the merged extra.

See [`./configuration.md`](./configuration.md) for the field-by-field
table.
