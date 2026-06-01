# Configuration reference

All settings are resolved by
`clawchat_gateway.config.ClawChatConfig.from_platform_config`. The
priority order is documented in [`./architecture.md`](./architecture.md):
process env → `hermes_cli.config.get_env_value` → `$HERMES_HOME/.env`
→ `platforms.clawchat.extra` → dataclass default.

Credential tokens are the exception: `CLAWCHAT_TOKEN` and
`CLAWCHAT_REFRESH_TOKEN` are resolved only from env-backed sources, not from
`platforms.clawchat.extra`.

## Credentials (written by activation)

| Env var                              | `extra.*` key      | Default | Notes |
|--------------------------------------|--------------------|---------|-------|
| `CLAWCHAT_TOKEN`                     | —                  | —       | Required for the gateway to start. Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_REFRESH_TOKEN`             | —                  | —       | Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_USER_ID`                   | `user_id`          | `""`    | ClawChat user id of the bot account. |
| `CLAWCHAT_AGENT_ID`                  | `agent_id`         | JWT `aid` claim from `CLAWCHAT_TOKEN` | Set by activation. This is the REST agent record id (`agt_...`), distinct from owner metadata `agent_user_id` (`usr_...`). |
| `CLAWCHAT_OWNER_USER_ID`             | `owner_user_id`    | `""`    | Identifies the human owner of the agent account. |

The token / refresh-token pair are stored in `.env` for runtime resolution and
in plugin SQLite for the latest activation record. The plugin never copies them
into `config.yaml` (`activate.persist_activation` calls `extra.pop("token",
None)` and `extra.pop("refresh_token", None)`).

## Connection

| Env var                                              | `extra.*` key   | Default                              | Notes |
|------------------------------------------------------|-----------------|--------------------------------------|-------|
| `CLAWCHAT_BASE_URL`                                  | `base_url`      | `https://app.clawling.com`           | REST API base. Trailing slashes are stripped. |
| `CLAWCHAT_WEBSOCKET_URL` (or `CLAWCHAT_WS_URL`)      | `websocket_url` | `wss://app.clawling.com/ws`          | Derived from `base_url` when activation runs (`activate._derive_websocket_url`). |

## Rich interactions and display

| Env var                                | `extra.*` key                  | Default        |
|----------------------------------------|--------------------------------|----------------|
| —                                      | `enable_rich_interactions`     | `false`        |
| —                                      | `output_visibility`            | `"normal"`     |
| —                                      | `runtime_status_messages`      | `false`        |

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

## Hermes display settings for ClawChat

Hermes display settings are read from `$HERMES_HOME/config.yaml`, not from
`platforms.clawchat.extra`. During activation, the ClawChat plugin overwrites
four global display settings so ClawChat starts quiet even if the prior Hermes
config used different values:

```yaml
display:
  busy_input_mode: queue
  busy_ack_enabled: false
  background_process_notifications: off
  tool_progress_command: false
```

Activation also writes the `normal` platform-scoped default block while leaving
the setting visible for the operator to edit:

```yaml
display:
  platforms:
    clawchat:
      tool_progress: off
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
| `background_process_notifications` | yes | no | `display.background_process_notifications: off` | Controls notifications from background terminal processes. Valid values: `all`, `result`, `error`, `off`. | The user starts `/background run the deploy check`. | `off` sends no watcher updates for background process output or completion. |
| `tool_progress_command` | yes | no | `display.tool_progress_command: false` | Controls whether messaging users can use `/verbose` to cycle tool progress verbosity. | The user sends `/verbose` in ClawChat. | `false` prevents `/verbose` from enabling or changing tool progress display. |
| `tool_preview_length` | yes | no | `display.tool_preview_length: 0` | Controls maximum tool-call preview length. | Tool progress displays a long shell command. | `0` means no preview length limit; it does not hide previews. |
| `tool_progress` | yes | yes | `display.platforms.clawchat.tool_progress: off` | Controls tool progress messages. Valid values: `off`, `new`, `all`, `verbose`. | The agent runs `rg`, reads files, or executes commands. | `off` hides ClawChat tool progress messages and leaves only final assistant replies. |
| `show_reasoning` | yes | yes | `display.platforms.clawchat.show_reasoning: false` | Controls whether model reasoning/thinking is shown in replies. | The model produces reasoning for a complex question. | `false` hides reasoning in ClawChat replies. |
| `streaming` | yes | yes | `display.platforms.clawchat.streaming: false` | Controls platform streaming display behavior when supported by the gateway adapter. | The agent writes a long reply. | `false` avoids progressive ClawChat reply streaming. |
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
  background_process_notifications: off
  tool_progress_command: false
```

Activation writes this platform-scoped ClawChat block when keys are missing:

```yaml
display:
  platforms:
    clawchat:
      tool_progress: off
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
| —                                      | `group_sessions_per_user` | `true`    | Hermes-compatible group session isolation flag |
| —                                      | `groups.<chat_id>.group_mode`         | inherits `group_mode`         | per-group override          |
| —                                      | `groups.<chat_id>.group_command_mode` | inherits `group_command_mode` | per-group override          |
| —                                      | `groups.<chat_id>.group_sessions_per_user` | inherits `group_sessions_per_user` | per-group override |
| —                                      | `groups["*"].group_mode`              | inherits `group_mode`         | wildcard group default      |
| —                                      | `groups["*"].group_command_mode`      | inherits `group_command_mode` | wildcard group default      |
| —                                      | `groups["*"].group_sessions_per_user` | inherits `group_sessions_per_user` | wildcard group default |

`group_mode=all` makes every inbound group message eligible for a reply;
`group_mode=mention` requires a structured `@` mention.
`group_sessions_per_user=true` keeps Hermes' default group behavior: each
participant in a group gets an isolated session. `group_sessions_per_user=false`
makes the group share one session across participants. In shared group sessions,
sender-specific facts still belong in the current message context, not
session-level prompts.
`effective_group_mode` / `effective_group_command_mode` /
`effective_group_sessions_per_user` in
`clawchat_gateway.config` resolve the precedence (`chat_id` exact →
`"*"` → top-level).

## Allowlist / home channel (read by Hermes platform registry)

| Env var                              | `extra.*` key             | Default        | Notes |
|--------------------------------------|---------------------------|----------------|-------|
| `CLAWCHAT_ALLOWED_USERS`             | —                         | unset          | Hermes-level user allowlist (passed through `register_platform(allowed_users_env=...)`). |
| `CLAWCHAT_ALLOW_ALL_USERS`           | —                         | `"true"`       | `configure_clawchat_allow_all` writes this to `.env` on plugin load so ClawChat users are allowed by default. |
| `CLAWCHAT_HOME_CHANNEL`              | —                         | unset          | When set, enables the plugin's home-channel mode (`env_enablement_fn`). |
| `CLAWCHAT_HOME_CHANNEL_NAME`         | —                         | `"ClawChat"`   | Display name passed to the home channel descriptor. |
| `CLAWCHAT_HOME_CHANNEL_THREAD_ID`    | —                         | unset          | Optional thread id added to the home descriptor. |

Activation sets `CLAWCHAT_HOME_CHANNEL` to the conversation id returned
by `agents-connect` and `CLAWCHAT_HOME_CHANNEL_NAME` to `ClawChat`.

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

## Media

| Env var                                | `extra.*` key         | Default                  | Notes |
|----------------------------------------|-----------------------|--------------------------|-------|
| `CLAWCHAT_MEDIA_LOCAL_ROOTS`           | `media_local_roots`   | `()`                     | OS-pathsep-separated list (env) or array (extra). Roots from which local file paths may be uploaded. |
| —                                      | `media_download_dir`  | `/tmp/clawchat-media`    | Where inbound media gets staged. |

## Memory

`memory_root` is derived from `HERMES_HOME` (or
`hermes_constants.get_hermes_home` when importable) — typically
`$HERMES_HOME/memories`. It is not user-configurable through
`platforms.clawchat.extra`.

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
  background_process_notifications: off
  tool_progress_command: false
  platforms:
    clawchat:
      tool_progress: off
      show_reasoning: false
      streaming: false
      interim_assistant_messages: true
      long_running_notifications: false
      busy_ack_detail: false
      cleanup_progress: false
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
