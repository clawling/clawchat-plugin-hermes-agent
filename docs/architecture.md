# How the plugin plugs into Hermes

The plugin is a Python module loaded at runtime by a Hermes Agent
v0.12.0+ process. Its single public entrypoint is the `register(ctx)`
function in the repo-root `__init__.py` (the Hermes entrypoint module —
distinct from the package surface `clawchat_gateway/__init__.py`).

## Naming map

These names refer to different layers and are not interchangeable:

| Name                                                  | Where it appears                                     | What it identifies                          |
|-------------------------------------------------------|------------------------------------------------------|---------------------------------------------|
| `clawchat`                                            | Hermes plugin id, gateway platform name, slash-command prefix, install dir | The Hermes-side handle for the plugin       |
| `clawchat-gateway`                                    | `pyproject.toml`, wheel name                         | The Python distribution                     |
| `clawling/clawchat-plugin-hermes-agent`                            | `hermes plugins install <here>`                      | The GitHub source spec Hermes pulls from    |
| `clawchat:clawchat-core`                              | Bundled-skill qualified name (`skill_view(...)`)     | The Hermes Plugin Bundle skill              |
| `$HERMES_HOME/plugins/clawchat/`                      | Disk path after install                              | The installed plugin tree                   |

## Registration surface

`register(ctx)` calls these Hermes ABI hooks in order:

| Call                                                         | Provided by              | Effect                                                                                       |
|--------------------------------------------------------------|--------------------------|----------------------------------------------------------------------------------------------|
| `ctx.register_platform(name="clawchat", ...)`                | `__init__._register_platform` | Registers the gateway platform. Requires Hermes v0.12.0+; raises otherwise.            |
| `ctx.register_tool(name, "clawchat", schema, handler, ...)`  | `clawchat_gateway.plugin_tools.register_tools` | Registers all thirty-three `clawchat_*` tools. List is also in `plugin.yaml`. |
| `ctx.register_skill("clawchat-core", path, description=...)` | `__init__._register_skill` | Registers the bundled Plugin Bundle skill `clawchat:clawchat-core` (path `skills/clawchat-core/SKILL.md`), then any extra skills present in the managed manifest (`$HERMES_HOME/clawchat-skills/manifest.json`) that were delivered by a dynamic skill update, so they survive restarts. Also captures the registrar (`skill_update.set_skill_registrar`) so a brand-new skill applied after owner consent is hot-registered immediately (`skill_update.hot_register_new_skills`) without a restart. Skipped silently if the host does not implement `register_skill`. Load also runs `skill_update.ensure_external_skills_dir()`, which idempotently adds `clawchat-skills` to the host config's `skills.external_dirs` so all managed skills additionally appear in the `<available_skills>` index / `skills_list` under their bare names. |
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

Every other target is delegated to Hermes' original parser, which for an
unknown platform returns `(None, None, False)` — "not explicit". The host then
resolves the reference through `gateway.channel_directory.resolve_channel_name`,
i.e. the friendly-name → id lookup, and re-parses the result. That is a
legitimate path (it is how "send to <agent nickname>" works) and must not be
short-circuited: the directory is what turns a nickname into the `cnv_…` the
patch then accepts.

## Outbound chat_id validity gate

`ClawChatConnection.send_frame` drops any frame whose `chat_id` is present but
does not start with `cnv_` (`connection.is_valid_chat_id`), logging at WARNING
with the offending value and `reason=invalid_chat_id`.

The check reads the **raw frame**, not `queued.chat_id`: `_queued_frame`
coerces a missing key to `""`, which would make an explicitly-empty `chat_id`
indistinguishable from an absent one. The rule is *a frame that carries a
`chat_id` must carry a valid one* — an absent `chat_id` is fine, an empty one
is not.

`CHAT_ID_PREFIX` and the rejected/accepted sample set are **cross-plugin
contract**, pinned by `clawchat_gateway/fixtures/permission_events/invalid-chat-id-outbound.json`
— a byte-identical copy of the openclaw plugin's fixture, asserted from both
suites (`tests/test_parity.py`). The openclaw side enforces the same rule at
its own structured boundaries (`sendRawEnvelope`, `sendAlignedAckableEnvelope`,
`sendOpenclawClawlingReaction`).

Every ClawChat conversation id is minted by member-backend with the `cnv_`
prefix and msghub resolves a chat_id only through member-backend, so anything
else is refused upstream with `code=400: invalid conversation id` — a wasted
round trip and an ERROR line in msghub for a frame that never had a recipient.
Production (2026-07-30) saw agents address peers by their `usr_…` idcode and by
a host-composed `direct:{self}:{peer}` key.

The gate lives at the connection boundary rather than at any single caller
because the plugin forwards a chat_id chosen upstream — by the host's target
parser, its channel directory (whose entry ids are `{chat_id}` or
`{chat_id}:{thread_id}`, so it *can* compose values that are not ClawChat
conversation ids), a cron target, or a tool argument. Guarding the one point
every frame passes through does not depend on enumerating those paths.

Unlike the dead-chat gate, this one is **not** re-checked when the reconnect
queue replays: a dead chat is a revocable server state a queued frame can fall
into, whereas a malformed chat_id is wrong at construction time and can never
become valid — rejecting it at the entry point keeps it out of the queue
entirely.

The drop surfaces to the caller as `send_frame` → `False`, which
`ClawChatAdapter.send` turns into `SendResult(success=False)` and
`standalone_send` into `{"error": …}`, so the agent is told the send failed
rather than reading it as delivered.

## Standalone sender (out-of-process `hermes send` / cron)

`register_platform` also passes `standalone_sender_fn=_clawchat_standalone_send`
(`__init__` → `clawchat_gateway.standalone_send.standalone_send`). Hermes'
`send_message` tool falls back to this hook when no live gateway adapter
exists in the calling process — the `hermes send` CLI and `deliver=clawchat`
cron jobs running outside the gateway process. On older Hermes builds whose
`PlatformEntry` lacks the field, registration retries without it (out-of-process
delivery then stays unavailable).

ClawChat has no REST send endpoint, so the standalone path opens an
**ephemeral** `ClawChatConnection` (reusing credential loading, the challenge
handshake, token refresh, and ack tracking), sends one `message.send` frame
with `wait_for_ack=True`, and closes. Two invariants:

- **Sibling device id.** msghub enforces single-session per
  `(user_id, device_id)` with takeover semantics — connecting with the
  canonical device id would kick a gateway daemon running in another process
  off its socket. The ephemeral session therefore presents
  `<canonical id>-standalone` on the WS connect payload
  (`ClawChatConnection.use_sibling_connect_device_id`). Server-side message
  state is user-scoped with per-device replay cursors, so the sibling session
  never consumes messages on the real device's behalf; its only durable
  footprint is its own replay cursor, which the server expires after a period
  of inactivity.
- **Canonical refresh id.** `/v1/auth/refresh` rejects a mismatched
  `X-Device-Id` with a 10003 forced re-login, so token refresh keeps using the
  canonical resolved device id — only the WS connect payload is overridden.

Media attachments work on the standalone path too: `/media/upload` is plain
REST (bearer token only, no live adapter needed), so the ephemeral session
uploads each file via `media_runtime.upload_outbound_media` — using the
post-handshake token from the connection config — and attaches the resulting
fragments to the same `message.send` frame. If every upload fails the send is
aborted with an error rather than silently degrading to text-only. The
media-delivery patch (`_send_clawchat_media_via_live_adapter`) prefers the
live adapter when the gateway runs in-process and falls back to this
standalone path otherwise.

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
