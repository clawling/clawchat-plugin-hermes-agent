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
| `ctx.register_tool(name, "clawchat", schema, handler, ...)`  | `clawchat_gateway.plugin_tools.register_tools` | Registers all forty-seven `clawchat_*` tools. List is also in `plugin.yaml`. |
| `ctx.register_skill("clawchat-core", path, description=...)` | `__init__._register_skill` | Registers the bundled Plugin Bundle skill `clawchat:clawchat-core` (path `skills/clawchat-core/SKILL.md`), then any extra skills present in the managed manifest (`$HERMES_HOME/clawchat-skills/manifest.json`) that were delivered by a dynamic skill update, so they survive restarts. Also captures the registrar (`skill_update.set_skill_registrar`) so a brand-new skill applied after owner consent is hot-registered immediately (`skill_update.hot_register_new_skills`) without a restart. Skipped silently if the host does not implement `register_skill`. Load also runs `skill_update.ensure_external_skills_dir()`, which idempotently adds `clawchat-skills` to the host config's `skills.external_dirs` so all managed skills additionally appear in the `<available_skills>` index / `skills_list` under their bare names. |
| `ctx.register_cli_command("clawchat", ...)`                  | `__init__._register_cli_commands` | Adds `hermes clawchat activate <CODE>` on Hermes builds that expose `register_cli_command`. |
| `ctx.register_command("clawchat-activate", ...)`             | `__init__._register_commands` | Adds the `/clawchat-activate <CODE>` slash command for in-session activation.        |
| `ctx.register_command("clawchat-output", ...)`               | `__init__._register_commands` | Adds the `/clawchat-output minimal\|normal\|full` slash command ([`./output-visibility.md`](./output-visibility.md)). |
| `ctx.register_hook("pre_api_request", ...)`                  | `__init__._register_llm_context_debug_hooks` | Installs the LLM-context debug observer (inert unless `CLAWCHAT_LLM_CONTEXT_DEBUG` is set). |
| `ctx.register_hook("post_api_request", ...)`                 | `__init__._register_session_usage_hook` | Records the prompt size of each ClawChat conversation's latest model request (`clawchat_gateway.sediment`), which times the sediment turn before compression. |
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

## `send_message` media delivery patch

`__init__._patch_send_message_media_delivery` wraps Hermes'
`tools.send_message_tool._send_to_platform`. For `platform="clawchat"` with
`MEDIA:` attachments it delivers through the live adapter
(`_send_clawchat_media_via_live_adapter`, an immediate media send) or, with no
gateway in this process, the standalone sender. This is how the agent sends a
file into **another** conversation, e.g. into a group while talking to its
owner in a direct chat: `send_message(target="clawchat:cnv_<group>",
message="caption MEDIA:/abs/path")`. `clawchat_mention_message` stays text
only: a mention plus a file is two sends (the mention, then `send_message`),
which keeps one media path, with the host's own `MEDIA:` path checks and the
plugin's credential denylist, for every chat.

Hermes' `[[as_document]]` arrives as `force_document`; it is passed to the
adapter (`_clawchat_force_document`) and the standalone sender, and
`send_document` sets it too, so an image goes out as a `file` fragment
instead of inline (`media_runtime.upload_outbound_media(force_document=True)`).

**Terminal-send marker.** `clawchat_mention_message` marks the chat it posted
into (`terminal_send.mark_terminal_clawchat_send`) so the turn's normal
follow-up reply there is dropped: the mention already was the reply.
`ClawChatAdapter.send` consumes that marker only for a follow-up reply. An
explicit send is delivered and leaves the marker in place: a media send
(`_clawchat_immediate_media_send`), a call from Hermes' `send_message`
(detected on the call stack, and by the `terminal_send.explicit_tool_send()`
context the patch sets around every ClawChat send — a context variable,
because the host may run the adapter call on the gateway loop, where the
stack no longer shows the tool). Before this, "mention the group, then send it
the file" returned success and dropped the file.

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
contract**, pinned by the tracked fixture
`clawchat_gateway/fixtures/permission_events/invalid-chat-id-outbound.json`
— a byte-identical copy of the openclaw plugin's fixture. The fixture is the
shared artefact; the parity assertions over it live in each plugin's own test
suite, which is **not** part of this published checkout (`.gitignore: tests/`).
The openclaw side enforces the same rule at
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
cron jobs running outside the gateway process. It also passes
`cron_deliver_env_var="CLAWCHAT_HOME_CHANNEL"`: Hermes 0.21+ pre-flights a cron
job's `deliver=` target and only accepts a plugin platform that names its
home-channel env var (without it, ClawChat cron jobs end `blocked_config`).
Older `PlatformEntry` builds reject unknown fields with `TypeError`, so
registration degrades one field at a time — both → without
`cron_deliver_env_var` → without both — and a host that knows the sender but
not the cron field keeps the sender.

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

Besides real inbound messages, three server signals produce **synthetic
turns** through the same `_handle_inbound` path (each `raw_message` carries
`synthetic: True` plus a discriminator): the owner activation bootstrap
(`bootstrap`, `greeting.load_activation_bootstrap_prompt`), the first message
to a newly added non-owner friend (`friend_greeting`,
`greeting.load_friend_greeting_prompt`; triggered by `friend.added`, gated by
the `friend_greeting` config flag, conversation id resolved via
`ClawChatApiClient.get_direct_conversation`, deduped by signal `event_id` in
the message ledger — see `docs/configuration.md`), and `permission_result`
receipts. Each runs on a tracked task set cancelled in `disconnect()`.

### Inbound dispatch is off the read loop

The connection's read loop is the only reader of `message.ack`, and
handling an inbound message can send a reply and wait for its ack: the
plugin's own confirm replies, and every command Hermes dispatches inline
while a session is busy (an approval `yes`, `/new`, `/stop`, a clarify
answer). So the read loop never awaits the adapter's `on_message`.
`message.send` / `message.reply` / `message.recall` frames are handed to a
**per-chat lane** (`ClawChatConnection._dispatch_to_lane`): one worker
task per `chat_id` handles that chat's frames strictly in arrival order,
one at a time, while acks and other chats keep flowing. A handler that
raises costs that frame only. Lanes survive a reconnect (a chat's order
holds across it) and are cancelled by `ClawChatConnection.stop()`, which
spares the lane that called it.

### Group turns run one at a time

A group is one shared Hermes session, so the group coalescer
(`group_message_coalescer.GroupMessageCoalescer`, `is_busy=`) holds a group's
next batch while that group's turn is running — tracked from the plugin's own
dispatch (`ClawChatAdapter._group_dispatching`) and then from the host base
adapter's `_active_sessions` guard — and flushes it as one batch once the
session is free. See [`./configuration.md`](./configuration.md#group-session-seeding-and-queueing).

### Reply streaming (experimental, opt-in, direct chats)

With `display.platforms.clawchat.streaming: true` Hermes sends a reply's first
chunk with a cursor and then edits it with the cumulative text, finalizing at
the end (twice, because the adapter sets `REQUIRES_EDIT_FINALIZE`); with it
`false` (what activation and the output presets write) Hermes sends the
finished reply once. By default (`extra.stream_replies: false`) the adapter
buffers those drafts and sends the reply once as a plain `message.reply`, and
drops a draft Hermes never finalizes, as Hermes itself abandons it on `/stop`
and `/new`. With `extra.stream_replies: true`, in a direct
chat the adapter relays each step as a Protocol-v2 stream —
`message.created`, `message.add` (cumulative `text` plus `delta`), and at the
finalizing edit `message.done` followed by the acked `message.reply` under the
same `message_id` — so the conversation keeps one message. Group replies are
buffered and sent whole either way. The holds, the `message.failed` cases and
the post-turn sweep of unfinished replies (opt-in only) are described in
[`./client-integration.md`](./client-integration.md) §8.0.

### Sediment turns before reset and compression

Hermes v0.12.0+ has no memory flush at compression or reset (upstream removed
`AIAgent.flush_memories` and the gateway's `_flush_memories_for_session`);
the hooks left at those moments do not fit: `on_session_reset` /
`on_session_finalize` fire after the session is gone, and
`MemoryProvider.on_pre_compress` only reaches the single external memory
provider an operator selects with `memory.provider`. The adapter therefore runs
its own no-reply turn in the conversation's session first
(`ClawChatAdapter._run_sediment_turn`): before a `/new` / `/reset` from
ClawChat, and after a turn whose request size (from the `post_api_request`
hook) nears the compression cap. Outbound frames to that chat are dropped until
the base adapter's `on_processing_complete` reports that very event finished
(the host's session guard stays up across queued turns, so the event identity,
not the guard, ends the suppression). See
[`./configuration.md`](./configuration.md#sediment-turns).

### Group exec approvals forwarded to the owner

When a dangerous command needs approval inside a **group**, Hermes calls
`send_exec_approval(chat_id=<group>, command=…, session_key=…,
description=…, metadata=…, allow_permanent=…, allow_session=…,
smart_denied=…)`. The three scope flags exist on hosts since v2026.7.20;
older hosts omit them and the defaults reproduce the previous behaviour.
The signature also takes `**kwargs`: a host flag the adapter does not
accept raises `TypeError`, which the host swallows and answers with a
plain-text fallback that carries no route, so the owner's reply would
resolve nothing. The prompt offers only the scopes the host allows (no
session/always choice for a Smart-DENY override, no always choice when
nothing can be permanently allowlisted), mirroring Hermes' own text
fallback.

The card is not posted to the group. It goes to the owner's direct chat
(the activation conversation), and every group shares that one chat, so
each forwarded card carries an **approval code**: one letter (`A`–`Z`
without `I`/`O`) followed by one digit (`2`–`9`), e.g. `K7`. A code is
unique among the owner's pending approvals and is not reused while its
approval is pending. Routes live in
`ClawChatAdapter._owner_approval_routes` as `owner_chat_id → code →
(session_key, group chat_id, expiry)`; the code is reserved before the
card is sent and released if the send fails.

Owner replies in that direct chat (`_handle_owner_forwarded_approval`;
codes are case-insensitive and may appear before or after the scope word):

| Reply | Pending group approvals | Result |
|---|---|---|
| `/approve <code>` / `/deny <code>` (also `/always`, `/cancel`, `session` / `always` scope words, `all`) | any | Resolves that code's session only. |
| bare `/approve` / `/deny` | exactly one | Resolves it (the single-approval UX is unchanged). |
| bare `/approve` / `/deny` | two or more | Nothing is resolved; the reply lists the pending codes and their groups. |
| code that is unknown, resolved, or expired | any (including none) | Nothing is resolved; the reply lists the pending codes. A code-shaped reply is never handed to the host, which would otherwise apply it to the direct chat's own session. |
| bare `/approve` / `/deny` | none | Not intercepted; the host's own `/approve` handler runs for the direct chat's session. |

While any group route is pending, a bare `/approve` in the owner's direct
chat is taken as a group decision, as before. Several approvals from the
**same** group session get distinct codes, but the host resolves a
session's queue oldest-first (it does not hand the adapter its
`request_id`), so the code selects the session, not the individual
request. `/deny <reason>` text is not relayed.

Routes are bounded by:

- **Resolution** — the code is removed once the host resolves it, or once
  a card button (`interaction.submit`) resolves the same session.
- **Host timeout** — at every forward and every owner reply, a route whose
  session the host no longer blocks on (`tools.approval.has_blocking_approval`
  is false: `approvals.timeout` elapsed, the run was interrupted, or the
  decision arrived another way) is dropped. The host timeout is
  configurable, so it is observed rather than mirrored.
- **TTL backstop** — `OWNER_APPROVAL_ROUTE_TTL_SECONDS` (1 hour) drops a
  route even if the host check is unavailable.
- **Dissolution** — `_evict_chat_state` drops a dissolved group's routes, or
  every route when the owner's direct chat itself is dissolved.
- **Capacity** — at most one route per code (192). When every code is
  pending the card is not sent and `send_exec_approval` fails; the host
  then sends its own plain-text prompt, which carries no code and records
  no route, so that approval must be answered once the backlog clears or
  it times out.

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

1. `hermes_cli.config.get_env_value_prefer_dotenv(...)` (if importable) — the
   profile's `.env`, then a scope-checked process-env fallback.
2. `$HERMES_HOME/.env` parsed directly (standalone CLI, no `hermes_cli`).
3. Process environment (`CLAWCHAT_*` vars) — last, so a value inherited from
   another profile's gateway process cannot win. See
   [`./configuration.md`](./configuration.md).
4. `platforms.clawchat.extra` from `config.yaml`.
5. Hard-coded defaults from the dataclass.

`$HERMES_HOME` resolution itself lives in `clawchat_gateway.hermes_home` —
exported value, else the platform-native default (`~/.hermes` on POSIX,
`%LOCALAPPDATA%\hermes` on Windows). Never re-derive it inline.

`__init__._clawchat_platform_config_with_home_extra` merges the on-disk
`config.yaml` extra block into sparse runtime `PlatformConfig` values so
Hermes v0.12 can load gateway config before user platform names are
registered. Explicit runtime values always win over the merged extra.

See [`./configuration.md`](./configuration.md) for the field-by-field
table.

## Host `MessageEvent` verification (message-recall open item)

Verified 2026-08-20 against Hermes Agent 0.20.0 (a local Hermes source
checkout at `tmp/hermes`, git commit `3aeff239bfc49a5e025eb05fb6fd3a724104a1d6`;
no `hermes` binary is installed on this machine, so the version was read from that checkout's
`pyproject.toml` — `[project] name = "hermes-agent"` / `version = "0.20.0"`
— rather than from `hermes --version`). The live SQLite state at
`$HOME/.hermes/clawchat.sqlite` and `$HOME/.hermes/clawchat/clawchat.sqlite`
was also inspected (`sqlite3 <db> ".tables"`); neither contains the host's
own `messages` table — both only hold this plugin's own `clawchat_messages`
ledger (`activations`, `connections`, `owner_profile`, `tool_calls`,
`liveware_sample`, `schema_migrations`, `clawchat_messages`) — so the
`messages` schema below is taken from the source checkout, not a live DB.

| Question | Answer |
|---|---|
| Does `gateway.platforms.base.MessageEvent` accept `message_id=`? | yes — it is a `@dataclass` (`gateway/platforms/base.py:2054`) whose fields include `text: str`, `message_type: MessageType = MessageType.TEXT`, `source: SessionSource = None`, `raw_message: Any = None`, `message_id: Optional[str] = None`, `platform_update_id: Optional[int] = None`, `media_urls: List[str] = field(default_factory=list)`, `media_types: List[str] = field(default_factory=list)`, `reply_to_message_id: Optional[str] = None`, `reply_to_text: Optional[str] = None`, plus further reply/prompt/skill/channel fields not relevant here. |
| Does `messages.platform_message_id` exist? | yes — `hermes_state_common.py`'s `CREATE TABLE IF NOT EXISTS messages (... platform_message_id TEXT, ...)` (verbatim column declaration, no inline `UNIQUE`). |
| Is it uniquely indexed? | **no.** The only index over it, created in `hermes_state_schema.py`'s `_init_schema`, is:<br>`CREATE INDEX IF NOT EXISTS idx_messages_platform_msg_id ON messages(session_id, platform_message_id) WHERE platform_message_id IS NOT NULL`<br>This is `CREATE INDEX`, not `CREATE UNIQUE INDEX` — it does not enforce uniqueness at all, let alone give a unique lookup keyed on `platform_message_id`. Separately (and this would matter even if it *were* unique): `platform_message_id` is not the leading column — `session_id` is — so a unique index here would still require supplying the host's internal `session_id` alongside `platform_message_id` to get the guarantee, and the adapter's `MessageEvent(...)` construction site (`clawchat_gateway/adapter.py`, method `_handle_inbound`) has no host `session_id` in scope at that point — it builds a ClawChat `source` (`chat_id`, `user_id`, `chat_name`, `chat_type`) via `self.build_source(...)`, not a Hermes-internal session id. Both gaps are independently sufficient to fail the criterion; the non-`UNIQUE` declaration alone already does. |

**Decision:** do not populate `message_id=`. Outcome B: `MessageEvent` accepts `message_id=`, but `messages.platform_message_id` exists without a unique index (`CREATE INDEX`, not `CREATE UNIQUE INDEX`) — a non-unique column cannot be the basis of a host-side soft delete, and passing a kwarg nothing consumes as a reliable key would invite the next reader to assume a linkage that does not exist.

This matters because it is the precondition for any future **host-side** soft
delete of a recalled message. Today the `message.recall` purge reaches only this
plugin's own `clawchat_messages` ledger; the host's conversation history is
explicitly out of scope (spec §9.8, "What is **not** attempted"). Nothing
about the column's schema may be asserted without re-running this verification.
