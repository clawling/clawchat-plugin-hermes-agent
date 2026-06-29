# ClawlingChat Message Hub — Client Integration Guide

**Audience.** Anyone implementing a client (mobile, desktop, web, or agent
adapter) that talks to the ClawlingChat message hub.

**Scope.** This document is the complete, self-contained contract a client
needs:

- WebSocket real-time API (the ClawChat WebSocket service, path `/ws`) — Protocol v2
- HTTP media upload API (the ClawChat media service)
- Authentication, handshake, every event, every payload field, full wire examples

**Out of scope.** Server architecture, internal message-broker topology,
persistence internals, deployment. Clients never see those.

**Versioning.** This document describes Protocol **v2**. Every WebSocket
frame carries `"version": "2"` at the top level. The wire is JSON in both
directions.

**Conformance.** When this document and the server's reference
implementation disagree, the implementation is authoritative — file an
issue against this document.

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
9. [Out-of-band signals — typing, presence, metadata, notifications, and permissions](#9-out-of-band-signals--typing-presence-and-metadata)
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
| WebSocket | `ws://<host>/ws` (or `wss://` in production) | No query string, no subprotocol |
| Media upload | `https://<host>/media/upload` | `POST`, `multipart/form-data` |
| Media health | `https://<host>/health` | `GET`, returns `200 ok` |

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
| `http` | Forwarded to an upstream verifier; server accepts **any 2xx** with body `{"user_id": "...", "nick_name": "..."}` |

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
  "payload": { "nonce": "Wn9hZ3lJZkN1QXBkUEpYbmNk" }
}
```

`payload.nonce` is an 18-byte base64url string (24 chars, unpadded). The client MUST echo it
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
    "nonce": "Wn9hZ3lJZkN1QXBkUEpYbmNk",
    "device_id": "stable-device-id-optional",
    "capabilities": {
      "multi_device":         true,
      "device_replay":        true,
      "chat_meta_events":     true,
      "delivery_receipt":     true,
      "notify_signals":       true,
      "permission_events":    true,
      "history_sync":         true,
      "reliable_delivery":    true,
      "reliable_delivery_v2": true,
      "e2ee":                 false
    }
  }
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `token` | yes | Bearer token (see §2) |
| `nonce` | yes | Echoed from `connect.challenge` |
| `device_id` | optional | Stable identifier for this device. If omitted, the server uses the authenticated `user_id`. **Strongly recommended** for multi-device users — it is the key for replay state. |
| `capabilities` | optional | A map of feature flags. **A client SHOULD advertise every feature it supports and MAY omit any flag for a feature it does not** — omission disables that feature for this device, it never errors. Per-flag meaning in the table below. |

**Capability flags.** Every field is a boolean; omission is equivalent to `false`.

| Capability | Default if omitted | Meaning |
|------------|--------------------|---------|
| `multi_device` | off | **Functional.** Advertising it makes the user's *other* devices receive echoes of messages this device sends, and the server stamps `origin_device_id` on downlinks so each device can filter its own self-echo. Not advertising it preserves the legacy no-self-fanout behavior. |
| `device_replay` | (ignored) | **Legacy / forward-compat only.** The server runs device replay (§11) unconditionally regardless of this flag, and stamps `delivery_mode: "device_replay"` on `hello-ok`. Send it for clarity; do not depend on it gating anything. |
| `chat_meta_events` | off | Opts the client in to server-pushed `chat.metadata.invalidated` (§9.3). A client that does not advertise it never sees the event. |
| `delivery_receipt` | off | Client both emits `message.delivered` receipts (uplink) and understands them (downlink). The server gates receipt routing to devices that declared it; absent → no receipts in either direction (sender stays "sent"). See §14.4. |
| `notify_signals` | off | Opts the client in to **live** `notify.signal` system notifications (§9.4). A client that does not advertise it never receives the *live* frame. On **v1 / legacy** the reliable inbox path still replays the signal on the next reconnect regardless of this flag; on **v2** (`reliable_delivery_v2`) the replay path is **also** gated by this flag, so a v2 device that omits it is skipped on replay too. See §9.4. |
| `permission_events` | off | Opts an owner client in to `permission.request` / `permission.resolved` agent-approval signals (§9.5). A client that does not advertise it never receives these ephemeral frames. |
| `history_sync` | off | Declares support for `history.transit` sibling-device history transfer (§11.4). **Actively enforced on uplink** — a client that sends `history.transit` without having advertised `history_sync` gets a `message.error` with `code: "capability_missing"` (§14.3). |
| `e2ee` | off | Declares support for per-device ciphertext-fragment peeling on E2EE envelopes. A capable target receives the matching `ciphertext_fragments[device_id]`; a client that omits it sees only the sender-supplied placeholder payload. E2EE crypto detail is out of this document's scope — see the msghub Protocol v2 reference (owned by msghub). |
| `reliable_delivery` | off | **v1 reliable delivery.** Client emits `message.cursor_ack` after durably persisting received frames and understands `history.truncated`. The server then advances the replay cursor only on the ack (not on socket-write) and stamps the storage `seq` on downlinks. Absent → legacy advance-on-write. See §11 (reconnect/replay) and §6's `seq`/`dseq` field rows. ⚠️ If you advertise it you **MUST** implement `message.cursor_ack`. |
| `reliable_delivery_v2` | off | **v2 reliable delivery (dseq).** Successor to `reliable_delivery`: client acks the per-connection dense `dseq` via `message.sync_ack{dseq,epoch}`, verifies dseq density at the socket read layer, echoes the hello-ok `ack_epoch`, and quarantines un-persistable frames instead of stalling. Granted only when the server returns `hello-ok.ack_mode="dseq"`; otherwise fall back to v1/legacy. SHOULD be advertised **together with** `reliable_delivery` so an older server falls back cleanly. See §11. ⚠️ If you advertise it you **MUST** implement `message.sync_ack`. |

Note that `e2ee` is gated by client-side policy (advertised only when E2EE is enabled), whereas `history_sync` is advertised **unconditionally** by current clients because both the E2EE and plaintext history-transfer paths share the same `history.transit` wire (§11.4).

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

**v2 reliable-delivery negotiation.** If you advertised `reliable_delivery_v2`
and the server enabled it, `hello-ok.payload` additionally carries
`ack_mode: "dseq"` and a per-connection `ack_epoch` (a random ULID). Their
**presence** is the entire v2 grant signal:

- `ack_mode == "dseq"` present → use `message.sync_ack{dseq, epoch}` and echo the
  `ack_epoch` on every ack (§11). Reset your dseq density baseline and ack
  high-water state to zero on **every** such `hello-ok` (dseq + epoch are
  per-connection, starting from 1).
- `ack_mode` absent → the server did not grant v2; fall back to v1
  (`message.cursor_ack` over storage `seq`) or legacy advance-on-write.

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
| `origin_device_id` | string | downlink (multi-device) | Stamped on the `message.send` / `message.reply` and streaming (`message.created`/`add`/`done`/`failed`) downlinks (identifies the originating device) — **not** on `message.ack` or `message.error`. Self-echo **filtering** applies only when `sender.id` equals **your own** `user_id` — your sibling devices use it to suppress the echo to the originating device; from another user it is informational. **Clients MUST omit on uplink** — any client value is dropped. |
| `seq` | int64 | downlink, **v1** reliable only | OPAQUE, SPARSE, MUTABLE storage coordinate stamped only for clients that advertised `reliable_delivery`. Ack the highest you have durably persisted via `message.cursor_ack` as an opaque high-water mark; **never** wait for "missing" seqs (the space is ~75% holes). Absent on uplink, legacy, and v2-only downlinks. See §11. |
| `dseq` | int64 | downlink, **v2** reliable only | Per-connection DENSE delivery seq (`1,2,3…`, reset per connection) stamped only when the server granted `reliable_delivery_v2`. Ack the highest contiguous `dseq` via `message.sync_ack{dseq,epoch}`; verify `dseq == lastReadDseq+1` at the read layer. Absent on uplink and v1/legacy downlinks. See §11. |
| `target_device_id` | string | uplink, `history.transit` only | The sibling device this transfer is for (§11.4). |
| `ciphertext_fragments` | array | E2EE events | Opaque per-device E2EE payload; server never inspects it (§11.4). |
| `payload` | object | always | Event-specific body (§6 onward). |

**Auth, ping/pong, and legacy offline events** carry no `chat_id`,
`chat_type`, `to`, or `sender`.

### 4.1 `to` and `sender` shapes

```json
"to":     { "id": "chat-ab",   "type": "direct" },
"sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" }
```

- `sender.type` is server-stamped and is always `"direct"` — `sender`
  identifies a single user, even on group downlinks. This is the **routing
  type** (the sender is a single user), NOT a human-vs-agent distinction;
  human/agent is not carried on the wire (it is server-side metrics only,
  derived from the JWT `aid` claim).
- `to.type` is **client-supplied UI metadata**. The canonical values are
  `"direct"` and `"group"`; the server does not validate it and echoes it
  through unchanged. Clients SHOULD send `"direct"` / `"group"` and SHOULD
  tolerate unknown values on downlink as a forward-compat measure.

---

## 5. Routing model — `chat_id`, `to`, `sender`

Routing is driven by **`chat_id` alone**. The server resolves
`chat_id → {chat_type, members}`, stamps `chat_type` on the downlink, and
delivers a copy of the envelope to every member **except the sender** —
with one exception: when the sender advertised `multi_device` (§3.3), the
sender's *own other devices* also receive an echo (filtered via
`origin_device_id`, §4).

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
| `message.delivered` | C ↔ S | yes (`to` = original sender) | server-stamped (receiver) | n/a — is a receipt |
| `history.transit` | C ↔ S | no | **yes — client-set on uplink** (§11.4) | **no** — E2EE sibling-device history transfer (§11.4). Gated by `capabilities.history_sync` (enforced on uplink). Unknown event values MUST be tolerated. |
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
| `chat.metadata.invalidated` | S → C | no | no | server-pushed metadata-changed signal — see §9.3. Gated by `capabilities.chat_meta_events`; clients that do not advertise the capability never see it. Best-effort / ephemeral — contrast with the reliable `notify.signal` (§9.4). Unknown event values MUST be tolerated, not errored. |
| `notify.signal` | S → C | no | no | reliable, inbox-coalesced system notification (§9.4). Live delivery gated by `capabilities.notify_signals`; the reliable inbox path replays it on reconnect regardless. Unknown event values MUST be tolerated. |
| `permission.request` | S → C | no | no | owner-targeted agent-permission request signal (§9.5). Gated by `capabilities.permission_events`. Ephemeral, single-recipient. Unknown event values MUST be tolerated. |
| `permission.resolved` | S → C | no | no | owner-targeted agent-permission resolution signal (§9.5). Gated by `capabilities.permission_events`. Ephemeral, single-recipient. Unknown event values MUST be tolerated. |
| `replay.done` | S → C | no | no | terminal control frame ending device replay (§11.5); fires on every reconnect, even with zero backlog. Carries `dseq` on v2 connections (§11.7). Unknown event values MUST be tolerated. |
| `message.cursor_ack` | C → S | no | no | **no** — v1 reliable-delivery cursor ack (§11.7); only meaningful if you advertised `reliable_delivery`. |
| `message.sync_ack` | C → S | no | no | **no** — v2 reliable-delivery (dseq) ack (§11.7); replaces `message.cursor_ack`; only meaningful if the server granted `reliable_delivery_v2`. |
| `sync.mark` | S → C | no | no | v2 skip-coverage advance frame carrying `dseq` (§11.7); record + ack, do not persist. |
| `history.truncated` | S → C | no | no | reliable-delivery prune boundary (§11.7); render "earlier messages unavailable". |
| `device.cursor.reset` | C → S | no | no | **no** — server rewinds the cursor and closes the socket; the close is the signal (§11.6) |
| `offline.batch` | S → C | no | no | **deprecated / legacy** — superseded by device replay + `replay.done` (§11.5); see §11.3 |
| `offline.ack` | C → S | no | no | **deprecated / legacy** — superseded by device replay + `replay.done` (§11.5); see §11.3 |
| `offline.done` | S → C | no | no | **deprecated / legacy** — superseded by device replay + `replay.done` (§11.5); see §11.3 |
| `ping` | C ↔ S | no | no | yes (`pong`) |
| `pong` | C ↔ S | no | no | n/a |

### 6.1 Routing-type constants

| Constant | Wire value |
|----------|-----------|
| Direct chat | `"direct"` |
| Group chat | `"group"` |

---

## 7. Materialized messages

**Two-tier delivery status.** Messages have two status tiers on the wire:
`message.ack` = "sent" (the server accepted and durably enqueued the message);
`message.delivered` = "delivered" (a recipient device actually received the
message). A client that does not advertise `delivery_receipt` (or whose peer
doesn't) simply stays at "sent".

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
- **Format contract** (binding): a client-supplied `message_id` MUST match
  `^msg-[0-9A-HJ-NP-Z]{26}$` (`"msg-"` + a 26-char Crockford base32 ULID) and
  be **≤ 128 characters**. This is exactly what the server mints, and what
  the official clients already produce. Do **not**
  invent a different id scheme: the server preserves ids verbatim **today**,
  but planned input-validation hardening on the backend will start
  **rejecting** non-conforming ids.

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
| `message_mode` | optional | Server preserves it verbatim and does **not** default it — clients SHOULD treat empty as equivalent to `"normal"`. |

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
    "mutation":   { "type": "append", "target_fragment_index": 0 },
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
| `mutation` | yes | Currently `{"type": "append", "target_fragment_index": 0}`. The field is always present (no `omitempty`); current producers send `0`. The shape exists for future fragment-targeted updates. |
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

## 9. Out-of-band signals — typing, presence, metadata, notifications, and permissions

These events are lightweight, not part of the message-delivery
guarantees, and never get a `message.ack`. They share the same WS
connection as the message-send path. Beyond typing (§9.1) and presence
(§9.2), the **signal events** — `chat.metadata.invalidated` (§9.3),
`notify.signal` (§9.4), and the owner-targeted `permission.*` pair
(§9.5) — carry no business payload: they tell the client to refetch
authoritative state over REST. The one exception to "lightweight" is
`notify.signal`, which is **reliable** through the offline inbox even
though its live delivery is best-effort.

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

**Loss tolerance:** the cluster fans presence changes across instances,
and individual update frames may be dropped under load.
Clients re-sync state on reconnect by re-subscribing — a fresh snapshot
is always the recovery primitive.

### 9.3 Chat metadata invalidation — `chat.metadata.invalidated`

A server-pushed signal that some out-of-band field of a chat (currently
`title`, `description`, or `behavior` — see scope vocabulary below) has
changed and local cached copies should be refreshed. The frame carries
**no new data** — only an advisory `scope` of what changed — and the
client must fetch the new state from the ClawChat backend REST API. The
fetch endpoint depends on the scope (see vocabulary table below): group
title / description changes are read back from
`/v1/conversations/:cid`; `behavior` changes are read back from
`/v1/agents/:id` against the agent paired in that direct conversation.

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
Currently produced by the ClawChat backend:

| Scope | Triggered by | Refetch from |
|-------|--------------|--------------|
| `["title"]` | Successful group rename (`PATCH /v1/conversations/:cid` with `title`). | `GET /v1/conversations/:cid`. |
| `["description"]` | Successful group description / system-prompt change (`PATCH /v1/conversations/:cid` with `description`). | `GET /v1/conversations/:cid`. |
| `["behavior"]` | Owner edits the per-agent behavior / system prompt (`PATCH /v1/agents/:id` with `behavior`). Fired only when the value actually changes. Fanned over the agent's direct conversation; recipients are the owner and the agent's shadow user. | `GET /v1/agents/:id` against the agent paired in this direct conversation (the agent client typically caches its own id; the owner can resolve it via the conversation's other participant — the agent's shadow `user_id` → `agt_…`). |

A single mutation always emits a single-element scope today; future
changes may emit multi-element scope (e.g. `["title", "description"]`)
or new scope strings. Implementations that only recognize `title` must
still refresh on `description` or any unknown value.

**No-op short-circuit.** The producer (the ClawChat backend) does
**not** emit a signal when the new value equals the current value
(server-side "same title" / "same description" / "same behavior"
check). Clients will therefore never see a redundant
`chat.metadata.invalidated` for an unchanged field.

**Direct vs group.** The ClawChat WebSocket wire surface is type-agnostic and
the ClawChat backend uses it for both kinds:

- Group `title` / `description` edits flow through
  `PATCH /v1/conversations/:cid`, which rejects direct conversations
  with `ErrUnsupportedType` before the notifier fires — so the
  `["title"]` and `["description"]` scopes only appear on group chats.
- `PATCH /v1/agents/:id` with `behavior` fires the signal over the
  **direct conversation** between the owner and the agent's shadow
  user — so the `["behavior"]` scope only appears on direct chats. The
  `chat_type` field on the envelope distinguishes the two cases.

**Client handling recipe:**

1. Verify `event == "chat.metadata.invalidated"`.
2. Optionally compare `payload.version` against your last-seen version
   for `chat_id`; drop the frame if `version <= last_seen` (idempotent
   refresh — safe to skip if you implement #3 unconditionally).
3. Issue `GET /v1/conversations/:cid` against
   the ClawChat backend to fetch the authoritative state. Do
   **not** mutate local state from the signal frame alone — `scope` is
   advisory, not authoritative payload.
4. Update your conversation row / UI from the GET response.
5. Persist the new `version` cursor for step #2 on subsequent frames.

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

### 9.4 Reliable system notifications — `notify.signal`

A server-pushed, **content-free** signal that some entity in the user's
world has changed (a friend was added, a friend request arrived, a
conversation's roster moved, etc.) and the client should refetch the
authoritative state from the ClawChat backend over REST. The frame
carries **no business data** — only enough identity to dedup and to
decide *what* to refetch.

Unlike `chat.metadata.invalidated` (§9.3), which is purely ephemeral and
best-effort, **`notify.signal` is reliable**. The producer upserts it
into the per-user offline inbox under a coalesce key, so a device that
was offline or whose send buffer was full at emit time still receives
the signal — exactly once — on its next reconnect via device replay
(§11). Live delivery on top of that is a best-effort latency
optimization, gated by `capabilities.notify_signals`.

**Capability-gated.** A client only receives the *live* frame if its
`connect` payload advertised `capabilities.notify_signals: true`. The
reconnect/replay behavior then depends on the reliability generation
(§11.7): a **v1 / legacy** client that did not opt in is *still* served
the signal through the reliable inbox path on reconnect; a **v2**
(`reliable_delivery_v2`) client that did not opt in is **skipped on
replay too** — on v2 this capability gates both the live push *and* the
replayed signal. Advertise it if you want sub-second refetch while
connected (and, on v2, the replayed signal at all).

**Producer.** These are produced by the ClawChat backend; clients never
produce `notify.signal`.

**Wire shape** (S → C):

```json
{
  "version": "2",
  "event": "notify.signal",
  "trace_id": "notif-01JC...",
  "emitted_at": 1776162700300,
  "payload": {
    "type": "friend.added",
    "entity_id": "usr_bob",
    "version": 1776162700000,
    "event_id": "ntf_01JC...",
    "message_id": "notify:friend.added:usr_bob"
  }
}
```

`notify.signal` carries **no** `chat_id`, `chat_type`, `to`, or `sender`
— it is server-originated and addressed to one recipient at a time via
the WS routing layer.

**Payload schema:**

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `type` | `string` | yes | Logical event type — the discriminator the client routes on to decide which REST refetch to issue (e.g. `friend.added`, friend-request changes, conversation-roster changes). Unknown types MUST be tolerated as a generic "refetch the relevant surface" hint, not errored. |
| `entity_id` | `string` | yes | The id of the changed entity (e.g. the friend's `usr_…`). What it points at depends on `type`. |
| `version` | `int64` | yes | Monotonic cursor (ms since epoch at mutation time). Use for client-side duplicate detection if two signals for the same entity race. |
| `event_id` | `string` | yes | Globally-unique id for this signal occurrence. Use it as a cross-channel dedup key — the same logical change may also arrive via an off-app push notification, and `event_id` lets the client collapse the two. |
| `message_id` | `string` | yes | The inbox **coalesce key**, formatted `notify:{type}:{entity_id}`. This is the server-side dedup key, **not** a chat message id — see loss tolerance below. |

**Coalescing semantics.** The signal is upserted into the per-user offline
inbox keyed by `payload.message_id` (`notify:{type}:{entity_id}`). Re-firing
the same `{type, entity_id}` **overwrites** the prior inbox row
(last-write-wins, carrying the newest `version`) rather than queuing a
second copy. The practical effect: no matter how many times an entity
changes while a device is offline, the device gets **one** signal for it
on reconnect, reflecting the latest state — which is correct because the
signal is content-free and the client refetches anyway.

**Relationship to off-app push.** A subset of these system changes is also
delivered as an off-app push notification. The WS
`notify.signal` and the push are independent transports for the
same logical event; use `event_id` to dedup if you process both.

**Client handling recipe:**

1. Verify `event == "notify.signal"`.
2. Dedup on `event_id` (and optionally drop if `version <= last_seen`
   for `entity_id`).
3. Switch on `payload.type` to pick the REST refetch (friends list,
   friend-request list, a specific conversation, or the chat list). Do
   **not** mutate local state from the frame alone — it is a pure
   signal, not authoritative payload.
4. Issue the synchronous REST refetch against the ClawChat backend
   and update UI from the response.
5. Persist the `version` cursor for step #2.

**Loss tolerance.** This event is **reliable through the inbox,
best-effort live, and capability-gated** (the live path always; the
replay path too on v2 — see below):

- **Reliable inbox path.** Persisted to the offline inbox under the
  coalesce key; reconnect / device replay (§11) **redelivers** it
  exactly once — on **v1 / legacy** regardless of the `notify_signals`
  capability. **v2 exception:** a `reliable_delivery_v2` device that did
  not advertise `notify_signals` is skipped on replay, so on v2 the
  capability gates both the live *and* the replay path. This is the
  recovery primitive for capable devices — one that misses the live
  frame still catches up.
- **Best-effort live path.** The hub silently drops the live frame if
  the recipient's send buffer is full (no kick on backpressure for
  signal events) or if the device did not advertise `notify_signals`.
- No retry, no negative ack, no live delivery confirmation.

Because the inbox path is the source of truth, a client SHOULD treat
`notify.signal` received during device replay identically to one
received live — both mean "refetch now."

### 9.5 Agent permission approvals — `permission.request` / `permission.resolved`

A pair of server-originated, **owner-targeted** signals for the agent
permission-approval flow. When an agent needs the owner's approval to
perform a sensitive operation (read mail, access a resource, etc.), the
server emits a `permission.request` to **only the owner**; when that
request is decided or expires, it emits a matching `permission.resolved`.
Both are content-light signals — the **durable** record of the outcome
flows separately as a `permission_result` system message inside the
owner↔agent conversation, and the **decision itself flows back via
the ClawChat backend REST API, not over this WebSocket** (there is no
permission uplink frame).

**Single recipient, not member fanout.** Unlike business messages, these
are **not** fanned to all members of a chat. The server produces a
single record keyed by the owner's `user_id`, so only the owner's
devices ever see them. A non-owner participant (including the agent's own
shadow user) never receives these frames.

**Capability-gated.** A client only receives these if its `connect`
payload advertised `capabilities.permission_events: true`. A client that
does not opt in never sees either frame — the server filters at the hub
layer.

**`permission.request` wire shape** (S → C):

```json
{
  "version": "2",
  "event": "permission.request",
  "trace_id": "perm-01JC...",
  "emitted_at": 1776162700100,
  "chat_id": "cnv_01HXYZ...",
  "payload": {
    "request_id": "req_01JC...",
    "agent_id": "agt_assistant",
    "operation": "read_emails",
    "target": { "label": "Gmail" },
    "expires_at": 1776166300000
  }
}
```

Top-level `chat_id` identifies the owner↔agent direct conversation the
request belongs to. `sender` and `to` are **not** set — the frame is
server-originated and addressed to the single owner recipient via the WS
routing layer.

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `request_id` | `string` | yes | Correlation id. The later `permission.resolved` carries the same `request_id`; use it to find the on-screen approval card. |
| `agent_id` | `string` | optional | The `agt_…` id of the agent asking for approval. |
| `operation` | `string` | optional | Machine-readable operation name (e.g. `read_emails`). Unknown values MUST be tolerated and rendered generically. |
| `target` | `object` | optional | Opaque descriptor of the resource the operation touches (e.g. `{ "label": "Gmail" }`). Render `target.label` if present; treat the object as forward-compatible. |
| `expires_at` | `int64` | optional | ms since epoch after which the request auto-expires. The client SHOULD collapse the card on its own once this passes, even if no `permission.resolved` arrives. |

**`permission.resolved` wire shape** (S → C):

```json
{
  "version": "2",
  "event": "permission.resolved",
  "trace_id": "perm-02JC...",
  "emitted_at": 1776162700200,
  "chat_id": "cnv_01HXYZ...",
  "payload": {
    "request_id": "req_01JC...",
    "decision": "approved",
    "reason": "User granted"
  }
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `request_id` | `string` | yes | Matches a prior `permission.request`. Use it to locate and collapse the approval card. |
| `decision` | `string` | optional | Outcome — currently `approved`, `denied`, or `expired`. Unknown values MUST be tolerated. |
| `reason` | `string` | optional | Human-readable hint suitable for a toast or log line. |

**Client handling recipe:**

1. On `permission.request`, render an approval card keyed by
   `request_id` (owner UI only).
2. The owner approves/denies via a ClawChat backend REST call —
   **never** by sending a WS frame. There is no permission uplink event.
3. On `permission.resolved` with a matching `request_id`, collapse the
   card and reflect `decision`.
4. If `expires_at` passes with no `permission.resolved`, collapse the
   card locally as expired.
5. The authoritative, persistent outcome appears as a `permission_result`
   system message in the conversation — render that for history; the
   ephemeral pair is for live UI only.

**Loss tolerance.** Both events are **ephemeral, owner-targeted,
best-effort, and capability-gated** — the same dispatch contract as
`chat.metadata.invalidated` (§9.3) and `message.delivered` (§14.4):

- Never written to the offline inbox or offline store;
  reconnect / device replay will **not** redeliver them.
- The hub silently drops the frame if the owner's send buffer is full
  (no kick on backpressure for signal events).
- No retry, no negative ack, no delivery confirmation; never pushed.

A client that misses the live pair recovers from the durable
`permission_result` system message and from refetching the request's
state over REST. Do not build correctness on receiving the ephemeral
signals.

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
| `mention` | optional `user_id`, optional `display` |
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
deserializes fragments into a typed struct
on every relay, so unknown fields inside a known `kind` (e.g. a future
`caption` on an `image` fragment) are silently dropped on the way out.
Only the documented field set per kind survives a server-relay round
trip. Producers introducing new fields should coordinate a code+doc
update on the hub before depending on them.

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
  A client advertising `delivery_receipt` DOES emit `message.delivered` for
  messages received during replay — receipts are not suppressed during replay
  (only self-echo is excluded).
- **How the cursor advances depends on the reliability generation you
  negotiated** (§11.7): **legacy** advances on socket-write success; **v1**
  (`reliable_delivery`) advances only on your `message.cursor_ack`; **v2**
  (`reliable_delivery_v2`) advances only on your `message.sync_ack`. In all
  cases a connection that closes mid-replay resumes from the last advanced point
  on the next session.
- Once replay catches up, the connection transitions seamlessly into live
  delivery. At the **seam** (the brief window where the server is catching
  up to the inbox tail), live writes published to the user's delivery
  stream can be delivered concurrently with the final replay frames.
  Net effect: the on-wire arrival order is monotonic by cursor but is
  **not** strictly ordered by sender `emitted_at`. Clients that sort by
  `payload.message_id` order (ULID-time-prefixed) on receive will get a
  stable timeline regardless of the seam.
- The server marks the end of replay with an explicit `replay.done`
  control frame (§11.5) — it always precedes the first live frame, and
  fires even when the backlog was empty.

### 11.1 New devices

The first time a `(user_id, device_id)` pair connects, the server
initialises its cursor to **seq 0** — meaning a fresh device backfills the
full retained inbox (server retention window) on first connect, recovering
all chat history the server has kept.

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

### 11.4 E2EE sibling-device history transfer — `history.transit`

When a user registers a **new device**, an existing device of the *same
user* can transfer prior chat history to it directly, device-to-device,
so the new device starts with backlog the server never had in plaintext.
This is carried by the `history.transit` event. It is part of the E2EE
multi-device feature set; full cryptographic detail (X3DH,
Double-Ratchet, fragment structure) is **out of this document's scope** —
see the msghub Protocol v2 reference (owned by msghub). This section
documents only the WS wire surface a client needs to route the frame.

**Capability-gated and enforced.** A client MUST advertise
`capabilities.history_sync: true` to send `history.transit`. The server
**actively enforces** this on uplink: a `history.transit` from a client
that did not declare the capability is rejected with a `message.error`
carrying `code: "capability_missing"` (§14.3). Current clients advertise
`history_sync` **unconditionally** — independent of the `e2ee` flag —
because the same `history.transit` wire carries both the encrypted and
the plaintext history-transfer paths.

**Sibling-only routing.** The frame is routed by the sender's **own**
`user_id`, so it can only ever reach the sender's other devices — never
another user. The server delivers it **only** to the device whose
`device_id` equals the envelope's `target_device_id`; sibling devices
that do not match skip the frame and advance their replay cursor past it
(they never see it and never delete it).

**Server treats the payload as opaque.** The server never decrypts and
never inspects `payload` or `ciphertext_fragments` — for E2EE transfers
the fragments are fully opaque ciphertext; for plaintext transfers
`ciphertext_fragments` is omitted entirely. There is no push, no unread
bump, and no `last_message` update for `history.transit`.

**Wire shape** (uplink example; downlink is the same frame relayed to the
target sibling):

```jsonc
{
  "version": "2",
  "event": "history.transit",
  "trace_id": "hist-01JC...",
  "emitted_at": 1776162700400,
  "target_device_id": "device-new",
  // Sender MUST set sender + origin_device_id on this event (see below).
  "sender": { "id": "user-alice", "type": "direct", "nick_name": "Alice" },
  "origin_device_id": "device-old",
  "payload": { "kind": "history_sync_message" },
  "ciphertext_fragments": [
    { "device_id": "device-new", "type": "msg",
      "ratchet": { "dh": "<b64>", "pn": 5, "n": 3 },
      "ciphertext": "<b64>" }
  ]
}
```

| Field | Type | Required | Meaning |
|-------|------|----------|---------|
| `target_device_id` | `string` | yes (uplink) | The sibling `device_id` this transfer is for. The server delivers the frame only to that device. |
| `sender` | object | yes (uplink) | **Divergence from §4 / §13:** for `history.transit` the server does **not** stamp `sender`. The sending client MUST populate `sender.id` with its own `user_id`. The receiving device keys its decryption session by `(sender.id, origin_device_id)`. |
| `origin_device_id` | `string` | yes (uplink) | The sending (old) device's id. Likewise client-set, not server-stamped, for this event. |
| `payload.kind` | `string` | yes | One of `history_sync_request`, `history_sync_message`, `history_sync_progress`, `history_sync_done`, `history_sync_cancel` — the handshake/transfer phase. The server treats the whole payload as transparent; the client dispatches on `kind`. |
| `ciphertext_fragments` | array | optional | Opaque per-device E2EE fragments (omitted on the plaintext path). Structure defined by the E2EE spec. |

> **Uplink stamping is the client's job here.** This is the one event
> where `sender` and `origin_device_id` are **not** server-stamped — the
> server cannot, because it never decrypts and the receiver needs the
> originating identity to key the ratchet. Clients sending
> `history.transit` MUST set both; clients receiving it MUST read
> `sender.id` and `origin_device_id` off the envelope to key decryption.
> (Mobile implements exactly this.)

**Inbox & replay behavior.** `history.transit` is written to
the offline inbox on the same path as `message.send`, so an offline
target device catches up via device replay (§11) on reconnect. **It is
deleted from the inbox immediately after delivery to the target device**
— so sync traffic does not accumulate and is never re-delivered after it
lands. Non-target sibling devices advance their cursor past it without
deleting.

**Out-of-scope note.** This document deliberately does not specify the
cryptographic envelope, the `ciphertext_fragments` schema, or the
`history_sync_*` handshake state machine. Treat the above as the
routing contract only; consult the E2EE spec for the rest. Mobile
implements both the encrypted and plaintext variants today.

### 11.5 End-of-replay marker — `replay.done`

After the server finishes streaming a device's replay backlog (§11), it
emits exactly one `replay.done` control frame, then transitions to live
delivery.

```json
{
  "version": "2",
  "event": "replay.done",
  "trace_id": "rd-01",
  "emitted_at": 1776162700500,
  "payload": {}
}
```

Empty payload; no `chat_id`, no `to`, no `sender`. It is emitted the
instant the inbox drains, guaranteeing it is the **last** replayed frame
and always precedes any live traffic. It fires on **every** reconnect,
including a zero-backlog reconnect, so the client always observes a clean
boundary between historical and live delivery.

Use it to: clear any "replaying" UI gate, suppress the new-message chime
during replay then re-enable it, and mark the timeline as caught-up.

This is **not** the deprecated `offline.done` (§11.3) — `replay.done` is
the device-replay terminator and is unrelated to the legacy
`offline.batch` handshake.

**Loss tolerance.** Ephemeral control frame, never persisted, never
replayed; a client that disconnects mid-replay simply gets a fresh
replay (and a fresh `replay.done`) on the next session. Robust clients
SHOULD also keep a short idle-timeout fallback (e.g. ~5 s with no frames)
to clear the replay gate defensively if `replay.done` is somehow missed
mid-backlog — mobile does this.

### 11.6 Client-requested full re-pull — `device.cursor.reset`

A client MAY request a full inbox re-pull by rewinding its replay cursor
to seq 0.

```json
{
  "version": "2",
  "event": "device.cursor.reset",
  "trace_id": "reset-01",
  "emitted_at": 1776162700000,
  "payload": {}
}
```

Empty payload; no `chat_id`, no `to`, no `sender`. The authenticated
device sends it on an established connection. The server resets
this `(user_id, device_id)` pair's replay cursor to 0,
then closes **this** connection only and returns **no ack** — the
connection close *is* the acknowledgement. The client reconnects with the
**same** `device_id`; on reconnect the cursor is found at 0 and device
replay (§11) re-streams the full retained inbox from the beginning,
ending with `replay.done` (§11.5). On a server-side error the connection
is left open for the client to retry.

Not capability-gated — any authenticated client may send it. Mobile
exposes this as a debug "migrate data" / full-resync action. Use
sparingly: it re-streams the entire retained backlog.

> **v1/v2 interaction.** The reset is in storage-seq space (the durable
> per-device replay cursor), shared by both v1 and v2 — v2's in-memory dseq
> ledger is discarded on the close anyway. A v2 device's reset works identically:
> cursor → 0, connection closes, and the **new** connection starts a fresh
> `ack_epoch`/`dseq` stream from 1 while replaying the full inbox.

### 11.7 Reliable delivery — durable cursor models (v1 seq vs v2 dseq)

By default the replay cursor advances on socket-write success — which loses a
frame if the client crashes after the write but before durably persisting it. To
close that window, advertise a reliability capability at `connect` (§3.3). There
are two generations; advertise **both** so an older server cleanly falls back.

> **Two version numbers.** The envelope `"version":"2"` is the **Protocol v2**
> wire format (every client speaks it; unchanged here). **legacy / v1 / v2** below
> are generations of the **reliable-delivery mechanism**, chosen by capability —
> not envelope versions.

**v1 — `reliable_delivery` (storage `seq`, mutable + sparse).**

- The server stamps the per-recipient inbox `seq` on each downlink (top-level
  `seq`); the cursor advances **only** when you send `message.cursor_ack`.
- `seq` is an **opaque, sparse, mutable** storage coordinate. Ack the highest
  `seq` you have **durably persisted** as an opaque high-water mark, and **never**
  wait for "missing" seqs — a recipient's seq space is ~75% holes (other users'
  rows interleave; coalesce upserts re-bump rows to the storage tail). A client that
  acks only a strictly-contiguous prefix freezes at the first hole. This is the
  known v1 cursor-stall; v2 fixes it.

  ```json
  { "version": "2", "event": "message.cursor_ack", "trace_id": "ack-01",
    "emitted_at": 1776162710000, "payload": { "seq": 42 } }
  ```

- `history.truncated{oldest_seq}` arrives at replay start if the inbox was pruned
  past your cursor; render an "earlier messages unavailable" boundary and accept
  that the server skips the pruned hole.

**v2 — `reliable_delivery_v2` (per-connection `dseq`, dense + gap-free + epoch-bound).**

Granted only when `hello-ok` carried `ack_mode: "dseq"` (§3.4). Then:

- The server stamps a **dense** per-connection `dseq` (`1,2,3…`) on exactly the
  ackable downlink events (`message.send`, `message.reply`, `history.transit`,
  `notify.signal`, `sync.mark`, `replay.done`). You ack the highest **contiguous**
  `dseq` you have durably persisted, echoing the `ack_epoch`:

  ```json
  { "version": "2", "event": "message.sync_ack", "trace_id": "sync-ack-01",
    "emitted_at": 1776162710000,
    "payload": { "dseq": 42, "epoch": "01JXYZ8K3MNPQRSTVWXYZ0AB" } }
  ```

- **Density check (MUST).** For every `dseq`-bearing frame, verify
  `dseq == lastReadDseq + 1` at the socket read layer. On violation: log, count,
  and **disconnect + reconnect** (fail-safe — replay refills the gap). Because the
  stream is dense by construction, all structural holes (other users' rows,
  coalesce bumps, rows you legitimately can't receive) never reach your ack space.
- **Per-connection baseline (MUST).** `dseq` and `epoch` reset to 1 every
  connection. On **each** `hello-ok` granting `ack_mode:"dseq"`, zero out
  `lastReadDseq` **and** your ack high-water/pending state alongside refreshing
  `epoch`. `lastReadDseq` often lives outside the per-connection ack tracker, so
  "rebuild the tracker" alone does not cover it — miss this and the first frame
  (`dseq=1`) fails the density check and loops.
- **`sync.mark` (S → C).** A `dseq`-bearing frame with `payload.covers_seq` the
  server emits when it skipped inbox rows for you (self-echo, non-target transit,
  capability-filtered `notify.signal`) and had no following deliverable frame to
  carry the coverage. **Record its `dseq` and ack it** like any other; do **not**
  persist it (`covers_seq` is diagnostic only).

  ```json
  { "version": "2", "event": "sync.mark", "dseq": 43, "emitted_at": 1776162710500,
    "payload": { "covers_seq": 15894 } }
  ```

- **`replay.done` carries `dseq` on v2** — ack it like any other frame, and flush
  the ack immediately after.
- **Ack rhythm (MUST):** 200ms debounce after the high-water mark advances; flush
  after acking `replay.done`; flush on graceful disconnect; and unconditionally
  resend the current high-water mark **every 30s** while connected (covers a
  zombie socket that swallowed an ack — the server's GREATEST/idempotent pop makes
  the resend free).
- **Persist is always upsert by `message_id`** — "conflict overwrites" counts as
  persisted, then record the `dseq`. Never "skip the write because the row exists,
  then ack": that silently drops coalesce re-writes (e.g. an agent's finalize
  `message.reply` overwriting an earlier row). Any retransmit MUST reuse a stable
  `payload.message_id`.
- **Poison-frame quarantine (MUST):** if a `dseq`-bearing frame cannot be
  persisted (corrupt payload), record the failure out-of-band and **ack its
  `dseq`** so the stream continues — do not stall the connection on one bad frame.
- **`history.transit` is delete-on-ack** on v2: the server deletes the inbox row
  when its `dseq` is acked, so ack only **after** durably persisting the transit
  payload.

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
| `emitted_at` | Restamped on every server-constructed downlink (materialized `message.send` / `message.reply`, `message.ack`, `message.error`, `message.delivered`, `typing.update`, all streaming lifecycle events, `presence.snapshot` / `presence.update`). Echoed verbatim only on `pong`. |
| `payload.message.streaming` | Filled on materialized `message.send` / `message.reply` downlinks. MUST be omitted on uplink. |

> **One exception to the `sender` / `origin_device_id` stamping rule:**
> the sibling-routed `history.transit` event (§11.4). The server does
> **not** stamp `sender` or `origin_device_id` for it — the sending
> client sets both, and the receiving sibling reads them to key
> decryption. This is the only event where a client-supplied `sender`
> survives.

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
- **Exception — `history.transit` (§11.4):** this is the one event where
  the client MUST set top-level `sender` (its own `user_id`) and
  `origin_device_id`, because the server does **not** stamp them for
  sibling-routed history transfer. Every other event keeps the
  omit-`sender` rule above.

---

## 14. Error semantics

### 14.1 WebSocket close

The server may close the connection in several scenarios:

| Trigger | What the client sees | Recommended response |
|---------|----------------------|----------------------|
| Handshake timeout | Close with no `hello-fail` | Reconnect; check token / clock |
| `hello-fail` — token rejected (upstream auth `4xx`) | `hello-fail` envelope (auth-failure reason) + close | **Acquire a fresh token** (refresh / re-login) before retry. Do not hot-loop the same token. |
| `hello-fail` — auth service unavailable (upstream `5xx` / timeout) | `hello-fail` with reason `"remote auth service unavailable"` + close | **Backoff-reconnect with the same token** — the token may be valid; the auth backend (the ClawChat backend) is down. Do **NOT** trigger token refresh/re-login here (a 5xx storm would otherwise become a mass-refresh storm). |
| Duplicate session for `(user_id, device_id)` — you are the **older** session | Socket closes without an envelope, no `hello-fail` | A newer instance of you took over; reconnect with backoff. The token is still valid. |
| Missed pongs | Close when no `Pong` control frame arrives within `ping_interval * max_miss_pong + pong_timeout` (~70 s with defaults) | Reconnect with backoff |
| Server backpressure | Close (the server kicks slow clients to protect itself) | Reconnect with backoff; messages will replay via §11 |

> **Enforcement status.** The `4xx → fresh token` behavior is current. The
> **distinct `5xx` reason string + handshake auth deadline** are planned
> backend hardening. Implement the 4xx/5xx branch **now** so the
> client is ready before the server begins emitting the distinct 5xx reason —
> until then a 5xx surfaces as a generic `hello-fail` and the safe default is
> backoff-reconnect (not refresh).

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

The payload has four fields: `message_id` (mirrors the uplink's
`payload.message_id`, omitted when the uplink left it blank), `code`,
`reason` (a human-readable hint, omitted when empty), and `rejected_at`
(server clock, ms since epoch).

```json
{
  "version": "2",
  "event": "message.error",
  "trace_id": "trace-send-01",
  "emitted_at": 1776162601000,
  "chat_id": "chat-unknown",
  "to": { "id": "chat-unknown", "type": "direct" },
  "payload": {
    "message_id":  "msg-client-chosen-or-empty",
    "code":        "chat_not_found",
    "reason":      "chat chat-unknown: chat not found",
    "rejected_at": 1776162601000
  }
}
```

| Field | Meaning |
|---|---|
| `message_id` | Mirrors the failing uplink's `payload.message_id`; empty when the uplink omitted it. Use it to find the local outbound row to mark failed. |
| `code` | Stable machine-readable reason (see code table below). |
| `reason` | Human-readable hint; suitable for a debug log or fallback toast. Omitted when empty. |
| `rejected_at` | Server clock at rejection time (ms since epoch). |

| `code` value | Meaning |
|---|---|
| `chat_not_found` | The chat resolver returned no member set for `chat_id`. The send was dropped server-side; do not retry without a corrected `chat_id`. |
| `capability_missing` | A capability-gated uplink (currently `history.transit`) was sent without the required capability advertised at `connect` (here `history_sync`). The frame was dropped server-side; declare the capability and reconnect before retrying. See §11.4. |
| `not_member` | The authenticated sender is not a member of the chat it tried to send to (e.g. an agent unpaired/removed from the conversation but still holding a valid JWT). The uplink was dropped (no fanout). Terminal — re-resolve membership before any retry. |
| `unsupported_version` | The uplink envelope's `version` was not the string `"2"` (missing counts as not-`"2"`). Fires for **every** uplink event, including control frames, before any other handling. Terminal — the client must speak Protocol v2; the connection is **not** closed. |

Clients MUST implement `message.error` — it is the **only** wire-level
negative ack on the send path. Treating it as an unknown event will
leave UI state stuck in "sending" forever. Other application-level
failures (e.g. permission denied) may still manifest as silent drops;
out-of-band channels (REST error replies, push) remain the fallback for
those.

### 14.4 `message.delivered` — device-level delivery receipt

**Who emits it and when.** A receiving client that advertised
`delivery_receipt` emits `message.delivered` (uplink) after it **actually
receives** a `message.send` or `message.reply` from **another user** (not a
self-echo, not a streaming lifecycle event). Receipts MUST also be emitted for
messages received during device-replay on reconnect — they are not suppressed
like self-echo.

**Payload.** `{ "message_id": "<server-minted id>", "delivered_at": <ms> }`
where `delivered_at` is the receiving client's clock in milliseconds since
epoch.

**`to` field.** Set `to` = the original sender's user ID (the `sender.id` on
the incoming envelope). This tells the server where to route the receipt.

**Server-side processing.** The server stamps `sender` = the receiving device's
authenticated identity (client-supplied `sender` is always dropped). It then
produces ONE delivery record keyed by the **original sender's `user_id`**
(from `to.id`), routing the receipt to the sender on any instance in a
multi-instance deployment. The relay gates delivery to only those of the original
sender's connected devices that declared `delivery_receipt`.

**Ephemeral — best-effort only.**

- Never written to the offline inbox store.
- Never written to the offline message store.
- Never replayed on reconnect.
- Never retransmitted on kick / backpressure.
- Silently dropped if the original sender is offline at delivery time.

This is the same dispatch contract as `chat.metadata.invalidated` and the
`permission.*` events — ephemeral, online-only, best-effort.

**Capability-gated.** Both emission and reception require `delivery_receipt` to
be advertised at connect. A sender whose peer has not declared the capability
simply stays at "sent" — there is no error, no timeout, and the absence of a
receipt is not a failure condition.

---

## 15. HTTP media upload

Use the ClawChat media service to upload binary content (images, video, audio, PDFs, etc.)
**before** sending a `message.send` that references it. The upload returns
a JSON object whose shape matches a single `Fragment` — drop it directly
into your `fragments` array.

### 15.1 `POST /media/upload`

```
POST https://<host>/media/upload
Authorization: Bearer <token>
Content-Type: multipart/form-data; boundary=...
```

Body: a single multipart part named `file` carrying `Content-Type` and
`filename`. `filename` is **optional** — if omitted or empty the server
falls back to a stored object name of `file` (no extension). When
provided, its extension (lowercased) is preserved in the stored object
key. (Producers SHOULD still send a meaningful `filename` so the stored
key and the returned `name` are useful.)

Example (curl):

```bash
curl -s -X POST https://<host>/media/upload \
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
| Max single-file size | **100 MiB** (`media.max_size_bytes`; `413` if exceeded) |
| Allowed MIME prefixes | **all types by default** — `media.allowed_mime_prefixes` is empty, so nothing is rejected by MIME. An operator may set a prefix allowlist (e.g. `image/`, `video/`) to make non-matching uploads return `415`. |
| Object retention | 15 days (server-side bucket lifecycle deletion) |

The MIME sniffer reads the first 512 bytes of the upload and **may**
override the client's declared type, but only when the declared type is
empty, `application/octet-stream`, or non-singular (contains wildcards
or commas). A singular declared type like `image/png` is
trusted by the sniffer — uploading an `.exe` declared as `image/png` will
pass the sniffer (and any configured allowlist) and be stored as
`image/png`. Producers
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
| `503` | `50301` | `storage not configured` | Server object-storage credentials missing — retry after operator fix |

### 15.7 `GET /health`

```
GET https://<host>/health   →   200 text/plain "ok"
```

No auth, no body. Safe for liveness probes. Does **not** exercise the
storage backend — a successful `/health` does not imply uploads will work.

---

## 16. Canonical wire examples

The canonical, test-asserted wire frames live in a single source of truth:
the msghub Protocol v2 reference, §9 wire examples — the canonical set (owned by msghub).
That set covers every client path referenced in this guide — handshake
(`connect.challenge` / `connect` / `hello-ok` / `hello-fail`),
send → ack → downlink, `message.error`, the streaming sequence, device
replay, typing, ping / pong, and the presence subscription lifecycle — and is
kept in lockstep with the server test suite. There are no client-side wire
deltas beyond it, so refer to §9 directly rather than a second copy here.

---

## 17. Client implementation checklist

Use this list as a final pass before integration testing.

### Connection

- [ ] Open `ws://<host>/ws` (or `wss://`). No subprotocol.
- [ ] Receive `connect.challenge`; capture the `nonce`.
- [ ] Send `connect` with `token`, the echoed `nonce`, a stable `device_id`,
      and a `capabilities` map.
- [ ] Advertise every feature you support in `capabilities`: at minimum
      `multi_device`, `device_replay`, `chat_meta_events`,
      `delivery_receipt`; add `notify_signals`, `permission_events`,
      `history_sync`, and `e2ee` as you implement them. Omit only what you
      do not support (omission disables that feature, never errors). See §3.3.
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
- [ ] If you advertise `notify_signals`, implement a `notify.signal`
      handler (§9.4): dedup on `event_id`, switch on `payload.type` to pick
      the REST refetch, and treat the frame as content-free. Handle it
      identically whether it arrives live or during device replay.
- [ ] If you are an owner client and advertise `permission_events`,
      implement `permission.request` / `permission.resolved` (§9.5): render
      and collapse an approval card keyed by `request_id`, send the decision
      via the ClawChat backend REST API (never a WS frame), and self-expire the card
      at `expires_at`.

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
- [ ] Handle `replay.done` (§11.5) as the explicit end-of-replay boundary —
      clear any "replaying" UI gate and re-enable the new-message chime.
      Expect it on every reconnect, even with zero backlog. Keep a short
      idle-timeout fallback in case it is missed mid-backlog.
- [ ] Do **not** confuse `replay.done` (device-replay terminator) with the
      deprecated `offline.done` handshake (§11.3) — implement only `replay.done`.
- [ ] If you offer a "full re-pull / migrate data" action, send
      `device.cursor.reset` (§11.6, empty payload), expect the server to
      close the socket, then reconnect with the **same** `device_id` to
      re-stream the full inbox.
- [ ] **If you advertise `reliable_delivery*` you MUST implement the matching
      ack** (§11.7): `message.cursor_ack` (highest durably-persisted storage
      `seq`, opaque high-water mark, never wait for missing seqs) for v1; or, when
      `hello-ok` returns `ack_mode:"dseq"`, `message.sync_ack{dseq,epoch}` for v2
      — with socket-read-layer density check, per-connection baseline reset,
      200ms/replay.done/disconnect/30s ack rhythm, upsert-by-`message_id`
      persistence, and poison-frame quarantine. Advertise both flags so an older
      server falls back to v1.
- [ ] Implement exponential backoff with jitter for reconnect.

### E2EE / history sync (if supported)

- [ ] To send `history.transit` (§11.4) you MUST advertise `history_sync`
      — otherwise the server rejects the uplink with `message.error
      code: "capability_missing"` (§14.3). Current clients advertise
      `history_sync` unconditionally (both plaintext and E2EE paths use it).
- [ ] On `history.transit` uplink, set `target_device_id`, and (uniquely
      for this event) set top-level `sender.id` to your own `user_id` and
      set `origin_device_id` — the server does not stamp these. On downlink,
      key your decryption session by `(sender.id, origin_device_id)`.
- [ ] Treat the cryptographic envelope (`ciphertext_fragments`,
      `history_sync_*` handshake) per the E2EE spec — it is out of scope
      for this guide.

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
