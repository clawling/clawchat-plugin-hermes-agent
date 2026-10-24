# Configuration reference

All settings are resolved by
`clawchat_gateway.config.ClawChatConfig.from_platform_config`. The
priority order is documented in [`./architecture.md`](./architecture.md):
`hermes_cli.config.get_env_value_prefer_dotenv` → `$HERMES_HOME/.env`
→ process env → `platforms.clawchat.extra` → dataclass default.

Process env is deliberately **last**. Hermes launches a named profile's gateway
as a child of a default-profile process with only an env *overlay*
(`HERMES_HOME`, `VIRTUAL_ENV`, `PYTHONPATH`), and its inherited-key scrub covers
a hardcoded first-party allow-list that `CLAWCHAT_*` can never join — so reading
the process env first made a second profile authenticate as the *first* profile's
agent and post into its home channel. Hermes states the contract in
`env_loader._clear_known_keys_missing_from_dotenv`: cross-profile credential
isolation happens at read time. Process env stays in the chain because env-only
deployments (a pod with credentials injected and no `.env`) have nowhere else to
put them.

`$HERMES_HOME` itself resolves through `clawchat_gateway.hermes_home`: the
exported value when set, else the platform-native default — `~/.hermes` on
POSIX, `%LOCALAPPDATA%\hermes` on Windows.

Credential tokens are the exception: at startup the runtime connection first
adopts the latest complete activation row in plugin SQLite
(`_load_startup_activation_credentials`) — activation and token rotation
persist there, so the row is the durable source of truth. Env-backed
`CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` bootstrap the no-row case only:
they are used to connect and are seeded into a SQLite row on the first token
refresh (§C.2). To make an agent that already has an activation row pick up
new env credentials, re-pair (the new activation row wins) or clear the row
first — either way, a restart of the agent process is required for the new
env credentials to take effect.

## Credentials (written by activation)

| Env var                              | `extra.*` key      | Default | Notes |
|--------------------------------------|--------------------|---------|-------|
| `CLAWCHAT_TOKEN`                     | —                  | —       | Required to connect to ClawChat. If absent, the adapter waits for activation without blocking gateway startup. Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_REFRESH_TOKEN`             | —                  | —       | Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_USER_ID`                   | `user_id`          | JWT `sub` claim from `CLAWCHAT_TOKEN` | ClawChat user id of the bot account. The token's `sub` claim is authoritative for the session the token creates: when the token carries `sub`, it **overrides** a disagreeing configured value (with a boot warning) and fills the value when none is configured. A wrong user_id (e.g. the owner's id) silently breaks self-echo detection — every owner message is dropped as the bot's own echo. |
| `CLAWCHAT_AGENT_ID`                  | `agent_id`         | JWT `aid` claim from `CLAWCHAT_TOKEN` | Set by activation. This is the REST agent record id (`agt_...`), distinct from owner metadata `agent_user_id` (`usr_...`). |
| `CLAWCHAT_OWNER_USER_ID`             | `owner_user_id`    | JWT `oid` claim from `CLAWCHAT_TOKEN` | Identifies the human owner of the agent account. When neither the env var nor `extra.owner_user_id` is set, it is derived from the token's `oid` claim (mirrors `agent_id`/`aid`). This keeps an agent connectable when a provisioner injects `CLAWCHAT_TOKEN`+`user_id` but omits the owner — without it the connection gate (`_has_connect_credentials`) leaves the agent stuck in `activation_wait`. |

Activation also stamps one non-credential identity key:

| Env var | `extra.*` key | Default | Notes |
|---------|---------------|---------|-------|
| —       | `profile`     | unset   | The Hermes profile name activation ran in (`activate._active_profile_name`). Read back on later boots to detect a `config.yaml` that belongs to a different profile. A config written before this key existed carries no stamp. See [`./activation.md`](./activation.md) (One profile, one agent) and [`./install.md`](./install.md). |

The token / refresh-token pair are stored in plugin SQLite as the
authoritative runtime credential record; `.env` holds the bootstrap copy
written by activation for the no-row case. The plugin never copies them into
`config.yaml`
(`activate.persist_activation` calls `extra.pop("token", None)` and
`extra.pop("refresh_token", None)`).

`CLAWCHAT_REFRESH_TOKEN` is no longer dormant: the plugin uses it to refresh
`CLAWCHAT_TOKEN` automatically before/at its 24h expiry and rewrites the rotated
pair to **both** `.env` and SQLite (see
[`./activation.md`](./activation.md#automatic-token-refresh--auto-logout)). On a
permanent refresh failure a SHA-256 fingerprint of the rejected refresh token
and rejection time are persisted to SQLite before either store is cleared.
Both keys become empty `.env` tombstones and the SQLite token columns are blanked;
clear failures are logged separately. The durable marker overrides every
credential source, including legacy `extra.token` / `extra.refresh_token`.
When a marker exists, empty refresh tokens and tokens matching that marker
cannot refresh or connect, including after restart. The connection stays in
`AUTH_FAILED` waiting for activation. Startup load and activation polling also
reject the latched access token, including access-token-only credentials rejected
by hello. Only adopting credentials with a different access token that pass the
refresh-fingerprint guard clears both latches and resumes connection. Without a
marker, access-token-only setups continue to connect unless their access token
is latched. Marker read failures preserve existing credentials, while the
in-memory fingerprint guard and access latch still apply. Fresh installs waiting
for activation retain their original state.

A non-200 refresh response is permanent only when its parseable JSON body has
integer `code:10003`. Every other non-200 response, including `code:400`, remains
transient and keeps credentials. Integer `code:400` is permanent with HTTP 200.

## Connection

| Env var                                              | `extra.*` key   | Default                              | Notes |
|------------------------------------------------------|-----------------|--------------------------------------|-------|
| `CLAWCHAT_BASE_URL`                                  | `base_url`      | `https://app.clawling.com`           | REST API base. Trailing slashes are stripped. |
| `CLAWCHAT_WEBSOCKET_URL` (or `CLAWCHAT_WS_URL`)      | `websocket_url` | `wss://app.clawling.com/ws`          | Activation records the env value when set, else derives one from `base_url` (`activate._derive_websocket_url`). |
| `CLAWCHAT_DEVICE_ID`                                 | —               | derived (see below)                  | **Stable device id for this agent.** When set, it is used verbatim (only sanitized to the allowed `[A-Za-z0-9_.:-]` charset and `hermes-` prefixed if not already). **Strongly recommended to set in any containerized / Kubernetes deployment** — see the durability note below. |

### Device id durability (deployment requirement)

The ClawChat server keeps a **per-device delivery cursor**. A stable device id
is therefore required for correct offline-message replay, push-badge accuracy,
and ack-aware inbox pruning. When `CLAWCHAT_DEVICE_ID` is **not** set,
`clawchat_gateway.device_id.get_device_id` falls back to a host fingerprint
(macOS `IOPlatformUUID` → Linux `/etc/machine-id` → hostname+MAC hash). In a
pod that fingerprint changes on every reschedule, which makes the server treat
each restart as a brand-new device:

- the new device's cursor starts at `0`, forcing a full inbox replay; and
- the old device's cursor becomes an **orphan** that pins the user's
  ack-aware prune retention window.

**Deployment requirement:** set `CLAWCHAT_DEVICE_ID` to a fixed, per-agent value
(e.g. derived from the agent's stable identity) in the pod spec / `.env` /
`config.yaml` so the device id survives restarts and rescheduling. Example pod
env:

```yaml
env:
  - name: CLAWCHAT_DEVICE_ID
    value: "hermes-agt-<stable-agent-id>"
```

**Agent-scoped since 0.14.0-86.** Without a pin, the fingerprint is suffixed
`-p<12 hex>` for a *named* profile, so two Hermes profiles on one host no
longer share a device id (the redeem safety gate and the plugin-report row are
keyed on device id alone). The default profile keeps the exact legacy value.
Nothing changes for an agent that is already paired: it keeps the id stored on
its `activations` row (or the token's `did`); a named profile paired before
that column existed keeps presenting the legacy host id, which is what its
session was minted with. Only a brand-new activation derives the new id.

`CLAWCHAT_DEVICE_ID` is read profile-first (`$HERMES_HOME/.env` before the
process env), because a named profile's gateway inherits the default profile's
exported value. The id itself is now **agent**-scoped — one per profile — not
host-scoped, so it matches the one-identity-per-profile invariant: see
[`./activation.md`](./activation.md) (One profile, one agent).

The server-assigned `resolved_device_id` from the `hello-ok` handshake is
recorded in plugin SQLite (`connections.resolved_device_id`) for diagnostics.
The id the plugin *presents* on connect resolves in order: the
`activations.device_id` persisted in plugin SQLite → the `did` claim of the
current access token → (a token is present but neither above resolved) the
legacy `legacy_host_device_id()` fingerprint, for a named profile paired
before device ids were persisted → the `get_device_id()` fingerprint (used
only for a truly unpaired process, then persisted to SQLite at first
connect). The first three sources are re-read on every boot, so an
already-paired agent keeps a stable device id across container recreation
even without the env pin — provided the SQLite database lives on a
persistent volume.

### Device id is also the token-refresh precondition

The plugin now automatically refreshes its 24h access token (see
[`./activation.md`](./activation.md#automatic-token-refresh--auto-logout)).
`POST /v1/auth/refresh` requires an `X-Device-Id` header equal to the
**connect-time** device id, and the server rejects the refresh outright when it
does not match. The connect-time id is captured in the `activations.device_id`
column at activation, so a pinned `CLAWCHAT_DEVICE_ID` keeps refresh working
across restarts.

If `CLAWCHAT_DEVICE_ID` is **unpinned** and the host fingerprint changes (e.g. a
pod reschedule), the refresh device id no longer matches the connect-time id, so
refresh fails with a **device mismatch** — which is a *permanent* refresh
failure that forces auto-logout and a manual re-pair. The plugin emits a boot
warning (`clawchat_gateway.device_id.warn_if_device_id_unpinned`) for exactly
this reason — but only when device-id resolution actually falls through to the
volatile fingerprint (no SQLite `activations.device_id`, no token `did` claim,
no env pin). An already-paired agent whose id is read back from SQLite or the
token boots without the warning.

**Deployment requirement (restated for refresh):** pin `CLAWCHAT_DEVICE_ID` to a
stable, per-agent `hermes-`-prefixed value in any containerized deployment.
Beyond the delivery-cursor correctness above, it is what lets unattended token
refresh survive a reschedule instead of silently logging the agent out.

## Rich interactions and display

| Env var                                | `extra.*` key                  | Default        |
|----------------------------------------|--------------------------------|----------------|
| —                                      | `enable_rich_interactions`     | `false`        |
| —                                      | `output_visibility`            | `"normal"`     |
| —                                      | `runtime_status_messages`      | `false`        |
| —                                      | `awareness_note`               | `false`        |
| —                                      | `friend_greeting`              | `true`         |
| —                                      | `liveware_sample`              | `true`         |
| —                                      | `stream_replies`               | `false`        |

`output_visibility` is the ClawChat visibility preset controlled by
`/clawchat-output minimal|normal|full`. `runtime_status_messages` controls
Hermes model/runtime status callbacks that do not have a dedicated Hermes
display key, for example provider retry, fallback, compression, or
empty-response notices. The `/clawchat-output` command keeps it aligned with the
selected preset: `false` for `minimal` and `normal`, `true` for `full`. It does
not control tool progress, command previews, long-running heartbeats, interim
assistant messages, approval prompts, or background-process notifications; use
the Hermes display settings below for those categories.

See [`./output-visibility.md`](./output-visibility.md) for the complete
`minimal`, `normal`, and `full` config mappings.

`awareness_note` controls one **consolidated, content-free** note delivered to
the owner's direct chat when a lightweight awareness signal arrives
(`friend.added`, `friend.removed`, `friend.profile_updated`, or any
`conversation.*` other than `conversation.dissolved`). It is debounced: while a
note is pending, further events do not schedule another. The default is `false`
— the events are still logged, just not surfaced to the owner. Moment-comment
notes (`moment.comment.created` / `moment.comment.replied`) are a separate,
per-event path and are **not** gated by this flag
(`adapter._emit_awareness_note` / `adapter._emit_moment_comment_note`).

`friend_greeting` controls whether the agent **speaks first** to a newly added
friend who is not its owner. On a `friend.added` signal whose `entity_id` is
not the owner, the adapter resolves the new direct conversation through
`POST /v1/conversations/direct` (the signal only carries the counterparty
`usr_…` id; the server created the conversation inside the accept transaction)
and runs one synthetic turn in it with the prompt from
`$HERMES_HOME/clawchat/friend-greeting.md`, falling back to the built-in
`greeting.FRIEND_GREETING_PROMPT`. Before that turn the adapter refreshes the
friend's profile (`_refresh_user_profile`, bounded by
`FRIEND_GREETING_PROFILE_TIMEOUT_SECONDS` = 5 s) and puts their nickname in the
turn's `sender_name`: a brand-new friend has never written, so nothing has
cached their profile yet and the turn would otherwise name their `usr_…` id. A
slow or failed lookup greets without the name. Both directions count — someone adding the
agent (auto-accepted by the `friend.accept` policy) and the agent's own request
being accepted. The owner's `friend.added` is skipped (the activation bootstrap
greets the owner). Dedupe is persisted in the message ledger keyed on the
signal's `event_id` (fallback `message_id`), so a reconnect replay never greets
twice; a failed conversation lookup is logged and dropped. The default is
`true`; set `false` to keep `friend.added` a pure awareness event
(`adapter._schedule_friend_greeting` / `adapter._dispatch_friend_greeting`).
This flag is independent of `awareness_note`.

Both `greeting.md` and `friend-greeting.md` (see
[the agent's own files](#the-agents-own-files) for where they live) are
**partial** overrides, not full replacements: whichever body they supply (or
the built-in fallback, if the file is absent/empty/unreadable) gets a trailing
`Reply in <Language>.` instruction appended — the override says what to say,
not which language to say it in.

That line is **conditional**. The language is resolved from the owner's
reported app locale, read off disk via `adapter._owner_locale()` through
`owner_language.resolve_owner_language_if_known` — which returns `None`, not
`en`, when no locale has been reported or the owner metadata has not landed
yet. A `None` language appends **no line at all**. Neither greeting dispatch
path may grow an await to chase the locale, and hard-asserting "Reply in
English." at an owner who may not speak English is worse than asserting
nothing: with no line the model infers the language from context. (The
Liveware Sample intro lookup is the opposite case and keeps its English
fallback — there a copy has to be chosen and `en` is the documented default.)

The friend-greeting turn uses the **owner's** resolved language even though its
recipient is the new friend, not the owner: friends are usually in the same
language circle, and the backend only exposes `locale` on the owner profile
endpoint, so no third-party locale is obtainable.

### The agent's own files

`greeting.md`, `friend-greeting.md` and `onboarding.json` belong to one agent,
and every Hermes profile is a separate agent, so they live in the profile:
**`$HERMES_HOME/clawchat/`** (`clawchat_gateway.agent_files`, resolved through
`hermes_home()`, which follows a multiplexing gateway's per-profile home).
They used to be read from `~/clawchat/`, which every profile on the host shares,
so one agent's greeting and onboarding facts (capability tier, wiki report id)
became every agent's.

| Profile | Read order |
|---|---|
| default | `$HERMES_HOME/clawchat/<file>`, then the legacy `~/clawchat/<file>` |
| named (`hermes -p <name>`) or a custom `HERMES_HOME` | `$HERMES_HOME/clawchat/<file>` only |

Only a missing file falls back; a present but empty `greeting.md` /
`friend-greeting.md` is a reset to the built-in prompt even when a legacy file
exists. The agent is told the absolute directory in every turn in its owner's
direct chat (a `## ClawChat Agent Files` section with `agent_files_dir`), so it
never has to build the path from `~`: Hermes may run the agent's shell with a
different `HOME` (in a container it is `$HERMES_HOME/home`).

`liveware_sample` controls the Liveware Sample demo-app auto-boot on first
activation. The default is `true`; set it to `false` explicitly to disable
auto-boot for an agent. See
[`./liveware-sample.md`](./liveware-sample.md) for the full trigger
conditions, lifecycle, and troubleshooting.

### Reply streaming (experimental)

`stream_replies` (default `false`) is **experimental: only enable it when every
ClawChat client your agent talks to renders Protocol-v2 §8 streaming**
(`message.created` / `message.add` / `message.done` / `message.failed`, see
[`./client-integration.md`](./client-integration.md) §8.0). It is the Hermes
counterpart of the OpenClaw plugin's `streamReplies`.

Streaming needs both switches:

| `extra.stream_replies` | `display.platforms.clawchat.streaming` | What the owner sees |
|------------------------|----------------------------------------|---------------------|
| `false` (default) | `false` (default) | Hermes sends each reply once, finished; the adapter sends it as one `message.reply`. |
| `false` | `true` | Hermes sends a first chunk and edits it; the adapter buffers those drafts and still sends one `message.reply` at the end. No streaming frame is ever sent. A reply Hermes never finalizes (`/stop`, `/new`) is dropped. |
| `true` | `false` | Same as the default: Hermes produces no drafts to stream. |
| `true` | `true` | A direct-chat reply streams and ends as one message; group replies are sent whole. |

Activation and every `/clawchat-output` preset write
`display.platforms.clawchat.streaming: false`, and plugin load never changes
it. Set both keys yourself, then restart the gateway:

```yaml
platforms:
  clawchat:
    extra:
      stream_replies: true
display:
  platforms:
    clawchat:
      streaming: true
```

A later `/clawchat-output` writes `streaming: false` again.

## Hermes display settings for ClawChat

Hermes display settings are read from `$HERMES_HOME/config.yaml`, not from
`platforms.clawchat.extra`. During activation, the ClawChat plugin overwrites
four global display settings so ClawChat starts quiet even if the prior Hermes
config used different values:

```yaml
display:
  busy_input_mode: queue
  busy_ack_enabled: false
  background_process_notifications: "off"
  tool_progress_command: false
```

> **`off` must be quoted.** `background_process_notifications` and
> `tool_progress` are *string* enums, and the plugin writes and compares the
> string `"off"` (`clawchat_gateway/activate.py`,
> `clawchat_gateway/output_visibility.py`). A bare `off` is parsed as the
> boolean `false` by YAML 1.1 loaders — a different value. Quote it whenever
> you hand-edit `config.yaml`.

Activation also writes the `normal` platform-scoped default block while leaving
the setting visible for the operator to edit:

```yaml
display:
  platforms:
    clawchat:
      tool_progress: "off"
      show_reasoning: false
      streaming: false
      interim_assistant_messages: true
      long_running_notifications: false
      busy_ack_detail: false
      cleanup_progress: false
```

Activation also writes this global Hermes agent setting on every activation so
Hermes does not emit gateway "still working" heartbeats or inactivity warnings
into ClawChat:

```yaml
agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
```

Activation fills missing keys in `display.platforms.clawchat` but preserves
existing values, so an operator can manually replace platform-scoped settings
and keep those values across later activations. The four global display
settings and agent settings above
are intentionally overwritten on each activation because Hermes does not support
ClawChat-only platform overrides for them. Activation does not write top-level
`streaming.*` settings.

Use these verified Hermes display keys when tuning ClawChat behavior:

| Setting | Global setting exists | Platform override exists | Recommended ClawChat location | Function | Example input | Result with the shown setting |
|---------|-----------------------|--------------------------|-------------------------------|----------|---------------|-------------------------------|
| `busy_input_mode` | yes | no | `display.busy_input_mode: queue` | Controls how Hermes handles a new message while the agent is already running. Valid values: `interrupt`, `queue`, `steer`. | The agent is running tests; the user sends "also check README". | `queue` keeps the current run alive and handles the new message as the next turn. |
| `busy_ack_enabled` | yes | no | `display.busy_ack_enabled: false` | Controls whether Hermes sends a busy acknowledgment message. | The agent is busy; the user sends "continue checking logs". | `false` suppresses the busy acknowledgment. The message still follows `busy_input_mode`. |
| `background_process_notifications` | yes | no | `display.background_process_notifications: "off"` | Controls notifications from background terminal processes. Valid values: `all`, `result`, `error`, `off`. | The user starts `/background run the deploy check`. | `off` sends no watcher updates for background process output or completion. |
| `tool_progress_command` | yes | no | `display.tool_progress_command: false` | Controls whether messaging users can use `/verbose` to cycle tool progress verbosity. | The user sends `/verbose` in ClawChat. | `false` prevents `/verbose` from enabling or changing tool progress display. |
| `tool_preview_length` | yes | no | `display.tool_preview_length: 0` | Controls maximum tool-call preview length. | Tool progress displays a long shell command. | `0` means no preview length limit; it does not hide previews. |
| `tool_progress` | yes | yes | `display.platforms.clawchat.tool_progress: "off"` | Controls tool progress messages. Valid values: `off`, `new`, `all`, `verbose`. | The agent runs `rg`, reads files, or executes commands. | `off` hides ClawChat tool progress messages and leaves only final assistant replies. |
| `show_reasoning` | yes | yes | `display.platforms.clawchat.show_reasoning: false` | Controls whether model reasoning/thinking is shown in replies. | The model produces reasoning for a complex question. | `false` hides reasoning in ClawChat replies. |
| `streaming` | yes | yes | `display.platforms.clawchat.streaming: false` | Controls whether Hermes writes a reply progressively (first chunk, then edits). | The agent writes a long reply. | `false` sends every reply whole. ClawChat only streams with `extra.stream_replies: true` as well; see [Reply streaming (experimental)](#reply-streaming-experimental). |
| `interim_assistant_messages` | yes | yes | `display.platforms.clawchat.interim_assistant_messages: true` | Controls natural mid-turn assistant messages sent separately from final replies. | The model says "I will inspect the config first" during a turn. | `true` allows that separate interim ClawChat message in the `normal` and `full` presets. |
| `long_running_notifications` | no | yes | `display.platforms.clawchat.long_running_notifications: false` plus `agent.gateway_notify_interval: 0` | Controls long-running "still working" heartbeat messages. | A task runs for several minutes. | `agent.gateway_notify_interval: 0` disables gateway heartbeat messages such as "Still working...". |
| `busy_ack_detail` | no | yes | `display.platforms.clawchat.busy_ack_detail: false` | Controls whether busy acknowledgments and long-running heartbeats include detailed runtime state. | The agent is busy and receives another message. | `false` keeps busy/heartbeat messages terse when those messages are enabled. |
| `cleanup_progress` | no | yes | `display.platforms.clawchat.cleanup_progress: false` | Controls automatic deletion of progress/status bubbles on platforms whose adapter supports deletion. | Tool progress or heartbeat messages were sent earlier in the turn. | `false` leaves those messages in place instead of auto-deleting them. |

`interim_assistant_messages` is explicitly `true` in the ClawChat override
block because activation defaults to the `normal` output visibility preset. The
remaining ClawChat platform display settings are `off` or `false`.
On Hermes versions that do not yet implement every key, unknown keys remain
visible in `config.yaml` for future compatibility and operator editing.

Three of those global settings also have official environment variables:

| Environment variable | Equivalent config key | Example value | Notes |
|----------------------|-----------------------|---------------|-------|
| `HERMES_GATEWAY_BUSY_INPUT_MODE` | `display.busy_input_mode` | `queue` | Gateway busy-input behavior. Valid values are `interrupt`, `queue`, and `steer`. |
| `HERMES_GATEWAY_BUSY_ACK_ENABLED` | `display.busy_ack_enabled` | `false` | Enables or disables busy acknowledgment messages. |
| `HERMES_BACKGROUND_NOTIFICATIONS` | `display.background_process_notifications` | `off` | Background-process notification mode. Valid values are `all`, `result`, `error`, and `off`. |

`display.tool_progress_command` does not have a confirmed official
environment variable. If an operator changes any of these four global settings
after activation, a later activation writes the ClawChat defaults again.

## Group behavior

| Env var                                | `extra.*` key            | Default   | Allowed values         |
|----------------------------------------|--------------------------|-----------|------------------------|
| `CLAWCHAT_GROUP_MODE`                  | `group_mode`             | `"all"`   | `"all"`, `"mention"`   |
| `CLAWCHAT_GROUP_COMMAND_MODE`          | `group_command_mode`     | `"owner"` | `"owner"`, `"all"`, `"off"` |
| —                                      | `groups.<chat_id>.group_mode`         | inherits `group_mode`         | per-group override          |
| —                                      | `groups.<chat_id>.group_command_mode` | inherits `group_command_mode` | per-group override          |
| —                                      | `groups["*"].group_mode`              | inherits `group_mode`         | wildcard group default      |
| —                                      | `groups["*"].group_command_mode`      | inherits `group_command_mode` | wildcard group default      |

`group_mode=all` makes every inbound group message eligible for a reply;
`group_mode=mention` requires a structured `@` mention. Mentions are read from
the union of the `mention` fragments and the `context.mentions` list, and the
reserved id `"all"` is the **`@everyone` sentinel** — it is not a real `usr_…`
id and the server never expands it per member, so a message carrying it counts
as mentioning this agent (see `docs/client-integration.md` §10.2).
`effective_group_mode` / `effective_group_command_mode` in
`clawchat_gateway.config` resolve the precedence (`chat_id` exact →
`"*"` → top-level).

### One session per group

Each group is **one Hermes session**, shared by everyone who speaks in it —
what one member said is in the same context another member talks to. Every
group message reaches Hermes with the same stand-in user id
(`adapter.GROUP_SHARED_SESSION_USER_ID`), so the host's session key is
`<group chat_id>` plus that constant whatever Hermes' own per-user setting is;
the real speaker of each message is in the turn's transcript lines and in
`ClawChat Group Message Metadata`. Direct chats are unchanged: one session per
conversation.

Releases up to 0.14.0-100 split a group into one session per speaker by
default. On upgrade each group starts a new shared session; the old
per-speaker sessions stay in Hermes' session store (searchable, not
continued), and the first turn of the new session is seeded with the group's
recent messages (below). A `group_sessions_per_user: true` an operator wrote into an existing
`config.yaml` (under `platforms.clawchat.extra`, top level or under `groups`) is
still honoured for the groups it covers, with one deprecation warning at
startup; remove it to switch those groups to a shared session. The top-level key
is read from `config.yaml` itself (`hermes_cli.config.read_raw_config`), not
from the platform config Hermes hands the adapter: Hermes fills
`group_sessions_per_user` into every platform's `extra` from its own
host-wide setting, which defaults to `true`, so its presence there says nothing
about the operator. Hermes' host-wide `group_sessions_per_user` does not apply to
ClawChat groups.

**Session names.** The session source the adapter builds
(`ClawChatAdapter._session_names`) names a group session after the group's
title (`group_title` in the group's metadata) and leaves `user_name` empty,
since the current speaker is not the shared session's user; a per-speaker
group session (above) also gets the speaker's nickname. A direct chat's
`chat_name` and `user_name` are the peer's nickname (the owner's in the owner's
chat). Hermes shows `chat_name` in its session list and resolves "send to
<name>" through it. With nothing cached yet, `chat_name` is the conversation
id, as before.

### Group session seeding and queueing

**Seeding.** The first turn of a group's shared session — the first group turn
after upgrading from per-speaker sessions, or the first turn ever in a group —
starts with the group's recent history from the plugin's own message ledger:
the last `rebuild-recent-messages` messages (factory 20) within
`rebuild-recent-chars` characters (factory 4000, newest kept first), oldest
first, each line `Name (usr_…): text` or `you: text` for the agent's own
replies, under a header saying it is context only. The name is the sender's
`nick_name`; when the hub sends the user id there, it comes from the agent's
cached profiles like in live batches (`owner.md` for the agent owner, the
group's metadata for the group owner, `users/<id>.md` for anyone else), and the
line shows the bare id only when no name is known. Slash commands (`/new`,
`@agent /approve`, …) are left out of the seed and of the catch-up below: they
steer the gateway and are not part of the conversation
(`ClawChatAdapter._group_context_for_turn`). It goes into the turn's user
message, so it stays in the session. It happens once per group: the
`group_shared_sessions` table in plugin SQLite records groups whose shared
session was started. A later `/new` starts an empty session that is not
re-seeded (the agent's notes still come with every turn).

After that, an @-mention adds only the group messages the session has not been
given since its last turn — for example messages a mention-only group held
back — never the agent's own replies and never the messages of the batch
itself. They are bounded by `delta-budget-chars` characters (factory 8000,
newest kept first), not by a count; when some do not fit, one closing line says
how many were left out and where to look (ask in the group, or the group's
note), and they count as delivered so a later mention does not bring them back
out of order. The adapter remembers delivered message ids in memory, so the
first mention after a restart may repeat up to that budget of messages. Slash
commands and synthetic turns get no history. A legacy per-speaker group keeps
the older behaviour: the 10 messages before an @-mention, without speakers.

**Queueing.** A group's turns run one at a time. While a group's turn is
running — from the moment the plugin hands a batch to Hermes until Hermes'
session guard for that group is released — the group's next batch is not sent:
the coalescer keeps adding to it, and as soon as the session is free everything
that arrived goes out as one batch, in order, with its own metadata. The
plugin does this itself instead of relying on Hermes' busy handling, which
depends on `display.busy_input_mode` and on the host version (an interrupt, a
merge under the first queued message's metadata, or on old hosts an overwrite
of the queued slot). Slash commands are not batched, so `/stop` and `/new` still
reach a busy group at once.

### Owner-controlled per-group settings and batching

The owner (or an orchestrator agent) can set three values per group on the
server: `muted`, `reply_mode` (`"all"` / `"mention"`) and
`batch_delay_seconds` (1–3600). The plugin reads them with
`GET /v1/agents/me/group-settings` at startup, after every reconnect, and on
every `agent.config.changed` `notify.signal`. The read returns only groups with
a stored row; a group that is absent uses the server defaults (not muted,
reply mode from the static `group_mode` above, 10 s delay). A row that omits
`batch_delay_seconds` also gets 10 s. A backend row wins over the static
`group_mode`.

Group messages are gated twice:

1. **On arrival**, after waiting (at most 5 s) for any in-flight settings read,
   in this order (`agent-protocol.md` §3.3): a muted group stops here (slash
   commands included); then a Hermes slash command is judged by
   `group_command_mode` and, if allowed, dispatched straight to Hermes —
   **before** the reply-mode gate, so `/new` and `/compress` work in a
   mention-only group too; then, in mention mode, a message that does not
   mention this agent (own id or `"all"`) stops here. The message is still
   recorded. A command may open with @-mentions (`@agent /new`): the leading
   mention fragments are stripped and Hermes receives the bare `/new`. It only
   counts as this agent's command when those mentions include this agent or
   `"all"` — `@other-agent /new` is the other agent's.
   **The owner's command is answered in the owner's direct chat.** Hermes
   still runs it on the group's session; the plugin delivers what Hermes
   sends back for it (every message of it) to the owner's activation chat,
   each headed `From group "<group title>": <command>`, and reacts ✅ on the
   command in the group once the first one is delivered — whether the
   command succeeded or not. Hermes' confirm before `/new`, `/reset`,
   `/undo` … is part of that receipt: it is asked in the owner's direct chat,
   and the owner's bare `/approve`, `/always` or `/cancel` there answers it
   for the group's session. Not taken for it: plain words (they stay
   conversation), a code-bearing `/approve <code>` (a forwarded
   dangerous-command approval), and any reply while a dangerous-command
   approval blocks the direct chat's own session; another slash command
   drops the pending confirm. Answered in the group as before: a non-owner's
   command (`group_command_mode` `all`), a confirm/approval reply
   (`/approve`, `/deny`, `/cancel` …), a command that runs an agent turn
   (`/plan`, `/learn`, `/init`, `/blueprint`, `/btw`, `/background`,
   `/queue`, `/steer`, `/moa`, `/goal`, `/retry`), and — with a warning in the log — any
   reply that cannot reach the owner's direct chat (no activation chat stored,
   or the send failed).
2. **When the batch flushes.** Non-mention messages are coalesced per group:
   the batch runs after `batch_delay_seconds` of quiet, or after
   `max(30 s, batch_delay_seconds)` at most; a message that mentions the agent
   flushes it at once. Just before the batch reaches the model, the plugin
   waits again for any in-flight settings read and re-checks: if the group is
   now muted the batch is dropped, and if it is now mention-only the batch runs
   only when at least one of its messages mentioned the agent. So muting the
   agent, or switching the group to mention-only, also silences messages that
   were already queued.

## Session and note keys

These keys share their names and meaning with the other ClawChat agent
channels, so they are hyphenated and read from `platforms.clawchat.extra`.
Each has a factory value and a safe range in `clawchat_gateway.config`
(`SESSION_INT_KEYS`): a value outside the range is clamped to the nearest
bound, one that does not parse falls back to the factory value. They are read
when the adapter starts, so a change needs a gateway restart.

| `extra.*` key | Default | Range | What it controls |
|---------------|---------|-------|------------------|
| `note-cap-user` | `1500` | 300–6000 | Characters of one person's note (`users/<id>.md` or `owner.md`) shown in a turn. |
| `note-cap-group` | `2000` | 300–8000 | Characters of the group's note (`groups/<id>.md`) shown in a group turn. |
| `note-cap-turn` | `4000` | 1000–16000 | Characters of all notes shown in one turn together. |
| `rebuild-recent-messages` | `20` | 5–100 | Recent group messages that seed a group's new shared session. |
| `rebuild-recent-chars` | `4000` | 1000–32000 | Character budget of that seed. |
| `delta-budget-chars` | `8000` | 2000–32000 | Character budget of the group messages an @-mention adds that the shared session has not seen since its last turn (newest kept; the rest announced in one line). |
| `session-cap-tokens` | `150000` | 50000–1000000 | Context size at which a conversation's session is compressed; filled into Hermes' `compression.threshold_tokens` when that is missing (below). |
| `sediment-margin-tokens` | `10000` | 2000–50000 | How far below the point Hermes compresses at (the lower of the cap and its ratio threshold) a turn has to reach for a sediment turn to run before compression. |
| `sediment-on-compact` | `on` | `on` / `off` | Run a sediment turn before compression. |
| `sediment-on-reset` | `on` | `on` / `off` | Run a sediment turn before `/new` / `/reset`. |

Keys of the shared set that the Hermes plugin does **not** read, and why:

| Key | Why not here |
|-----|--------------|
| `idle-reset-dm`, `idle-reset-group`, `daily-reset` | Resetting a Hermes session by time is the host's job. Hermes up to 0.21.0 does it itself (`session_reset`, by default daily at 04:00 or after 24 h idle, with no plugin hook before it); Hermes 0.21.1 dropped time-based resets entirely, and a session is then bounded by compression at `session-cap-tokens`. A plugin-driven reset would have to go through the host's confirm-guarded `/new`. |
| `archive-muted-groups` | The plugin's message ledger records muted groups' messages anyway. |
| `digest-every-messages` | Applies to channels without a host session; every Hermes conversation has one. |

### Sediment turns

Compression and `/new` drop what a conversation established from the agent's
working context. Hermes itself no longer saves anything at those moments: its
pre-compression / pre-reset memory flush was removed upstream before v0.12.0
in favour of the periodic background memory review, which the plugin turns off
(`memory.nudge_interval: 0`, see [Hermes built-in memory](#hermes-built-in-memory))
because it can only write the single-user `USER.md` / `MEMORY.md`. So the
plugin runs its own **sediment turn** in the conversation's session just before
either happens:

- **Before `/new` / `/reset`** sent from ClawChat (`sediment-on-reset`): the
  sediment turn runs and finishes first, then the command goes to Hermes (whose
  confirmation prompt, if enabled, follows). Skipped when the session is busy —
  `/new` must stay an immediate way out of a stuck turn — and when nothing was
  said since the last reset.
- **Before compression** (`sediment-on-compact`): after a turn whose last model
  request reached `sediment-margin-tokens` below the point Hermes will compress
  this conversation at, a sediment turn runs as soon as the conversation is idle,
  once per Hermes session id.

  Hermes compresses at the lower of `compression.threshold_tokens` (read by
  Hermes 0.20.1 and later) and its ratio threshold `compression.threshold`
  (default 0.5 of the model's context window minus the output reservation;
  raised to 0.75 for windows under 512 K). Plugin load and activation fill
  `compression.threshold_tokens` with `session-cap-tokens` when the key is
  missing (the key is global, so it applies to the host's other platforms too;
  set it yourself to keep a different value). The plugin records each
  conversation's request size and context window (`post_api_request`:
  `usage`, `context_length`) and output reservation (`pre_api_request`:
  `max_tokens`), and computes the same lower point with Hermes' own
  `ContextCompressor` threshold maths when it can import it (the same rule
  otherwise). So on a 128 K window the sediment point is 0.75 × 128000 − 10000
  = 86000, not `session-cap-tokens − sediment-margin-tokens` = 140000. A
  per-model ratio override in Hermes is not seen; until a conversation's
  window is known (the first request of a gateway process), the cap alone
  decides.

A sediment turn is a synthetic message in the same session telling the agent to
read and then append to only this conversation's notes: in a direct chat the
peer's `users/<id>.md` (or `owner.md` for the owner); in a group the group's
`groups/<id>.md`, `users/<id>.md` of up to 10 recent speakers and `owner.md`
(only facts about the owner said in the group).
In a shared group session the turn also carries the group messages the session
has not seen yet (within `delta-budget-chars`, the same ones a mention would add), so a request
made since the agent's last turn — in a mention-only group, one that did not
mention the agent — is in front of it. It is told to skip anything someone in
the conversation asked it not to remember or to forget, not to use Hermes'
`memory` tool, not to send messages, and to answer with the no-reply token.
Which facts fall under such a request, and keeping to the listed notes, are
the model's judgement steered by that prompt: the plugin does not filter what
a sediment turn writes. What code does hold is the read scope of the memory
tools, the same as in any turn of that conversation.
Whatever it does send to that chat — text, edits, media, typing — is dropped
until Hermes reports the turn finished (`on_processing_complete`); other chats
are unaffected. A group's next batch waits for it like for any group turn. The
sediment message and its answer stay in the session's history.

With `display.busy_input_mode: interrupt` (not the ClawChat default), a direct
message sent during a sediment turn can be folded into that turn by newer
Hermes versions and its answer dropped with it; the default `queue` runs it
afterwards.

See [`./reference/prompt-injection.md`](./reference/prompt-injection.md) for
where the notes appear and how they are cut.

## Allowlist / home channel (read by Hermes platform registry)

| Env var                              | `extra.*` key             | Default        | Notes |
|--------------------------------------|---------------------------|----------------|-------|
| `CLAWCHAT_ALLOWED_USERS`             | —                         | unset          | Hermes-level user allowlist (passed through `register_platform(allowed_users_env=...)`). |
| `CLAWCHAT_ALLOW_ALL_USERS`           | —                         | `"true"`       | `configure_clawchat_allow_all` writes this to `.env` on plugin load so ClawChat users are allowed by default. Slash commands in direct chats are still owner-gated by the plugin (below). |
| `CLAWCHAT_HOME_CHANNEL`              | —                         | unset          | When set, enables the plugin's home-channel mode (`env_enablement_fn`). |
| `CLAWCHAT_HOME_CHANNEL_NAME`         | —                         | `"ClawChat"`   | Display name passed to the home channel descriptor. |
| `CLAWCHAT_HOME_CHANNEL_THREAD_ID`    | —                         | unset          | Optional thread id added to the home descriptor. |

Activation sets `CLAWCHAT_HOME_CHANNEL` to the conversation id returned
by `agents-connect` and `CLAWCHAT_HOME_CHANNEL_NAME` to `ClawChat`. The plugin
registers `CLAWCHAT_HOME_CHANNEL` as the platform's `cron_deliver_env_var`, which
Hermes' cron pre-flight requires before it accepts `deliver=clawchat`.

### Slash commands in direct chats

The plugin gates Hermes slash commands in direct chats.
See [slash commands in direct chats](tool-grants.md#slash-commands-in-direct-chats) for the non-owner allow-list, confirmation handling and gateway-control boundary.

### Host tools in turns the owner did not start

| `extra.*` key          | Default | Values             |
|------------------------|---------|--------------------|
| `non-owner-host-tools` | `off`   | `off` / `approve`  |
| `tool-grants` | `on` | `on` / `off` |
| `public-tools` | unset | Array of exact tool names; see [Public tools](#public-tools) |

Non-owner turns use restricted host tools by default.
Per-person rules and the owner's configuration determine which calls may proceed.
See [non-owner boundaries](non-owner-boundaries.md#host-tools-in-turns-the-owner-did-not-start) for enforcement and host compatibility, and [tool grants](tool-grants.md) for owner operations.

### Per-person tool grants

The owner manages per-person access from the activation direct chat.
Rules select tools and an access mode.
The [owner guide](tool-grants.md#per-person-tool-grants) covers selectors, precedence, confirmation and management.

#### Public tools

`public-tools` configures additions to non-owner defaults.
It is managed through configuration.
See [Public tools](tool-grants.md#public-tools) for validation, exclusions and the interaction with per-person rules.

#### Selectors and capabilities

See [selectors and capabilities](tool-grants.md#selectors-and-capabilities) for selector syntax and the code-owned capability mapping.

#### Rule resolution and all tools

See [rule resolution and all tools](tool-grants.md#rule-resolution-and-all-tools) for precedence and batch intersection.

#### Confirmation and per-call approval

See [confirmation and per-call approval](tool-grants.md#confirmation-and-per-call-approval) for cards, warnings and approval requirements.

#### Management and turn snapshots

See [owner management](tool-grants.md#management-and-turn-snapshots) and [execution snapshots](non-owner-boundaries.md#management-and-turn-snapshots).

#### Local attachments

Replies and file tools share a local attachment policy.
The policy checks both the turn's authority and the file being delivered.
See [Local attachments](non-owner-boundaries.md#local-attachments) for Files grants, artifacts and file identity checks.

#### Narrow-tool artifact contract

See the [narrow-tool artifact contract](non-owner-boundaries.md#narrow-tool-artifact-contract) for structured completion declarations.

#### Non-owner hints and refusal reasons

See [hints and refusal reasons](tool-grants.md#non-owner-hints-and-refusal-reasons) for the caller guidance and reason codes.

#### Cross-session routing and known limits

Cross-session execution and cross-chat message delivery have separate boundaries.
A tool grant is evaluated together with routing and resource checks.
See [routing and known limits](non-owner-boundaries.md#cross-session-routing-and-known-limits) for the audited routes and host limitations.

#### Storage upgrade

The grant database has upgrade compatibility requirements.
Existing rules and requests are preserved by the migrations.
Read [Storage upgrade](tool-grants.md#storage-upgrade) before changing plugin versions or sharing a database.

#### Guest access requests and resumption

See [guest access requests](tool-grants.md#guest-access-requests-and-resumption) for owner decisions and [wake and notice delivery](non-owner-boundaries.md#guest-access-requests-and-resumption) for resumption.

#### Spontaneous turns and origin inheritance

Host-generated turns require trusted attribution to inherit authority.
Owner review forks have additional host compatibility requirements.
See [Spontaneous turns and origin inheritance](non-owner-boundaries.md#spontaneous-turns-and-origin-inheritance) for accepted sources, limits and internal APIs.

## Reconnect, heartbeat, ack

| `extra.*` key                          | Default        |
|----------------------------------------|----------------|
| `reconnect_initial_delay_ms`           | `500`          |
| `reconnect_max_delay_ms`               | `15000`        |
| `reconnect_jitter_ratio`               | `0.3`          |
| `reconnect_max_retries`                | `inf`          |
| `heartbeat_interval_ms`                | `20000`        |
| `heartbeat_timeout_ms`                 | `10000`        |
| `ack_timeout_ms`                       | `15000`        |
| `ack_auto_resend_on_timeout`           | `false`        |

`ack_auto_resend_on_timeout` is **parsed but not implemented.** It is read into
`ClawChatConfig` (`config.py`) and nothing consumes the field, so setting it to
`true` changes no behaviour today. Do not rely on it for delivery guarantees.

These reconnect values are **fixed constants, not `extra.*` keys**
(`clawchat_gateway/connection.py`):

| Constant                          | Value   | Purpose |
|-----------------------------------|---------|---------|
| `BACKOFF_RESET_AFTER_SECONDS`     | `5.0`   | READY time after which the exponential backoff resets to `reconnect_initial_delay_ms`. **Not applied when the connection ended in a takeover** — see below. |
| `TAKEOVER_BACKOFF_FLOOR_SECONDS`  | `60.0`  | Minimum wait before reconnecting after msghub closed the socket with `4001` (a newer session for the same `(user_id, device_id)` took over). Doubles per consecutive eviction. |
| `TAKEOVER_BACKOFF_MAX_SECONDS`    | `600.0` | Ceiling for that escalation. |
| `INSTANCE_LOCK_RETRY_SECONDS`    | `30.0`  | Retry a host-local lock while in STANDBY; these waits do not consume `reconnect_max_retries`. |
| `INSTANCE_LOCK_REMINDER_SECONDS` | `600.0` | Minimum interval between WARNING reminders while a lock remains held elsewhere. |

The takeover floor exists because mutual evictions can occur farther apart
than `reconnect_max_delay_ms` (15 s), so ordinary exponential backoff alone
cannot reliably break a loop between two instances sharing a device id.
See `docs/client-integration.md` §3.6.

### Host-local single-instance locks

Before loading activation credentials or refreshing/connecting, a canonical
gateway acquires `<profile_home>/clawchat/gateway.lock`. Once `user_id` is known,
it also acquires `<hermes_root>/clawchat-locks/<sha256(user_id)[:16]>.lock`.
`profile_home` follows Hermes' per-profile home override under multiplexing;
`hermes_root` is the root owning `profiles/`, resolved by
`profile_collision.hermes_root()`. The identity lock is shared across profiles
under that root, including two processes bound to the same agent. Changing
activation identity releases the old identity lock before acquiring the new one.
Neither device IDs nor tokens, cursors, or stored activation data change.

Contention sets `ConnectionState.STANDBY` (value `"standby"`): no WebSocket
connection or token refresh occurs. Hermes counts the adapter as started, so its
watcher does not repeatedly rebuild it. `instance_lock_standby` logs one ERROR
with lock kind/path, holder pid/hostname/started_at, and retry interval, followed
by WARNING reminders at most every ten minutes. On takeover,
`instance_lock_acquired` logs INFO with `waited_seconds`. For diagnostics,
`connection.instance_lock_status()` returns the waiting lock and holder details.

Read the lock file as JSON to identify the holder. Records contain only pid,
hostname, UTC start time, kind, profile home and plugin version; the identity
filename hashes the user ID and the record contains no tokens or clear user ID.
New directories use mode `0700`, lock files `0600`. The OS releases ownership
when the holder exits, including after `SIGKILL` (`kill -9`); a standby gateway
retries within 30 seconds, then resumes normal activation/startup refresh/connect.
Shutdown cancels standby promptly. In-process replacement retains ownership
across the old supervisor's stop; an in-flight HTTP worker retains its own
reference until it finishes.

Leftover lock files are harmless: ownership comes from the kernel lock, not
file existence or JSON contents. **Do not delete lock files while a gateway
runs**: another process could then lock a different inode at the same path.
POSIX uses nonblocking `flock`; Windows locks one byte at a far offset so holder
JSON remains readable. Unsupported locking (for example a network filesystem
without lock support, or an unwritable path) logs one WARNING per path and
proceeds without that lock. This safeguard is host-local: different OS users
with separate `HERMES_HOME` roots do not share the identity lock, and separate
hosts use installation-conflict handling when supported, with protocol takeover
backoff as the legacy fallback. Standalone senders use a sibling connect device ID and are exempt from both locks.

### Installation conflicts and recovery

Canonical connections report an `install_id` and `capabilities.install_conflict: true`.
The ID is the first 24 hex characters of SHA-256 over a random seed, the existing
host device fingerprint, hostname, and resolved absolute profile home. Only the
hash is sent. `<profile_home>/clawchat/install_seed` is published once using an
atomic hard link from a fully written, fsynced temporary file with mode `0600`;
concurrent creation never replaces an existing seed. It survives restarts and may
travel with a copied profile. If hard links are unavailable, installation identity
is omitted rather than publishing a partial seed.
Changing the machine, hostname, or profile directory produces a different ID.
If the seed cannot be read or written, or is malformed, a WARNING is logged once
and both fields are omitted, preserving legacy `4001` handling. Standalone sibling
connections omit both fields.

When two different installations use the same agent/device, a supporting hub sends
terminal close **`4010`** to the displaced installation, before or after `hello-ok`.
The plugin enters `ConnectionState.SUPERSEDED`: the process stays alive, retains
its instance locks, and stops reconnecting and starting new token refreshes. The
HTTP worker rechecks supersession before sending. A successful rotation already
in flight is still saved to both credential stores (if WS teardown cancels the
credential watcher meanwhile, the save completes before the cancellation is
honoured and a later reclaim re-reads the stored pair); it never schedules a
reconnect while superseded. The token remains valid and credentials are preserved. State is atomically written with mode `0600`
to `<profile_home>/clawchat/install_superseded.json`, including agent/device/installation
IDs, UTC time, optional winner version, and owner-notice outcome. Creation, owner
notice updates, and reclaim are serialized under a process-local lock plus the
persistent `<profile_home>/clawchat/install_superseded.lock` (mode `0600`). POSIX
uses `flock`; Windows uses a byte-range lock. The lock file is never removed.
Unsupported locking or an inaccessible lock file reports one WARNING per path and
refuses state changes; no mutation proceeds without the cross-process lock.
Contention retries for up to 120 seconds and then fails without performing an
unlocked update. A failed event creation blocks locally without sending a notice;
a failed reclaim reports an error and the CLI returns a nonzero exit code.

Failed event creation is cached only in this process's memory to block connection
and refresh. Cached events are never written back to disk, even after storage
recovers or another identity's stale state remains on disk. Owner-notice results
only update the matching disk event; they cannot persist a memory-only event.
A failed result update logs a WARNING and is never retried.

When a connection first observes `4010` it reads a snapshot of
`<profile_home>/clawchat/install_reclaim_generation`. Every reclaim first
atomically replaces this generation file (mode `0600`) under the state lock, even
when no superseded state exists on disk or in the reclaiming process; if that
fails, the reclaim fails with disk and memory state unchanged (restarting the
gateway drops memory-only state). Event creation compares the generation again
under the state lock: if a reclaim completed after the snapshot, the event is not
recorded, no notice is sent, and the connection proceeds as reclaimed. A waiting
process checks every 30 seconds: only a readable, present generation different
from its readable snapshot releases its cached event; this check never writes
state or requires the state file lock. An absent generation file represents the
initial generation, a file that disappears later proves nothing, and an
unreadable file is unknown and cannot establish that a reclaim occurred (the
event is then recorded and the installation stays stopped: fail-closed).
On restart, a matching (or unreadable) state file blocks connection before startup
refresh, holding only the profile lock; a different agent identity ignores it.
If configuration has no user ID yet, a read-only SQLite identity lookup supplies
that filter without loading credentials or changing startup order. A failed
lookup keeps the fail-closed behavior.
A live `4010` retains any identity lock already held. Without superseded state,
an unpaired gateway keeps its existing startup path and takes no identity lock
until connection credentials are available.
An ERROR log includes reclaim instructions; reminders are at most every ten minutes.

For each live `4010` event the plugin attempts one owner DM through a standalone
sibling device, with a total timeout and no token refresh. The durable `owner_notice`
records `pending`, `sent` (message ID), `failed` (error type), or `skipped` (no owner
conversation). When available, the notice includes the other installation's plugin
version, limited to 64 printable characters. Failure and restart never retry the
notice. Reclaim cancels pending notices on the connection's event loop; external
reclaim cancels them when the standby loop detects the cleared state.
Because the standby poll runs every 30 seconds while a notice may take up to its
total timeout, the notice also re-checks right before connecting and again right
before sending that its event is still on disk; after any reclaim it is not
sent. Cancelled
notices do not write a result. Other standalone sends return an error while
superseded.

Run `hermes clawchat status` to inspect the short install ID, superseded time,
winner version, notice result, and instance-lock status when available in this
process. Status filters superseded state using the configured user ID, falling
back to a read-only SQLite activation identity lookup. It reports `identity` as
the first eight characters or `unknown`; an unknown identity keeps the fail-closed
view. A different identity's residual state reports `is_superseded: false` and
`stale_state_for_other_identity: true`. A separate CLI process reports lock state
as `unknown` and includes the last holder record; that persisted record alone
does not prove a running holder. Run `hermes clawchat reclaim` **on the installation
you want to use** to clear the state. A running gateway checks
within 30 seconds; an offline gateway connects on its next start. This can displace
the other installation. `/clawchat-reclaim` in local Hermes also wakes this process's
waiting connections immediately. Successful activation, including `--repair` and
`--new-account`, clears the state too. If that cleanup fails, activation still
succeeds and logs a warning to retry `hermes clawchat reclaim`. The compatibility
`clawchat_cli.py` accepts both `status` and `reclaim`. Status is read-only: an absent
or invalid seed produces no install ID and creates no seed, state or lock file,
and the plugin database is opened read-only without initialization or
migration (SQLite may still maintain its own `-wal` / `-shm` side files for
an existing WAL database).
If logs show `cannot persist superseded installation`, run
`hermes clawchat reclaim` at any time on this installation. Even a response saying
there is no disk record advances the generation; a waiting gateway with a readable
initial and current generation releases its memory-only state and reconnects
within 30 seconds. If the initial generation was unreadable, or the current
generation remains unreadable, restart the gateway (memory-only state does not
survive a restart); `/clawchat-reclaim` in that Hermes process also works once
the generation file is writable again. Process-local reclaim clears its cache directly, and a
restart loses memory-only state; without a disk state, startup connects normally.

## Typing

| `extra.*` key                          | Default        |
|----------------------------------------|----------------|
| `typing_max_continuous_seconds`        | `900.0`        |

While a reply is in progress Hermes refreshes the typing indicator every 2 s,
and the adapter sends each refresh as `typing.update{is_typing:true}`; only
calls closer together than 1.5 s (`TYPING_REFRESH_SECONDS`) are collapsed. A
receiver keeps the indicator lit for a few seconds after each `true` (the
reference client: 6 s), so this keeps it lit without gaps. `is_typing:false`
goes out when the turn ends and, for any indicator still lit, on graceful
shutdown. A sediment turn sends no typing at all. The cadence is not
configurable: the throttle must stay below the host's 2 s refresh, or every
other refresh is dropped.

`typing_max_continuous_seconds` stops emitting the typing indicator after this
many seconds of continuous typing on one conversation — a safety net against a
leaked upstream keepalive that never sends a matching `stop_typing`. Tripping
the limit only suppresses the typing indicator; it never interrupts an
in-flight reply. The window resets on `stop_typing` and restarts on the next
`send_typing` call for that conversation.

The default is deliberately well above the longest expected single reply: if it
trips during a legitimate long agent run, the user's "typing…" indicator
disappears while the reply is still being generated. Lower it only if your
agents always reply quickly and you want a tighter bound on a leaked keepalive.

A value that fails to parse as a number (missing/blank, `"abc"`, etc.) falls
back to the `900.0` default rather than raising — it never takes down platform
validation. `0` or a negative value also falls back to the default: there is
no "disable the TTL" sentinel.

## Dissolved-conversation eviction

When the server sends `notify.signal{type: "conversation.dissolved"}` for a
conversation, the plugin marks that `chat_id` dead in an in-memory, bounded
(512-entry, FIFO) set and immediately drops all per-chat state it was
tracking for it (typing state, inbound rate-limit window, known chat type,
active runs, etc.). While a chat is marked dead:

- inbound messages for it are dropped before reaching the LLM/business
  pipeline;
- `send_typing`/`stop_typing` uplinks for it are suppressed.

The dead-chat set is **not durable** — a process restart clears it, so a
dissolved conversation looks live again until the server re-delivers the
dissolve signal (or another message on it is naturally rejected server-side).
It is not a substitute for `typing_max_continuous_seconds`, which is the
actual backstop against a leaked keepalive that never sends a
`conversation.dissolved` signal at all.

## Media

| Env var                                | `extra.*` key         | Default                  | Notes |
|----------------------------------------|-----------------------|--------------------------|-------|
| `CLAWCHAT_MEDIA_BASE_URL`              | `media_base_url`      | `""`                     | Overrides the host used for `/media/upload` and media downloads. When empty, the base is derived from `websocket_url` / `base_url` (`media_runtime.derive_base_url`). Trailing slashes are stripped. Set it when media is served from a different origin than the REST API. |
| `CLAWCHAT_MEDIA_LOCAL_ROOTS`           | `media_local_roots`   | `()`                     | OS-pathsep-separated list (env) or array (extra). **Legacy / no longer restricting.** See below. |
| —                                      | `media_download_dir`  | `/tmp/clawchat-media`    | Where inbound media gets staged. |
| `CLAWCHAT_MEDIA_ALLOW_HTTP` | `media_allow_http` | `false` | Also allow HTTP for inbound downloads, including redirects. HTTPS is always allowed. |
| `CLAWCHAT_MEDIA_ALLOW_PRIVATE_NETWORK` | `media_allow_private_network` | `false` | Skip inbound public-address checks, including the localhost hostname restriction. For local development / testing only; scheme checks still apply. |
| `CLAWCHAT_MEDIA_PROXY_MODE` | `media_proxy_mode` | `"auto"` | Environment proxy routing: `auto`, `on`, or `off` (case-insensitive). Invalid values fall back to `auto` with a warning at config load. Only `http://` proxies are supported. |
| `CLAWCHAT_MEDIA_MAX_BYTES` | `media_max_bytes` | `104857600` (100 MiB) | Maximum bytes per inbound attachment. Invalid or non-positive values fall back to the default. |
| `CLAWCHAT_MEDIA_DOWNLOAD_TIMEOUT_SECONDS` | `media_download_timeout_seconds` | `30` | Socket connection / individual read timeout in seconds. Accepts fractional seconds; invalid or non-positive values fall back to the default. |
| `CLAWCHAT_MEDIA_DOWNLOAD_DEADLINE_SECONDS` | `media_download_deadline_seconds` | `300` | Total time limit in seconds for one inbound attachment, across resolution and redirects. Accepts fractional seconds; invalid or non-positive values fall back to the default. |

### Inbound media downloads

See [inbound media downloads](non-owner-boundaries.md#inbound-media-downloads) for address checks, proxy behavior and residual risk, Authorization handling, and download limits.

### `media_local_roots` no longer scopes outbound delivery

See [outbound delivery and the denylist](non-owner-boundaries.md#media_local_roots-no-longer-scopes-outbound-delivery) for the legacy setting and the credential/system-path policy.

### Known limits of the attachment policy

See [known limits of the attachment policy](non-owner-boundaries.md#known-limits-of-the-attachment-policy) for delivery without a ClawChat turn and the host's prior existence check.

## Debug / diagnostics env vars

These are **not** part of the profile-first resolution chain above: they are
read straight from the process environment (`os.environ` / `os.getenv`), because
they are per-process debugging switches rather than per-profile settings. All
of them default to off and none of them are written by activation.

| Env var | Read by | Effect |
|---------|---------|--------|
| `CLAWCHAT_DEBUG_PROMPT_INJECTION` | `adapter._debug_prompt_injection_enabled` | Truthy (`1`/`true`/`yes`/`on`) logs the composed channel prompt, the event text, and the Hermes output text at WARNING, wrapped in `----- BEGIN/END CLAWCHAT DEBUG … -----` markers. **Logs full message content** — do not enable on a shared or production log sink. |
| `CLAWCHAT_LLM_CONTEXT_DEBUG` | `llm_context_debug.ClawChatLlmContextDebug` | Master switch. Truthy enables JSON snapshots of each turn's LLM context. Everything below is inert without it. |
| `CLAWCHAT_LLM_CONTEXT_CAPTURE_FULL_INPUT` | same | Also capture the full assembled model input (prompt parts, injection parts, request messages, request tools). |
| `CLAWCHAT_LLM_CONTEXT_CAPTURE_OUTPUT` | same | Also capture the raw model output in the snapshot. |
| `CLAWCHAT_LLM_CONTEXT_RELOAD_PROMPTS` | same | Re-read the prompt files on each turn instead of using the cached copy, so prompt edits take effect without a restart. |
| `CLAWCHAT_LLM_CONTEXT_SNAPSHOT_DIR` | same | Snapshot root. Default `.clawchat-llm-context-debug` relative to the process CWD; snapshots land under `<root>/hermes/runs/`. |

Snapshots contain prompts, message text, and (with the capture flags) model
output — treat the snapshot directory as sensitive.

### Internal restart handoff (not operator-configurable)

`clawchat_gateway.restart.schedule_gateway_restart` spawns a detached launcher
and passes two variables to it in the child environment:

| Env var | Value |
|---------|-------|
| `CLAWCHAT_HERMES_BIN` | The resolved `hermes` executable the launcher `exec`s. |
| `CLAWCHAT_GATEWAY_RESTART_DELAY` | Seconds the launcher sleeps before restarting the gateway (from the caller's `delay_seconds`, default `2`). |

Both are **set** by the plugin, not read from the operator's environment — the
child env is built as `{**os.environ, …}` with these keys overwritten last, so
exporting them yourself has no effect.

## Database location

Plugin SQLite lives under `$HERMES_HOME/clawchat/`:
`clawchat.sqlite` for the default profile, `clawchat-<profile>.sqlite` for named
profiles. Upgrading from a version that stored `clawchat.sqlite` directly in
`$HERMES_HOME` relocates it into `clawchat/` automatically on first start (with
its WAL sidecars); no re-pairing is needed.

## Memory

`memory_root` is derived from `HERMES_HOME` (or
`hermes_constants.get_hermes_home` when importable) — typically
`$HERMES_HOME/memories`. It is not user-configurable through
`platforms.clawchat.extra`.

The plugin keeps its social notes there: `owner.md` (the owner),
`users/<usr_id>.md` (one person) and `groups/<cnv_id>.md` (one group), each a
metadata block the plugin maintains plus a body the agent writes with the
`clawchat_memory_*` tools.

### Hermes built-in memory

Hermes' own memory is built for one user per agent: its `memory` tool has a
`user` target meaning "the person you are talking to", the system prompt carries
a `USER PROFILE` block, and a background review periodically saves "the user's"
details into `USER.md` / `MEMORY.md`. A ClawChat agent talks to many people, so
the plugin turns the single-user parts off in the host `config.yaml`:

```yaml
memory:
  user_profile_enabled: false   # no USER.md target / USER PROFILE block
  nudge_interval: 0             # no background memory review (skill review unaffected)
```

`memory_enabled` is left as is: `MEMORY.md` remains the agent's global notebook
for facts that do not depend on who it is talking to. Facts about one person or
one group go into the ClawChat notes above. By default, owner-side turns can
write it (including trusted owner-only group batches); non-owner turns leave
the `memory` tool out or block it. Per-person grants and the `non-owner-host-tools: approve`
opt-in apply here too (see
[Host tools in turns the owner did not start](#host-tools-in-turns-the-owner-did-not-start)).

Activation writes both keys (overwriting, like the `agent.*` defaults). Plugin
load (`activate.ensure_clawchat_host_defaults_on_load`, called from
`__init__._register_platform`) fills them in only when they are missing, so an
existing install picks them up on its next start while a value an operator set
by hand is kept. Hermes has read both keys since v0.12.0.

Facts saved before this split stay where they were. Once per profile, after the
first connection that knows the owner's direct chat, the plugin counts the
entries that look like they are about the owner or a specific person — every
`USER.md` entry, and each `MEMORY.md` entry (entries are separated by `§`) that
names an `usr_` id, a known nickname (owner.md, users/*.md) or reads like a fact
about someone (`clawchat_gateway/memory_migration.py`). If there are any, it gives
the agent one maintenance turn in the owner's direct chat: tell the owner how
many, in their language, and offer to move them into `owner.md` /
`users/<usr_id>.md`; move nothing until the owner agrees, and remove an entry from
the global memory only after it was written to the note. The plugin itself never
edits, moves or deletes `MEMORY.md` / `USER.md`. The marker
`$HERMES_HOME/clawchat/.memory-migration-hint-done` records that the profile was
checked; delete it to be asked again.

## Worked example — `config.yaml` after activation

```yaml
platforms:
  clawchat:
    enabled: true
    extra:
      base_url: https://app.clawling.com
      websocket_url: wss://app.clawling.com/ws
      user_id: usr_...
      agent_id: agt_...
      owner_user_id: usr_...
      output_visibility: normal
      runtime_status_messages: false
display:
  busy_input_mode: queue
  busy_ack_enabled: false
  background_process_notifications: "off"
  tool_progress_command: false
  platforms:
    clawchat:
      tool_progress: "off"
      show_reasoning: false
      streaming: false
      interim_assistant_messages: true
      long_running_notifications: false
      busy_ack_detail: false
      cleanup_progress: false
memory:
  user_profile_enabled: false
  nudge_interval: 0
compression:
  threshold_tokens: 150000
```

`$HERMES_HOME/.env` after activation contains at least:

```ini
CLAWCHAT_TOKEN=...
CLAWCHAT_REFRESH_TOKEN=...
CLAWCHAT_ALLOW_ALL_USERS=true
CLAWCHAT_HOME_CHANNEL=cnv_...
CLAWCHAT_HOME_CHANNEL_THREAD_ID=
CLAWCHAT_HOME_CHANNEL_NAME=ClawChat
```
