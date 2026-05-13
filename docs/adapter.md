# Adapter — `clawchat_gateway/adapter.py`

Implements `ClawChatAdapter`, a `BasePlatformAdapter` subclass that bridges hermes-agent ↔ the ClawChat WebSocket. Hermes calls `connect`, `send`, `edit_message`, `on_run_complete`, `send_typing`, etc.; the adapter translates these into ClawChat protocol frames via `protocol.py` and hands them to `ClawChatConnection`.

## Module constants

| Name | Value | Purpose |
|---|---|---|
| `TYPING_REFRESH_SECONDS` | `10.0` | Throttle window for repeated `typing.update` active frames. |
| `_THINK_BLOCK_RE`, `_THINK_OPEN_RE` | compiled regex | Strip closed / dangling `<think>...</think>` spans. |
| `_TOOL_TAG_BLOCK_RE`, `_TOOL_TAG_OPEN_RE` | compiled regex | Strip XML-style tool tags (`<tool>`, `<tool_call>`, `<function_call>`, etc.). |
| `_TOOL_FENCE_BLOCK_RE`, `_TOOL_FENCE_OPEN_RE` | compiled regex | Strip ```` ```tool ... ``` ```` fenced tool blocks. |
| `_TOOL_PROGRESS_LINE_RE` | compiled regex | Detect tool-progress ticker lines (used by `_should_suppress_tool_progress`). |
| `_ACTIVATION_INTENT_RE` | compiled regex | Match activation intent: `clawchat`, `claw chat`, `激活码`, `activate`, `activation`, `invite code`. |
| `_CLAWCHAT_SKILL_PROMPT` | str | Injected `channel_prompt` that nudges the LLM toward the `clawchat` skill when activation intent is detected. |

`build_group_channel_prompt()` is imported from `group_context.py` and used only for inbound group messages. The resulting `channel_prompt` is a Hermes ephemeral system prompt path, not a ClawChat WebSocket field.

## Helper dataclass

### `_ActiveRun`

```python
@dataclass
class _ActiveRun:
    chat_id: str
    chat_type: str
    message_id: str
    started_order: int
    last_text: str = ""
    reply_to_message_id: str | None = None
    sequence: int = 0
```

Tracks an in-flight streaming reply keyed by `message_id`. `last_text` feeds `compute_delta`; `sequence` goes into every `message.add` / `message.done` payload.

## Module-level function

### `check_clawchat_requirements(platform_config) -> bool`

Verifies `websockets` is importable and that `platform_config.extra` has non-empty `websocket_url` and `token`. Logs a warning and returns `False` otherwise. Used by the legacy `adapter_factory` patch; the v0.12+ platform registry path has equivalent requirement/config validators in the plugin entrypoint.

## `ClawChatAdapter`

```python
class ClawChatAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    MAX_MESSAGE_LENGTH = 0
```

### Construction

`__init__(self, platform_config)` — builds `ClawChatConfig.from_platform_config(platform_config)`, creates a `ClawChatConnection` bound to `_on_message` / `_on_state_change`, and initialises empty active-run / typing state maps.

### Lifecycle

| Method | Purpose |
|---|---|
| `async connect() -> bool` | Start the connection supervisor; always returns `True`. |
| `async disconnect() -> None` | Stop the connection cleanly. |
| `async get_chat_info(chat_id) -> dict` | Stub: `{"name": chat_id, "type": "direct", "chat_id": chat_id}`. |

### Typing indicators

| Method | Behaviour |
|---|---|
| `async send_typing(chat_id, metadata=None)` | Emit `typing.update` with `is_typing=True`, throttled to one per `TYPING_REFRESH_SECONDS` per chat. |
| `async stop_typing(chat_id, metadata=None)` | Emit `typing.update` with `is_typing=False` (suppressed if already inactive). |
| `_should_skip_typing(chat_id, *, active) -> bool` | Throttle logic backing both methods; updates `self._typing_state[chat_id] = (active, now)`. |

### State & inbound

| Method | Purpose |
|---|---|
| `async _on_state_change(state: ConnectionState)` | Log-only. |
| `async _on_message(frame: dict)` | Route `interaction.submit`, parse via `parse_inbound_message`, skip if filtered, then delegate to `_handle_inbound`. |
| `async _handle_inbound(inbound: InboundMessage)` | Resolve `reply_preview`, download media, build `MessageEvent`, attach group-only covenant `channel_prompt` and activation `auto_skill` / prompt when applicable, then `await self.handle_message(event)`. |
| `_should_attach_activation_skill(text) -> bool` | `True` iff `_ACTIVATION_INTENT_RE` matches. |
| `_compose_channel_prompt(inbound)` | Build the per-event `channel_prompt`: group covenant for `chat_type == "group"`, activation prompt for activation intent, joined with a blank line when both apply. Direct non-activation messages return `None`. |
| `async _download_inbound_media(inbound)` | Thin wrapper around `media_runtime.download_inbound_media`. |

### Outbound streaming

| Method | Signature | Notes |
|---|---|---|
| `async send` | `(chat_id, content="", reply_to=None, metadata=None, **kwargs) -> SendResult` | Suppresses tool-progress noise according to `show_tool_progress`, filters `<think>` / raw tool blocks according to `show_*_output`, builds fragments, then either emits a single `message.reply` (static mode — non-stream config or media-only/rich interaction) or `message.created` + `message.add` with the first delta. Registers an `_ActiveRun` for the new `message_id`. |
| `async edit_message` | `(chat_id, message_id, content) -> SendResult` | Resolve active run; compute delta against `run.last_text`; emit `message.add` with `sequence += 1`. No-op when delta is empty. If a late edit targets a completed run, returns `success=True` without sending frames so Hermes does not fall back to a duplicate send. Returns `success=False, error="no active run for message_id"` only for unknown runs. |
| `async on_run_complete` | `(chat_id, final_text, message_id=None) -> None` | Flush final delta, emit `message.done`, and discard the run from tracking maps. It intentionally does not emit a trailing materialized `message.reply`; static mode is the only path that emits `message.reply`. Duplicate completion for the same completed `message_id` is a no-op. Hermes v0.12+ reaches this through the registered platform adapter lifecycle; legacy installs wire it through `post_stream_hook` / `normal_stream_done_hook` patches. |
| `async send_image` | `(chat_id, image_url, caption=None, reply_to=None, metadata=None) -> SendResult` | Merge `[image_url]` into `metadata["media_urls"]` and delegate to `send`. |
| `async send_image_file` | `(chat_id, image_path, caption=None, reply_to=None, **kwargs) -> SendResult` | Same shape as `send_image` for local paths. |

### Internal helpers

| Method | Purpose |
|---|---|
| `_resolve_chat_type(metadata, kwargs) -> str` | Look up `chat_type` in metadata dict or kwargs, default `"direct"`. |
| `_map_source_chat_type(chat_type) -> str` | Map `"direct"` → `"dm"` for `SessionSource`, leave others unchanged. |
| `_next_run_order() -> int` | Monotonic counter for `_ActiveRun.started_order`. |
| `_resolve_active_run(*, chat_id, message_id=None) -> _ActiveRun \| None` | Lookup by `message_id` (validating `chat_id`) or fall back to latest run for the chat. |
| `_discard_run(run)` | Remove from `_active_runs_by_id`; if it was the latest for its chat, pick a replacement via `_find_latest_run_for_chat` or drop the chat entry. |
| `_remember_completed_run(message_id)` | Bounded completed-run cache used to make late edits / duplicate completion callbacks idempotent. |
| `_find_latest_run_for_chat(chat_id) -> _ActiveRun \| None` | Highest `started_order` whose `chat_id` matches. |
| `_should_use_static_mode(fragments) -> bool` | `True` when `reply_mode != "stream"` or any fragment is non-text. |
| `_filter_output_content(content) -> str` | Apply the `<think>` / tool-block regexes unless the corresponding `show_*_output` flag is on. |
| `_should_suppress_tool_progress(content) -> bool` | `True` when every non-blank line matches `_TOOL_PROGRESS_LINE_RE` and `show_tool_progress` is disabled. |
| `async _build_fragments(content="", metadata=None, kwargs=None) -> list[dict]` | Produce rich interaction fragments when enabled, else `[{"kind": "text", ...}]`, plus any uploaded media fragments (empty text fallback if both are empty). |
| `_build_interaction_fragment(content, metadata, kwargs) -> dict \| None` | Builds `approval_request` from `/approve` + `/deny` fallback text or passes validated metadata `clawchat_interaction` / `interaction` through as `approval_request` / `action_card`. |
| `_handle_interaction_submit(frame) -> None` | Maps `interaction.submit` approve/deny decisions back to Hermes' existing `/approve` / `/deny` text command path because the adapter has no native Hermes approval callback. |
| `async _build_media_fragments(*, media_urls, metadata, kwargs) -> list[dict]` | Delegate to `media_runtime.upload_outbound_media` using adapter config. |
| `_infer_media_kind(*, media_url, index, metadata, kwargs) -> str` | Decide `image` / `audio` / `video` / `file` from a mime hint (per-URL map or parallel list) or the URL path suffix. |
| `_extract_media_mime_hint(...) -> str \| None` | Traverse metadata / kwargs for `media_content_types` or `media_mime_types`. |
| `_lookup_media_mime_hint(carrier, media_url, index) -> str \| None` | Helper for dict-or-list hint sources. |
| `_extract_reply_fields(reply_preview) -> tuple[str \| None, str \| None]` | Extract `reply_to_message_id` + reconstructed text (joining `fragments[*].text` where `kind == "text"`). Handles both top-level and nested `reply_preview.reply_preview` shapes. |
