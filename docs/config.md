# Config — `clawchat_gateway/config.py`

Frozen dataclass wrapping the `extra` section of hermes-agent's `PlatformConfig` for ClawChat. Hermes `config.yaml` keys are snake_case; OpenClaw-style camelCase keys are not read here.

## Helpers

| Function | Signature | Purpose |
|---|---|---|
| `_get_config_value` | `(data: dict, key: str, default=None) -> Any` | Look up one snake_case Hermes config key, else `default`. |
| `_read_env_file_value` | `(name: str) -> str` | Parse `$HERMES_HOME/.env` (default `~/.hermes/.env`); strip `export ` prefix and `"`/`'` quoting; return value for `name` or `""`. |
| `_read_hermes_env_value` | `(name: str) -> str` | Try `hermes_cli.config.get_env_value(name)`; return `""` on import / call failure. |
| `_get_env` | `(*names: str) -> str` | Three-pass lookup over the candidate names. Pass 1: try every name against **process env**. Pass 2: try every name against the **Hermes env helper**. Pass 3: try every name against **`$HERMES_HOME/.env`**. Returns the first non-empty match (or `""`). The pass order means process env always wins over the file even if the file lists a different alias. |

### Env-var resolution priority

For connectivity values (`websocket_url`, `base_url`, `token`, `refresh_token`, `user_id`, `reply_mode`, `group_mode`, `media_local_roots`), `from_platform_config` calls `_get_env(...)` first and only falls back to snake_case keys in `extra` if no env value is found. The full precedence is:

1. **Process env** — `os.environ[name]` for any of the candidate names.
2. **Hermes-managed env** — `hermes_cli.config.get_env_value(name)`, when the `hermes_cli` package is importable.
3. **`$HERMES_HOME/.env`** — line-parsed by `_read_env_file_value`.
4. **`extra` dict** — snake_case Hermes config keys only.
5. **Hardcoded default** — the field's dataclass default.

Tunables that are not surfaced as `CLAWCHAT_*` env vars (stream/reconnect/heartbeat/ack values) skip steps 1–3 and resolve directly from `extra`.

## `ClawChatConfig`

```python
@dataclass(frozen=True)
class ClawChatConfig:
    websocket_url: str
    base_url: str = ""
    token: str = ""
    refresh_token: str = ""
    user_id: str = ""
    reply_mode: str = "stream"
    group_mode: str = "all"
    stream_flush_interval_ms: int = 250
    stream_min_chunk_chars: int = 40
    stream_max_buffer_chars: int = 2000
    reconnect_initial_delay_ms: int = 500
    reconnect_max_delay_ms: int = 15000
    reconnect_jitter_ratio: float = 0.3
    reconnect_max_retries: float = float("inf")
    heartbeat_interval_ms: int = 20000
    heartbeat_timeout_ms: int = 10000
    ack_timeout_ms: int = 15000
    ack_auto_resend_on_timeout: bool = False
    media_local_roots: tuple[str, ...] = field(default_factory=tuple)
    media_download_dir: str = "/tmp/clawchat-media"
    show_tools_output: bool = False
    show_tool_progress: bool = False
    show_think_output: bool = False
    enable_rich_interactions: bool = False
```

Field groups:

- **Connectivity** — `websocket_url`, `base_url`, `token`, `refresh_token`, `user_id`. All are resolved via `_get_env(...)` first (see "Env-var resolution priority" above), then fall back to `extra`.
- **Behaviour** — `reply_mode` (`"stream"` enables live delta sends; anything else falls back to a single `message.reply`), `group_mode` (`"all"` accepts every inbound group message by default; `"mention"` opts into filtering group messages to those that @mention `user_id`).
- **Streaming tunables** — `stream_flush_interval_ms`, `stream_min_chunk_chars`, `stream_max_buffer_chars` (currently consumed externally / not by the adapter directly).
- **Reconnect** — initial delay, max delay, jitter ratio, max retries (`float("inf")` ≈ forever).
- **Heartbeat** — `ping_interval` / `ping_timeout` for the WebSocket library (in ms; divided by 1000 in `connection.py`).
- **Ack** — `ack_timeout_ms`, `ack_auto_resend_on_timeout` (wired through but not currently active in the send path).
- **Media** — `media_local_roots` is the allowlist for local file paths in outbound uploads (`media_runtime.ensure_allowed_local_path`); `media_download_dir` is where inbound media is cached.
- **Filtering** — `show_tools_output`, `show_think_output` — when `False`, the adapter strips `<think>` and tool-invocation blocks from visible content.
- **Progress and interactions** — `show_tool_progress` controls Hermes gateway progress ticker visibility separately from raw tool output; when omitted it inherits `show_tools_output` for compatibility. `enable_rich_interactions` allows rich `approval_request` / `action_card` fragments; when `False`, existing text fallback such as `/approve` and `/deny` is preserved.

### Classmethod

`from_platform_config(cls, platform_config) -> ClawChatConfig`

- Reads `platform_config.extra` (default `{}`).
- Nested `extra["stream"]` provides stream tunables.
- `media_local_roots` from `extra["media_local_roots"]` is coerced to a tuple.
- Booleans for `show_tools_output`, `show_tool_progress`, `show_think_output`, and `enable_rich_interactions` are force-cast with `bool(...)` so truthy strings from env vars round-trip correctly.

**When extending:** add the new field with a default, add a snake_case `_get_config_value` lookup in `from_platform_config`, and update any writer that persists config (e.g., `activate.persist_activation`, `install.configure_clawchat_streaming`) so the roundtrip stays consistent.
