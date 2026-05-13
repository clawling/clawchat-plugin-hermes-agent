# Tests — `tests/`

Pytest-asyncio with `asyncio_mode = "auto"` (from `pyproject.toml`), so async tests do not need `@pytest.mark.asyncio`. Run with `pytest`, or target a single file/test (`pytest tests/test_adapter.py::test_name`).

## Harness

### `tests/conftest.py`

Inserts `src/` onto `sys.path`, then calls `fake_hermes.install()` to register stub modules for `gateway`, `gateway.config`, `gateway.platforms`, `gateway.platforms.base`. Every test file pulls in this harness automatically.

### `tests/fake_hermes.py` — hermes-agent stubs

Provides in-process replacements so the adapter can be imported without a real hermes-agent checkout:

- `_Platform` enum (`CLAWLING`, `QQBOT`, `CLAWCHAT`).
- `_MessageType` enum (`TEXT`, `IMAGE`).
- `_SessionSource` dataclass (`platform`, `chat_id`, `user_id`, `chat_name`, `chat_type`, `thread_id`).
- `_MessageEvent` dataclass (all the fields the adapter sets: `text`, `source`, `raw_message`, `media_urls`, `media_types`, `reply_to_message_id`, `reply_to_text`, `auto_skill`, `channel_prompt`, `timestamp`).
- `_SendResult` dataclass.
- `_BasePlatformAdapter` base class with `build_source()` and `handle_message()` — the test subclass's `handled: list[_MessageEvent]` records everything the adapter would have dispatched to hermes-agent.
- `install()` — writes the stub module objects into `sys.modules` so `from gateway.platforms.base import BasePlatformAdapter` resolves here.

When you add a new import from `gateway.*` in production code, extend `fake_hermes.py` or tests will fail at import time.

### `tests/fake_ws.py` — in-memory WebSocket peer

- `_ConnectionBuffers` — `client_outbox` / `server_outbox` asyncio queues + a `closed` flag.
- `FakeClientConnection` — implements `send`, `close`, `ping`, `__aiter__`/`__anext__` over the buffers. `None` put on `server_outbox` signals close.
- `FakeClawChatServer`:
  - `async connect(url, **kwargs)` — push a new `_ConnectionBuffers`, return a `FakeClientConnection`. Patched into `connection._ws_connect_impl` by tests.
  - `enqueue_from_server(frame, *, connection_index=-1)` — push a server → client frame.
  - `async read_client_frame(timeout=1.0, *, connection_index=-1)` — pop a client → server frame.
  - `async disconnect(*, connection_index=-1)` — close the peer.
  - `set_auto_fail(value)` — next `connect` raises `ConnectionError("fake auto-fail")`, for backoff tests.
  - `connect_calls` — log of every `connect(url, kwargs)` invocation.

## Test files

### `tests/test_activate.py`

- `test_persist_activation_writes_secrets_to_env_and_config_without_secrets` — monkeypatches `$HERMES_HOME`; calls `activate.persist_activation` and checks that `.env` has the ClawChat tokens while `config.yaml` has enabled ClawChat, non-secret `extra` keys, and streaming/display defaults.
- `test_persist_activation_removes_stale_config_secrets_and_refresh_env` — ensures a reactivation removes old YAML token fields, updates `CLAWCHAT_TOKEN`, removes stale `CLAWCHAT_REFRESH_TOKEN` when no refresh token is returned, and preserves unrelated `.env` entries.

### `tests/test_group_context.py`

- `format_group_covenant_prompt` returns `None` for blank covenant text.
- Non-empty covenant text is wrapped under `ClawChat group covenant:`.
- `build_group_channel_prompt` formats the default covenant; tests monkeypatch the default with fixture text.

### `tests/test_adapter.py`

Uses a `FakeConnection` stand-in so no WebSocket I/O is performed. Coverage:

- `compute_delta` behaviour for append and reset cases.
- `_on_message` — builds `MessageEvent`, attaches a group-only covenant through `channel_prompt`, preserves direct messages without group covenant text, composes group covenant + activation prompt when both apply, attaches the `clawchat` skill on activation-intent text, downloads media before dispatch, logs inbound parse / dispatch, logs parse drops, maps `reply_preview` fields.
- `send` — static mode (`message.reply`); default filtering of `<think>` and tool output; override via `show_*_output`; suppression and preservation of gateway tool-progress tickers (both for `send` and `edit_message`); logging.
- Typing indicators — active / inactive / dedupe.
- Streaming mode — `message.created` → `message.add` sequence, incomplete-block filtering before delta; `edit_message` delta emission; targeting by `message_id` when multiple runs overlap; `on_run_complete` emits `message.done` + `message.reply` and finalises the requested run during overlap.
- Outbound media — forces static mode when media is present; classifies non-image MIME correctly; uploads local files before the static reply; `send_image_file` path.

### `tests/test_api_client.py`

Uses a local `BaseHTTPRequestHandler` fixture (`api_server`) to verify:

- `agents_connect` posts the fixed `{platform, type}` body and honours optional `tools` list.
- `upload_media` sends multipart with a Bearer token to `/media/upload`.
- `upload_avatar` uses `/v1/files/upload-url`.
- `update_my_profile` PATCHes `/v1/users/me`.
- Constructor validation rejects a non-http scheme.

### `tests/test_config.py`

- `test_config_defaults` — `ClawChatConfig.from_platform_config` with an empty extra falls back to every documented default.
- `test_config_reads_snake_case_hermes_extra_keys` — verifies Hermes `platforms.clawchat.extra` snake_case keys populate every config field.
- `test_config_ignores_openclaw_camel_case_extra_keys` — verifies OpenClaw-style camelCase keys are ignored by Hermes config loading.

### `tests/test_connection.py`

Patches `connection._ws_connect_impl` with `FakeClawChatServer.connect` and exercises the full state machine:

- Legacy handshake reaches `READY` (challenge → connect → hello-ok).
- Realtime subprotocol (`/v1/ws`) skips the handshake.
- `message.send` is ignored before `READY`.
- Bearer auth header is present on connect.
- Correct subprotocols are sent.
- Wrong `request_id` in hello-ok times out.
- Outbound frames queued before `READY` flush in order after `READY`.
- Connection logs receive / dispatch / send at the info level.
- Ready transition preserves queued frame ordering.
- Queued frames survive a failed flush + reconnect.
- A send failure while `READY` re-queues for the next connection.
- Backoff progresses both for repeated `connect` failures and for flapping `READY` sessions shorter than `BACKOFF_RESET_AFTER_SECONDS`.

### `tests/test_device_id.py`

- `CLAWCHAT_DEVICE_ID` env override sanitises / passes through the `hermes-` prefix.
- macOS path uses `ioreg`'s `IOPlatformUUID` output.
- LRU cache across tests is cleared by the fixture (via `monkeypatch`).

### `tests/test_git_plugin.py`

Imports the repo-root `__init__.py` via a dummy `_Ctx` context and verifies:

- `register(ctx)` adds the seven ClawChat tools and a skill.
- Tool handlers accept and echo `task_id`.

### `tests/test_plugin.py`

Comprehensive registration / schema / behavior tests for the repo-root `__init__.py`. Defines a `_Ctx` (tools + skills + hooks) and a richer `_PlatformCtx` (adds `register_platform`). Coverage:

- `test_plugin_registers_clawchat_platform_with_registry` — `register(ctx)` calls `ctx.register_platform("clawchat", ...)` with the expected label, callables (`adapter_factory`, `check_fn`, `validate_config`, `is_connected`), `required_env`, allowlist env names, and a platform hint that mentions `MEDIA:/absolute/local/path` and forbids `MEDIA:https://`.
- `test_plugin_platform_check_only_verifies_dependencies` — the registered `check_fn` returns `True` when `_clawchat_dependencies_available` is True, **without** invoking `_clawchat_connection_configured` (separation of dependency check from credential validation).
- `test_plugin_platform_validation_falls_back_to_home_config` — `validate_config(SimpleNamespace(extra={}))` returns `True` when the merged `$HERMES_HOME/config.yaml` supplies `websocket_url` and the `.env` supplies `CLAWCHAT_TOKEN`.
- `test_plugin_adapter_factory_merges_home_config` — adapter factory merges `extra` from `$HERMES_HOME/config.yaml` so a sparse runtime config still produces a fully populated `ClawChatConfig`.
- `test_plugin_registers_all_tools` — registers exactly the seven `clawchat_*` tools, all `is_async=True`.
- `test_plugin_tool_descriptions_forbid_execute_fallbacks` — every tool description includes `"Do not use execute"`.
- `test_upload_media_tool_description_is_link_only_not_current_chat_delivery` — `clawchat_upload_media_file` description distinguishes "shareable URL" upload vs `MEDIA:/absolute/local/path` for current-chat delivery.
- `test_clawchat_skill_uses_plugin_tools_not_shell_commands` / `…_distinguishes_media_delivery_from_media_link_uploads` — direct text assertions on `skills/clawchat/SKILL.md` keep the skill aligned with the tool registration.
- `test_plugin_tool_handlers_return_json_strings_for_hermes_v012` — `_handle_clawchat_get_account_profile` returns a JSON string (not a dict) because Hermes v0.12 expects strings; verifies UTF-8 round-trip.
- `test_activate_schema_triggers_on_chinese_activation_code_phrase` — schema description and `code` parameter description both include the bilingual trigger phrasing the LLM uses to extract the code.
- `test_plugin_upload_avatar_image_rejects_relative_path` — handler returns a `validation` error envelope for relative paths (without making any API calls).

### `tests/test_plugin_manifest.py`

Static check that `plugin.yaml` has `kind: platform` and `requires_env == ["CLAWCHAT_TOKEN", "CLAWCHAT_REFRESH_TOKEN"]`. Catches manifest drift when the env-var contract changes.

### `tests/test_self_echo_hook.py`

Behavior of `_clawchat_pre_gateway_dispatch`:

- Self-echo (CLAWCHAT platform + sender `user_id == bot_user_id`) returns `{"action": "skip", "reason": "clawchat-self-echo"}`.
- Real user message on CLAWCHAT (different `user_id`) returns `None` (no skip).
- Different platform (e.g., QQBOT) is left alone even when sender matches the configured CLAWCHAT bot user_id.
- When the gateway has no configured bot user_id, the hook does not skip (defensive: would otherwise drop everything).
- Empty / missing sender `user_id` is left alone.

### `tests/test_inbound.py`

Matrix of `parse_inbound_message` edge cases:

- Default group mode `all` accepts unmentioned group messages.
- Group-mode `mention` requires the bot to be mentioned.
- `reply_preview` passes through.
- Group-mode `mention` accepts when mentioned; group-mode `all` accepts without mention.
- Mixed text + media fragments produce Markdown placeholders and populate `media_urls`.
- `message.body` as string, dict, and list of `{type, content}` fragments.
- Truthy-but-non-dict `payload`, `message`, `context`, `sender` all return `None`.

### `tests/test_install.py`

- `test_build_patches_contains_expected_ids` — the expected legacy patch ids are present.
- `test_apply_and_remove_patch_with_indentation` — indentation is preserved and removal is idempotent.
- `test_cli_platform_registry_patch_inserts_clawchat` — specific check for the CLI registry patch.
- `test_install_state_round_trip` — state file write/read.
- `test_install_and_uninstall_skill` — installs the skill, removes the legacy plugin dir, round-trips uninstall.
- `configure_clawchat_allow_all` — writes + updates `$HERMES_HOME/.env`.
- `clear_skills_prompt_snapshot`.
- `configure_clawchat_streaming` — writes a full config.yaml skeleton with the expected defaults.

### `tests/test_media_runtime.py`

- `infer_media_kind_from_mime` — normal cases + MIME parameters and casing.
- `ensure_allowed_local_path` — under-roots pass, outside-roots fail, empty roots fail closed, nested roots accepted.
- `derive_base_url` — prefers WebSocket origin; falls back to WebSocket origin when base is missing.
- `upload_outbound_media` — uploads local paths; skips a single failing item while proceeding with the rest.
- `download_inbound_media` — resolves relative URLs against the WebSocket origin and writes a local file.

### `tests/test_tools.py`

Handler-level coverage for the six new account/media tools:

- happy paths for profile fetch, user fetch, friends pagination, profile update, avatar upload, and media upload.
- config errors for missing config, token, or user id.
- validation errors for empty user ids, invalid pagination, empty updates, relative/missing/oversized upload paths.
- API error mapping for `auth`, `api`, `transport`, and unexpected exceptions.
- validation tests assert the fake client was not called.

### `tests/test_profile.py`

CLI and loader coverage:

- `load_profile_config` reads token from process env / `.env` / legacy YAML fallback and raises when token or `user_id` is missing.
- `profile get` calls `tools.get_account_profile` and prints JSON to stdout.
- `profile update` with no fields prints a validation error to stderr.
- `profile upload-avatar` rejects relative local paths.
- `profile friends --page ... --page-size ...` passes pagination to `tools.list_account_friends`.

### `tests/test_protocol.py`

Frame-builder unit tests:

- `compute_client_sign` outputs lower-hex.
- `new_frame_id` uses the expected prefixed UUID shape.
- `build_connect_request` emits the realtime `connect` event with token/client/sign.
- `build_message_add_event` carries `full_text` and `delta`.
- `build_message_done_event` matches the v2 streaming payload shape.
- `build_message_reply_event` includes reply context when `reply_to_message_id` is present.
- `build_typing_update_event` shape.
- `extract_nonce` returns `None` for non-dict payload / non-dict `payload.data`; reads the nested data nonce.
- `is_hello_ok` rejects non-dict payloads and wrong payload type; accepts the realtime event form.
