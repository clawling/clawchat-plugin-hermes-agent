# Configuration reference

All settings are resolved by
`clawchat_gateway.config.ClawChatConfig.from_platform_config`. The
priority order is documented in [`./architecture.md`](./architecture.md):
process env → `hermes_cli.config.get_env_value` → `$HERMES_HOME/.env`
→ `platforms.clawchat.extra` → dataclass default.

## Credentials (written by activation)

| Env var                              | `extra.*` key      | Default | Notes |
|--------------------------------------|--------------------|---------|-------|
| `CLAWCHAT_TOKEN`                     | `token`            | —       | Required for the gateway to start. Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_REFRESH_TOKEN`             | `refresh_token`    | —       | Written to `$HERMES_HOME/.env` by activation. |
| `CLAWCHAT_USER_ID`                   | `user_id`          | `""`    | ClawChat user id of the bot account. |
| `CLAWCHAT_AGENT_ID`                  | `agent_id`         | JWT `aid` claim from `CLAWCHAT_TOKEN` | Set by activation. |
| `CLAWCHAT_OWNER_USER_ID`             | `owner_user_id`    | `""`    | Identifies the human owner of the agent account. |

The token / refresh-token pair are **only** ever stored in `.env`; the
plugin never copies them into `config.yaml`
(`activate.persist_activation` calls `extra.pop("token", None)`).

## Connection

| Env var                                              | `extra.*` key   | Default                              | Notes |
|------------------------------------------------------|-----------------|--------------------------------------|-------|
| `CLAWCHAT_BASE_URL`                                  | `base_url`      | `https://app.clawling.com`           | REST API base. Trailing slashes are stripped. |
| `CLAWCHAT_WEBSOCKET_URL` (or `CLAWCHAT_WS_URL`)      | `websocket_url` | `wss://app.clawling.com/ws`          | Derived from `base_url` when activation runs (`activate._derive_websocket_url`). |

## Reply mode and streaming

| Env var                                | `extra.*` key                  | Default        |
|----------------------------------------|--------------------------------|----------------|
| `CLAWCHAT_REPLY_MODE`                  | `reply_mode`                   | `"stream"`     |
| —                                      | `stream.flush_interval_ms`     | `250`          |
| —                                      | `stream.min_chunk_chars`       | `40`           |
| —                                      | `stream.max_buffer_chars`      | `2000`         |
| —                                      | `show_tools_output`            | `false`        |
| —                                      | `show_tool_progress`           | inherits `show_tools_output` |
| —                                      | `show_think_output`            | `false`        |
| —                                      | `enable_rich_interactions`     | `false`        |

`configure_clawchat_streaming` at plugin load also writes the top-level
`streaming.*` block (`enabled=true`, `transport=edit`,
`edit_interval=0.25`, `buffer_threshold=16`) and the
`display.platforms.clawchat.*` block (`tool_progress=off`,
`show_reasoning=false`) into `config.yaml` if any value is missing.

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
`group_mode=mention` requires a structured `@` mention.
`effective_group_mode` / `effective_group_command_mode` in
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
      reply_mode: stream
      show_tools_output: false
      show_think_output: false
streaming:
  enabled: true
  transport: edit
  edit_interval: 0.25
  buffer_threshold: 16
display:
  platforms:
    clawchat:
      tool_progress: off
      show_reasoning: false
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
