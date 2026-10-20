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
| `CLAWCHAT_TOKEN`                     | —                  | —       | Required for the gateway to start. Written to `$HERMES_HOME/.env` by activation. |
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

Activation writes these global ClawChat defaults on every activation:

```yaml
agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
display:
  busy_input_mode: queue
  busy_ack_enabled: false
  background_process_notifications: "off"
  tool_progress_command: false
```

Activation writes this platform-scoped ClawChat block when keys are missing:

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

Because every ClawChat user is allowed by default, the plugin itself decides
who may run a Hermes slash command in a direct chat. The agent's owner may run
any. Anyone else may only run commands that touch nothing but their own direct
session: `/new` (alias `/reset`), `/compress` (alias `/compact`), `/help`,
`/whoami`. Every other command — `/restart`, `/update`, `/config`, `/yolo`,
`/approve`, plugin commands such as `/clawchat-activate`, and any command a
future Hermes adds — is dropped before it reaches Hermes. This is an
allow-list (`NON_OWNER_DIRECT_SLASH_COMMANDS` in `clawchat_gateway/adapter.py`)
so a new Hermes command is owner-only until it is reviewed. If the owner id is
not known yet, only the allow-listed commands pass. Group commands are governed
by `group_command_mode` instead.

Hermes asks before `/new` / `/reset` (`approvals.destructive_slash_confirm`,
on by default) and takes text replies: approve, always or cancel, with
aliases. A non-owner answers that prompt through the plugin, not through
Hermes: for a direct chat with anyone but the owner the adapter implements
Hermes' `send_slash_confirm` hook, shows a prompt with only `/approve` and
`/cancel`, and resolves the reply itself via `tools.slash_confirm` for the
exact session and confirm id Hermes registered for that chat. Accepted
replies are Hermes' own spellings: `/approve` `/yes` `/ok` `/confirm`,
`approve` `approve once` `once`; `/cancel` `/no` `/deny` `/nevermind`,
`cancel` `no` `nevermind` (`!` works in place of `/`). Two host behaviours
stay out of reach this way:

- "always" (`/always`, `/remember`, `always`, `always approve`) persists
  `destructive_slash_confirm: false` in the owner's config.yaml. A non-owner's
  "always" is refused and the prompt stays open.
- Hermes sends `/approve` to a blocking dangerous-command approval in the
  same session ahead of the confirm. The plugin never forwards a non-owner's
  confirm reply to Hermes, so it can only ever resolve the confirm.

The owner, and groups, keep Hermes' own prompt and reply handling.

Hermes also reads some *plain text* as gateway control, which the slash-command
allow-list never sees: `always` / `approve` / `cancel` answer a pending
confirm, `yes` / `always` approve a blocking dangerous-command approval while
the agent is busy, and `restart gateway` in a direct chat becomes `/restart`.
On Hermes 0.20.1+ (whose `MessageEvent` has `allow_gateway_control`) the
plugin sets that field to `false` on every non-owner event except a command it
already let through (the allow-list above, or a group command allowed by
`group_command_mode`); a group batch counts as the owner's only when every
message in it is. Hermes then treats the text as conversation only, so it
still reaches the agent but controls nothing. One side effect: a non-owner's
answer to a Hermes `clarify` prompt arrives as a normal message instead of
resolving the prompt. On older hosts the field is not passed (they reject
it); instead the plugin drops a non-owner's `always`-type confirm reply, the
plain-text restart phrases, and an approval word while a dangerous-command
approval blocks that chat's session. In groups a non-owner's `/always` or
`/remember` is dropped even when `group_command_mode` is `all`.

For an ordinary dangerous-command approval inside a non-owner's direct chat,
Hermes' approval prompt (the command plus `/approve` / `/deny` choices) is
not shown to them, since they cannot approve it. The plugin denies that
approval immediately through Hermes' own `/deny` path
(`tools.approval.resolve_gateway_approval`), so the agent's tool call returns
BLOCKED at once instead of holding the chat's session for the approval
timeout, and tells the friend the step was declined because only the owner
can approve it. On Hermes hosts whose resolve call accepts a reason, the
agent also sees that it was declined by policy and must not retry; older
hosts relay a plain denial. The owner is not notified. On a host without
that resolve path the plugin only sends the note, and Hermes waits and
denies on timeout as before. Per-person ask grants are the exception: their
approval goes to the owner's direct chat instead (see below). Other owner-chat
and group approvals keep their existing behavior.

### Host tools in turns the owner did not start

| `extra.*` key          | Default | Values             |
|------------------------|---------|--------------------|
| `non-owner-host-tools` | `off`   | `off` / `approve`  |
| `tool-grants` | `on` | `on` / `off` |
| `public-tools` | unset | Array of exact tool names; see [Public tools](#public-tools) |

Hermes gives a ClawChat turn the platform's full host toolset, and its
factory `approvals.mode: smart` lets an auxiliary model run a "dangerous"
command it rates low-risk without asking anyone — while commands such as
`cat`, `ls` or `curl` never count as dangerous at all. The approval handling
above only sees calls that reach Hermes' gateway approval step, so on its own
it could not stop a friend, or anyone in a group, from getting the agent to
run commands on the owner's machine or read `$HERMES_HOME/.env`,
`config.yaml` or `owner.md`.

Non-owner turns — a friend's direct chat or a group batch with non-owner
speakers — lose these host toolsets by default (rules can restore authorized
tools; see below). A trusted owner-only batch keeps ordinary host tools even
in a group; grant management remains owner-direct-chat-only. The default
restricted toolsets are:
`terminal` (`terminal`, `process_manage`), `file` (`read_file`, `write_file`,
`patch`, `search_files`), `code_execution` (`execute_code`), `delegation`
(`delegate_task`), `cronjob` (`cronjob_manage`), `kanban` (every `kanban_*`
tool), `computer_use` and
`browser` (every `browser_*` tool: it opens `file://` URLs and runs page
scripts and raw CDP, so it reads local files too), Hermes' `memory` tool,
`session_search`, `connections` (`manage_connections` and every hosted
connector tool `connectors__<app>__<tool>`), and every **MCP tool** — the MCP
servers the owner configured for ClawChat (a filesystem or database server
reads the same files the `file` tools do).
Kanban tasks run in a separate process with the assignee profile's full tools
and without the initiating turn's session identity. The plugin's hook cannot
identify the caller there, so it blocks worker/subscriber-routing mutations
even with a grant (see the audited route list below).
For the same reason the hook also blocks Bot Mode's `message_agent` (the
receiving agent runs with its own full tools).
Authority is resolved for the trusted speakers in each batch. Restoring
toolsets can rebuild Hermes' cached agent; the hook still checks concrete
calls. In non-owner turns, local attachments require direct access to every
Files tool or a structured artifact from a successful, permitted call in this
turn (see “Local attachments” below). Other plugin local-path restrictions
remain. In non-owner turns, HTTP(S) attachments are fetched only from globally
routable unicast addresses. Connections use the checked IP directly, without
proxies, and each redirect is resolved and checked again. In non-owner ClawChat
turns, `send_message` to other platforms cannot carry attachments; ClawChat
targets retain the local-file and remote-address policy. Without a matching
explicit rule, web search/extract and skills keep their existing access.
Owner-side turns keep ordinary host access; plugin resource checks remain.

Who loses or regains access with these boundaries:

- People without an explicit matching grant, including those with only default
  tools, `public-tools` or group approve, lose the ability to send mentions,
  reactions or files to another conversation. Those defaults cover only the
  current conversation.
- A guest-started background process's completion turn uses the starters'
  **current** grants only when its trusted host event matches the plugin's
  registration; otherwise it gets only default tools. Unattributed host turns
  in groups no longer get approve as a fallback on any host, including with
  `tool-grants: off`.
- Host-generated turns in the owner's direct chat regain ordinary owner tools
  for the owner's own scheduled tasks, heartbeats, process completions and
  sub-agent reports. The four authorization tools remain unavailable.

`session_search` goes because it reads every conversation in Hermes' session
store — the owner's direct chat, other friends' chats, other groups, other
platforms, and with its `profile` argument other profiles' stores — and has no
way to stay inside the current chat. Recalling what a person said before still
works through their ClawChat note (`users/<usr_id>.md`, the group's note).
`connections` (Hermes 0.21.3+) goes because on a messaging platform
`manage_connections` installs, enables or authorizes MCP servers in the owner's
config without a confirmation card, and while it is in a turn the owner's
connected hosted accounts (mail, calendars, issue trackers, through
`tool_search` / `tool_call`) are callable there.

`memory` goes because what it writes to `MEMORY.md` / `USER.md` is in the
system prompt of every later conversation, the owner's direct chat included
(from the next new session, or the next compression of a running one), where
it reads like the agent's own note. The tool only writes (`add` / `replace` /
`remove`); reading is that system-prompt injection, which is unchanged.
`owner.md` is different: it is the plugin's own note, injected into every
ClawChat turn and writable from every one (see
[`./reference/tools.md`](./reference/tools.md#what-a-conversation-may-read));
what of it to say where is left to the model.

The owner's direct chat is the activation conversation. Host-generated
`internal=True` turns there, with no existing plugin snapshot, use ordinary
owner tools but never the four authorization tools. This relies on the
cross-session gate keeping guest-started work out of that chat; a new host
route that starts turns elsewhere must be audited before it is opened.
The plugin's own synthetic-turn rules are unchanged: its one-time activation
greeting keeps ordinary owner tools based on the stored pairing;
contact/comment notifications, permission receipts, memory migration hints and
sediment turns use defaults. Synthetic turns cannot manage grants. Management
requires the verified real owner in that direct chat. See
[Spontaneous turns and origin inheritance](#spontaneous-turns-and-origin-inheritance).
On old hosts without plugin hooks, plugin-generated turns that use defaults
are refused entirely; all supported host versions provide hooks.

Hermes before 0.19.1 does not pass the turn's chat type to plugins. There the
plugin falls back to the chat id: a turn is the owner's only in the owner's
activation conversation (ids compared case-insensitively) when the plugin
does not know that chat as a group; anything it cannot place is not the
owner's. The memory tools' read scope uses the same fallback, so the owner's
direct chat reads every note on those hosts too.

Two layers (`clawchat_gateway/host_tools_guard.py`):

- the adapter's per-source toolset override (`toolsets_for_source`, Hermes
  0.20.1+): without grants, `public-tools` or the `approve` opt-in, these
  restricted tools are not in the turn's schema at all. Grant management tools use a separate
  `clawchat-grants` toolset, removed from non-owner turns under both policies.
  MCP needs Hermes'
  `no_mcp` sentinel in that list: a list that names no MCP server makes
  Hermes add back every enabled one. On an older host the plugin logs a
  warning at load and relies on the second layer;
- a `pre_tool_call` hook that applies per-person grants first, then blocks
  ungranted restricted calls still reaching dispatch (or escalates them under
  `approve`). MCP tools are recognised by their host toolset
  (`mcp-<server>`), else by the `mcp_` name prefix (`mcp_<server>_<tool>`
  before Hermes 0.18.1, `mcp__<server>__<tool>` since); hosted connector tools
  by their `connectors__` prefix (the `tool_call` bridge hands the hook the
  underlying tool's name).

Without an applicable per-person grant, the hook also limits a few tools
those turns keep, by what a call asks for. Legacy toolset narrowing can leave
these tools in the schema, while the current catalog filter can omit them;
the channel prompt carries a line listing the limits:

- `skill_manage` is blocked: a skill it creates or edits in place (bundled and
  hub skills included, `scripts/` too) is in the owner's direct chat from then
  on. `skills_list` and `skill_view` stay — the plugin's own `clawchat-*`
  skills are read through them — except that when the owner turned on
  `skills.inline_shell` (Hermes then runs a skill's ``!`cmd``` snippets when it
  is viewed), `skill_view` opens only ClawChat's own skills (`clawchat:<name>`,
  or the bare name of a bundled or updated one). An unreadable host config
  counts as inline shell on;
- `vision_analyze`, `video_analyze` and `image_generate` (`image_url`,
  `reference_image_urls`) read any image on the machine on Hermes' default
  local backend. They take only `http(s)://` and `data:` sources and files
  inside the host's media caches (`$HERMES_HOME/cache`, `image_cache`, …) or
  the plugin's inbound attachment directory (`media_download_dir`, default
  `/tmp/clawchat-media`); paths are resolved (symlinks, `..`) first;
- `text_to_speech` with an `output_path` is blocked: it writes anywhere outside
  Hermes' protected paths. Without one the audio goes to Hermes' cache as usual.

An external memory provider (`memory.provider`: honcho, mem0, supermemory, …)
brings its own tools. Hermes gates them with the `memory` toolset only from
0.18.1, and only layer 1 drops that toolset, so the hook restricts ungranted calls by the
names the provider itself lists (loaded once per process). If the provider
cannot be loaded or lists no tools, a turn layer 1 does not narrow (no override,
or `approve`) blocks ungranted calls to tools the host's tool registry does not know, and the
plugin logs it.

Home Assistant (`homeassistant`, on by itself once `HASS_TOKEN` is set) and
Spotify (`spotify`) stay available in these turns: a friend or a group can
switch the owner's devices or play music on the owner's account, unless an
effective ask or deny rule requires approval or refuses the call. Owners who do
not want that should disable those toolsets for ClawChat
(`platform_toolsets.clawchat`).

Without grants or `public-tools`, `no_mcp` drops **every** MCP server from such
a turn, including the "portable" MCP servers other Hermes plugins bring: they
are configured by the owner too. The owner's direct chat keeps all of them.

What each Hermes version gets (block columns describe calls without an
applicable per-person grant). Per-person entries also require actual
`pre_llm_call` support and matching execution ids; a version alone is not proof:

| Hermes | Layer 1: tools left out of the schema | Layer 2: `pre_tool_call` block | `approve` | Memory provider tools | Per-person grants |
|---|---|---|---|---|---|
| 0.21.0 and later | yes | yes | yes — ungranted restricted calls go to the human gate | left out with `memory` (layer 1); under `approve` ungranted calls go to the gate | direct in both layers; ask with a reliable owner approval route and no bypass |
| 0.20.1 – 0.20.x | yes | yes | no — blocked instead (the tools stay visible) | left out with `memory` (layer 1); under `approve` blocked by name | direct in both layers; ask refused |
| 0.19.1 – 0.20.0 | no — the tools stay visible | yes | no — blocked instead | visible, blocked by name | direct through the hook only; ask refused |
| 0.12.0 – 0.19.0 | no — the tools stay visible | yes (no chat type from the host: the owner's activation chat id decides, see above) | no — blocked instead | visible, blocked by name | unsupported; management tools require upgrade to 0.19.1+ |

`connections` exists from Hermes 0.21.3. The argument limits above and the
memory provider restrictions are layer 2 only, on every version; applicable
grants take precedence over these restrictions.

Where ungranted tools stay visible but their calls are blocked, each such turn's
ClawChat channel prompt says so, so the model does not spend rounds trying
them. Every Hermes that can load the plugin (0.12.0+, `register_platform`)
can take the hook; should one ever load it without `register_hook`, no layer
could keep these tools out, so the plugin does not answer friends' direct
chats or groups at all there (it logs `reason=host_without_plugin_hooks`)
and only the owner's direct chat works.

`non-owner-host-tools: approve` is the owner's opt-in: restricted host tools stay
visible, and calls without an applicable per-person grant go to Hermes'
human-approval gate, which smart never answers. A friend's direct chat declines
these ungranted calls on the spot (above); a group's approval goes to the owner.
Cross-session execution routes remain blocked even under approve.
Cross-chat delivery requires an explicit matching grant; approve covers only
the current chat. Unattributed spontaneous turns get defaults without group
approve, including with `tool-grants: off`.
Effective per-person rules take precedence over `non-owner-host-tools`:
deny refuses; direct executes without this gate, while ask routes each call to
the owner's direct chat, including normally open tools such as `web_search`.
A host that cannot escalate a hook's `approve` blocks calls requiring approval.
Any other setting value means `off`.

Hermes ignores an exception from either layer and then *opens up* (all
platform tools; the hook skipped), so both fail closed: an override that
cannot work out the platform toolset narrows the turn to the plugin's own
`clawchat` toolset plus `no_mcp` (without the sentinel Hermes would add
every MCP server back), and a hook that cannot place the turn blocks the call.

### Per-person tool grants

`tool-grants: on` (default) lets the owner set per-person rules for exact host
or connected-app tools, capabilities, or all tools. The three modes are
`direct` (no per-call approval), `ask` (ask the owner on each call), and `deny`
(refuse). Sensitive tools can be `direct`; sensitivity changes the confirmation
warning, never the requested mode. The owner requests grants in their ClawChat
direct chat; code resolves names from explicit user ids.

#### Public tools

`platforms.clawchat.extra.public-tools` adds exact tool names to the default
tool row for every non-owner turn. Dispatch, execution checks, short channel
hints, turns without a snapshot and `tool-grants: off` all use the same row.
Only installed, available tools can run; the setting does not install or enable
host tools. For example:

```yaml
platforms:
  clawchat:
    extra:
      tool-grants: on
      public-tools:
        - feishu_doc_read
```

The value must be an array of strings. Names are case-sensitive, trimmed, and
must match `[A-Za-z0-9_][A-Za-z0-9_.:/-]*`. Any invalid item disables the **whole
list**: a non-string, an empty or malformed name, `all`, a `cap:`, `tool:` or
`group:` selector, any member of the six sensitive capabilities (Terminal,
Files, Code execution, Sub-agents, Scheduled tasks, Browser and desktop control),
their verified host aliases, any `browser_` name, or any of the four
authorization tools. One warning identifies only the first problem item and
its reason, never the whole configuration; non-string values use a placeholder.
A non-array value is also rejected. Repeated reads of the same invalid value
do not repeat the warning. An omitted value or empty array adds nothing and
logs nothing.

These additions do not appear in the grant list and cannot be changed in chat.
With grants enabled, a person's explicit matching deny (exact tool, capability
or `all`, using normal precedence) still closes that tool for them. Listing a
tool does not lift the plugin's per-call restrictions on `skill_manage` writes,
`skill_view` with inline shell, local-file media parameters, or
`text_to_speech.output_path`. Public access covers only the current chat;
cross-chat delivery still needs an explicit matching grant.

#### Selectors and capabilities

The `tools` array accepts these selectors:

| Input | Meaning | Stored `what_kind` / `what` |
|---|---|---|
| `tool:<exact-name>` | One exact tool; preserve name and case after the first `tool:` | `tool` / exact name |
| `cap:<id>` | One capability from the table below | `capability` / ID |
| Bare capability ID, e.g. `terminal` | Same as `cap:terminal` | `capability` / ID |
| Bare tool name, e.g. `read_file` | Same as `tool:read_file` unless reserved | `tool` / exact name |
| `all` | Runtime all-tools rule | `all` / `all` |

Capability IDs and bare `all` are reserved. Colliding tools must use
`tool:terminal` or `tool:all`. Names are case-sensitive; whitespace, control
characters, `*`, `?` and host `group:*` selectors are not accepted. MCP server
and toolset names do not select groups of tools.

Hermes validates exact names against its trusted registry and availability
against its effective host catalog. Unknown tools or capability IDs return
`invalid-what`; known but unavailable tools, or capabilities with no available
mapped member, return `not-available`. Neither error writes pending/deny rows
or sends a card. The entire selection is validated, normalized and deduplicated
before execution; an invalid entry rejects it all. Revoke can still name stored
tools that have been uninstalled.

| Capability ID | English display name | Sensitive | Hermes tool mapping |
|---|---|---|---|
| `terminal` | Terminal | Yes | `terminal`, `process_manage`, `process` |
| `files` | Files | Yes | `read_file`, `write_file`, `patch`, `search_files` |
| `code` | Code execution | Yes | `execute_code` |
| `subagent` | Sub-agents | Yes | `delegate_task`, `kanban_show`, `kanban_list`, `kanban_complete`, `kanban_block`, `kanban_schedule`, `kanban_request_review`, `kanban_request_changes`, `kanban_heartbeat`, `kanban_comment`, `kanban_attach`, `kanban_attach_url`, `kanban_attachments`, `kanban_create`, `kanban_unblock`, `kanban_link` |
| `schedule` | Scheduled tasks | Yes | `cronjob_manage`, `cronjob` |
| `browser` | Browser and desktop control | Yes | `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_type`, `browser_scroll`, `browser_back`, `browser_press`, `browser_get_images`, `browser_vision`, `browser_console`, `browser_cdp`, `browser_dialog`, `browser_vault`, `browser_exec`, `computer_use` |
| `cross-chat` | Cross-chat messaging | No | `clawchat_mention_message`, `clawchat_send_file`, `clawchat_react_message`, `send_message` |
| `memory` | Host memory | No | `memory` |
| `session-search` | Session search | No | `session_search` |
| `feishu-docs` | Feishu documents | No | `feishu_doc_read`, `feishu_drive_list_comments`, `feishu_drive_list_comment_replies`, `feishu_drive_reply_comment`, `feishu_drive_add_comment` |
| `connections` | Connections | No | `manage_connections` |
| `web` | Web search and reading | No | `web_search`, `web_extract`, `x_search` |

Only tools actually registered and available participate. Legacy `process`,
`cronjob` and `send_message` are compatibility mappings, not new tools; current
Hermes does not expose `send_message` to the model. All listed `kanban_*` tools
belong to `subagent`. Exact `browser_*` tools also receive sensitive warning
copy, but a prefix does not grant access or add arbitrary third-party tools to
the capability. MCP, dynamic connector and other unclassified tools need an
exact rule or `all`. `memory` means host memory, not conversation-scoped
`clawchat_memory_*`; their resource and identity checks remain in force.

#### Rule resolution and all tools

For each person and concrete call, ignore expired rows, then choose matching
**exact tool > capability > all**. Only within the highest matching level,
choose **deny > ask > direct**, including overlapping capabilities. A more
specific direct rule can override a broader deny. For example, `all` direct
plus `cap:terminal` deny blocks Terminal; adding `tool:process_manage` direct
opens only that exact tool.

Exact-name matching mirrors the verified host aliases `process` →
`process_manage`, `cronjob` → `cronjob_manage`, `todo` → `todo_list`,
`tour` → `gui_tour`, and `tip` → `show_tip`. Stored rows and old pending cards
keep their original names; alias conflicts use deny > ask > direct at execution.

With no matching rule, use the host's ordinary default for that call. Explicit
ask or deny also applies to normally open tools such as `web_search`. In a
group with `non-owner-host-tools: approve`, a restricted tool with no matching
row defaults to ask for that person, then participates in the intersection.
This fallback is **per tool**, not per person: a grant for Feishu does not
remove approval fallback for an unrelated tool. Friend direct chats do not get
that fallback. Neither cross-chat delivery nor unattributed spontaneous turns
get it. Explicit deny and failed resolution
cannot be bypassed by approve.

Resolve each non-owner speaker first, then intersect: any deny refuses; any
missing permission refuses; otherwise any ask requires approval; only all-direct
runs directly. The owner's own messages are neutral. A nonempty trusted batch
whose speakers are all the owner has full ordinary host tools, including in a
group, but cannot manage grants there. Only the owner's own user id is recognized;
other agents belonging to the owner remain ordinary users. Missing/invalid
speakers and grant-store failures use defaults, never an owner-only empty-set
shortcut or an approve fallback that could miss a deny. Spontaneous turns use
the [origin rules below](#spontaneous-turns-and-origin-inheritance).
An allowed person may ask again alone after a mixed-turn block.

`all` is stored as one rule and evaluated at runtime against the host's
**effective tool surface**, including sensitive tools, connected tools and
**tools installed in the future**. The surface applies host configuration,
platform, global disabled toolsets, dependencies and credentials before this
plugin's per-turn narrowing. Execution rechecks current availability. It excludes
`clawchat_tool_grant`, `clawchat_tool_revoke`, `clawchat_tool_grants_list` and
`clawchat_tool_request`. It cannot bypass host global disablement, host policy,
platform availability or tool identity/resource checks. Exact and capability
exceptions still override `all`.

On this machine, turns started by others cannot dispatch sub-agents, create or run scheduled tasks, or create Kanban tasks; they can only view and stop existing ones.
Cards and activation receipts repeat this restriction for `all`, sub-agent and
schedule capabilities, and related exact tools.

#### Confirmation and per-call approval

Direct and ask resolve every person's name in code, freeze selectors, mode
and expiry, and send a confirmation card only to the activation-recorded
owner direct chat. Nothing is active until a real owner message there sends
`/clawchat-allow <6-digit code>` within ten minutes. The command confirms the
frozen mode, including ask; it does not change it to direct. Wrong, expired
and reused codes never activate a grant. Codes are account-scoped, random
ASCII digits (leading zeros preserved), reserved in SQLite with at most five
collision retries, and never returned to the model. Confirmation atomically
writes rows and consumes the code. `deny` writes immediately without a card.

Cards use the owner's language (zh, zh_Hant or en; others use en).
`richInteractions` does not change this text-and-command card. They show names
and ids, capability display names/exact tools, mode and an explicit
`Expires: No limit` / `Expires: {expiry}` line (`到期：不限` / `到期：{expiry}`),
plus a hint to say “until Friday” to set a deadline. Dates use the known owner
time zone, otherwise UTC marked `(UTC)`. Ordinary consequence copy says:
“This means: {names} can have me call these tools for them. Whatever you can
see or do with these tools, they can too.” It also says access is by tool and
does not guarantee restriction to one document or resource.

For direct/ask grants containing `cap:schedule`, `cap:subagent`, `all`, or an
exact member of either capability (including host aliases), cards also add the
code-generated self-running consequence line (R):

> Scheduled tasks, sub-agents, and kanban tasks that {names} starts run with the owner's configuration and all tools.

It is absent for deny. This line describes the host's execution model; it does
not open the cross-session routes Hermes blocks. The Hermes restriction copy
remains on the card as well. Guest request cards for `schedule` or `subagent`
show both lines, since they offer direct/ask choices.

A sensitive direct card, including `all`, adds this consequence copy (using
code-resolved names):

> {names}能在这台机器上跑任何命令、读写任何文件，包括这个 Agent 的登录凭据、你的模型 key 和这张授权列表。拿到凭据的人可以冒充这个 Agent 跟别人说话。只给你完全信任的人。

Every `all` card explicitly includes future tools and sensitive tools, and
excludes the four authorization tools. Ask cards say “These operations will
ask you first every time.” When per-call approval is unavailable, cards say
“This machine cannot ask you each time. You can only choose Use directly or
Deny.” Ask is not offered: an ask grant returns `error:"validation"`,
`message:"ask-unavailable"` and that explanation in a `note` in the owner's
language (English fallback), without a card or grant write; direct cards include
that explanation. The current proactive card
confirms a preselected mode; guest request cards instead select direct/ask/deny.
Profile names and labels neutralize Markdown markers, backticks and slashes
with fullwidth characters; only the code-generated confirmation command is clickable.

Ask needs Hermes 0.21.0+ and a working owner approval route. Card creation,
confirmation and execution all check this. Yolo, `approvals.mode: off`,
session/permanent allowlist bypass, unsupported hooks, unreadable policy or a
broken route refuse ask; runtime never downgrades it to direct. Each call has
a fresh rule key and shows speakers' names (ids when unavailable), conversation
type, tool and raw arguments to the owner. Only “Approve Once” and “Deny” are
offered. Session/permanent choices and aliases are treated as deny and persist
no permission. Rejection/timeout is `ask-declined`; unavailable routing or
failed delivery is `ask-unavailable`. Files, Terminal and Code execution direct
calls accept local-path arguments; other restricted direct calls retain the
local-path check. Attachment delivery retains its separate resource checks.

#### Management and turn snapshots

[Tool parameters and return shapes](./reference/tools.md#per-person-tool-grants)
cover grant, revoke, list and request. The three management tools require `tool-grants: on`, ClawChat,
the activation direct chat, a real owner sender and a live nonce bound to the
host's exact `(session_id, turn_id)`. Hook and handler both verify them.
Groups (even owner-only batches), friends, other platforms and synthetic
turns cannot manage grants. Group grants freeze current membership, excluding
the owner and this Agent; other people's agents may be included. Every name
must resolve. New members do not inherit access and leaving does not revoke it.

Revoke changes storage immediately without a confirmation card, including
removing deny (`plan_deny_removal` returns `immediate`). Removing deny
returns to remaining rules or defaults. Changes affect subsequent turn
snapshots; they do not rewrite an already running snapshot. List includes
unexpired deny rows.

With `tool-grants: off`, stored rules are retained but ignored, management
and request tools are not registered and the grants hint is omitted. If registration cannot
read configuration, tools remain discoverable by the owner, but execution still
enforces off. The independent `non-owner-host-tools` policy keeps its behavior,
including approve when an old host cannot bind grants to an execution turn,
as long as the plugin turn names its speakers. A capable host with a missing/bad binding cannot use approve to bypass
potentially unseen deny rows.
Unattributed spontaneous turns have no group approve fallback on any host,
also when `tool-grants: off`. With grants off, non-owner cross-chat delivery is refused
as `not-granted`; `public-tools` still supplies default tools for the current chat.

The adapter freezes grants before dispatch and carries a nonce in the source
message id. `pre_llm_call` binds it once to the actual session and turn; calls
must match nonce, chat, session and turn, with no latest-session fallback.
Overlapping snapshots in one session intersect. Messages that could mix
permissions with an executing granted non-owner turn wait for the next batch;
the owner's direct chat admits approvals and control messages immediately.
Plugin synthetic or unidentified input in that direct chat removes owner
verification and intersects live grants with defaults until the session is
idle; finishing one nonce alone cannot restore it. Trusted host-internal turns
use the origin rules below and never gain grant-management verification.
Read failures, expired grants and missing snapshots add no authority.
Inner calls without execution IDs, such as `execute_code` RPC calls, use the
snapshot already bound to `HERMES_SESSION_MESSAGE_ID`; an explicit deny on a
default tool still applies. On a host supporting execution binding, failure to
recover it means defaults without approve. Hosts lacking `pre_llm_call` keep
approve for calls without execution IDs only when the plugin turn nonce in the
host's trusted `HERMES_SESSION_MESSAGE_ID` context names a turn with speakers;
host-started and plugin synthetic turns get defaults without approve. Hosts
that do not expose `HERMES_SESSION_MESSAGE_ID` to tools cannot attribute any
turn, so group approve is unavailable there.

With grants enabled, a bound non-owner turn can use `tool_search` and
`tool_describe` only when the grants catalog filter is installed and the
snapshot read by that filter matches the bound grants and non-owner speakers,
including the chat ID. The host reads this authorized catalog with
`skip_tool_search_assembly=True`; it includes `clawchat_tool_request` even under
`all:deny` and excludes ungranted, denied and expired tools. In guest turns the
plugin neutralizes the host's remote connector search/describe, registry
fallback and list of declared but unavailable sources, so the bridges only see the filtered catalog, including after an
authorization expires between checks and dispatch. Without a complete guard, guest schemas skip search assembly, so no catalog
listing or source summary reaches the model. Bridges stay `not-available`
when that guard cannot be installed or verified, filtering is missing, or
snapshots mismatch. `tool_describe` returns the same refusal as a direct call
for registered names hidden in this turn or unavailable on the host. Owner turns
and other platforms keep the host's original search behavior. `tool_call` is
unpacked by the host and the real inner tool still passes through the execution hook.

#### Local attachments

Replies and `clawchat_send_file` share the same local-file policy. A non-owner
turn can send a local file into its current conversation when **every caller
has direct access to every Files tool** (`read_file`, `write_file`, `patch`,
`search_files`). The ordinary selector priority and group intersection apply:
`cap:files` with mode `ask` does not open attachments, and an exact-tool ask/deny exception
to Files or `all:direct` closes this route. Cross-chat access alone does not
open local attachments.

Alternatively, a successful tool call permitted by `pre_tool_call` can declare
its own generated files in the structured completion result. These artifacts
are available only for that live turn, account, session, caller batch and
conversation. The permit binds the call id (or tool name plus a canonical
argument fingerprint), also verifies tool name and arguments when an id exists,
and is consumed once. An approved call under `non-owner-host-tools: approve`
also qualifies with `tool-grants: off`; that approval never permits another
call or a later completion. Failed/blocked completions consume without adding
artifacts. Ending the turn immediately clears pending permits and artifacts.

Guest delivery rejects symlinks (including parent directories), hard links,
non-regular files, credential/system paths, and files changed since declaration.
Artifact registration opens the file and records its kernel-reported real path
and `fstat` identity, rather than combining a resolved path with pathname stats.
Guest attachment uploads open one descriptor, look up its actual path (`/proc/self/fd`
on Linux, `F_GETPATH` on macOS, `GetFinalPathNameByHandle` on Windows), reapply
the complete path policy to that path, and compare device/inode/size/mtime/ctime
and link count from `fstat`. Both paths are resolved for comparison; Windows
prefixes and case are normalized too. Missing descriptor-path support refuses
guest delivery. The reader uses at most the checked size (100 MiB limit), rechecks the descriptor
path, policy and identity after the read and final pathname check, and uploads
only those bytes. Owner and local-operator delivery keep their original path
checks and byte reads without descriptor lookup or additional rejection rules.
Avatar, moment image, app icon and media upload tools also retain their original
read paths. Files direct does not relax public-address checks for remote media.

#### Narrow-tool artifact contract

A narrow tool must **declare only files it generated itself** and **construct
the output path itself**. It must not echo an arbitrary caller-supplied path
into the artifact result or declare pre-existing owner files. The plugin trusts
installed tool code to honor this contract; granting a tool that violates it
can expose local files.

Return a structured object (or its JSON serialization) through the host's
`post_tool_call` completion hook, for example:

```json
{"details":{"ok":true,"media":{"path":"/tmp/generated-speech.mp3"}}}
```

Only `details.media` is recognized: local `media`, `path`, `url`, `mediaUrl`,
`filePath`, `fileUrl`, strings in `mediaUrls`, or those keys in `attachments`
objects. `outbound:false`, error/failure/pending results and unknown shapes
grant nothing. Text in `content`, legacy `details.path`, and Hermes `MEDIA:`
lines are not artifact declarations. A model can reference an already allowed
file for delivery, but cannot create its permission by writing a path.

The artifact route requires a registered completion hook, a live nonce snapshot,
matching session/turn ids and explicit host `status:"ok"`, with no completion
error. These fields are not available on every supported Hermes release:
missing hooks/fields close the artifact route. No new host helper import or
`transform_tool_result` fallback is used; Files direct still works wherever
the existing execution binding is supported.

#### Non-owner hints and refusal reasons

The dynamic short prompt lists host capabilities with at least one currently
available member and no available member authorized as direct or ask, as
English display names plus IDs, e.g.
`Not available to the caller yet: Terminal (terminal), Files (files).`
It omits uninstalled/globally disabled capabilities and omits the list sentence
when empty. One authorized available member, including an exact-tool exception
to a capability denial, removes the capability from this hint; schemas and
execution still check each tool separately. Catalog lookup failure emits only
generic guidance. Mixed batches
describe shared availability without identifying who lacks access. The rest
says to call `clawchat_tool_request` with the ability ID and one short reason
for missing access, never request after denial or a mixed-turn block, and
explain only the requested ability in the chat's language. It forbids inventing reasons or revealing
others' access, settings, paths or versions.

Refusals carry `reasonCode` and fixed English guidance in `blockReason` /
`message` (legacy ordinary-policy refusals retain their existing messages):

| Reason code | Meaning and guidance |
|---|---|
| `not-granted` | Missing access; call `clawchat_tool_request` with the ability ID and one short reason. Do not try another route. |
| `denied` | The owner has not opened this ability. Do not retry or request it again. |
| `batch-intersection` | Multiple non-owner speakers lack a common permission. An allowed person can ask again alone; do not reveal who lacks it. |
| `ask-declined` | The owner rejected or has not answered this call. Do not retry this request; try later or contact the owner. |
| `ask-unavailable` | Per-call approval cannot be performed. Do not treat it as direct access; contact the owner. |
| `not-available` | The tool is unavailable here. Do not suggest requesting access or another route. |
| `cross-session` | The call would start or route work in another session. Do not retry or use another route; ask the owner directly. |

Current host unavailability takes precedence over grant refusal. Permission
refusals with multiple non-owner speakers use `batch-intersection`; the owner
plus one guest still uses that guest's `denied` / `not-granted` reason.

#### Cross-session routing and known limits

**No grant opens another execution session from a non-owner turn.** This
boundary also applies to `all` and group approve. The audited routes are:

| Tool/route | Non-owner boundary |
|---|---|
| `delegate_task` | Only `list` and `stop` actions can pass authorization; `spawn` (also the omitted action), `steer` and other actions are blocked. |
| `message_agent` | Blocked: routes work to another agent. |
| `cronjob` / `cronjob_manage` | Only `list`, `pause`, `remove` can pass; create, run, enable and every other action are blocked, even with origin/local delivery. |
| Kanban worker/subscriber mutations | `kanban_create`, `kanban_complete`, `kanban_block`, `kanban_schedule`, `kanban_request_review`, `kanban_request_changes`, `kanban_unblock`, `kanban_link`, `kanban_comment` are blocked because they can start workers or wake persisted subscribers. |

Thus Hermes `subagent` and `schedule` grants currently open only read or
management actions such as list/stop/pause/remove and remaining non-routing
Kanban actions, subject to other checks. They do not permit delegated work or
scheduled execution. Newly introduced execution-routing tools/actions must be
added to this audited list; arbitrary argument scanning cannot prove the
destination is safe. Parent grants are not inherited into child sessions or
sessions created after compression.

Cross-chat **messages** are separate: `cap:cross-chat` applies to
`clawchat_mention_message`, `clawchat_react_message`, `clawchat_send_file` and
`send_message` on older hosts that actually register it (current Hermes does
not expose it to the model). Non-owner delivery to another conversation needs
an **explicit matching grant**, resolved by exact tool > capability > `all`
and the batch intersection: direct sends; ask asks the owner for each call
(`ask-unavailable` if that cannot be done); deny or no grant refuses with
`denied`, `not-granted` or `batch-intersection`. Missing or unresolvable targets
count as cross-chat, never as the current chat. `CROSS_CHAT_GATE_POLICY` is
`grant`; `resolve_cross_chat_gate` returns `allow`, `ask` or `block`.

Default tools, `public-tools` and group approve cover **only the current chat**.
With grants off, or on old hosts without execution binding, non-owner cross-chat
delivery is `not-granted`. Owner-side turns are exempt. A cross-chat deny alone
does not hide schemas when a current-chat action is permitted; execution still
checks the actual target. Target, attachment and resource checks remain:
a cross-chat grant alone does not open local attachments. Tools that start or
continue work in another execution session still return `cross-session`, even
with a cross-chat grant.

Other host limits:

- Per-person grants need execution ids and `pre_llm_call` binding support;
  unknown/missing capabilities never imply authorization. On hosts unable to
  bind, legacy approve remains where the host supports it.
- Before Hermes 0.20.1, management tools can remain visible to non-owner turns,
  but execution refuses them. Toolset restoration can expose several members
  on older hosts; the hook still checks each concrete tool. Where supported,
  the catalog filter narrows schemas and search results after host caching
  and before search assembly.
- Granted group batches can rebuild the host agent/toolset cache. Other
  messages cannot interject into a held granted turn.

#### Storage upgrade

Migration **14** (`tool_grants_v2`) transactionally rebuilds `tool_grants`
using `what_kind + what` instead of `tool`, verifies copied row counts, and
uses primary key `(account_id, grantee_user_id, what_kind, what)`.
`what_kind` is `tool`, `capability` or `all`; mode is `direct`, `ask` or `deny`.
Existing exact names migrate as `what_kind='tool'`, even if named `terminal`;
names, labels, modes and expiry are preserved, and old ask rows stay ask.
`tool_grant_pending` adds `purpose` (`grant` / `request`) and `payload_version`
(1 / 2). Old pending payloads remain version 1 exact-tool confirmations;
new grant cards use version 2 normalized selectors. Expiry timestamps are UTC
epoch milliseconds; `NULL` means no limit and `expires_at <= now` is inactive.

**After this upgrade, do not downgrade to an old plugin. Old and new plugin
processes must not share one database.** Old queries are incompatible with
the rebuilt table.

Migration **15** (`tool_grant_requests`) adds durable requests and wake notices.
It records the trusted requester and original chat/session/message, one opaque
request ID, one shared six-digit pending code, capability, sanitized reason and
summary, timestamps, decision/decider, unique wake key, wake state and notice
consumption time. Indexes cover person/capability/time, account/time and
status/expiry. Migration **16** (`tool_request_notice_claims`) adds notice
claim tokens and timestamps, preserving existing requests and decisions.
**Migrations 14–16 cannot be downgraded. New and old processes must not share a DB.**

#### Guest access requests and resumption

`clawchat_tool_request({capability, reason})` is independently visible to every
non-owner turn, even under `all:deny`. It does not inherit from `all` and cannot
specify a user or destination. In a group batch containing the owner and exactly
one trusted non-owner, that non-owner is the requester regardless of message
order. Two or more non-owners create no request context. Deny, mixed-turn blocks, unavailable capabilities
and existing access send no card. Unclassified tools require an owner-created
exact-tool grant. Model arguments never determine requester identity or routing.
For `cross-chat` requests, only unexpired explicit tool, capability or `all`
rules with mode `direct` or `ask` count as existing access; public current-chat
defaults do not.

One transaction checks the person × capability six-hour cooldown and the
Agent-wide rolling 24-hour limit of 20 request cards, reserves a code in
`tool_grant_pending` and creates `sending`. Code collisions retry at most five
times after the initial attempt.
A successful send changes it to `pending`; failure changes it to `failed` and
never claims the owner was asked. An existing pending card or any non-failed
request within the six-hour cooldown returns `waiting`, `sent:false`, with the
same waiting guidance, even after a decision. This creates no new card or code
and writes no grant. Daily quota exhaustion returns `blocked` with
`reason="rate-limited"` and `sent:false`; the fixed guidance states that no
request was sent and the caller can ask the owner directly. Failed sends retain
the daily quota but do not impose the person cooldown. `REQUEST_DAILY_WINDOW_POLICY`
is `rolling-24h`: `resolve_request_daily_window(now)` defines `(now − 24h, now]`,
independent of the owner's timezone. Failed sends also count toward the maximum
of 20; a card exactly 24 hours old no longer occupies quota.

Only the owner's activation direct chat receives a code-generated card.
Profile names, group names and the relayed reason are cleaned, with links,
controls and hidden characters removed and Markdown/slash commands neutralized;
reasons are limited to 60 Unicode code points and marked “Their words, relayed”.
Subagent and schedule cards include the same three-language Hermes limitations
and self-running consequence line (R) as proactive direct/ask cards; other
capabilities do not include those lines.
A request card lasts 24 hours. `/clawchat-allow`, `/clawchat-ask` and
`/clawchat-deny` select direct/ask/deny without another confirmation card.
Ask is absent and refused when the actual approval route cannot ask reliably.
A proactive 10-minute card still accepts only `/clawchat-allow` for its frozen
mode. Every matching command is consumed before model dispatch; only a real,
platform-identified owner in this account's owner direct chat may confirm.
A card left in `sending` is also decidable: receiving the code proves delivery.

Confirmation atomically consumes the code, writes the capability rule, records
allowed/asked/denied with the decider and sets `wake_state=pending`. Write errors
roll back all of it. Receipt rendering failure after commit still reports success.
A unique `wake_key` serializes `pending → dispatching` before asynchronous work.
The plugin dispatches an internal continuation through `_handle_inbound` into
exactly the original session; the sole speaker is the requester. It re-reads
current grants and rechecks expiry at host dispatch. Deny wakes only to explain
the refusal. No caller becomes owner-side or gains ordinary owner tools.

Startup dispatches unexpired pending wakes. A dispatching claim older than ten
minutes becomes failed, with a pending notice; taking notices also recovers
stale claims. Failed wakes retain the effective grant and inject one short
English notice only on that requester's next real turn in the original
chat/session, expiring with the card. Notices are atomically claimed with a
ten-minute lease and marked consumed only after the host accepts the event.
Rejection or exceptions release the claim; expired claims are reclaimed on
startup or notice retrieval. Claim tokens prevent a late acknowledgement from
consuming a newer claim. Ambiguous dispatches are not retried;
the host has no guaranteed idempotent delivery, so this is one claimed dispatch,
not a promise of strict end-to-end exactly-once delivery.

#### Spontaneous turns and origin inheritance

`ORIGIN_INHERITANCE_POLICY` is `receipt-and-process-completion`.
`resolve_origin_grant_context` recognizes only two sources for inheriting a
requester's grants:

- Authorization-card wakeups (`grant-receipt`), with trusted origin attached
  inside the plugin, resume as the requester with current grants as above.
- Background process completions (`process-completion`) from a successful
  `terminal` call with both `background: true` and `notify: true` (older hosts:
  `notify_on_complete: true`). Explicit non-`None` `notify` takes precedence over
  the old flag; missing or `None` `notify` falls back to the old flag.
  With grants enabled, `post_tool_call` records the process ID, starting turn's
  plugin nonce, non-owner speaker set, account, chat and session route. Only a
  host `internal=True` event can match: metadata `notification_origin` must be
  `process_registry_synthetic`, `original_trigger_message_id` must equal that
  starting turn's plugin nonce, and the source chat and recorded
  `gateway_session_key` must match. Both ends need the host session key; a host
  that does not expose `HERMES_SESSION_KEY` to tool hooks gets no inheritance. The completion's first-line process ID
  narrows the match to one registered process; message text never identifies
  the requester. The registration is consumed once. Current grants are loaded
  again into a new turn snapshot, intersecting multiple starters' permissions.
  Revoked or expired access no longer applies. The turn is never owner-side,
  and unmatched tools get defaults without group approve.

The process registry is in memory, limited to 512 origin entries with a
seven-day lifetime. Restart, expiry or eviction loses inheritance. Watch and
heartbeat notifications, batched completions and mismatched or missing records
use only defaults. Process-completion inheritance is disabled with
`tool-grants: off`. Scheduled tasks, sub-agent reports and Kanban work do not
inherit the requester's grants. An unattributed spontaneous turn is treated as
a person without grants and has no group approve fallback on any host, also
with grants off.

There is one separate owner-chat rule: a host `internal=True` turn in the
activation owner's direct chat, without an existing plugin snapshot, uses
ordinary owner tools for the owner's own scheduled tasks, heartbeats, exec
completions and sub-agent reports. It does not gain verified-owner status and
cannot use the four authorization tools. This depends on guest turns being
confined to their own execution session by `cross-session` checks, unable to
start a turn in the owner's chat. If the host adds a tool that can start turns
in another session before the plugin's audited route inventory is updated,
that premise breaks. The plugin's own synthetic-turn rules described above
remain unchanged.

Host background review forks with a nonempty thread tool whitelist also use
ordinary owner tools in the owner's direct-chat session, subject to the host's
whitelist, when no snapshot is bound to the fork and its nonce proves that the
parent turn was owner-side in the same chat. Its snapshot must still be live or
have been recently released as owner-side. Released attribution keeps only
nonce, chat, owner-side status and release time in memory, limited to 512 entries
with a one-hour lifetime; restart, expiry or oldest-entry eviction loses this
fallback. Empty or unknown nonces and non-owner parents never qualify. Forks
gain no verified-owner status and cannot use the four authorization tools.
Forks in guest or group sessions gain no additional access.
Hermes can run `pre_tool_call` in a separate thread, where its
`_thread_tool_whitelist` is invisible. At registration the plugin mirrors the
host's set/clear whitelist API into a ContextVar carried by the host's copied
hook context, and also requires the called tool to be in that nonempty
whitelist. Clearing the host whitelist resets the mirror only in the current
context. Contexts copied before clear, including contexts propagated to threads
spawned inside a fork, retain its whitelist; they still require the owner's
direct chat, a same-chat owner-side parent and membership of the called tool
in that whitelist, and always refuse the four authorization tools. If an older
host or an unavailable mirror supplies no whitelist in the hook context, the fork
remains denied; same-thread hooks can still read the host's thread-local value.
When the host review fork builds its whitelist without a live snapshot, the
catalog retains ordinary owner tools, including `skill_manage`, only on a thread
named exactly `bg-review`, with `enabled_toolsets` supplied as a nonempty list
or tuple containing only `memory` and/or `skills`, and with the same
owner-direct-chat and owner-side parent checks. All four authorization tools
remain excluded. A renamed thread or missing, empty or different toolsets keep
the ordinary filtering without this exception.

Known limitation: this fork rule relies on Hermes internals that are not a
public plugin API — `hermes_cli.plugins.set_thread_tool_whitelist` /
`clear_thread_tool_whitelist`, the thread-local `_thread_tool_whitelist`, and
the review thread name `bg-review` (all as of Hermes 0.21.6). If a host release
renames or removes any of them, owner review forks lose `skill_manage` and
similar owner tools again (they are refused); no session gains access.

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

The takeover floor exists because `reconnect_max_delay_ms` (15 s) is below the
eviction periods measured in prod (9–163 s), so ordinary exponential backoff can
never outrun a mutual-eviction loop between two instances sharing a device id.
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
reclaim cancels them when the standby loop detects the cleared state. Cancelled
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

Inbound downloads allow HTTPS only; `media_allow_http` explicitly enables HTTP.
Relative URLs use the media base derived from the settings above and undergo the
same checks. Non-HTTP(S) schemes are rejected before joining a URL to its base.
For a direct connection, each hop resolves the hostname once, rejects the
request if **any** resolved address is not public unicast, and connects to a validated IP while retaining
the hostname for Host and TLS verification. Localhost names are also rejected.
Connection failures try the remaining distinct validated IPs in order without
resolving again. TCP connection and TLS handshake each use a timeout capped by
the remaining attachment deadline. At most four inbound DNS lookups may remain
pending; further lookups are rejected until a slot becomes available.
Every redirect chooses its route again and repeats the applicable checks, with
at most five redirects; HTTPS-to-HTTP redirects require the HTTP opt-in too.
Backslashes in redirect destinations are
normalized before resolving and validating their target host.

`media_proxy_mode` controls environment proxies on every hop:

- `auto` (default): use the target scheme's proxy from Python's
  `urllib.request.getproxies()` (including `HTTP_PROXY` / `HTTPS_PROXY`). Without
  a proxy for that scheme, connect directly with full address validation.
  Unsupported proxy URLs, including `socks5://`, fall back to direct connections
  with a warning once per process; the warning omits the proxy URL.
- `on`: require a supported proxy for that scheme. A missing or unsupported
  proxy rejects the download.
- `off`: always connect directly with full DNS / public-address checks and IP
  pinning, ignoring environment proxies.

In `auto` and `on`, a hostname matched by `urllib.request.proxy_bypass(host)`
(`no_proxy` / `NO_PROXY`) takes the direct path with full address validation.
This also applies after redirects. Only `http://` proxy URLs are supported:
HTTPS targets use CONNECT followed by TLS with target hostname verification;
HTTP targets use an absolute-URI GET and still require `media_allow_http`.
The proxy itself may have a private address.

**Residual risk:** when `auto` (default) or `on` uses a proxy, target hostname
DNS resolution and private-network checks are delegated to that proxy. The
plugin rejects private, loopback and link-local IP literals before contacting
the proxy, and rejects localhost names, unless `media_allow_private_network`
is enabled. It does not resolve other target hostnames locally. If the proxy
can access private networks, the plugin cannot prevent a request whose
hostname resolves to an internal address. Set `media_proxy_mode: "off"` for
strict local destination validation (and leave `media_allow_private_network`
disabled).

Downloads **never attach Authorization** to the target, even for URLs on the
configured base origin: media URLs must be public or presigned GET URLs.
Proxy URL userinfo is sent only as Basic `Proxy-Authorization` to the proxy
(on CONNECT for HTTPS), and never logged or sent through the tunnel.

The default limit is 100 MiB per attachment. An oversized Content-Length is
rejected immediately; bodies stream in 64 KiB chunks with the same byte limit.
The socket timeout defaults to 30 seconds and the total attachment deadline to
300 seconds. Temporary `.part` files are removed on failure and renamed only
after a complete download. A rejected or failed attachment is logged without
URL userinfo, queries or fragments and skipped; remaining attachments and the
text message continue normally. Environment values take precedence over the
matching extra keys for all six inbound download settings.

### `media_local_roots` no longer scopes outbound delivery

`media_local_roots` used to be an allowlist of directories a local file could be
uploaded from. It is still parsed and still threaded through to
`media_runtime.ensure_allowed_local_path`, but that function accepts the value
for backward compatibility only and **does not restrict delivery** any more —
eligible local files may be sent from any directory.

What is actually enforced is a fixed **credential / system-path denylist** in
`clawchat_gateway/media_runtime.py`, applied after symlinks are resolved so it
cannot be bypassed via a symlink:

- absolute prefixes `/etc`, `/proc`, `/sys`, `/dev`, `/root`;
- paths under the user's home: `.ssh`, `.aws`, `.gnupg`, `.kube`,
  `.config/gcloud`, `.hermes/.env`;
- each resolved Hermes home and its named profiles: the `clawchat/` plugin data
  directory, `.env`, `config.yaml`, `auth.json`, and plugin database files
  (including SQLite sidecars and existing stores at custom paths);
- the Windows equivalents resolved from the environment (`%SystemRoot%` /
  `%WINDIR%`, and the Credential Manager / DPAPI / Vault directories under
  `%APPDATA%` and `%LOCALAPPDATA%`).

A path that matches the denylist is rejected with a neutral `ValueError` in all
turns; ordinary local files remain available in the owner's direct chat or on a
local operator surface, regardless of `media_local_roots`.

### Known limits of the attachment policy

The plugin judges attachments by the ClawChat turn they come from. Where no
ClawChat turn is in play, it does not restrict further:

- A delivery made with no session context and no trusted live turn nonce (Hermes sends a turn's reply after
  clearing the turn's context; cron delivery) may carry attachments into the
  owner's direct chat, local files and http(s) URLs alike, fetched the ordinary
  way. The only recipient there is the owner. Into any other chat it carries
  no local file. A trusted live reply nonce can retain only that turn's Files
  or artifact permission; a released nonce cannot recover it.
- A `send_message` call from another platform's own session, or with no session
  context, to a non-ClawChat target is left to Hermes and that platform: the
  plugin does not decide what another platform's turns may send.
- Hermes itself checks that a `MEDIA:` path exists before the plugin sees the
  call. The plugin applies its attachment policy afterwards; for a refused
  attachment nothing is read or sent, but that existence check has already happened.

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
