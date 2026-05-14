# Connection — `clawchat_gateway/connection.py`

WebSocket lifecycle: supervisor with exponential backoff, WebSocket authentication plus challenge handling, bounded outbound queue, ack tracking, protocol heartbeat, and read dispatcher.

## Module-level imports & constants

| Name | Value | Purpose |
|---|---|---|
| `_ws_connect_impl` | `websockets.asyncio.client.connect` or `None` | Import is soft-failed; `_ws_connect` raises `RuntimeError("websockets library not available")` if absent. Tests monkeypatch this. |
| `HANDSHAKE_TIMEOUT_SECONDS` | `10.0` | Max wait for `hello-ok` after sending `connect` on challenge-based endpoints. |
| `SEND_QUEUE_MAX` | `128` | Max buffered frames when disconnected, reconnecting, or flushing. Full queue drops the oldest queued frame before enqueuing the new frame. |
| `ACKABLE_EVENTS` | `{"message.send", "message.reply"}` | Only these events can create pending ack waits. |
| `BACKOFF_RESET_AFTER_SECONDS` | `5.0` | If a connection stays `READY` for at least this long, reset backoff + retry counter. |

## Helpers

| Function | Signature | Purpose |
|---|---|---|
| `_ws_connect` | `async (url, **kwargs) -> Any` | Thin wrapper that raises if `websockets` isn't installed. |

## `ConnectionState` enum

String enum with values: `disconnected`, `connecting`, `handshaking`, `ready`, `reconnecting`, `auth_failed`, `closed`.

## Type aliases

```python
OnMessage = Callable[[dict[str, Any]], Awaitable[None]]
OnStateChange = Callable[[ConnectionState], Awaitable[None]]
```

## `ClawChatConnection`

### Construction

```python
ClawChatConnection(config: ClawChatConfig, *, on_message: OnMessage, on_state_change: OnStateChange | None = None, account_id: str = "default")
```

Initialises state to `DISCONNECTED`, no supervisor, no websocket, no hello-wait future, empty structured `_send_queue`, `_flushing_send_queue=False`, empty pending ack map, and a reconnect tracker.

### Public lifecycle

| Method | Purpose |
|---|---|
| `async start()` | Idempotent; launches `_supervisor()` as a named task (`clawchat-supervisor`). |
| `async stop()` | Set `_stopping=True`; transition to `CLOSED`; cancel the read task and hello-wait future; close the ws; cancel & await the supervisor task. Silent on cancellation. |
| `async send_frame(frame: dict, *, wait_for_ack=False)` | If `READY` with empty queue and no flush in progress, send immediately (on failure, requeue at the front and re-raise). Otherwise enqueue for later flush. When `wait_for_ack=True` and the event is `message.send` / `message.reply`, the coroutine resolves on matching `message.ack` or raises ack timeout after the frame is actually written. |
| `is_ready -> bool` (property) | `True` iff state is `READY`. |

### State transitions

| Method | Purpose |
|---|---|
| `async _set_state(state: ConnectionState)` | No-op when unchanged; fires `on_state_change` (exceptions are logged, not propagated). |

### Supervisor loop

`async _supervisor()` — while not stopping:

1. `CONNECTING`.
2. `_run_one_connection()` owns one WebSocket session and schedules a stable-ready reset timer when the session enters `READY`.
3. If the connection remains `READY` for `BACKOFF_RESET_AFTER_SECONDS`, the timer immediately resets `reconnect_count`, resets supervisor backoff on the next disconnect, and logs `reconnect_backoff_reset` while the socket is still ready.
4. On exception: log canonical `connection_lost`, continue.
5. Increment `retries`; if over `reconnect_max_retries`, break.
6. `RECONNECTING`, log `reconnect_scheduled`, sleep `delay + jitter`, double the delay up to `reconnect_max_delay_ms`.

Final state is `CLOSED`.

### Connection

`async _run_one_connection() -> bool` —

1. Open the WebSocket with headers `Authorization: Bearer <token>`, `X-Device-Id: <device_id>`, subprotocols `["clawchat.v1", f"bearer.{token}"]`, `ping_interval` and `ping_timeout` from config.
2. Transition to `HANDSHAKING`, answer `connect.challenge` with the msghub `ConnectPayload`, and wait for matching `hello-ok`.
3. On any `hello-fail` while `HANDSHAKING`, log `auth_failed` (with `trace_id_match=true|false` for diagnostic) carrying `payload.reason`, set state to `AUTH_FAILED`, close the socket, and stop reconnect attempts until credentials are refreshed. The `trace_id` match is logged but not required, because `hello-fail` is terminal and only one `connect` is in flight per session — strict matching would let any server bug that omits the echo (or any close before the match runs) hide the rejection reason behind a reconnect storm.
4. Record `ready_started_at`, log `handshake_ok`, `_flush_send_queue(ws)`, then `await self._read_task` (idle until the server disconnects).
4. `finally` branch: cancel read task, close ws.
5. Cancel the stable-ready reset timer if the session disconnects before it fires.

### Handshake Helpers

| Method | Purpose |
|---|---|
| `async _handle_challenge(frame)` | Extract the challenge nonce, build a `connect` request with token, nonce, stable device id, and `{multi_device, device_replay}` capabilities, and send it. |
| `async _maybe_finish_handshake(frame)` | Resolve the hello-wait future to `True` only when the `hello-ok` response matches the pending `connect` trace id. Any `hello-fail` received while `HANDSHAKING` resolves to `False`, sets `AUTH_FAILED`, and stops reconnect (terminal frame, one connect in flight); the `trace_id` match is logged via `trace_id_match` for diagnostic but not gated. |

### Send queue

| Method | Purpose |
|---|---|
| `async _flush_send_queue(ws)` | Sets `_flushing_send_queue=True`, sends queued frames FIFO, and pops only after a successful write. If a write fails, the current frame remains at the queue head and reconnect handles the retry. |
| `_enqueue_frame(queued, *, front=False, log_queued=True)` | Add a structured frame to the outbound queue. When full, drop and log the oldest queued frame for normal enqueue; direct-send requeue uses `front=True` to keep the failed frame at the head. |
| `_start_ack_timer_if_needed(queued)` | For ackable frames with an ack future, starts the timeout only after successful WebSocket write. |
| `_handle_ack(frame)` | Resolves pending ack by `trace_id`, logs `ack_received`, or logs `ack_unmatched` when no pending waiter exists. Ack timeout rejects the waiting send call and does not reconnect. |

### Read loop + dispatch

| Method | Purpose |
|---|---|
| `async _read_loop(ws)` | `async for raw in ws`: decode; on malformed frame log a warning and continue; otherwise log and call `_dispatch_inbound`. |
| `async _dispatch_inbound(frame)` | During `HANDSHAKING`, route `connect.challenge` to `_handle_challenge` and `hello-ok` / `hello-fail` / `res` frames to `_maybe_finish_handshake`. While `READY`, only `message.send` and `message.reply` dispatch to `_on_message(frame)`. `message.ack`, JSON `ping` / `pong`, and legacy `offline.batch` / `offline.ack` / `offline.done` are consumed by the connection/control layer. `interaction.submit`, stream lifecycle events, and unknown events log `inbound_ignored` and do not trigger a Hermes agent reply. |
| `_schedule_stable_ready_reset()` / `_cancel_stable_ready_reset()` | Start or cancel the five-second stable-ready timer. The timer is cancelled on stop or disconnect before the stable window completes. |
| `async _handle_heartbeat_timeout()` | Logs canonical `heartbeat_timeout` and closes the socket so the supervisor schedules reconnect. WebSocket protocol ping/pong remains the liveness mechanism; JSON `ping`/`pong` are ordinary protocol frames. |
