# Connection — `clawchat_gateway/connection.py`

WebSocket lifecycle: supervisor with exponential backoff, WebSocket authentication plus challenge handling, send queue, and read dispatcher.

## Module-level imports & constants

| Name | Value | Purpose |
|---|---|---|
| `_ws_connect_impl` | `websockets.asyncio.client.connect` or `None` | Import is soft-failed; `_ws_connect` raises `RuntimeError("websockets library not available")` if absent. Tests monkeypatch this. |
| `HANDSHAKE_TIMEOUT_SECONDS` | `10.0` | Max wait for `hello-ok` after sending `connect` on challenge-based endpoints. |
| `SEND_QUEUE_MAX` | `128` | Max buffered frames when disconnected. Full queue drops the oldest (or the just-requeued item when `front=True`). |
| `BACKOFF_RESET_AFTER_SECONDS` | `5.0` | If a connection stays `READY` for at least this long, reset backoff + retry counter. |

## Helpers

| Function | Signature | Purpose |
|---|---|---|
| `_ws_connect` | `async (url, **kwargs) -> Any` | Thin wrapper that raises if `websockets` isn't installed. |

## `ConnectionState` enum

String enum with values: `disconnected`, `connecting`, `handshaking`, `ready`, `reconnecting`, `closed`.

## Type aliases

```python
OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
OnStateChange = Callable[[ConnectionState], Awaitable[None]]
```

## `ClawChatConnection`

### Construction

```python
ClawChatConnection(config: ClawChatConfig, *, on_message: OnMessage, on_state_change: OnStateChange | None = None)
```

Initialises state to `DISCONNECTED`, no supervisor, no websocket, no hello-wait future, empty `_send_queue` (deque), `_flushing_send_queue=False`.

### Public lifecycle

| Method | Purpose |
|---|---|
| `async start()` | Idempotent; launches `_supervisor()` as a named task (`clawchat-supervisor`). |
| `async stop()` | Set `_stopping=True`; transition to `CLOSED`; cancel the read task and hello-wait future; close the ws; cancel & await the supervisor task. Silent on cancellation. |
| `async send_frame(frame: dict)` | If `READY` with empty queue and no flush in progress, send immediately (on failure, requeue at the front and re-raise). Otherwise enqueue for later flush. Logs event + frame id + byte length at every step. |
| `is_ready -> bool` (property) | `True` iff state is `READY`. |

### State transitions

| Method | Purpose |
|---|---|
| `async _set_state(state: ConnectionState)` | No-op when unchanged; fires `on_state_change` (exceptions are logged, not propagated). |

### Supervisor loop

`async _supervisor()` — while not stopping:

1. `CONNECTING`.
2. `_run_one_connection()` — returns `True` if the session stayed `READY` for `BACKOFF_RESET_AFTER_SECONDS`.
3. On stable session: reset `delay_seconds` and `retries`.
4. On exception: log warning, continue.
5. Increment `retries`; if over `reconnect_max_retries`, break.
6. `RECONNECTING`, sleep `delay + jitter`, double the delay up to `reconnect_max_delay_ms`.

Final state is `CLOSED`.

### Connection

`async _run_one_connection() -> bool` —

1. Open the WebSocket with headers `Authorization: Bearer <token>`, `X-Device-Id: <device_id>`, subprotocols `["clawchat.v1", f"bearer.{token}"]`, `ping_interval` and `ping_timeout` from config.
2. Transition to `HANDSHAKING`, answer `connect.challenge` with the msghub `ConnectPayload`, and wait for matching `hello-ok`.
3. Record `ready_started_at`, `_flush_send_queue(ws)`, then `await self._read_task` (idle until the server disconnects).
4. `finally` branch: cancel read task, close ws.
5. Return `True` iff session stayed `READY` for at least `BACKOFF_RESET_AFTER_SECONDS`.

### Handshake Helpers

| Method | Purpose |
|---|---|
| `async _handle_challenge(frame)` | Extract the challenge nonce, build a `connect` request with token, nonce, stable device id, and `{multi_device, device_replay}` capabilities, and send it. |
| `async _maybe_finish_handshake(frame)` | Resolve the hello-wait future only when the `hello-ok` response matches the pending `connect` trace id. |

### Send queue

| Method | Purpose |
|---|---|
| `async _flush_send_queue(ws)` | Sets `_flushing_send_queue=True`, pops & sends from the deque in order. Resets the flag in `finally`. |
| `_enqueue_text(text, *, front=False)` | Append `text` to the deque (or prepend when `front=True`, used when re-queuing a frame whose `send` failed). When the queue is at `SEND_QUEUE_MAX`, drop one frame to make room: `popleft()` (oldest) on a normal append, or `pop()` (newest) when `front=True` so the re-queued frame keeps its head-of-queue position. |

### Read loop + dispatch

| Method | Purpose |
|---|---|
| `async _read_loop(ws)` | `async for raw in ws`: decode; on malformed frame log a warning and continue; otherwise log and call `_dispatch_inbound`. |
| `async _dispatch_inbound(frame)` | During `HANDSHAKING`, route `connect.challenge` to `_handle_challenge` and `hello-ok` / `hello-fail` / `res` frames to `_maybe_finish_handshake`. While `READY`, route `message.send`, `message.reply`, and `interaction.submit` to `_on_message(frame)`. JSON `ping` is answered with JSON `pong`; JSON `pong` is logged and ignored. The `interaction.submit` event carries approve/deny decisions from the ClawChat client back to `adapter._handle_interaction_submit`, which maps them onto Hermes' existing `/approve` / `/deny` text-command path. All other frames are logged and ignored. |
| `async _handle_heartbeat_timeout()` | Logs canonical `heartbeat_timeout` and closes the socket so the supervisor schedules reconnect. WebSocket protocol ping/pong remains the liveness mechanism; JSON `ping`/`pong` are ordinary protocol frames. |
