# hermes-clawchat — Code Reference

Per-module catalogue of structures and functions. For runtime flow and the boot sequence, see [architecture.md](./architecture.md).

## Index

### Plugin entrypoint

| File | Purpose | Doc |
|---|---|---|
| `__init__.py` (repo root) | Hermes plugin entrypoint — registers the ClawChat platform, tool module, CLI, slash command, hooks, and skill | [plugin-entrypoint.md](./plugin-entrypoint.md) |
| `plugin.yaml` | Hermes plugin manifest | [plugin-entrypoint.md](./plugin-entrypoint.md) |
| `clawchat_gateway/__init__.py` | Package surface | [plugin-entrypoint.md](./plugin-entrypoint.md) |

### Gateway runtime

| File | Purpose | Doc |
|---|---|---|
| `clawchat_gateway/adapter.py` | `ClawChatAdapter` — hermes-agent platform adapter | [adapter.md](./adapter.md) |
| `clawchat_gateway/connection.py` | WebSocket supervisor, handshake, queue, ack, heartbeat, inbound dispatch | [connection.md](./connection.md) |
| `clawchat_gateway/protocol.py` | Frame builders, encoding, and connect payload helpers (Python API) | [protocol.md](./protocol.md) |
| `clawchat_gateway/ws_log.py` | Deterministic WebSocket lifecycle log formatter | [ws-log.md](./ws-log.md) |
| `clawchat_gateway/ws_state.py` | WebSocket reconnect attempt/retry tracker | [ws-state.md](./ws-state.md) |
| `clawchat_gateway/inbound.py` | Inbound WebSocket frame parser | [inbound.md](./inbound.md) |
| `clawchat_gateway/group_context.py` | Group covenant prompt formatting for `MessageEvent.channel_prompt` | [group-context.md](./group-context.md) |
| `clawchat_gateway/stream_buffer.py` | `compute_delta` streaming helper | [stream-buffer.md](./stream-buffer.md) |
| `clawchat_gateway/media_runtime.py` | Upload / download helpers + local-root guards | [media-runtime.md](./media-runtime.md) |
| `clawchat_gateway/device_id.py` | Stable `X-Device-Id` resolver | [device-id.md](./device-id.md) |

### Configuration & lifecycle

| File | Purpose | Doc |
|---|---|---|
| `clawchat_gateway/config.py` | `ClawChatConfig` dataclass + env-var resolution | [config.md](./config.md) |
| `clawchat_gateway/api_client.py` | REST HTTP client (`ClawChatApiClient`) | [api-client.md](./api-client.md) |
| `clawchat_gateway/activate.py` | Activation helpers (`/v1/agents/connect`, persistence, restart orchestration) | [activate.md](./activate.md) |
| `clawchat_gateway/commands.py` | Hermes `/clawchat-activate` slash command handler | [commands.md](./commands.md) |
| `clawchat_gateway/cli.py` / `clawchat_cli.py` | Native Hermes `hermes clawchat ...` command wiring and v0.12 compatibility CLI entrypoint | [cli.md](./cli.md) |
| `clawchat_gateway/setup.py` | Interactive Hermes gateway setup hook | [setup.md](./setup.md) |
| `clawchat_gateway/restart.py` | `schedule_gateway_restart` — detached `hermes gateway restart` | [restart.md](./restart.md) |
| `clawchat_gateway/runtime_defaults.py` | Startup defaults for ClawChat auth and streaming display | [runtime-defaults.md](./runtime-defaults.md) |

### Tool surface

| File | Purpose | Doc |
|---|---|---|
| `clawchat_gateway/plugin_tools.py` | Hermes tool schemas, registration, and JSON-string tool handlers | [plugin-tools.md](./plugin-tools.md) |
| `clawchat_gateway/tools.py` | Async handlers for the six account/profile/media tools | [tools.md](./tools.md) |
| `clawchat_gateway/profile.py` | Profile CLI (`get`, `get-user`, `friends`, `update`, `upload-avatar`, `upload-media`) | [profile.md](./profile.md) |
| `skills/clawchat/SKILL.md` | Hermes skill surfaced to the model | [skill.md](./skill.md) |
| `tests/` | Pytest suite and hermes-agent stubs | [tests.md](./tests.md) |

### Wire-protocol references

| File | Purpose |
|---|---|
| [clawchat-protocol.md](./clawchat-protocol.md) | ClawChat Protocol v2 wire spec — event names, payload fields, error codes, sequence diagrams |
| [openclaw-clawchat.md](./openclaw-clawchat.md) | Cross-reference: the OpenClaw counterpart plugin (`@newbase-clawchat/openclaw-clawchat`). Useful when changing wire shape, since both adapters must move together. |
