# ClawChat Protocol v2 — Client Integration Guide

**Audience.** Anyone implementing a client (mobile, desktop, web, or agent
adapter) that talks to the ClawChat server over Protocol v2.

**Scope.** This document is the complete, self-contained contract a client
needs:

- WebSocket real-time API (port `8080`, path `/ws`) — Protocol v2
- HTTP media upload API (port `8082`)
- Authentication, handshake, every event, every payload field, full wire examples

**Out of scope.** Server architecture, internal topology, persistence, and
deployment. Clients never see those.

**Versioning.** This document describes Protocol **v2**. Every WebSocket
frame carries `"version": "2"` at the top level. The wire is JSON in both
directions.

**Conformance.** This document is the authoritative protocol contract for
client adapters. When the wire protocol changes, update this document.

---

## Table of contents

1. [Endpoints](#1-endpoints)
2. [Authentication](#2-authentication)
3. [WebSocket handshake](#3-websocket-handshake)
4. [Envelope shape](#4-envelope-shape)
5. [Routing model — `chat_id`, `to`, `sender`](#5-routing-model--chat_id-to-sender)
6. [Event catalogue](#6-event-catalogue)
7. [Materialized messages — `message.send` / `message.reply` / `message.ack`](#7-materialized-messages)
8. [Streaming messages — `message.created` / `add` / `done` / `failed`](#8-streaming-messages)
9. [Out-of-band signals — typing, presence, and metadata](#9-out-of-band-signals--typing-presence-and-metadata)
10. [Fragments — content schema](#10-fragments--content-schema)
11. [Reconnection & device replay](#11-reconnection--device-replay)
12. [Heartbeat — `ping` / `pong`](#12-heartbeat--ping--pong)
13. [Server-injected fields & contract checks](#13-server-injected-fields--contract-checks)
14. [Error semantics](#14-error-semantics)
15. [HTTP media upload](#15-http-media-upload)
16. [Canonical wire examples](#16-canonical-wire-examples)
17. [Client implementation checklist](#17-client-implementation-checklist)

---

## 1. Endpoints

| Surface | URL | Notes |
|---------|-----|-------|
| WebSocket | `ws://<host>:8080/ws` (or `wss://` in production) | No query string, no subprotocol |
| Media upload | `https://<host>:8082/media/upload` | `POST`, `multipart/form-data` |
| Media health | `https://<host>:8082/health` | `GET`, returns `200 ok` |

The WebSocket and media services may run on different hosts in production —
do not assume they share an origin. Both accept the **same** bearer token.

---

## 2. Authentication

Every authenticated surface uses an opaque bearer token:

```
Authorization: Bearer <token>
```

For the WebSocket the token travels inside the `connect` envelope (see §3),
not in an HTTP header — the upgrade itself is unauthenticated.

The server delegates verification to a pluggable `Authenticator`. Three
provider types may be configured server-side; clients do not need to know
which is in use. They MUST treat the token as opaque:

| Provider | What the server expects |
|---------|-------------------------|
| `mock` | Pre-seeded test tokens (dev / E2E only) |
| `jwt` | HS256 or RS256 token; server enforces `exp`, optionally `iss` |
| `http` | Forwarded to an upstream verifier; server expects `200 {"user_id": "...", "nick_name": "..."}` |

Client implication: do not parse, decode, or rely on any structure inside
the token. Acquire it from your identity provider and forward it verbatim.

---

## 3. WebSocket handshake

The TCP/TLS upgrade is plain WebSocket — no subprotocol negotiation. After
the upgrade, the server drives a **challenge / response** handshake before
any business traffic is allowed.

### 3.1 Sequence

```
client                                 server
  │── WebSocket upgrade ─────────────→│
  │                                   │
  │←── connect.challenge {nonce} ─────│  (server emits within ms of upgrade)
  │                                   │
  │── connect {token, nonce, …} ─────→│  (echo nonce within ~10 s)
  │                                   │
  │←── hello-ok  or  hello-fail ──────│
  │                                   │
  │   normal traffic begins here      │
```

### 3.2 `connect.challenge` (S → C)

```json
{
  "version": "2",
  "event": "connect.challenge",
  "trace_id": "challenge",
  "emitted_at": 1776162600000,
  "payload": { "nonce": "Wn9hZ3lJZkN1QXBkUEpYbg" }
}
```

`payload.nonce` is an 18-byte base64url string. The client MUST echo it
verbatim in the `connect` reply. The challenge frame's `trace_id` is the
literal string `"challenge"` — it is not correlated with the client's
`connect` reply, which uses its own `trace_id` that the server echoes on
`hello-ok` / `hello-fail`.

### 3.3 `connect` (C → S)

```json
{
  "version": "2",
  "event": "connect",
  "trace_id": "t1",
  "emitted_at": 1776162600100,
  "payload": {
    "token": "<bearer token>",
    "nonce": "Wn9hZ3lJZkN1QXBkUEpYbg",
    "device_id": "stable-device-id-optional",
    "capabilities": {
      "multi_device":     true,
      "device_replay":    true,
      "chat_meta_events": true
    }
  }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `token` | yes | Bearer token (see §2) |
| `nonce` | yes | Echoed from `connect.challenge` |
| `device_id` | optional | Stable identifier for this device. If omitted, the server uses the authenticated `user_id`. **Strongly recommended** for multi-device users — it is the key for replay state. |
| `capabilities` | optional | Advisory. `multi_device` / `device_replay` are accepted for forward-compat (the server runs device replay unconditionally). `chat_meta_events` opts the client in to receiving server-pushed `chat.metadata.invalidated` events (§6). A client that does not advertise the capability never sees the event. |

The client MUST send `connect` within the server's handshake timeout
(default ~10 seconds, may be shorter in test environments). Missing the
deadline causes the server to close the socket **without** a `hello-fail`.

### 3.4 `hello-ok` (S → C)

```json
{
  "version": "2",
  "event": "hello-ok",
  "trace_id": "t1",
  "emitted_at": 1776162600200,
  "payload": {
    "device_id": "stable-device-id-optional",
    "delivery_mode": "device_replay"
  }
}
```

`device_id` echoes the resolved id (the supplied one, or `user_id` if
omitted). `delivery_mode` is `"device_replay"` for all currently accepted
clients (treat any other value as forward-compatible).

After `hello-ok`, the server immediately begins device replay (§11) before
new live messages, then transitions to live delivery.

### 3.5 `hello-fail` (S → C)

```json
{
  "version": "2",
  "event": "hello-fail",
  "trace_id": "t1",
  "emitted_at": 1776162600200,
  "payload": { "reason": "nonce mismatch" }
}
```

| `reason` | Meaning |
|----------|---------|
| `"nonce mismatch"` | The echoed nonce does not match the one issued in `connect.challenge`. |
| `"authentication failed"` | The token was rejected. |
| `"invalid connect event"` | First frame was not a parseable `connect`. |
| `"invalid connect payload"` | `payload` could not be decoded. |

After `hello-fail` the server closes the socket. Do not retry without a new
token / new connection.

### 3.6 Duplicate-session policy

If the same `(user_id, device_id)` pair is already connected to the same
server instance, the server uses **takeover** semantics: the **older**
socket is closed without an envelope, and the **newer** socket proceeds
to `hello-ok` as normal.

Client implication: a `hello-ok` you receive may be racing an older
session you previously held. If the older session is still running
locally, it will see its connection drop without a `hello-fail`. Treat an
unexplained socket close on a previously-good session as a likely
takeover by another instance of yourself, and reconnect with backoff —
do **not** assume the token has been revoked.

---

## 4. Envelope shape

Every WebSocket frame is a JSON object with this top level:

```json
{
  "version":    "2",
  "event":      "<event-name>",
  "trace_id":   "<client-or-server-chosen string>",
  "emitted_at": 1776162600000,
  "chat_id":    "<chat id>",
  "chat_type":  "direct",
  "to":         { "id": "...", "type": "direct" },
  "sender":     { "id": "...", "type": "direct", "nick_name": "..." },
  "payload":    { /* event-specific */ }
}
```

| Field | Type | When present | Notes |
|-------|------|--------------|-------|
| `version` | string | always | Currently `"2"` |
| `event` | string | always | One of the constants in §6 |
| `trace_id` | string | always | Client-chosen on uplink; the server echoes it on the matching ack/response |
| `emitted_at` | int64 | always | Milliseconds since epoch. Treat the sender's `emitted_at` as advisory: the server restamps on every downlink it constructs (materialized `message.send` / `message.reply`, `message.ack`, `message.error`, and every streaming lifecycle event). The only paths that echo the client's value verbatim are the `ping` ↔ `pong` heartbeat. |
| `chat_id` | string | every business event | Drives routing (§5). Empty / missing values are rejected on uplink. |
| `chat_type` | string | downlink business events only | `"direct"` or `"group"`. Server-stamped. **Clients MUST omit on uplink** — any client value is dropped. |
| `to` | object | optional everywhere | UI context only (which conversation row to render under). Never used for routing. Echoed verbatim end-to-end. |
| `sender` | object | downlink business events | Identifies the originating user. **Clients MUST omit on uplink** — the server stamps it from the authenticated identity. |
| `payload` | object | always | Event-specific body (§6 onward). |

**Auth, ping/pong, and legacy offline events** carry no `chat_id`,
`chat_type`, `to`, or `sender`.

### 4.1 `to` and `sender` shapes

```json
"to":     { "id": "chat-ab",   "type": "direct" },
"sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" }
```

- `sender.type` is server-stamped and is always `"direct"` — `sender`
  identifies a single user, even on group downlinks.
- `to.type` is **client-supplied UI metadata**. The canonical values are
  `"direct"` and `"group"`; the server does not validate it and echoes it
  through unchanged. Clients SHOULD send `"direct"` / `"group"` and SHOULD
  tolerate unknown values on downlink as a forward-compat measure.

---

## 5. Routing model — `chat_id`, `to`, `sender`

Routing is driven by **`chat_id` alone**. The server resolves
`chat_id → {chat_type, members}`, stamps `chat_type` on the downlink, and
delivers a copy of the envelope to every member **except the sender**.

This means:

- DMs and group chats use the **same** code path. A "DM" is just a chat
  with two members; a "group" is a chat with more.
- `chat_type` on the downlink is `"direct"` (2 members) or `"group"` (>2),
  computed by the server from the chat resolver.
- `to` is preserved for UI context (which row to render), but the server
  **does not look at it for routing**. A client may legitimately omit `to`
  on uplink and the message will still route correctly.
- `sender` on uplink is **always** overwritten with the authenticated
  identity. Clients MUST NOT send a `sender` — any value is dropped.

### 5.1 Legacy alias

The server's chat resolver layer accepts the legacy string `"chat"` as an
input alias and normalises it to `"direct"` before it ever appears on the
wire. **Clients must use `"direct"` / `"group"` only.**

---

## 6. Event catalogue

Full list of `event` values. C = client, S = server.

| `event` | Direction | Carries `to` | Carries `sender` | Server emits ack? |
|---------|-----------|--------------|------------------|-------------------|
| `connect.challenge` | S → C | no | no | n/a |
| `connect` | C → S | no | no | yes (`hello-ok` / `hello-fail`) |
| `hello-ok` | S → C | no | no | n/a |
| `hello-fail` | S → C | no | no | n/a |
| `message.send` | C ↔ S | yes (UI) | server-only on downlink | **yes** (`message.ack`) on uplink |
| `message.ack` | S → C | yes | no | n/a |
| `message.error` | S → C | yes (UI) | no | **negative ack** for `message.send` / `message.reply` / streaming uplinks — see §14.3 |
| `message.reply` | C ↔ S | yes (UI) | server-only on downlink | **yes** (`message.ack`) on uplink |
| `message.created` | C ↔ S | yes (UI) | server-only on downlink | **no** |
| `message.add` | C ↔ S | yes (UI) | server-only on downlink | **no** |
| `message.done` | C ↔ S | yes (UI) | server-only on downlink | **no** |
| `message.failed` | C ↔ S | yes (UI) | server-only on downlink | **no** |
| `typing.update` | C ↔ S | yes (UI) | server-injected | **no** |
| `presence.subscribe` | C → S | no | no | yes (`presence.snapshot`) — see §9.2 |
| `presence.unsubscribe` | C → S | no | no | **no** — see §9.2 |
| `presence.snapshot` | S → C | no | no | reply to subscribe; see §9.2 |
| `presence.update` | S → C | no | no | server-pushed transition; see §9.2 |
| `chat.metadata.invalidated` | S → C | no | no | server-pushed metadata-changed signal — see §9.3. Gated by `capabilities.chat_meta_events`; clients that do not advertise the capability never see it. Unknown event values MUST be tolerated, not errored. |
| `offline.batch` | S → C | no | no | **deprecated** — see §11 |
| `offline.ack` | C → S | no | no | **deprecated** — see §11 |
| `offline.done` | S → C | no | no | **deprecated** — see §11 |
| `ping` | C ↔ S | no | no | yes (`pong`) |
| `pong` | C ↔ S | no | no | n/a |

### 6.1 Routing-type constants

| Constant | Wire value |
|----------|-----------|
| Direct chat | `"direct"` |
| Group chat | `"group"` |

---

## 7. Materialized messages

### 7.1 `message.send` (uplink, client → server)

A normal message. Client emits this when a user composes and sends.

Required:

- Top-level `chat_id`.
- `payload.message`.
- `payload.message.body.fragments` (may be a one-element text fragment).
- `payload.message.context` (with `mentions: []` and `reply: null` when
  not applicable).

Forbidden:

- Top-level `sender` — server stamps it.
- Top-level `chat_type` — server stamps it.
- `payload.message.streaming` — server fills it on the downlink.
- `payload.message_id` — usually omitted; server mints a `msg-<ULID>`.
  See §7.4 for the **rare exception**.

```json
{
  "version": "2",
  "event": "message.send",
  "trace_id": "trace-send-01",
  "emitted_at": 1776162600000,
  "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": {
    "message_mode": "normal",
    "message": {
      "body":    { "fragments": [{ "kind": "text", "text": "hi bob" }] },
      "context": { "mentions": [], "reply": null }
    }
  }
}
```

### 7.2 `message.ack` (S → C, back to the sender)

Emitted only in response to an uplink `message.send` or `message.reply`.
Carries the **server-minted** (or preserved) `message_id`.

```json
{
  "version": "2",
  "event": "message.ack",
  "trace_id": "trace-send-01",
  "emitted_at": 1776162601000,
  "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": {
    "message_id":  "msg-01HVB6S7K8L9M0N1P2Q3R4S5T6",
    "accepted_at": 1776162601000
  }
}
```

`trace_id` echoes the sender's `trace_id`. The `message_id` here equals
the `message_id` recipients see on the downlink — clients can correlate
the local "sent" UI state with the remote materialized message.

### 7.3 `message.send` (downlink, server → recipient)

What recipients see. Note the added fields:

- Server-stamped `chat_type`.
- Server-stamped top-level `sender`.
- Server-filled `payload.message_id`.
- Server-filled `payload.message.streaming` block (`status: "static"`,
  `mutation_policy: "sealed"` for non-stream messages).

```json
{
  "version": "2",
  "event": "message.send",
  "trace_id": "trace-send-downlink-01",
  "emitted_at": 1776162601500,
  "chat_id":   "chat-ab",
  "chat_type": "direct",
  "to":     { "id": "chat-ab",   "type": "direct" },
  "sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" },
  "payload": {
    "message_id":   "msg-01HVB6S7K8L9M0N1P2Q3R4S5T6",
    "message_mode": "normal",
    "message": {
      "body":    { "fragments": [{ "kind": "text", "text": "hi bob" }] },
      "context": { "mentions": [], "reply": null },
      "streaming": {
        "status": "static",
        "sequence": 0,
        "mutation_policy": "sealed",
        "started_at": null,
        "completed_at": null
      }
    }
  }
}
```

### 7.4 `message.reply`

Same shape as `message.send`, but `payload.message.context.reply` is
populated with a `ReplyContext`:

```json
"context": {
  "mentions": [],
  "reply": {
    "reply_to_msg_id": "msg-target-01HVB...",
    "reply_preview": {
      "id":        "user-alice",
      "nick_name": "Alice",
      "fragments": [{ "kind": "text", "text": "hi" }]
    }
  }
}
```

| Field | Meaning |
|-------|---------|
| `reply_to_msg_id` | The `message_id` of the message being replied to |
| `reply_preview.id` | User id of the original sender |
| `reply_preview.nick_name` | Display name of the original sender |
| `reply_preview.fragments` | A trimmed snapshot of the original body — enough for an inline quote, not necessarily complete |

`reply_preview` lets the receiving UI render the inline quote without an
extra round-trip. The sender is responsible for trimming `fragments` to a
reasonable preview size.

### 7.5 `payload.message_mode`

A string at `payload.message_mode`. The hub treats this as opaque content
metadata — it is preserved end-to-end but does not change routing. The
server does **not** default it: a client that omits the field on uplink
will see `"message_mode": ""` on the downlink, not `"normal"`. Producers
SHOULD send `"normal"` for ordinary messages and may use other values
(e.g. `"thinking"`) to mark specialized content; clients may render
specific modes differently and SHOULD treat empty string as equivalent
to `"normal"`.

### 7.6 `payload.message_id` rules

- **Uplink**: usually omit. The server mints `msg-<26-char ULID>`.
- **Downlink**: always populated.
- **Reuse exception**: a streaming producer that closes a stream with a
  trailing `message.reply` MAY (and usually SHOULD) reuse the same
  `message_id` it used for the stream — see §8.4.

The server **preserves** any client-supplied `message_id` verbatim. Do not
rely on this to forge a counterfeit identity for someone else's message —
`sender` is always server-stamped from the authenticated identity, so
recipients cannot be tricked into attributing the message to another user.

The offline inbox UNIQUE key is `(recipient_user_id, message_id)`. Two
different senders that happen to pick the same `message_id` and target
the same recipient will collide at the inbox layer (last write wins).
Client-minted IDs SHOULD use a globally-unique scheme (e.g. a ULID with a
producer-specific prefix) to avoid this.

---

## 8. Streaming messages

Streaming exists for use cases like AI agents that emit content
incrementally. The lifecycle is:

```
[message.created]  → opens the stream for one message_id
[message.add]*     → zero or more fragment increments (monotonic sequence)
[message.done]     → finalize successfully
or [message.failed]→ abort (no consolidated reply is materialized)
```

All four lifecycle events use a **flat** payload — fragments, streaming
state, and timestamps live at `payload` top level, **not** inside
`payload.message.body`.

### 8.1 `message.created`

Minimal payload — opens the stream for one `message_id`.

```json
{
  "version": "2",
  "event": "message.created",
  "trace_id": "trace-stream-01",
  "emitted_at": 1776406831000,
  "chat_id": "chat-alice",
  "payload": {
    "message_id":   "agent-stream-01K...",
    "message_mode": "thinking"
  }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `message_id` | yes | Client-chosen. **MUST stay identical across every event in this stream.** |
| `message_mode` | optional | Defaults to `"normal"`. |

The producer chooses the `message_id`; the server preserves it. Use a
prefix that distinguishes producer-generated ids from server-minted ones
(e.g. `"agent-stream-<ULID>"`) to make logs greppable.

### 8.2 `message.add`

```json
{
  "version": "2",
  "event": "message.add",
  "trace_id": "trace-stream-add-3",
  "emitted_at": 1776406831114,
  "chat_id": "chat-alice",
  "payload": {
    "message_id": "agent-stream-01K...",
    "sequence":   3,
    "mutation":   { "type": "append", "target_fragment_index": null },
    "fragments":  [{ "kind": "text", "text": "Hello, world", "delta": ", world" }],
    "streaming":  {
      "status": "streaming",
      "sequence": 3,
      "mutation_policy": "append_text_only",
      "started_at": null,
      "completed_at": null
    },
    "added_at": 1776406831114
  }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `message_id` | yes | Identical across the stream |
| `sequence` | yes | Monotonic, starts at **0** on the first `add` and increments by 1 per subsequent `add`. (The example above shows `sequence: 3` — i.e. the fourth `add` in the stream.) |
| `mutation` | yes | Currently `{"type": "append", "target_fragment_index": null}`. The shape exists for future fragment-targeted updates. |
| `fragments` | yes | Cumulative fragment list. Each text fragment carries both `text` (cumulative) and `delta` (new piece). |
| `streaming` | yes | `status: "streaming"`, `mutation_policy: "append_text_only"` |
| `added_at` | optional | Producer's clock, ms since epoch |

#### The `delta` invariant

For every text fragment on `message.add`:

```
text_n_minus_1 + delta_n  ==  text_n
```

If a producer cannot satisfy this (e.g. the upstream model rewrites prior
text), it MUST fail the stream with `message.failed` and start a new one.

`delta` is **absent** on `message.created`, `message.done`, and on
materialized `message.send` / `message.reply`.

### 8.3 `message.done`

Final fragments, no `delta`.

```json
{
  "version": "2",
  "event": "message.done",
  "trace_id": "trace-stream-done",
  "emitted_at": 1776406831120,
  "chat_id": "chat-alice",
  "payload": {
    "message_id": "agent-stream-01K...",
    "fragments":  [{ "kind": "text", "text": "Hello, world" }],
    "streaming":  {
      "status": "done",
      "sequence": 3,
      "mutation_policy": "append_text_only",
      "started_at": null,
      "completed_at": 1776406831120
    },
    "completed_at": 1776406831120
  }
}
```

`message.failed` has the same shape with `streaming.status: "failed"`.

### 8.4 The finalize-reply pattern (recommended)

Many producers want to follow a stream with a polished, "official" reply
that includes context like `reply_to_msg_id`. The recommended pattern is
to send a `message.reply` **reusing the stream's `message_id`**:

```
typing.update{is_typing: true}
message.created(message_id = M1, message_mode = "thinking")
message.add   (message_id = M1, sequence = 0, delta = "Thinking ")
message.add   (message_id = M1, sequence = 1, delta = "hard...")
message.done  (message_id = M1)
typing.update{is_typing: false}
message.reply (message_id = M1, body = [text: "Final answer: 42."])
                   ↑
       same message_id → offline replay store collapses to ONE row
       containing the final reply (not two rows: stream + reply)
```

**Note this is a persistence-layer property, not a live-delivery one.**
Online recipients still see every wire frame: the stream events as they
arrive AND the trailing `message.reply`. The "collapse" only happens for
recipients who are *offline during the stream* — the server stores at
most one row keyed by `(recipient_user_id, message_id)`, and reusing the
stream's id makes the polished reply overwrite the auto-merged stream
record. If the producer instead used a fresh `message_id` for the
trailing reply, offline recipients would see **both** the merged stream
and the reply as separate messages on reconnect.

### 8.5 Streaming acks

The server does **not** emit `message.ack` for any streaming lifecycle
event. Producers should detect failures by:

- Observing the WebSocket close.
- Looking for `message.failed` on the downlink (some failure modes may
  cause the server to publish a failure on behalf of the producer).
- Timing out their own outbound flow if no `message.ack` for an in-flight
  trailing `message.reply` arrives within an SLA they choose.

### 8.6 Streaming and offline recipients

When a recipient is offline during a stream, the server:

- Does **not** persist `message.created` / `message.add` / `message.done`
  individually.
- Once the stream completes, persists a **single** `message.reply` envelope
  whose `payload.message` carries the full merged `fragments` (no `delta`),
  `streaming: {status: "static", mutation_policy: "sealed"}`, and the same
  `message_id` as the stream.

The recipient receives this single envelope on reconnect via device replay
(§11) — not as a stream.

### 8.7 Streaming uplink rules

A streaming producer (typically an AI agent connected as a normal WS
client) MAY push lifecycle events as **uplink** envelopes. Constraints:

- `payload.message_id` is chosen by the client and MUST stay identical
  across every event in one stream.
- `payload.sequence` MUST be monotonic starting at 0 on the first `add`.
- Top-level `sender` is always overwritten from the authenticated identity;
  any client-supplied value is discarded.
- `chat_id` is **required** on every streaming uplink event — streaming
  events fan out through the same chat routing as `message.send`. The
  server stamps `chat_type` on the downlink.

---

## 9. Out-of-band signals — typing, presence, and metadata

These events are lightweight, not part of the message-delivery
guarantees, and never get a `message.ack`. They share the same WS
connection as the message-send path.

### 9.1 Typing — `typing.update`

```json
{
  "version": "2",
  "event": "typing.update",
  "trace_id": "trace-typing-01",
  "emitted_at": 1776162600000,
  "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": { "is_typing": true }
}
```

Fanned out to every member of `chat_id` **except the sender** — the
sender's own typing event never loops back. `emitted_at` is restamped by
the server. Best-effort delivery: typing indicators may be dropped under
load and have a much shorter retention than messages.

The server fills `sender` on the downlink (server-injected, not echoed
from the client).

### 9.2 Presence subscriptions

Clients watch another user's online state by subscribing over the same
WS connection. Subscriptions are scoped to that conn — they're cleaned
up automatically on disconnect, and on reconnect you must re-subscribe.
Hard cap: **16 active subscriptions per connection**, silently
enforced. Over-cap subscribes still get a `presence.snapshot` reply,
but the registry isn't grown.

Invalid / malformed `user_id` is a **silent no-op** — there is no
negative-ack envelope, matching Protocol v2's general style. Legal
`user_id` values start with `usr_` or `agt_`.

**Subscribe** (C → S):

```json
{
  "version": "2",
  "event": "presence.subscribe",
  "trace_id": "trace-pres-01",
  "emitted_at": 1776162800000,
  "payload": { "user_id": "usr_bob" }
}
```

Idempotent: subscribing twice to the same `user_id` on the same conn
re-issues the snapshot, doesn't duplicate the subscription. Subscribing
to self is allowed.

**Snapshot reply** (S → C, echoes the subscribe's `trace_id`):

```json
{
  "version": "2",
  "event": "presence.snapshot",
  "trace_id": "trace-pres-01",
  "emitted_at": 1776162800002,
  "payload": {
    "user_id": "usr_bob",
    "online": false,
    "last_seen_at": "2026-05-13T12:34:56Z"
  }
}
```

`last_seen_at` is RFC3339 UTC. The key is **omitted** when the watched
user has never connected. When `online == true`, treat `last_seen_at`
as informational and prefer the `online` flag for the headline state.

**Live transition** (S → C, server-minted `trace_id`):

```json
{
  "version": "2",
  "event": "presence.update",
  "trace_id": "trace-pres-fanout-7",
  "emitted_at": 1776162900000,
  "payload": {
    "user_id": "usr_bob",
    "online": true,
    "last_seen_at": "2026-05-13T12:35:00Z"
  }
}
```

Same payload shape as `presence.snapshot`. The event name is the
discriminator (snapshot = "initial state after subscribe", update =
"live transition").

**Unsubscribe** (C → S, no reply):

```json
{
  "version": "2",
  "event": "presence.unsubscribe",
  "trace_id": "trace-pres-02",
  "emitted_at": 1776162950000,
  "payload": { "user_id": "usr_bob" }
}
```

Idempotent — unsubscribing from a `user_id` the conn isn't subscribed
to is a silent no-op.

**Loss tolerance:** the cluster fans presence changes through Redis
pub/sub, and individual update frames may be dropped under load.
Clients re-sync state on reconnect by re-subscribing — a fresh snapshot
is always the recovery primitive.

### 9.3 Chat metadata invalidation — `chat.metadata.invalidated`

A server-pushed signal that some out-of-band field of a chat (currently
`title`, `description`, or `behavior` — see scope vocabulary below) has
changed and local cached copies should be refreshed. The frame carries
**no new data** — only an advisory `scope` of what changed — and the
client must fetch the new state from the ClawChat server. The
fetch endpoint depends on the scope (see vocabulary table below): group
title / description changes are read back from
`/v1/conversations/{cid}` (mobile) or `/clawnext/conversations/{cid}`
(web); `behavior` changes are read back from
`/v1/agents/{configured agent REST id}` against the agent paired in that direct
conversation.

**Capability-gated.** A client only ever receives this event if its
`connect` payload (see §3.3) advertised `capabilities.chat_meta_events:
true`. Clients that do not opt in never see the frame — the server
filters at the hub layer. This is forward-compat scaffolding so older
clients without a dispatcher for the new event name are not exposed to
it.

**Wire shape** (S → C):

```json
{
  "version": "2",
  "event": "chat.metadata.invalidated",
  "trace_id": "signal-01JC...",
  "emitted_at": 1776163000000,
  "chat_id": "cnv_01HXYZ...",
  "chat_type": "group",
  "payload": {
    "scope": ["title"],
    "version": 1776163000000,
    "updated_at": 1776163000000
  }
}
```

Top-level `chat_id` and `chat_type` are always present. `sender` and
`to` are **not** set on this event — it is server-originated and
addressed to one recipient at a time via the WS routing layer.

**Payload schema:**

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `scope` | `string[]` | optional | Advisory list of changed fields. Empty or absent ⇒ "refetch everything". |
| `version` | `int64` | optional | Monotonic cursor (today: ms since epoch at mutation time). Use for client-side duplicate detection if two invalidations race. |
| `updated_at` | `int64` | optional | ms since epoch at mutation time. Informational. |

**Scope vocabulary.** Open-ended and additive — clients **must** treat
unknown scope strings as a generic "refetch everything" hint, not error.
Currently produced by the server:

| Scope | Triggered by | Refetch from |
|-------|--------------|--------------|
| `["title"]` | Successful group rename (`PATCH /v1/conversations/{cid}` with `title`). | `GET /v1/conversations/{cid}` (mobile) / `GET /clawnext/conversations/{cid}` (web). |
| `["description"]` | Successful group description / system-prompt change (`PATCH /v1/conversations/{cid}` with `description`). | `GET /v1/conversations/{cid}` (mobile) / `GET /clawnext/conversations/{cid}` (web). |
| `["behavior"]` | The per-agent behavior / system prompt changes — via `PATCH /v1/agents/me/behavior` (agent self-update) or the owner-driven `PATCH /v1/agents/{id}`; both have identical effect. Fired only when the value actually changes. Fanned over the agent's direct conversation; recipients are the owner and the agent's ClawChat user id. | `GET /v1/agents/{configured agent REST id}` against the agent paired in this direct conversation. |

A single mutation always emits a single-element scope today; future
changes may emit multi-element scope (e.g. `["title", "description"]`)
or new scope strings. Implementations that only recognize `title` must
still refresh on `description` or any unknown value.

**No-op short-circuit.** The server does
**not** emit a signal when the new value equals the current value
(server-side "same title" / "same description" / "same behavior"
check). Clients will therefore never see a redundant
`chat.metadata.invalidated` for an unchanged field.

**Direct vs group.** The Protocol v2 wire surface is type-agnostic and
the server uses it for both kinds:

- Group `title` / `description` edits flow through
  `PATCH /v1/conversations/{cid}`, which rejects direct conversations
  with `ErrUnsupportedType` before the notifier fires — so the
  `["title"]` and `["description"]` scopes only appear on group chats.
- `PATCH /v1/agents/{configured agent REST id}` with `behavior` fires the signal over the
  **direct conversation** between the owner and the agent's ClawChat
  user id — so the `["behavior"]` scope only appears on direct chats. The
  `chat_type` field on the envelope distinguishes the two cases.

**Client handling recipe:**

1. Verify `event == "chat.metadata.invalidated"`.
2. Optionally compare `payload.version` against your last-seen version
   for the cache entry you would refresh; drop the frame if
   `version <= last_seen` (idempotent refresh — safe to skip if you
   implement #3 unconditionally).
3. Choose fetches from `scope`; do **not** mutate local profile or chat
   state from the signal frame alone — `scope` is advisory, not
   authoritative payload.
4. For `scope` containing `"behavior"`, issue `GET /v1/agents/{configured agent REST id}`
   and save the returned agent `behavior` from the GET response.
5. For `scope` containing `"title"`, `"description"`, any unknown string,
   or for empty / absent `scope`, issue `GET /v1/conversations/{cid}`
   (mobile) or `GET /clawnext/conversations/{cid}` (web) and save the
   returned conversation/group profile fields.
6. If a future frame contains multiple scope values, perform every
   applicable fetch. For example, `['behavior', 'title']` refreshes both
   the agent behavior and the conversation profile.
7. Persist the new `version` cursor for subsequent frames only after the
   authoritative GET response has been applied.

**Loss tolerance.** This event is **ephemeral, best-effort, and
capability-gated**:

- Never written to the inbox or offline mirror; reconnect / device
  replay will **not** redeliver it.
- The hub silently drops the frame if the recipient's send buffer is
  full (no kick on backpressure for signal events).
- No retry, no negative ack, no delivery confirmation.

A client that briefly disconnects or whose buffer was full during the
event misses it. The recovery primitives are:

- **Fresh fetch on reconnect.** After `hello-ok`, refresh visible chat
  metadata for any chat whose detail screen is currently mounted.
- **Fresh fetch on chat open.** When the user navigates into a chat
  detail screen, refresh first, then subscribe to live events.
- **Conversation list refresh.** The existing `GET /v1/conversations`
  endpoint already returns current `title` / `description`; a periodic
  or focus-driven refresh masks missed signals.

The signal is a **latency optimization** — it lets currently-mounted UI
update within seconds of the change — not a delivery guarantee. Do not
build correctness on receiving it.

---

## 10. Fragments — content schema

`payload.message.body.fragments` (and `payload.fragments` on streaming
events) is an ordered list of heterogeneous content fragments. Each carries
a `kind` discriminator; only fields relevant to that kind are populated.

### 10.1 Authoritative TypeScript union

```ts
type Fragment =
  | { kind: "text",    text: string, delta?: string }
  | { kind: "mention", user_id?: string, display?: string }
  | { kind: "image",   url: string, name?: string, mime?: string,
                       size?: number, width?: number, height?: number }
  | { kind: "file",    url: string, name?: string, mime?: string,
                       size?: number }
  | { kind: "audio",   url: string, name?: string, mime?: string,
                       size?: number, duration?: number }
  | { kind: "video",   url: string, name?: string, mime?: string,
                       size?: number, width?: number, height?: number,
                       duration?: number };
```

### 10.2 Per-kind populated fields

| `kind` | Fields |
|--------|--------|
| `text` | `text`; `delta` **only** on `message.add` |
| `mention` | `user_id`, optional `display` |
| `image` | `url`, optional `name`, `mime`, `size`, `width`, `height` |
| `video` | `url`, optional `name`, `mime`, `size`, `width`, `height`, `duration` |
| `audio` | `url`, optional `name`, `mime`, `size`, `duration` |
| `file` | `url`, optional `name`, `mime`, `size` |

### 10.3 Units

| Field | Unit |
|-------|------|
| `width`, `height` | pixels |
| `duration` | milliseconds |
| `size` | bytes |

### 10.4 Forward compatibility

Unknown `kind` values MUST be **preserved** by intermediaries (do not strip
unknown fragments — they may render fine on a newer client) and rendered
as "unsupported content" by clients that cannot display them.

**Caveat on unknown *fields* within a known fragment.** The server
deserializes fragments into a typed structure on every relay, so unknown
fields inside a known `kind` (e.g. a future `caption` on an `image`
fragment) are silently dropped on the way out. Only the documented field
set per kind survives a server-relay round trip. Producers introducing new
fields should coordinate a server + doc update before depending on them.

### 10.5 Media URLs

For `image` / `video` / `audio` / `file`, `url` is a directly browser-retrievable
URL. Two forms are possible (the client should treat both as opaque):

- **Public**: a permanent CDN-style URL whose path contains an unguessable
  ULID — the URL itself is the capability. Lifetime is bounded by the
  bucket's retention policy (default 15 days).
- **Presigned**: a signed GET URL valid for up to 7 days.

Always download / display the URL as-is. Do not try to construct or modify
URLs. There is no re-signing endpoint — if a URL expires, you must re-fetch
the media from its original source.

---

## 11. Reconnection & device replay

After `hello-ok`, the server replays missed messages **before** delivering
new live ones. Replay is keyed by **`(user_id, device_id)`**:

- Missed messages are sent as **ordinary downlink envelopes** (`message.send`,
  `message.reply`, etc.) in cursor order. They are **not** wrapped in any
  special envelope, and clients do **not** send replay-acknowledgement frames.
- The cursor advances only after each WebSocket write succeeds. A
  connection that closes mid-replay resumes from the same point on the next
  session.
- Once replay catches up, the connection transitions seamlessly into live
  delivery. At the **seam** (the brief window where the server is catching
  up to the inbox tail), live writes published to the user's inbox stream
  can be delivered concurrently with the final replay frames.
  Net effect: the on-wire arrival order is monotonic by cursor but is
  **not** strictly ordered by sender `emitted_at`. Clients that sort by
  `payload.message_id` order (ULID-time-prefixed) on receive will get a
  stable timeline regardless of the seam.

### 11.1 New devices

The first time a `(user_id, device_id)` pair connects, the server
initialises its cursor to the **current inbox tail** — meaning a fresh
device sees only **new** messages from that point on, not the user's full
history. (This avoids drowning new devices in backlog; full history catch-up
is out of scope for the ClawChat server and should be served by a separate
history API if your product requires it.)

### 11.2 `device_id` choice

- Pick a stable, per-device identifier and reuse it across reconnects.
  Replay state will not be lost.
- Choosing a fresh `device_id` on every connection effectively resets the
  cursor → you will only see new messages and never get backlog.
- Omitting `device_id` makes the server use `user_id` — fine for
  single-device deployments, but **all** of the user's connections then
  share one cursor and will fight each other.

### 11.3 Deprecated: `offline.batch` / `offline.ack` / `offline.done`

These three events still exist in the protocol enum for backwards
compatibility but are **no longer used by current clients or current
servers**. Do not implement them on a new client. If a server in front of
you ever sends `offline.batch`, the items inside are ordinary downlink
envelopes — you can process them inline, then send a single `offline.ack`
with the matching `batch_id`.

---

## 12. Heartbeat — `ping` / `pong`

Two heartbeat mechanisms coexist on the same socket; clients must
support both:

**1. WebSocket protocol-level `Ping`/`Pong` (RFC 6455 control frames).**
The server emits these every `ping_interval` (default 30 s) on the write
pump. Any compliant WebSocket library (incl. browser `WebSocket`,
gorilla, `web_socket_channel`, etc.) auto-responds with a `Pong` control
frame — you do not need to handle these in application code. If the
server does not receive a `Pong` within `ping_interval * max_miss_pong
+ pong_timeout` (default `30s * 2 + 10s = 70 s`), it closes the socket.

**2. JSON-envelope `ping` / `pong` events.** Either side can emit a
JSON-level ping; the peer **must** echo a `pong` with the same
`trace_id` and an empty `payload`, and the responder echoes the
sender's `emitted_at` verbatim (it is not restamped).

```jsonc
// C → S — client probes liveness
{ "version": "2", "event": "ping", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {} }

// S → C — server echoes (same trace_id, same emitted_at)
{ "version": "2", "event": "pong", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {} }
```

Clients SHOULD use the JSON-level `ping` if they want to actively probe
liveness from application code — e.g. to detect a half-open TCP
connection after a phone moves between networks — since the
protocol-level `Pong` is handled inside the WebSocket library and is
not visible at the envelope layer. The server does **not** emit
JSON-level `ping` frames itself; its heartbeat is the protocol-level
control frame only.

`ping` and `pong` carry no `chat_id`, no `to`, no `sender`.

---

## 13. Server-injected fields & contract checks

The server **always** overwrites or fills the following on uplink — any
client value is dropped:

| Field | Behaviour |
|-------|-----------|
| `sender` | Stamped from the authenticated identity. Defends against impersonation. |
| `chat_type` | Stamped from the resolved chat record on every downlink. |
| `payload.message_id` | Minted (`msg-<ULID>`) when the client omits it on `message.send` / `message.reply`. **Preserved** when the client sets it. |
| `emitted_at` | Restamped on every server-constructed downlink (materialized `message.send` / `message.reply`, `message.ack`, `message.error`, `typing.update`, all streaming lifecycle events, `presence.snapshot` / `presence.update`). Echoed verbatim only on `pong`. |
| `payload.message.streaming` | Filled on materialized `message.send` / `message.reply` downlinks. MUST be omitted on uplink. |

### 13.1 The "must not violate" list

- Client `message.send` / `message.reply` MUST omit top-level `sender` and
  `payload.message.streaming`. SHOULD omit `payload.message_id` unless
  reusing a stream id.
- `message.send.payload.message` and `message.reply.payload.message` MUST
  NEVER nest `chat`, `sender`, `to`, or any timestamp fields.
- `message.created` opens the stream for one `payload.message_id`. Minimal
  shape — only `message_id` (and optional `message_mode`) is required.
- `message.add` MUST carry `payload.{message_id, sequence, mutation,
  fragments, streaming, added_at}`; every text fragment MUST carry both
  `text` and `delta`.
- `message.done` MUST carry `payload.{message_id, fragments, streaming,
  completed_at}` with `streaming.status == "done"`; fragments do NOT carry
  `delta`.
- `message.failed` mirrors `message.done` with `streaming.status == "failed"`.
- All streaming lifecycle events for one stream reuse the same
  `payload.message_id`.
- Routing is driven by top-level `chat_id` alone.
- Top-level `to` is UI context only — never routing.
- `chat_type` is server-stamped on every downlink; uplinks MUST omit it.

---

## 14. Error semantics

### 14.1 WebSocket close

The server may close the connection in several scenarios:

| Trigger | What the client sees | Recommended response |
|---------|----------------------|----------------------|
| Handshake timeout | Close with no `hello-fail` | Reconnect; check token / clock |
| `hello-fail` then close | `hello-fail` envelope + close | Do not retry without acquiring a fresh token |
| Duplicate session for `(user_id, device_id)` — you are the **older** session | Socket closes without an envelope, no `hello-fail` | A newer instance of you took over; reconnect with backoff. The token is still valid. |
| Missed pongs | Close when no `Pong` control frame arrives within `ping_interval * max_miss_pong + pong_timeout` (~70 s with defaults) | Reconnect with backoff |
| Server backpressure | Close (the server kicks slow clients to protect itself) | Reconnect with backoff; messages will replay via §11 |

**Reconnection strategy.** Implement exponential backoff with jitter,
capped at e.g. 30 s. On reconnect, supply the same `device_id` as before
to resume the replay cursor — you do **not** need to track which messages
you missed; the server does.

### 14.2 In-stream errors

`message.failed` is informational — the stream's buffered state on the
recipient side is dropped, no consolidated `message.reply` is materialized,
and offline recipients will not see the stream at all.

### 14.3 `message.error` — negative ack on the send path

When an uplink `message.send`, `message.reply`, or streaming lifecycle
event names a `chat_id` the server cannot resolve, the server emits a
`message.error` envelope **back to the sender** instead of a
`message.ack`. The envelope:

- Echoes the uplink's `trace_id` so the sender can correlate it with the
  in-flight send.
- Carries the offending `chat_id` (and optional `to`) for UI context.
- Carries no `sender` and no `chat_type` (the chat could not be resolved).

```json
{
  "version": "2",
  "event": "message.error",
  "trace_id": "trace-send-01",
  "emitted_at": 1776162601000,
  "chat_id": "chat-unknown",
  "to": { "id": "chat-unknown", "type": "direct" },
  "payload": {
    "code": "chat_not_found",
    "message": "chat not resolvable"
  }
}
```

| `code` value | Meaning |
|---|---|
| `chat_not_found` | The chat resolver returned no member set for `chat_id`. The send was dropped server-side; do not retry without a corrected `chat_id`. |

Clients MUST implement `message.error` — it is the **only** wire-level
negative ack on the send path. Treating it as an unknown event will
leave UI state stuck in "sending" forever. Other application-level
failures (e.g. permission denied) may still manifest as silent drops;
out-of-band channels (REST error replies, push) remain the fallback for
those.

---

## 15. HTTP media upload

Use the media upload endpoint to upload binary content (images, video, audio, PDFs, etc.)
**before** sending a `message.send` that references it. The upload returns
a JSON object whose shape matches a single `Fragment` — drop it directly
into your `fragments` array.

### 15.1 `POST /media/upload`

```
POST https://<host>:8082/media/upload
Authorization: Bearer <token>
Content-Type: multipart/form-data; boundary=...
```

Body: a single multipart part named `file` carrying `Content-Type` and
`filename`. `filename` is required — its extension (lowercased) is
preserved in the stored object key.

Example (curl):

```bash
curl -s -X POST https://<host>:8082/media/upload \
  -H "Authorization: Bearer <token>" \
  -F "file=@./photo.png;type=image/png"
```

### 15.2 Successful response

```json
{
  "code": 0,
  "msg":  "ok",
  "data": {
    "kind": "image",
    "url":  "https://pub-xxx.example/media/01KPD0HG51X0P0H36ZXKES2W9G.png",
    "name": "photo.png",
    "mime": "image/png",
    "size": 47123
  }
}
```

`data` is shaped exactly like a `Fragment` (§10). `name` is always
populated by the upload service (mirroring the multipart `filename`).
The response does **not** include `width`, `height`, or `duration` —
the client may enrich `data` with those locally (computed from the
uploaded file) before pasting it into a `message.send`.

### 15.3 Response envelope

All media endpoints use this envelope:

```json
{ "code": 0,        "msg": "ok",                "data": { ... } }
{ "code": 41501,    "msg": "mime type not allowed: ...",         }
```

- `code == 0` ⇒ success; `data` is populated.
- `code != 0` ⇒ business error; `msg` describes; `data` is absent.
- HTTP status remains meaningful for proxies / retries; **application code
  should branch on `code`**, not on HTTP status.

### 15.4 `kind` inference

Inferred from the final stored MIME prefix:

| MIME prefix | `kind` |
|-------------|--------|
| `image/*` | `image` |
| `video/*` | `video` |
| `audio/*` | `audio` (voice messages too) |
| anything else | `file` |

### 15.5 Limits

| Constraint | Default |
|------------|---------|
| Max single-file size | **20 MB** (`413` if exceeded) |
| Allowed MIME prefixes | `image/`, `video/`, `audio/`, `application/pdf`, `text/markdown`, `text/plain` (`415` otherwise) |
| Object retention | 15 days (server-side bucket lifecycle deletion) |

The MIME sniffer reads the first 512 bytes of the upload and **may**
override the client's declared type, but only when the declared type is
empty, `application/octet-stream`, or non-singular (contains wildcards
or commas). A singular, allow-listed declared type like `image/png` is
trusted — uploading an `.exe` declared as `image/png` will pass both
the allowlist and the sniffer and be stored as `image/png`. Producers
that need defense against type-mismatched content should validate on
their own side before upload.

For files that need to outlive the retention window, the client must store
them somewhere else and reference that URL. There is no re-signing endpoint.

### 15.6 Error catalogue

| HTTP | `code` | `msg` (sample) | Cause |
|------|--------|----------------|-------|
| `400` | `40001` | `missing file: ...` | Missing or malformed multipart part |
| `400` | `40002` | `empty upload` | File declared zero bytes |
| `401` | `40101` | `missing Bearer token` | No `Authorization` header |
| `401` | `40101` | `invalid token` | Token rejected by `Authenticator.Verify` |
| `413` | `41301` | `upload too large: N bytes (limit M)` | Exceeds `max_size_bytes` |
| `415` | `41501` | `mime type not allowed: ...` | MIME outside the allowed prefix list |
| `500` | `50001` | `upload failed` | Storage backend error (logged server-side) |
| `503` | `50301` | `storage not configured` | Server R2 credentials missing — retry after operator fix |

### 15.7 `GET /health`

```
GET https://<host>:8082/health   →   200 text/plain "ok"
```

No auth, no body. Safe for liveness probes. Does **not** exercise the
storage backend — a successful `/health` does not imply uploads will work.

---

## 16. Canonical wire examples

These are the exact frames asserted by the server's test suite. Use them
as ground truth.

### 16.1 Handshake

```json
// S → C
{ "version": "2", "event": "connect.challenge", "trace_id": "challenge",
  "emitted_at": 1776162600000, "payload": { "nonce": "Wn9hZ3lJZkN1QXBkUEpYbg" } }

// C → S
{ "version": "2", "event": "connect", "trace_id": "t1",
  "emitted_at": 1776162600100,
  "payload": { "token": "<bearer>", "nonce": "Wn9hZ3lJZkN1QXBkUEpYbg" } }

// S → C (success)
{ "version": "2", "event": "hello-ok", "trace_id": "t1",
  "emitted_at": 1776162600200,
  "payload": { "device_id": "device-alice-001", "delivery_mode": "device_replay" } }

// S → C (failure)
{ "version": "2", "event": "hello-fail", "trace_id": "t1",
  "emitted_at": 1776162600200, "payload": { "reason": "nonce mismatch" } }
```

### 16.2 Send → ack → downlink

```json
// C → S
{ "version": "2", "event": "message.send", "trace_id": "trace-send-01",
  "emitted_at": 1776162600000, "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": {
    "message_mode": "normal",
    "message": {
      "body":    { "fragments": [{ "kind": "text", "text": "hi bob" }] },
      "context": { "mentions": [], "reply": null }
    }
  } }

// S → C (back to sender)
{ "version": "2", "event": "message.ack", "trace_id": "trace-send-01",
  "emitted_at": 1776162601000, "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": {
    "message_id":  "msg-01HVB6S7K8L9M0N1P2Q3R4S5T6",
    "accepted_at": 1776162601000
  } }

// S → C (to recipient)
{ "version": "2", "event": "message.send", "trace_id": "trace-send-downlink-01",
  "emitted_at": 1776162601500, "chat_id": "chat-ab", "chat_type": "direct",
  "to":     { "id": "chat-ab",   "type": "direct" },
  "sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" },
  "payload": {
    "message_id":   "msg-01HVB6S7K8L9M0N1P2Q3R4S5T6",
    "message_mode": "normal",
    "message": {
      "body":      { "fragments": [{ "kind": "text", "text": "hi bob" }] },
      "context":   { "mentions": [], "reply": null },
      "streaming": {
        "status": "static", "sequence": 0, "mutation_policy": "sealed",
        "started_at": null, "completed_at": null
      }
    }
  } }
```

### 16.3 Streaming sequence

```jsonc
// 1) Open the stream
{ "version": "2", "event": "message.created", "chat_id": "chat-alice",
  "payload": { "message_id": "agent-stream-01K..." } }

// 2) Append fragments — text MUST carry both `text` (cumulative) and `delta` (new)
{ "version": "2", "event": "message.add", "chat_id": "chat-alice",
  "payload": {
    "message_id": "agent-stream-01K...",
    "sequence":   3,
    "mutation":   { "type": "append", "target_fragment_index": null },
    "fragments":  [{ "kind": "text", "text": "Hello, world", "delta": ", world" }],
    "streaming":  { "status": "streaming", "sequence": 3,
                    "mutation_policy": "append_text_only",
                    "started_at": null, "completed_at": null },
    "added_at":   1776406831114
  } }

// 3) Finalize — fragments cumulative, NO `delta`
{ "version": "2", "event": "message.done", "chat_id": "chat-alice",
  "payload": {
    "message_id": "agent-stream-01K...",
    "fragments":  [{ "kind": "text", "text": "Hello, world" }],
    "streaming":  { "status": "done", "sequence": 3,
                    "mutation_policy": "append_text_only",
                    "started_at": null, "completed_at": 1776406831120 },
    "completed_at": 1776406831120
  } }

// 4) (Optional) trailing reply REUSING the stream's id — collapses the offline replay row
{ "version": "2", "event": "message.reply", "chat_id": "chat-alice",
  "payload": {
    "message_id":   "agent-stream-01K...",
    "message_mode": "normal",
    "message": {
      "body":    { "fragments": [{ "kind": "text", "text": "Hello, world" }] },
      "context": {
        "mentions": [],
        "reply": {
          "reply_to_msg_id": "user-msg-01K...",
          "reply_preview":   {
            "id":        "user-alice",
            "nick_name": "Alice",
            "fragments": [{ "kind": "text", "text": "hi" }]
          }
        }
      }
    }
  } }
```

### 16.4 Typing indicator

```json
{ "version": "2", "event": "typing.update", "trace_id": "trace-typing-01",
  "emitted_at": 1776162600000, "chat_id": "chat-ab",
  "to": { "id": "chat-ab", "type": "direct" },
  "payload": { "is_typing": true } }
```

### 16.5 Device replay (post-`hello-ok`)

```jsonc
// Missed messages stream as ordinary downlink envelopes — no special wrapper.
{ "version": "2", "event": "message.send", "trace_id": "trace-replay-01",
  "emitted_at": 1776162700000, "chat_id": "chat-ab", "chat_type": "direct",
  "to": { "id": "chat-ab", "type": "direct" },
  "sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" },
  "payload": { "message_id": "msg-...", "message_mode": "normal",
               "message": { "body": { "fragments": [...] },
                            "context": { "mentions": [], "reply": null },
                            "streaming": { "status": "static", "sequence": 0,
                                           "mutation_policy": "sealed",
                                           "started_at": null, "completed_at": null } } } }
```

### 16.6 Ping / pong

```json
{ "version": "2", "event": "ping", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {} }
{ "version": "2", "event": "pong", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {} }
```

`pong` echoes the ping's `emitted_at` verbatim — it is not restamped.

### 16.7 Presence subscription

```jsonc
// C → S — subscribe
{ "version": "2", "event": "presence.subscribe", "trace_id": "trace-pres-01",
  "emitted_at": 1776162800000, "payload": { "user_id": "usr_bob" } }

// S → C — snapshot (echoes the subscribe trace_id)
{ "version": "2", "event": "presence.snapshot", "trace_id": "trace-pres-01",
  "emitted_at": 1776162800002,
  "payload": { "user_id": "usr_bob", "online": false,
               "last_seen_at": "2026-05-13T12:34:56Z" } }

// S → C — live transition (server-minted trace_id)
{ "version": "2", "event": "presence.update", "trace_id": "trace-pres-fanout-7",
  "emitted_at": 1776162900000,
  "payload": { "user_id": "usr_bob", "online": true,
               "last_seen_at": "2026-05-13T12:35:00Z" } }

// C → S — unsubscribe (no reply)
{ "version": "2", "event": "presence.unsubscribe", "trace_id": "trace-pres-02",
  "emitted_at": 1776162950000, "payload": { "user_id": "usr_bob" } }
```

---

## 17. Client implementation checklist

Use this list as a final pass before integration testing.

### Connection

- [ ] Open `ws://<host>:8080/ws` (or `wss://`). No subprotocol.
- [ ] Receive `connect.challenge`; capture the `nonce`.
- [ ] Send `connect` with `token`, the echoed `nonce`, a stable `device_id`,
      and `capabilities: { multi_device: true, device_replay: true }`.
- [ ] Treat any close before `hello-ok` / `hello-fail` as a duplicate-session
      collision OR a handshake timeout.
- [ ] On `hello-fail`, do **not** retry without a fresh token / fresh socket.

### Sending messages

- [ ] Always set top-level `chat_id`.
- [ ] **Never** set top-level `sender`.
- [ ] **Never** set top-level `chat_type`.
- [ ] **Never** set `payload.message.streaming` on uplink.
- [ ] Omit `payload.message_id` on regular sends; the server mints one.
- [ ] Track local "sent" UI state by `trace_id`; reconcile to the server's
      `message_id` when `message.ack` arrives.
- [ ] **Handle `message.error` on the send path** (correlated by `trace_id`)
      — it is the only wire-level negative ack and surfaces unresolvable
      `chat_id` failures (§14.3).
- [ ] Render `to` (UI context) but route on `chat_id`.

### Receiving messages

- [ ] Treat any envelope with `chat_type` set as a downlink.
- [ ] Use `payload.message_id` to deduplicate against your local store.
- [ ] Preserve unknown `fragment.kind` values; render as "unsupported".
- [ ] **Tolerate unknown `event` values** (forward-compat — future
      events must not error or close the socket).
- [ ] If you opt in via `capabilities.chat_meta_events: true`, implement
      a handler for `chat.metadata.invalidated` — see §9.3 for the
      payload schema, scope vocabulary, and the GET-then-refresh recipe.
      Tolerate unknown `scope` strings as "refetch everything".
- [ ] On streaming downlinks, apply the `delta` invariant
      (`text_prev + delta == text`) when reconstructing locally.

### Streaming (producers only)

- [ ] Use one self-chosen `message_id` for the entire stream.
- [ ] `sequence` starts at 0 on the first `message.add`, monotonic.
- [ ] Every text fragment on `message.add` carries both `text` (cumulative)
      and `delta` (new piece).
- [ ] Always emit `message.done` (or `message.failed`) — do not let a stream
      dangle; the merge buffer waits for the terminator.
- [ ] If you follow up with a polished `message.reply`, **reuse the same
      `message_id`** so offline replay sees one row, not two.

### Reconnect & replay

- [ ] Use the **same** `device_id` across reconnects to preserve the cursor.
- [ ] Do not implement `offline.batch` / `offline.ack` / `offline.done` —
      they are deprecated.
- [ ] Treat ordinary `message.*` envelopes received immediately after
      `hello-ok` as replay; the transition to live delivery is seamless.
- [ ] Implement exponential backoff with jitter for reconnect.

### Heartbeat

- [ ] Echo every `ping` with a `pong` (same `trace_id`, empty `payload`).
- [ ] Optionally send your own `ping` if the server has been silent for
      ~30 s — protects against half-open connections.

### Media

- [ ] Upload via `POST /media/upload` **before** referencing the result in
      a `message.send`.
- [ ] Branch on the response envelope's `code` field, not on HTTP status.
- [ ] Drop the `data` object directly into your `fragments` array (it is
      already in `Fragment` shape).
- [ ] Treat `url` as opaque. Do not parse, modify, or reconstruct it.
- [ ] Plan for media URLs to expire after the configured retention window
      (default 15 days).

### Token handling

- [ ] Treat the token as opaque — do not parse it.
- [ ] Use the same token for the WebSocket `connect` and the media
      `Authorization` header.
- [ ] Refresh tokens out-of-band (not via this protocol). On refresh, close
      the WebSocket and reconnect with the new token.
