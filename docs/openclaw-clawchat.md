---
summary: "ClawChat WebSocket channel plugin (@newbase-clawchat/sdk 0.1.0)"
read_when:
  - You want to connect OpenClaw to ClawChat
  - You are configuring the `openclaw-clawchat` channel
  - You are onboarding an agent with an invite code
  - You are migrating or refactoring the clawchat protocol pipeline
title: "ClawChat"
---

# ClawChat

**Status:** bundled channel plugin (`extensions/openclaw-clawchat`), published as
`@newbase-clawchat/openclaw-clawchat`.

ClawChat connects OpenClaw to the ClawChat Protocol v2 over WebSocket via
`@newbase-clawchat/sdk@^0.1.0`, plus a small REST surface mounted under
`/v1/*` for profile / social / media operations. It supports text + media
chat in both direct and group rooms, progressive streaming replies with a
consolidated final `message.reply`, reply context pass-through, and an
invite-code based onboarding flow.

## Features

- WebSocket transport via `@newbase-clawchat/sdk` (auto reconnect with exponential backoff, heartbeat, ack tracking)
- Invite-code onboarding (`openclaw channels setup --channel openclaw-clawchat --code INV-XXXX`)
- Inbound `message.send` and `message.reply` with reply context
- Direct + group chat. Group trigger policy is configurable (`groupMode: "all" | "mention"`)
- Outbound text replies: static or progressive streaming with a final consolidated `message.reply`
- Typing lifecycle events for both reply modes
- Media fragments (image / file / audio / video) in either direction
- Filtered forwarding for thinking / tool-call content
- `clawchat_*` agent tools for profile, friends, media, and self-activation

## Protocol essentials (v2)

Every envelope is routed on two root-level fields — **no `to`** (deprecated
in the new protocol, fully removed from both SDK and wire):

| Field        | Type                    | Meaning                                             |
| ------------ | ----------------------- | --------------------------------------------------- |
| `chat_id`    | string                  | The conversation / subject id. Primary routing key. |
| `chat_type`  | `"direct"` \| `"group"` | Whether this is a DM or a group.                    |

An inbound envelope also carries a `sender`:

```ts
interface RoutingSender {
  id: string;        // individual user's id (not the chat_id)
  nick_name: string; // display handle
}
```

`chat_id` identifies *who to reply to*; `sender.id` identifies *who
spoke*. In a direct chat these resolve to the same user; in a group they
don't (the `chat_id` is the group, `sender.id` is the speaker).

### Reply preview

When an inbound message is a reply, `payload.message.context.reply` carries:

```ts
interface ReplyPreview {
  id: string;        // the replied-to message's sender id
  nick_name: string;
  fragments: Fragment[];
}
```

This replaces the legacy `sender_id` / `display_name` names.

## Endpoints

All REST endpoints live under `/v1/*` **except** `/media/upload` which is
intentionally unversioned on the upstream service. Every request carries
`Authorization: Bearer <token>` and `X-Device-Id: openclaw-clawchat`.

| Method | Path                          | Used for                                 |
| ------ | ----------------------------- | ---------------------------------------- |
| POST   | `/v1/agents/connect`          | Exchange invite code → `access_token`    |
| GET    | `/v1/users/me`                | Fetch this agent's profile               |
| GET    | `/v1/users/<userId>`          | Fetch a user profile                     |
| GET    | `/v1/friends?page=&pageSize=` | List friends, paginated                  |
| PATCH  | `/v1/agents/<userId>`         | Update `nick_name` / `avatar`            |
| POST   | `/v1/files/upload-url`        | Upload an avatar image                   |
| POST   | `/media/upload` *(unversioned)* | Upload general media (≤ 20MB)          |

### HTTP envelope

All REST responses follow a single unified shape:

```json
{ "code": 0, "msg": "ok", "data": { ... } }
```

- `code` is a number (business status code). `code === 0` is success.
- Any non-zero `code` raises a `ClawlingApiError`, with the original `code`
  preserved on `error.meta.code` and `msg` surfaced as the error message.
- `data` may be any JSON value (object / array / null) depending on the
  endpoint's contract.

Example error response:

```json
{ "code": 40101, "msg": "invite code expired or already consumed", "data": null }
```

## Onboarding (activation)

ClawChat does not ship raw WebSocket credentials — an agent onboards by
exchanging an invite code for an `access_token` + agent profile.

```bash
# One-shot: setup + login in one command (recommended)
openclaw channels setup --channel openclaw-clawchat --code INV-ABC123

# Setup is intentionally strict — a missing --code fails:
$ openclaw channels setup --channel openclaw-clawchat
Error: Clawling Chat setup requires --code (invite code from your admin).

# Interactive login (prompts via runtime.log + readline):
openclaw channels login --channel openclaw-clawchat

# Programmatic login from an agent turn:
# the LLM invokes the `clawchat_activate` tool with `{ code: "INV-ABC123" }`
# — trigger phrases include "clawchat INV-ABC123", "activate clawchat",
# "use invite code XYZ".
```

The `agents/connect` request body is fixed:

```json
{ "code": "<invite>", "platform": "openclaw", "type": "clawbot" }
```

The response persists the following into `channels.openclaw-clawchat`:

- `data.access_token` → `token`
- `data.refresh_token` → `refreshToken`
- `data.agent.user_id` → `userId`

`websocketUrl` and `baseUrl` stay whatever they already were (they each
fall back to built-in defaults in `config.ts` if not explicitly set).

## Quick setup

Edit `~/.openclaw/openclaw.json` (or let `setup` + `login` write it for you):

```json5
{
  channels: {
    "openclaw-clawchat": {
      enabled: true,
      // websocketUrl / baseUrl default to DEFAULT_WEBSOCKET_URL /
      // DEFAULT_BASE_URL in config.ts — override only when self-hosting.
      replyMode: "stream",
      groupMode: "all",         // "all" | "mention"
      forwardThinking: true,
      forwardToolCalls: false,
      // token / userId / refreshToken are written by the login flow.
    },
  },
}
```

## Configuration reference

| Key                       | Type                    | Default                 | Description                                                                         |
| ------------------------- | ----------------------- | ----------------------- | ----------------------------------------------------------------------------------- |
| `enabled`                 | boolean                 | `true`                  | Enable or disable the channel                                                       |
| `websocketUrl`            | string                  | `DEFAULT_WEBSOCKET_URL` | ClawChat WebSocket endpoint (override only for self-hosted)                         |
| `baseUrl`                 | string                  | `DEFAULT_BASE_URL`      | ClawChat HTTP API root (no trailing slash)                                          |
| `token`                   | string                  | — (written by login)    | Bearer token returned by `/v1/agents/connect`                                       |
| `refreshToken`            | string                  | — (written by login)    | Refresh token paired with `token`                                                   |
| `userId`                  | string                  | — (written by login)    | Stable agent id; used on mention detection and sender identity                      |
| `replyMode`               | `"static" \| "stream"`  | `"static"`              | Reply style                                                                         |
| `groupMode`               | `"all" \| "mention"`    | `"all"`                 | Group trigger policy (see below)                                                    |
| `forwardThinking`         | boolean                 | `true`                  | Forward reasoning / thinking content to chat                                        |
| `forwardToolCalls`        | boolean                 | `false`                 | Forward tool call content to chat                                                   |
| `stream.flushIntervalMs`  | integer                 | `250`                   | Streaming throttle window                                                           |
| `stream.minChunkChars`    | integer                 | `40`                    | Minimum chars to flush on the interval boundary                                     |
| `stream.maxBufferChars`   | integer                 | `2000`                  | Hard cap before forced flush                                                        |
| `reconnect.initialDelay`  | integer                 | `500`                   | First retry delay (ms)                                                              |
| `reconnect.maxDelay`      | integer                 | `15000`                 | Backoff cap (ms)                                                                    |
| `reconnect.jitterRatio`   | number                  | `0.3`                   | Jitter ratio applied to each scheduled retry                                        |
| `reconnect.maxRetries`    | number                  | `Infinity`              | Reconnect attempts cap (gateway is long-lived)                                      |
| `heartbeat.interval`      | integer                 | `20000`                 | Ping interval (ms)                                                                  |
| `heartbeat.timeout`       | integer                 | `10000`                 | Pong-timeout (ms) before teardown + reconnect                                       |
| `ack.timeout`             | integer                 | `15000`                 | Outbound ack wait (ms)                                                              |
| `ack.autoResendOnTimeout` | boolean                 | `false`                 | Kept off — reconnect path already re-queues                                         |

### `groupMode`

Controls how group-chat messages are handled:

- **`"all"` (default)**: every group message triggers a reply — open-listen mode.
- **`"mention"`**: only messages whose `context.mentions` list contains our
  `userId` trigger a reply. Quiet groups stay quiet.

Direct chats are always triggered regardless of this setting.

## Agent-initiated outbound (addressing scheme)

When the agent proactively sends a message via the openclaw outbound API
(`sendText({ to, text })`), the `to` string is parsed into a
`chat_id` + `chat_type` pair. Accepted forms (case-insensitive scheme,
prefix-before-first-colon):

| Form                        | Interpreted as                   |
| --------------------------- | -------------------------------- |
| `cc:{chat_id}`              | direct                           |
| `clawchat:{chat_id}`        | direct                           |
| `cc:direct:{chat_id}`       | direct (explicit)                |
| `cc:group:{chat_id}`        | group                            |
| `clawchat:direct:{chat_id}` | direct (explicit)                |
| `clawchat:group:{chat_id}`  | group                            |
| bare `{chat_id}`            | direct (backward-compat default) |

Unknown schemes (anything other than `cc:` / `clawchat:`) are treated as
a bare chat_id and default to direct.

## Tools

All tool names start with `clawchat_`. The activation tool is registered
**unconditionally**; the rest register only when the account is configured
(i.e. after a successful login).

| Tool                         | When registered | Purpose                                              |
| ---------------------------- | --------------- | ---------------------------------------------------- |
| `clawchat_activate`          | always          | Exchange invite code for a token (onboarding)        |
| `clawchat_get_my_profile`    | after login     | Fetch the agent's own profile                        |
| `clawchat_get_user_info`     | after login     | Fetch a user profile by `userId`                     |
| `clawchat_list_friends`      | after login     | Paginated friend list (`page`, `pageSize`)           |
| `clawchat_update_my_profile` | after login     | Patch `nickname` / `avatar` on the agent's profile   |
| `clawchat_upload_file`       | after login     | Upload a local file (≤ 20MB), returns the public URL |

Tool trigger hints (visible to the LLM via each tool's `description`):

- `clawchat_activate`: fires on `clawchat <code>` / `activate clawchat` / `login to clawchat` / any invite-code looking paste.
- `clawchat_update_my_profile`: fires on name-change phrases (`your name is X`, `change your name to X`, `你叫 X`, `改名为 X`, etc.) and avatar-change phrases (`change your avatar`, `生成头像`, `换个头像`) — the agent is expected to chain `clawchat_upload_file` → `clawchat_update_my_profile` for avatar URLs.

## Agent prompt hints

The channel also publishes a `agentPrompt.messageToolHints` list that is
appended to the agent's system prompt when a clawchat turn arrives:

- Omit `target` to reply to the current chat (auto-inferred). To target a
  specific chat explicitly, use `cc:{chat_id}` (direct, default) or
  `cc:group:{chat_id}` (group). `clawchat:` is accepted as a synonym.
- ClawChat supports media fragments (image / file / audio / video) alongside text in the same message.
- ClawChat stream mode emits `message.created` → progressive `message.add` deltas → `message.done`, followed by a consolidated `message.reply` with the merged text.

## Media

### Inbound

When an upstream message contains `image` / `file` / `audio` / `video`
fragments, the channel downloads the public URL via the shared media
runtime and exposes the local paths to the agent through the
`MediaPath` (first item) / `MediaPaths` (full list) context fields. The
text body keeps a markdown placeholder for each media item
(`![name](url)` for images, `[name](url)` for the others) so the agent
can refer to them in language. Each fetch is capped at 20 MB; failures
on a single item are logged at info level and dropped.

### Outbound

The agent's reply payload may carry `mediaUrl` (single) or `mediaUrls`
(array). Each URL can be either a remote HTTP(S) URL or a local file
path (gated by the runtime's allowed roots). The channel uploads each
asset via `POST /media/upload`, and emits the returned URL as a
fragment of the corresponding kind alongside the text body.

Avatar uploads use the separate `POST /v1/files/upload-url` endpoint,
accessible to agents through the `clawchat_upload_file` tool's avatar
path. Non-avatar media flows through the unversioned `/media/upload`
route.

When a reply carries media, the channel forces **static** reply mode
even if `replyMode: "stream"` is configured — streaming + media isn't
supported on the wire. When `replyCtx` is present together with media,
the channel downgrades to a non-reply `sendMessage` and logs the
choice.

## Protocol mapping

- inbound `message.send` → new chat turn
- inbound `message.reply` → chat turn with reply context
- outbound static reply → SDK `sendMessage` / `replyMessage` (ack-tracked)
- outbound stream reply → `message.created` / `message.add` (many) /
  `message.done` / then a consolidated `message.reply` — all four frames
  share the **agent-side** `payload.message_id` minted at `created` time
  (never reuses the inbound user id)
- typing indicator → `typing.update`

### Streaming frame shapes

`message_id` on stream frames is a fresh agent-side id minted at
`message.created`; the inbound user message id lives only on
`reply.reply_to_msg_id` inside the final `message.reply`.

```jsonc
// message.created — intentionally minimal
{
  "version": "2",
  "event": "message.created",
  "chat_id": "chat-alice",
  "chat_type": "direct",
  "payload": { "message_id": "agent-stream-01K..." }
}

// message.add — each delta carries BOTH cumulative `text` and the new `delta`
{
  "version": "2",
  "event": "message.add",
  "chat_id": "chat-alice",
  "chat_type": "direct",
  "payload": {
    "message_id": "agent-stream-01K...",
    "sequence": 3,
    "mutation": { "type": "append", "target_fragment_index": null },
    "fragments": [
      { "kind": "text", "text": "Hello, world", "delta": ", world" }
    ],
    "streaming": {
      "status": "streaming",
      "sequence": 3,
      "mutation_policy": "append_text_only",
      "started_at": null,
      "completed_at": null
    },
    "added_at": 1776406831114
  }
}

// message.done — carries the full merged final text
{
  "version": "2",
  "event": "message.done",
  "chat_id": "chat-alice",
  "chat_type": "direct",
  "payload": {
    "message_id": "agent-stream-01K...",
    "fragments": [{ "kind": "text", "text": "Hello, world" }],
    "streaming": { "status": "done", "sequence": 3, "mutation_policy": "append_text_only", "started_at": null, "completed_at": 1776406831120 },
    "completed_at": 1776406831120
  }
}

// message.reply — consolidated final, SAME message_id, references user's inbound
{
  "version": "2",
  "event": "message.reply",
  "chat_id": "chat-alice",
  "chat_type": "direct",
  "payload": {
    "message_id": "agent-stream-01K...",
    "message_mode": "normal",
    "message": {
      "body": { "fragments": [{ "kind": "text", "text": "Hello, world" }] },
      "context": {
        "mentions": [],
        "reply": {
          "reply_to_msg_id": "user-msg-01K...",
          "reply_preview": {
            "id": "user-alice",
            "nick_name": "Alice",
            "fragments": [{ "kind": "text", "text": "hi" }]
          }
        }
      }
    }
  }
}
```

### Lazy open & empty runs

In streaming mode the session is opened **lazily** on first real content.
A run that produces no content (unknown agent, send-policy denied, etc.)
emits **no** frames at all — neither `message.created` nor `message.done`
nor a consolidated `message.reply` — it only logs
`no merged final content; skip consolidated reply`.

### Inbound envelope

```jsonc
{
  "version": "2",
  "event": "message.send",
  "chat_id": "chat-alice",
  "chat_type": "direct",
  "sender": { "id": "user-alice", "nick_name": "Alice" },
  "payload": {
    "message_id": "user-msg-01K...",
    "message_mode": "normal",
    "message": {
      "body": { "fragments": [{ "kind": "text", "text": "hi" }] },
      "context": { "mentions": [], "reply": null }
    }
  }
}
```

### Group envelope

Same shape; `chat_type` flips to `"group"` and `chat_id` identifies the
group (not the speaker). `sender.id` stays the speaker's id; we look up
`context.mentions` to detect `@<self>`.

```jsonc
{
  "version": "2",
  "event": "message.send",
  "chat_id": "group-abc",
  "chat_type": "group",
  "sender": { "id": "user-alice", "nick_name": "Alice" },
  "payload": {
    "message_id": "user-msg-01K...",
    "message_mode": "normal",
    "message": {
      "body": { "fragments": [{ "kind": "text", "text": "@bot ping" }] },
      "context": { "mentions": ["bot-user-id"], "reply": null }
    }
  }
}
```

## Chat interaction diagram

End-to-end view across operator setup, agent onboarding, inbound
dispatch, and streaming outbound. Use this as the migration map when
refactoring the pipeline.

```mermaid
sequenceDiagram
  autonumber
  participant OP as Operator
  participant CLI as openclaw CLI
  participant PLG as openclaw-clawchat plugin
  participant LOGIN as login.runtime
  participant HTTP as Clawling HTTP (/v1/*)
  participant CFG as openclaw.json
  participant GW as channel gateway
  participant SDK as @newbase-clawchat/sdk
  participant WS as Clawling WebSocket
  participant SRV as Clawling server
  participant AGT as Agent runner
  participant USER as User (Alice)

  %% --- onboarding ---
  rect rgb(245,246,252)
    note over OP,CFG: Onboarding — one-shot setup + login
    OP->>CLI: openclaw channels setup --channel openclaw-clawchat --code INV-ABC
    CLI->>PLG: setup.validateInput({ code })
    PLG-->>CLI: ok
    CLI->>PLG: setup.applyAccountConfig
    PLG-->>CFG: write { enabled: true }
    CLI->>PLG: setup.afterAccountConfigWritten
    PLG->>LOGIN: runOpenclawClawlingLogin({ readInviteCode: () => "INV-ABC" })
    LOGIN->>HTTP: POST /v1/agents/connect\nX-Device-Id: openclaw-clawchat\n{ code, platform:"openclaw", type:"clawbot" }
    HTTP-->>LOGIN: { code:0, msg:"ok", data: { agent, access_token, refresh_token } }
    LOGIN-->>CFG: write { token, userId, refreshToken }
    LOGIN-->>OP: "login succeeded (user_id=…, nick_name=…)"
  end

  %% --- gateway boot ---
  rect rgb(242,249,244)
    note over GW,SRV: Gateway boot + handshake
    OP->>CLI: openclaw gateway run
    CLI->>PLG: gateway.startAccount
    PLG->>GW: startOpenclawClawlingGateway
    GW->>SDK: createWSClient({ url, token, reconnect, heartbeat, ack })
    GW->>SDK: client.connect()
    SDK->>WS: open WS
    WS-->>SDK: connect.challenge { nonce }
    SDK-->>WS: connect { nonce, token, signature = HMAC(token, nonce) }
    WS-->>SDK: hello-ok
    SDK-->>GW: resolved
    GW-->>PLG: status { connected: true, running: true }
  end

  %% --- inbound ---
  rect rgb(253,250,241)
    note over USER,AGT: Inbound message → dispatch
    USER->>SRV: "hi"
    SRV->>WS: message.send<br/>chat_id="chat-alice", chat_type="direct"<br/>sender={id:"user-alice", nick_name:"Alice"}<br/>payload.message_id="user-msg-01K…"
    WS->>SDK: envelope
    SDK->>GW: client.on("message")
    GW->>GW: dispatchOpenclawClawlingInbound<br/>(dedupe, extract fragments,<br/>groupMode gate if chat_type=group)
    GW->>GW: fetchInboundMedia (if any)
    GW->>CFG: recordInboundSession
    GW->>AGT: rt.reply.dispatchReplyFromConfig(ctx)
  end

  %% --- streaming outbound ---
  rect rgb(252,244,249)
    note over AGT,USER: Streaming outbound (replyMode="stream")
    AGT-->>GW: onReplyStart (resets accumulators — no frame yet)
    AGT-->>GW: first partial / reasoning / block content
    GW->>GW: queueStreamSnapshot → openSessionIfNeeded<br/>mint fresh agent-side message_id
    GW->>SDK: emitStreamCreated<br/>message.created<br/>chat_id, chat_type, payload.message_id="agent-stream-…"
    SDK->>WS: frame
    WS->>SRV: forward
    SRV-->>USER: stream begins

    loop onPartialReply / onReasoningStream / deliver(block|tool)
      AGT-->>GW: partial snapshot
      GW->>GW: buffered-stream.queueSnapshot<br/>(merge + flush on minChunkChars / flushIntervalMs)
      GW->>SDK: emitStreamAdd<br/>fragments:[{ kind:"text", text:&lt;cumulative&gt;, delta:&lt;new&gt; }]
      SDK->>WS: frame
    end

    AGT-->>GW: onIdle (agent run complete)
    GW->>SDK: emitStreamDone<br/>fragments:[{ kind:"text", text:&lt;merged&gt; }]
    SDK->>WS: frame
    GW->>SDK: emitFinalStreamReply (low-level transport send)<br/>message.reply<br/>payload.message_id = "agent-stream-…"<br/>reply_to_msg_id = user-msg-01K…
    SDK->>WS: frame
    WS->>SRV: forward
    SRV-->>USER: final reply
  end

  %% --- static outbound ---
  rect rgb(240,246,252)
    note over AGT,USER: Static outbound (replyMode="static" OR media present)
    AGT-->>GW: deliver(final, { text, mediaUrls })
    GW->>HTTP: POST /media/upload (per asset)
    HTTP-->>GW: { code:0, msg:"ok", data:{ url, size, mime } }
    GW->>SDK: client.sendMessage / replyMessage<br/>{ chat_id, chat_type, body: { fragments: [text + media] } }
    SDK->>WS: message.send / message.reply
    WS-->>SDK: message.ack { message_id, accepted_at }
    SDK-->>GW: ack
    WS->>SRV: forward
    SRV-->>USER: final reply
  end

  %% --- reconnect ---
  rect rgb(252,245,245)
    note over SDK,WS: Transport failure → SDK reconnect (no work for us)
    WS--xSDK: close / error
    SDK->>SDK: scheduleReconnect<br/>computeBackoff(attempt) = initialDelay * 2^attempt,<br/>capped at maxDelay, jitterRatio applied
    SDK->>WS: retry openSocket (delay elapses)
    WS-->>SDK: connect.challenge (restart handshake)
  end
```

### Text-only architectural view

```
┌──────────────────┐       operator cmds        ┌────────────────────┐
│   openclaw CLI   ├────────────────────────────►   plugin adapter   │
└──────────────────┘                             │  (channel.ts)      │
                                                 │  ├─ setup          │
                                                 │  ├─ auth.login     │
                                                 │  ├─ gateway        │
                                                 │  ├─ outbound       │
                                                 │  │   sendText      │
                                                 │  │   (cc:/clawchat:│
                                                 │  │    URI parser)  │
                                                 │  └─ agentPrompt    │
                                                 └─────┬──────────┬───┘
                                                       │          │
                                             dispatch  │          │ HTTP
                                                       ▼          ▼
  ┌──────────────────────────────┐       ┌────────────────────────┐
  │  runtime.ts                  │       │ api-client.ts          │
  │  ├─ dispatchInbound          │       │ ├─ /v1/agents/connect  │
  │  ├─ fetchInboundMedia        │       │ ├─ /v1/users/me        │
  │  ├─ createReplyDispatcher    │       │ ├─ /v1/users/<id>      │
  │  │   ├─ sendStatic           │       │ ├─ /v1/friends         │
  │  │   └─ openBufferedStream   │       │ ├─ /v1/agents/<id>     │
  │  │      (lazy on 1st content)│       │ ├─ /v1/files/upload-url│
  │  └─ client.on("message")     │       │ └─ /media/upload       │
  └──────────┬───────────┬───────┘       └────────────────────────┘
             │           │
   inbound ◀─┘           └─▶ outbound
             ▼                  ▼
   ┌───────────────────────────────────────┐
   │       @newbase-clawchat/sdk client    │
   │ ┌──────────┐  ┌──────────┐ ┌─────────┐│
   │ │ transport│  │ heartbeat│ │ ack bag ││
   │ └──────────┘  └──────────┘ └─────────┘│
   └─────────────────────┬─────────────────┘
                         │ WebSocket (chat_id + chat_type routed)
                         ▼
                  Clawling server
```

## Troubleshooting

### No messages arrive
- Confirm the WebSocket URL is reachable; the SDK backs off but also keeps retrying.
- Confirm the upstream service emits protocol v2 envelopes (with `chat_id` + `chat_type` — anything still using `to` is pre-v2 and will be dropped).
- Check gateway logs for dropped envelopes (missing `payload.message_id` or wrong `message_mode`).

### `NO reply was produced (no final / block / tool dispatched)`
- The configured agent name doesn't exist in `cfg.agents`, or a send-policy denied the run, or another plugin claimed the binding. The log line enumerates the configured agents for cross-checking.

### `no merged final content; skip consolidated reply`
- The agent run produced no text / media. In streaming mode this means **no frames** are emitted (the session is opened lazily; empty runs stay silent). Upstream of this is usually the same root cause as the previous entry.

### Replies do not send
- Confirm `token` is valid; `auth failed` in the logs means the onboarding needs to be redone.
- For per-call ack timeouts, see `ack.timeout` / `ack.autoResendOnTimeout`.

### Reply shape is wrong for your client
- Use `replyMode: "static"` for clients expecting one final message.
- Use `replyMode: "stream"` for clients that render incremental assistant output — they'll see `message.created` → many `message.add` → `message.done`, followed by a consolidated `message.reply` (same `message_id`).

### Group bot is too chatty
- Default `groupMode: "all"` replies to every group message. Switch to `"mention"` for quieter groups, and make sure the client's `context.mentions` includes your `userId` when the bot should reply.

### Noisy chat
- Set `forwardThinking: false` to suppress reasoning blocks.
- Keep `forwardToolCalls: false` (default) to suppress tool invocations / results.

## Related docs

- [Chat Channels](/channels/index)
- [Channel troubleshooting](/channels/troubleshooting)
