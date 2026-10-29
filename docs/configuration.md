# Configuration reference

For operators looking up environment variables, `platforms.clawchat.extra` keys and activation defaults.

`ClawChatConfig.from_platform_config` reads profile-first: Hermes' `get_env_value_prefer_dotenv` → profile `.env` → process env → `extra` → factory default (exceptions below). Process env is last among env sources to prevent a named profile inheriting another agent's credentials/home channel, while allowing env-only deployments.

With a secret scope installed or multiplexing active, `_get_env` uses profile `.env` then Hermes' `get_scoped_secret`; it follows the host's scope rules and adds no raw-process fallback for secondary profiles. Empty `.env` tombstones for `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` suppress env fallbacks. [Debug switches](#debug--diagnostics-env-vars) read process env directly.

`HERMES_HOME` resolves through `hermes_home()` (including multiplex per-profile overrides): exported value, else `~/.hermes` on POSIX or `%LOCALAPPDATA%\hermes` on Windows.

At startup, the runtime adopts the latest complete SQLite activation row as the durable credential source; env credentials bootstrap only the no-row case and are seeded into SQLite on first refresh. To replace an existing row, re-pair or clear it and restart; see [credential recovery](token-refresh.md#c2-hermes-recovery-unification).

## Credentials (written by activation)

| Env var | `extra.*` key | Default | Notes |
| --- | --- | --- | --- |
| `CLAWCHAT_TOKEN` | — | — | Required to connect to ClawChat. If absent, the adapter waits for activation without blocking gateway startup. Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_REFRESH_TOKEN` | — | — | Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_USER_ID` | `user_id` | JWT `sub`, else env → extra → empty | The token's `sub` is authoritative; mismatches warn and use `sub`. This plugin intentionally derives the bot user id from JWT claims; an owner id here breaks self-echo detection. |
| `CLAWCHAT_AGENT_ID` | `agent_id` | empty | Env → JWT `aid` → `extra.agent_id`; token identity precedes possibly foreign-profile extra. REST agent record (`agt_...`), distinct from `agent_user_id` (`usr_...`). |
| `CLAWCHAT_OWNER_USER_ID` | `owner_user_id` | JWT `oid`, else empty | Env → extra → JWT `oid`; human owner identity, required by `_has_connect_credentials` to leave `activation_wait`. |

Activation also stamps one non-credential identity key:

| Env var | `extra.*` key | Default | Notes |
| --- | --- | --- | --- |
| — | `profile` | unset | Activation stamps `_active_profile_name()` to detect a foreign-profile config on boot; see [profile isolation](activation.md). |

Activation writes the credential pair to SQLite and `.env`, never `config.yaml`; legacy `extra.token` / `extra.refresh_token` (default empty) remain config-reader fallbacks when env is empty.

Automatic refresh, permanent-failure markers, empty credential tombstones and access-token latches follow [token-refresh.md §C](token-refresh.md#c-auto-logout-on-permanent-expiry) and [activation recovery](activation.md#automatic-token-refresh--auto-logout).

## Connection

| Env var | `extra.*` key | Default | Notes |
| --- | --- | --- | --- |
| `CLAWCHAT_BASE_URL` | `base_url` | `https://app.clawling.com` | REST API base. Trailing slashes are stripped. |
| `CLAWCHAT_WEBSOCKET_URL` (or `CLAWCHAT_WS_URL`) | `websocket_url` | `wss://app.clawling.com/ws` | Activation records the env value when set, else derives one from `base_url` (`activate._derive_websocket_url`). |
| `CLAWCHAT_DEVICE_ID` | — | derived (below) | Pin per agent in container deployments; used verbatim with `hermes-` prefix, otherwise sanitized to `[A-Za-z0-9_.:-]` and prefixed. |
| `CLAWCHAT_WIKI_VERSION` | — | empty | Optional install-page stamp for connect/connect-check onboarding telemetry; profile-first, trimmed to 40 characters (`onboarding.wiki_version`), never gates activation. |

### Device id durability (deployment requirement)

Pin `CLAWCHAT_DEVICE_ID` to a stable, per-agent value in the pod environment or profile `.env`:

```yaml
env:
  - name: CLAWCHAT_DEVICE_ID
    value: "hermes-agt-<stable-agent-id>"
```

The server's per-device cursor requires stability: a changed id starts replay at 0 and leaves an orphan cursor pinning ack-aware retention. Presented id order: SQLite `activations.device_id` → token `did` → legacy host id when a token exists → `get_device_id()` for a truly unpaired process, persisted on first connect.

The legacy host id uses the env pin or host fingerprint; fresh unpinned ids use macOS `IOPlatformUUID` → Linux machine-id → hostname+MAC hash, with `-p<12 hex>` for named profiles. The default profile keeps the host value. SQLite/token ids are re-read each boot and survive recreation if storage persists; `connections.resolved_device_id` records the server's handshake result for diagnostics.

### Device id is also the token-refresh precondition

Refresh requires `X-Device-Id` equal to the connect-time id. An unpinned volatile fingerprint can change on reschedule, causing permanent **device mismatch**, auto-logout and manual re-pair; the boot WARNING points here. It fires only on fingerprint fallback, not when SQLite, token `did` or an env pin supplies the id. See [automatic refresh](activation.md#automatic-token-refresh--auto-logout).

## Rich interactions and display

| Env var | `extra.*` key | Default |
| --- | --- | --- |
| — | `enable_rich_interactions` | `false` |
| — | `output_visibility` | `"normal"` |
| — | `runtime_status_messages` | `false` |
| — | `awareness_note` | `false` |
| — | `friend_greeting` | `true` |
| — | `liveware_sample` | `true` |
| — | `stream_replies` | `false` |

`output_visibility` is the `minimal|normal|full` preset controlled by `/clawchat-output`; see [output visibility](output-visibility.md) for mappings. The command aligns `runtime_status_messages` to `false|false|true`; the adapter derives runtime-status delivery from the preset. Without a preset, legacy `runtime_status_messages: true` resolves to `full`; otherwise default is `normal`. Other output categories use the host controls below.

`awareness_note` emits one debounced, content-free owner-DM note for `friend.added`, `friend.removed`, `friend.profile_updated` and `conversation.*` except `conversation.dissolved`; disabled events are still logged. `moment.comment.created` / `moment.comment.replied` notes are per-event and independent.

`friend_greeting` starts a synthetic greeting for a newly added non-owner friend independently of `awareness_note`; [greeting flows and conditional language instructions](reference/prompts.md#greeting-flows-and-language) describe lookup, dedupe and partial overrides. `liveware_sample` controls [demo-app auto-boot](liveware-sample.md) on first activation; set `false` to disable it.

### The agent's own files

Agent files resolve through `agent_files` / `hermes_home()`, including multiplex per-profile homes:

| File / profile | Location or read rule |
| --- | --- |
| `greeting.md`, `friend-greeting.md`, `onboarding.json` | `$HERMES_HOME/clawchat/` for this agent |
| default profile | Profile path first; missing file falls back to `~/clawchat/<file>` |
| named profile or custom `HERMES_HOME` | Profile path only |
| present, empty greeting file | Reset to built-in prompt; no legacy fallback |
| owner-DM prompt | `ClawChat Agent Files` supplies absolute `agent_files_dir`; use it because shell `HOME` can differ (`$HERMES_HOME/home` in containers) |

### Reply streaming (experimental)

`stream_replies` (default `false`) is **experimental: only enable it when every
ClawChat client your agent talks to renders Protocol-v2 §8 streaming**
(`message.created` / `message.add` / `message.done` / `message.failed`, see
[`./client-integration.md`](./client-integration.md) §8.0). It is the Hermes
counterpart of the OpenClaw plugin's `streamReplies`.

Streaming needs both switches:

| `extra.stream_replies` | `display.platforms.clawchat.streaming` | What the owner sees |
| --- | --- | --- |
| `false` (default) | `false` (default) | Hermes sends each reply once, finished; the adapter sends it as one `message.reply`. |
| `false` | `true` | Hermes sends a first chunk and edits it; the adapter buffers those drafts and still sends one `message.reply` at the end. No streaming frame is ever sent. A reply Hermes never finalizes (`/stop`, `/new`) is dropped. |
| `true` | `false` | Same as the default: Hermes produces no drafts to stream. |
| `true` | `true` | A direct-chat reply streams and ends as one message; group replies are sent whole. |

Activation fills missing `display.platforms.clawchat.streaming` with `false`;
every `/clawchat-output` preset overwrites it to `false`. Plugin load never
changes it. Set both keys yourself, then restart the gateway:

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

These Hermes-owned keys live in `$HERMES_HOME/config.yaml`, outside `platforms.clawchat.extra`. Activation overwrites the four global display values and both agent values below; it only fills missing keys in `display.platforms.clawchat`, preserving operator overrides. It never writes top-level `streaming.*`. Unknown host keys stay visible for future compatibility.

```yaml
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
agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
```

**Quote `"off"`** for `tool_progress` and `background_process_notifications`: these are string enums; YAML 1.1 reads bare `off` as boolean `false`, which differs from the strings the plugin writes/compares.

| Setting | Global setting exists | Platform override exists | Recommended ClawChat location | Function |
| --- | --- | --- | --- | --- |
| `busy_input_mode` | yes | no | `display.busy_input_mode: queue` | Busy input: `interrupt`, `queue`, `steer`. |
| `busy_ack_enabled` | yes | no | `display.busy_ack_enabled: false` | Busy acknowledgment. |
| `background_process_notifications` | yes | no | `display.background_process_notifications: "off"` | Background process notices: `all`, `result`, `error`, `off`. |
| `tool_progress_command` | yes | no | `display.tool_progress_command: false` | Allow `/verbose` to cycle tool progress. |
| `tool_preview_length` | yes | no | `display.tool_preview_length: 0` | Max tool preview length; `0` is unlimited. |
| `tool_progress` | yes | yes | `display.platforms.clawchat.tool_progress: "off"` | Tool progress: `off`, `new`, `all`, `verbose`. |
| `show_reasoning` | yes | yes | `display.platforms.clawchat.show_reasoning: false` | Show model reasoning. |
| `streaming` | yes | yes | `display.platforms.clawchat.streaming: false` | Host drafts/edits; also needs `extra.stream_replies`. |
| `interim_assistant_messages` | yes | yes | `display.platforms.clawchat.interim_assistant_messages: true` | Separate interim assistant messages. |
| `long_running_notifications` | no | yes | `display.platforms.clawchat.long_running_notifications: false` plus `agent.gateway_notify_interval: 0` | Long-running heartbeat notices. |
| `busy_ack_detail` | no | yes | `display.platforms.clawchat.busy_ack_detail: false` | Busy/heartbeat runtime detail. |
| `cleanup_progress` | no | yes | `display.platforms.clawchat.cleanup_progress: false` | Delete progress/status bubbles where supported. |

See [display effects](output-visibility.md#individual-display-effects) for each setting's concrete result.

| Environment variable | Host key | Values |
| --- | --- | --- |
| `HERMES_GATEWAY_BUSY_INPUT_MODE` | `display.busy_input_mode` | `interrupt`, `queue`, `steer` |
| `HERMES_GATEWAY_BUSY_ACK_ENABLED` | `display.busy_ack_enabled` | `true`, `false` |
| `HERMES_BACKGROUND_NOTIFICATIONS` | `display.background_process_notifications` | `all`, `result`, `error`, `off` |

`tool_progress_command` has no confirmed official env variable. Later activation overwrites manual changes to the four global display keys.

## Group behavior

| Env var | `extra.*` key | Default | Values |
| --- | --- | --- | --- |
| `CLAWCHAT_GROUP_MODE` | `group_mode` | `"all"` | `all`, `mention`; invalid → `all` |
| `CLAWCHAT_GROUP_COMMAND_MODE` | `group_command_mode` | `"owner"` | `owner`, `all`, `off`; invalid → `owner` |
| — | `groups.<chat_id>.group_mode`, `groups["*"].group_mode` | inherit | Same values |
| — | `groups.<chat_id>.group_command_mode`, `groups["*"].group_command_mode` | inherit | Same values |
| — | `group_sessions_per_user`, `groups.<chat_id>.group_sessions_per_user`, `groups["*"].group_sessions_per_user` | `false` / inherit | Legacy boolean opt-in; `true` warns once at startup |

Static precedence is exact chat id → `"*"` → top-level. `all` permits every group message; `mention` requires this agent in structured `mention` fragments or `context.mentions`, including the reserved `"all"` (@everyone) sentinel ([protocol §10.2](client-integration.md)).

### One session per group

Each group shares one Hermes session using `GROUP_SHARED_SESSION_USER_ID`; transcript lines and `ClawChat Group Message Metadata` retain real speakers. Direct chats keep one session per conversation. An explicit legacy `group_sessions_per_user: true` still splits by speaker; the top-level opt-in comes from raw `config.yaml`, ignoring Hermes' injected host-wide default.

Session names use group title (and speaker nickname only for legacy per-speaker sessions); shared sessions leave `user_name` empty. Direct chats use the peer nickname for both names; uncached `chat_name` falls back to conversation id (`ClawChatAdapter._session_names`).

### Group session seeding and queueing

Seeding happens **once per group**, tracked durably by SQLite `group_shared_sessions`; later `/new` is empty, never re-seeded. The seed keeps newest messages within `rebuild-recent-messages` / `rebuild-recent-chars`, presents them oldest first as context-only `Name (usr_…): text` / `you: text` in the user message, and uses cached profile nicknames when needed (bare id if unknown). Slash commands are excluded from seeds too.

Later @-mentions add unseen messages within `delta-budget-chars` (newest kept), excluding own replies, current-batch messages and slash commands. Overflow gets a count/location hint and counts as delivered; in-memory delivered ids mean the first mention after restart may repeat up to that budget. Commands and synthetic turns get no history; legacy per-speaker sessions use 10 preceding messages without speakers.

Group turns run serially until Hermes releases the session guard; the coalescer accumulates the next ordered batch with its own metadata. Slash commands bypass batching so `/stop` and `/new` reach busy groups immediately; this avoids host-version-dependent busy handling.

### Owner-controlled per-group settings and batching

Server settings (`GET /v1/agents/me/group-settings`) override static reply mode: `muted`, `reply_mode` (`all|mention`), `batch_delay_seconds` (1–3600). Read at startup, reconnect and `agent.config.changed`; absent rows mean unmuted/static mode/10 s, and missing delay means 10 s.

Arrival waits at most 5 s for an in-flight read, then gates **muted → slash command (`group_command_mode`) → mention**; filtered messages still enter the ledger. Commands therefore work in mention-only groups. Leading mentions are stripped from `@agent /new`; they must include this agent or `"all"`, not only another agent.

Owner command receipts go to the owner's activation DM, headed `From group "<group title>": <command>`, while Hermes runs the command on the group session; the first delivered receipt gets a ✅ reaction even on failure. Reset/undo confirmations go there too: bare `/approve`, `/always`, `/cancel` answer the group confirm; plain words, `/approve <code>` and replies while the DM's own dangerous-command approval is pending do not. Another slash command drops the pending confirm.

Receipts stay in-group for non-owner commands (`all`), confirm/approval replies, and agent-turn commands (`/plan`, `/learn`, `/init`, `/blueprint`, `/btw`, `/background`, `/queue`, `/steer`, `/moa`, `/goal`, `/retry`). Missing activation DM or failed delivery falls back to the group with a log warning.

Non-mention messages flush after the configured quiet delay, at most `max(30 s, batch_delay_seconds)`; a mention flushes immediately. **At flush**, wait for settings again and re-check muted/mention: drop a muted batch or a mention-only batch with no mention, including messages queued before the settings changed.

## Session and note keys

Read at adapter startup from `platforms.clawchat.extra` (hyphenated); restart the gateway after changes. Integer parse failures use the factory default; out-of-range values clamp to the nearest bound. Boolean keys accept on/off or true/false; invalid values use the default. String enums such as `group_command_mode: "off"` must also be quoted under YAML 1.1.

| Key | Default | Range / values | Use |
| --- | --- | --- | --- |
| `note-cap-user` | `1500` | 300–6000 | Per-person note chars per turn |
| `note-cap-group` | `2000` | 300–8000 | Group note chars per turn |
| `note-cap-turn` | `4000` | 1000–16000 | Total injected note chars |
| `rebuild-recent-messages` | `20` | 5–100 | Group seed message count |
| `rebuild-recent-chars` | `4000` | 1000–32000 | Group seed char budget |
| `delta-budget-chars` | `8000` | 2000–32000 | Unseen group context char budget |
| `session-cap-tokens` | `150000` | 50000–1000000 | Absolute compression cap |
| `sediment-margin-tokens` | `10000` | 2000–50000 | Margin below compression point |
| `sediment-on-compact` | `on` | on/off | Sediment before compression |
| `sediment-on-reset` | `on` | on/off | Sediment before `/new` / `/reset` |

Unconsumed shared keys: `idle-reset-dm`, `idle-reset-group`, `daily-reset` (host owns resets; see [Upgrade notes](#upgrade-notes)); `archive-muted-groups` (ledger records muted messages); `digest-every-messages` (Hermes already has sessions). Plugin-driven reset would need the host's confirm-guarded `/new`.

### Sediment turns

Hermes has no pre-compression/reset memory flush; its periodic single-user review is disabled below. The plugin runs a synthetic sediment turn in the same session before losing context:

- `/new` / `/reset`: finish sediment before the host command/confirmation; skip busy sessions or those with nothing said since reset, so reset remains an immediate escape.
- Compression: after a turn reaches the trigger, run when idle, once per Hermes session id. The compression point is the lower of the absolute cap and Hermes' ratio threshold (default 0.5; 0.75 below 512 K, accounting for output reservation). Plugin load/activation **fill only missing** `compression.threshold_tokens` from `session-cap-tokens`; this host key is global across platforms (Hermes 0.20.1+).

Trigger example without output reservation: `0.75 × 128000 − 10000 = 86000`, rather than `150000 − 10000 = 140000`. Computation uses `post_api_request` usage/context length, `pre_api_request` max tokens and Hermes `ContextCompressor` math when importable (same fallback rule). Per-model ratio overrides are unseen; until the first request supplies a window, the cap alone decides.

The prompt targets only this DM peer's note (`owner.md` for owner) or this group's note, up to 10 recent speakers' notes and owner facts said in-group. Shared groups also receive unseen context within `delta-budget-chars`. It asks the model to read then append, omit facts people asked it not to remember/asked to forget, avoid Hermes `memory`, send no messages and return the no-reply token.

**The plugin does not filter what a sediment turn writes**: prompt compliance is the model's judgement; memory-tool read scope remains enforced. Text, edits, media and typing to this chat are dropped until `on_processing_complete`; other chats work, the next group batch waits, and the synthetic exchange stays in history. With `display.busy_input_mode: interrupt`, newer hosts may fold a DM into sediment and drop its answer; default `queue` avoids this. See [prompt injection](reference/prompt-injection.md).

## Allowlist / home channel (read by Hermes platform registry)

| Env var | `extra.*` key | Default | Notes |
| --- | --- | --- | --- |
| `CLAWCHAT_ALLOWED_USERS` | — | unset | Registered with Hermes but ineffective while forced `CLAWCHAT_ALLOW_ALL_USERS=true` bypasses the allowlist. |
| `CLAWCHAT_ALLOW_ALL_USERS` | — | `"true"` | Forced to `true` in `.env` on every plugin load, overwriting `false`; cannot be disabled through this setting. Access control uses plugin owner/grant rules (below). |
| `CLAWCHAT_HOME_CHANNEL` | — | unset | When set, enables the plugin's home-channel mode (`env_enablement_fn`). |
| `CLAWCHAT_HOME_CHANNEL_NAME` | — | `"ClawChat"` | Display name passed to the home channel descriptor. |
| `CLAWCHAT_HOME_CHANNEL_THREAD_ID` | — | unset | Optional thread id added to the home descriptor. |

Activation sets `CLAWCHAT_HOME_CHANNEL` to the conversation id returned
by `agents-connect` and `CLAWCHAT_HOME_CHANNEL_NAME` to `ClawChat`. The plugin
registers `CLAWCHAT_HOME_CHANNEL` as the platform's `cron_deliver_env_var`, which
Hermes' cron pre-flight requires before it accepts `deliver=clawchat`.

### Slash commands in direct chats

The plugin gates Hermes slash commands in direct chats.
See [slash commands in direct chats](tool-grants.md#slash-commands-in-direct-chats) for the non-owner allow-list, confirmation handling and gateway-control boundary.

### Host tools in turns the owner did not start

| `extra.*` key | Default | Values |
| --- | --- | --- |
| `non-owner-host-tools` | `off` | `off` / `approve` |
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

| `extra.*` key | Default |
| --- | --- |
| `reconnect_initial_delay_ms` | `500` |
| `reconnect_max_delay_ms` | `15000` |
| `reconnect_jitter_ratio` | `0.3` |
| `reconnect_max_retries` | `inf` |
| `heartbeat_interval_ms` | `20000` |
| `heartbeat_timeout_ms` | `10000` |
| `ack_timeout_ms` | `15000` |
| `ack_auto_resend_on_timeout` | `false` |

`ack_auto_resend_on_timeout` is **parsed but not implemented.** It is read into
`ClawChatConfig` (`config.py`) and nothing consumes the field, so setting it to
`true` changes no behaviour today. Do not rely on it for delivery guarantees.

These reconnect values are **fixed constants, not `extra.*` keys**
(`clawchat_gateway/connection.py`):

| Constant | Value | Purpose |
| --- | --- | --- |
| `BACKOFF_RESET_AFTER_SECONDS` | `5.0` | READY time after which the exponential backoff resets to `reconnect_initial_delay_ms`. **Not applied when the connection ended in a takeover** — see below. |
| `TAKEOVER_BACKOFF_FLOOR_SECONDS` | `60.0` | Minimum wait before reconnecting after the server closed the socket with `4001` (a newer session for the same `(user_id, device_id)` took over). Doubles per consecutive eviction. |
| `TAKEOVER_BACKOFF_MAX_SECONDS` | `600.0` | Ceiling for that escalation. |
| `INSTANCE_LOCK_RETRY_SECONDS` | `30.0` | Retry a host-local lock while in STANDBY; these waits do not consume `reconnect_max_retries`. |
| `INSTANCE_LOCK_REMINDER_SECONDS` | `600.0` | Minimum interval between WARNING reminders while a lock remains held elsewhere. |

The takeover floor exists because mutual evictions can occur farther apart
than `reconnect_max_delay_ms` (15 s), so ordinary exponential backoff alone
cannot reliably break a loop between two instances sharing a device id.
See `docs/client-integration.md` §3.6.

### Host-local single-instance locks

A contended profile or identity lock puts the gateway in `STANDBY` without connecting or refreshing. Do not delete live lock files; inspect `hermes clawchat status` and see the [lock runbook and specification](instance-locks-and-install-conflicts.md#host-local-single-instance-locks).

### Installation conflicts and recovery

Close `4010` leaves the installation `SUPERSEDED`, preserving credentials and stopping reconnect/refresh across restarts. On the installation you want to use, run:

```bash
hermes clawchat status
hermes clawchat reclaim
```

See the [recovery runbook](instance-locks-and-install-conflicts.md#operator-runbook) for logs, generation failures and when to restart, and the [conflict specification](instance-locks-and-install-conflicts.md#installation-conflicts-and-recovery) for persistence and notice guarantees.

## Typing

| `extra.*` key | Default |
| --- | --- |
| `typing_max_continuous_seconds` | `900.0` |

Hermes refreshes typing every 2 s; the adapter collapses calls closer than 1.5 s (`TYPING_REFRESH_SECONDS`), sends `false` at turn end/graceful shutdown, and sends nothing for sediment (reference-client expiry: 6 s).

`typing_max_continuous_seconds` bounds one chat's continuous indicator, never its reply; `stop_typing` resets the window and the next `send_typing` starts it again. Unparseable, blank, `0` and negative values fall back to `900.0`; there is no disable sentinel. Lower values can hide typing during legitimate long runs.

## Dissolved-conversation eviction

`conversation.dissolved` adds the chat to a 512-entry FIFO dead-chat set, drops per-chat state (typing, rate-limit window, chat type, active runs), and suppresses inbound processing and typing uplinks. This in-memory set resets on restart; `typing_max_continuous_seconds` remains the backstop when no dissolve signal arrives.

## Media

The six inbound download env keys (`ALLOW_HTTP` through `DOWNLOAD_DEADLINE_SECONDS` below) override extra. Numeric limits must be positive and finite (booleans are invalid); parsing failures use factory defaults. Proxy `on`/`off` also accept YAML 1.1 booleans.

| Env var | `extra.*` key | Default | Notes |
| --- | --- | --- | --- |
| `CLAWCHAT_MEDIA_BASE_URL` | `media_base_url` | `""` | Overrides the host used for `/media/upload` and media downloads. When empty, the base is derived from `websocket_url` / `base_url` (`media_runtime.derive_base_url`). Trailing slashes are stripped. Set it when media is served from a different origin than the REST API. |
| `CLAWCHAT_MEDIA_LOCAL_ROOTS` | `media_local_roots` | `()` | OS-pathsep-separated list (env) or array (extra). **Legacy / no longer restricting.** See below. |
| — | `media_download_dir` | `/tmp/clawchat-media` | Where inbound media gets staged. |
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
read straight from the process environment (`os.getenv`), because
they are per-process debugging switches rather than per-profile settings. All
of them default to off and none of them are written by activation.

| Env var | Read by | Effect |
| --- | --- | --- |
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
| --- | --- |
| `CLAWCHAT_HERMES_BIN` | The resolved `hermes` executable the launcher `exec`s. |
| `CLAWCHAT_GATEWAY_RESTART_DELAY` | Seconds the launcher sleeps before restarting the gateway (from the caller's `delay_seconds`, default `2`). |

Both are **set** by the plugin, not read from the operator's environment — the
child env is a copy of the parent's environment with these keys overwritten last, so
exporting them yourself has no effect.

## Database location

Plugin SQLite lives under `$HERMES_HOME/clawchat/`: `clawchat.sqlite` for the default profile, `clawchat-<profile>.sqlite` for named profiles. See [Upgrade notes](#upgrade-notes) for relocation.

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
by hand is kept.

Once per profile after the owner DM is known, `memory_migration.py` counts every `USER.md` entry and person-like `MEMORY.md` entries (`§`-separated, naming an `usr_` id/known nickname or reading as a personal fact), then offers a localized maintenance turn if any exist.
The model must obtain owner agreement and write each target note before removing the global entry; the plugin itself never edits, moves or deletes these files.
`$HERMES_HOME/clawchat/.memory-migration-hint-done` marks the check; delete it to ask again.

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
      profile: default  # active Hermes profile name
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
agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
memory:
  user_profile_enabled: false
  nudge_interval: 0
compression:
  threshold_tokens: 150000
```

`$HERMES_HOME/.env` after activation and plugin load (ALLOW_ALL is written by plugin load, not activation; home-channel keys require an activation conversation):

```ini
CLAWCHAT_TOKEN=...
CLAWCHAT_REFRESH_TOKEN=...
CLAWCHAT_ALLOW_ALL_USERS=true
CLAWCHAT_HOME_CHANNEL=cnv_...
CLAWCHAT_HOME_CHANNEL_THREAD_ID=
CLAWCHAT_HOME_CHANNEL_NAME=ClawChat
```

## Upgrade notes

- 0.14.0-86 introduced named-profile device suffixes for fresh pairings; existing pairings retain SQLite/token/legacy ids.
- Through 0.14.0-100 groups defaulted to per-speaker sessions; upgrade starts shared sessions, keeps old sessions searchable and seeds once; explicit legacy `true` remains honored.
- Agent files moved from shared `~/clawchat/` to `$HERMES_HOME/clawchat/`; only the default profile retains missing-file legacy fallback.
- `media_local_roots` remains parsed for compatibility but does not restrict outbound delivery; see [attachment policy](non-owner-boundaries.md#media_local_roots-no-longer-scopes-outbound-delivery).
- Old `$HERMES_HOME/clawchat.sqlite` moves to `$HERMES_HOME/clawchat/` with WAL sidecars on first start; no re-pairing is needed.
- Legacy `extra.token` / `extra.refresh_token` migrate out of `config.yaml`; SQLite is authoritative and `.env` bootstraps the no-row case.
- Grant migrations 14–16 cannot be downgraded; never share the DB between old/new processes ([storage upgrade](tool-grants.md#storage-upgrade)).
- Hermes through 0.21.0 reset sessions daily at 04:00 or after 24 h idle without a plugin hook; 0.21.1+ uses compression only.
- Hermes has read `memory.user_profile_enabled` and `memory.nudge_interval` since v0.12.0.
