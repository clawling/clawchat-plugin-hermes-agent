# Architecture

## What this repo is

A **Hermes Agent plugin** that integrates the ClawChat messaging platform. On Hermes v0.12.0+ it is loaded like any other plugin and registers the `clawchat` gateway platform through `ctx.register_platform(...)`; no Hermes source patch is required. Older Hermes builds that do not expose the platform registry API are not supported.

The package `clawchat_gateway` is also pip-installable (`pyproject.toml` → `[project] name = "clawchat-gateway"`).

## Boot sequence

1. Hermes loads the repo root as a plugin. Module-level code in `__init__.py` prepends the plugin root to `sys.path` so absolute imports of `clawchat_gateway.*` resolve in this process. Hermes then calls `register(ctx)`.
2. `_register_platform(ctx)` calls `ctx.register_platform(...)` with the ClawChat adapter factory, `setup_fn`, config validation hooks, allowlist env vars, and platform prompt hint. If the platform registry API is not available, `register(ctx)` raises a clear `RuntimeError`.
3. `_configure_runtime_defaults()` seeds ClawChat allow-all / streaming defaults in `$HERMES_HOME`.
4. `clawchat_gateway.plugin_tools.register_tools(ctx)` registers fourteen account/profile/media/search/moment tools:
   - `clawchat_get_account_profile` — fetch the configured account profile.
   - `clawchat_get_user_profile` — fetch a public profile by explicit `userId`.
   - `clawchat_list_account_friends` — list friends.
   - `clawchat_search_users` — search users by username or nickname.
   - `clawchat_list_moments` / `clawchat_create_moment` / `clawchat_delete_moment` — view and manage moments/dynamics.
   - `clawchat_toggle_moment_reaction` — add or remove an emoji reaction.
   - `clawchat_create_moment_comment` / `clawchat_reply_moment_comment` / `clawchat_delete_moment_comment` — manage moment comments and replies.
   - `clawchat_update_account_profile` — update nickname, avatar URL, and/or bio.
   - `clawchat_upload_avatar_image` — upload a local avatar image and return a hosted URL.
   - `clawchat_upload_media_file` — upload a local media/file attachment and return a public URL.
5. `_register_skill(ctx)` registers the bundled plugin skill through `ctx.register_skill(...)` when Hermes exposes the skill API; Hermes resolves it by qualified name as `clawchat:clawchat`.
6. `_register_cli_commands(ctx)` exposes `hermes clawchat activate CODE` on Hermes builds that support plugin CLI commands.
7. `_register_commands(ctx)` exposes `/clawchat-activate CODE` on Hermes builds that support plugin slash commands.
8. `ctx.register_hook("pre_gateway_dispatch", _clawchat_pre_gateway_dispatch)` installs the self-echo guard (see "Self-echo guard" below).

## Runtime data flow

```
Hermes LLM  <---- tool results ----  handle_clawchat_* handlers  (plugin_tools.py)
    |                                        ^
    v                                        |
  (platform: CLAWCHAT)              clawchat_gateway.activate / tools / profile
    |
    v
ClawChatAdapter ----send----> ClawChatConnection (WebSocket)  <----> ClawChat server
  ^  ^
  |  |  Inbound `message.send` frames
  |  +---- parse_inbound_message (inbound.py) ---- build MessageEvent
  +------- handle_message (from BasePlatformAdapter, provided by hermes-agent)
```

- **Outbound** send: `adapter.send()` → optional `compute_delta` (stream_buffer) → `protocol.build_message_*` → `ClawChatConnection.send_frame()` → WebSocket.
- **Inbound** message: WebSocket → `ClawChatConnection._read_loop` → `_dispatch_inbound` → `adapter._on_message` → `parse_inbound_message` → `adapter._handle_inbound` → `handle_message` (hermes-agent dispatches to the LLM). Inbound streaming lifecycle frames are buffered by `message_id` in the connection layer and materialized into one `message.send`-compatible envelope on `message.done`.
- **Run lifecycle**: Hermes v0.12+ calls adapter lifecycle hooks registered through the platform registry. `adapter.on_run_complete` emits `message.done` after LLM streaming finishes; it does not also emit a materialized `message.reply`, because `message.done` already carries the final fragments and sending both creates duplicate visible replies on current ClawChat clients.
- **Media**: inbound media URLs are downloaded by `media_runtime.download_inbound_media`; outbound media is uploaded via `upload_outbound_media`, which enforces `media_local_roots` for local paths.

## Key design choices

- **Platform registry only.** Hermes v0.12+ provides `ctx.register_platform(...)`, so ClawChat registers its adapter, validation hooks, auth env vars, and platform prompt without modifying Hermes source.
- **Bundled plugin skill registration.** `skills/clawchat/SKILL.md` ships with the plugin and is registered through `ctx.register_skill("clawchat", ...)` when available. Hermes exposes it as the plugin-qualified skill `clawchat:clawchat`; it remains plugin-owned/read-only and is not copied into `$HERMES_HOME/skills/`.
- **`gateway.*` is imported at runtime, stubbed in tests.** Production adapter code imports `gateway.platforms.base.BasePlatformAdapter`; in tests `conftest.py` pre-registers stubs from `tests/fake_hermes.py` so the package can be imported without a real hermes-agent checkout.
- **WebSocket auth plus challenge response.** Every connection sends `Authorization`, `X-Device-Id`, and the `bearer.<token>` subprotocol during the WebSocket upgrade. The server then sends `connect.challenge`; the adapter answers it with the msghub `ConnectPayload` and waits for `hello-ok` before entering `READY`.
- **Deterministic WebSocket lifecycle logs.** `ws_log.format_ws_log` renders every canonical `clawchat.ws` line with fixed field order. Optional values render as `-`, and credential material is not passed into log fields.
- **Auth failure is terminal for the current credentials.** A matching `hello-fail` sets `auth_failed`, logs `auth_failed ... action=stop_reconnect`, closes the socket, and does not schedule reconnect. Reactivation or token refresh is required.
- **Reconnect counters are explicit.** `ws_state.ReconnectTracker` separates monotonic `attempt` from consecutive `reconnect_count`; the reconnect counter resets only after a ready session is stable for five seconds.
- **Outbound queue and ack semantics match msghub.** The connection queues up to 128 outbound frames while unavailable or flushing, drops the oldest on overflow, flushes FIFO, and keeps a failed flush frame at the queue head. Ack waiters exist only for `message.send` and `message.reply`; timeout rejects the waiting send without reconnect or automatic resend.
- **Heartbeat uses WebSocket protocol ping/pong.** JSON `ping` is answered with JSON `pong` as an ordinary protocol event, JSON `pong` is ignored after logging, and heartbeat timeout closes the socket so reconnect proceeds.
- **Replay is ordinary downlink flow.** Missed messages arrive as regular downlink envelopes after `hello-ok`; legacy `offline.batch.payload.items` is still supported as a compatibility path by replaying nested envelopes through normal dispatch and acknowledging documented `batch_id` values with `offline.ack`.
- **Streaming with deltas.** `stream_buffer.compute_delta(prev, curr)` produces the appended chunk so `message.add` carries only the delta. If the new text isn't a prefix-extension of the previous, the full text is resent.
- **Filter-before-send.** Adapter strips `<think>...</think>` blocks and tool-invocation fence/tag blocks out of visible output unless `show_think_output` / `show_tools_output` are explicitly enabled.
- **Shared activation helper.** `activate_and_maybe_restart(...)` is used by `/clawchat-activate CODE`, `hermes clawchat activate CODE`, `clawchat_cli.py activate CODE`, and `hermes gateway setup`. Slash-command and CLI activation schedule a detached restart by default; gateway setup passes `restart=False` because Hermes owns the final service action after setup: restart a running gateway, start an installed stopped gateway, or install/start a service when needed. The repo-root `clawchat_cli.py` entrypoint exists for Hermes v0.12.0, whose top-level `hermes` parser does not expose general plugin CLI commands even though `ctx.register_cli_command(...)` exists. Activation persists through `hermes_cli.config` only; missing official config helpers are treated as an activation error. See [activate.md](./activate.md), [commands.md](./commands.md), [cli.md](./cli.md), [setup.md](./setup.md), and [restart.md](./restart.md).
- **Self-echo guard.** `_clawchat_pre_gateway_dispatch` (registered as a `pre_gateway_dispatch` hook) drops inbound frames whose sender is the bot's own ClawChat `user_id`. Without it, hermes-agent's interrupt-on-new-message logic treats the WebSocket echo of the bot's own outbound chunks as a fresh user message and cancels the in-flight turn (interrupt loop). The bot user_id is re-resolved on every call so it picks up fresh activation, and the platform/config lookup accepts enum, dynamic enum, and string `"clawchat"` keys.
- **Group covenant injection.** Inbound group messages get a ClawChat covenant through `MessageEvent.channel_prompt`, which Hermes applies as an ephemeral system prompt at API-call time and does not persist to transcript history. Direct messages do not receive this group covenant. Activation-intent group messages compose the group covenant with the activation prompt instead of overwriting either one.

## Environment variables

Runtime (read by Hermes v0.12+ platform registration helpers and the ClawChat Python modules here):

- `HERMES_HOME` — Hermes data dir (default `~/.hermes`). Controls `config.yaml`, `.env`, `skills/`, `plugins/` locations.
- `CLAWCHAT_WEBSOCKET_URL` / `CLAWCHAT_WS_URL`, `CLAWCHAT_BASE_URL`, `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, `CLAWCHAT_USER_ID`, `CLAWCHAT_REPLY_MODE`, `CLAWCHAT_GROUP_MODE`, `CLAWCHAT_MEDIA_LOCAL_ROOTS` — override values in `platforms.clawchat.extra`. `CLAWCHAT_GROUP_MODE` defaults to `"all"`; set it to `"mention"` to require an @mention in group chats.
- `CLAWCHAT_ALLOWED_USERS`, `CLAWCHAT_ALLOW_ALL_USERS` — auth allowlist. The platform registration advertises these env vars to Hermes; `_configure_runtime_defaults()` sets `CLAWCHAT_ALLOW_ALL_USERS=true` by default.
- `CLAWCHAT_DEVICE_ID` — override the auto-derived device id.
