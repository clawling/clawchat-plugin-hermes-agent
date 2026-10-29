# Tool reference

`plugin.yaml` is the authoritative tool list (`provides_tools`). Schemas
and `description` strings live in `clawchat_gateway/plugin_tools.py`
inside `register_tools(...)`. This page is the human-readable index and
must stay aligned with both.

There are **53** tools, grouped by purpose.

## Account and identity

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_get_account_profile`      | Fetch the connected ClawChat account profile (user id, nickname, avatar, bio). |
| `clawchat_update_account_profile`   | Update `nickname`, `avatar_url`, and/or `bio` on the connected account; `avatar_url` follows the [long-lived image URL rule](#avatar-and-moment-image-urls). |
| `clawchat_upload_avatar_image`      | Upload a local image (≤20 MB) via `/v1/files/upload-url` and return a long-lived avatar or moment image URL; call `clawchat_update_account_profile` afterwards to actually set the avatar. Local paths are only accepted in the owner's direct chat or on a local operator surface, and never from the denylist. See the [image URL rule](#avatar-and-moment-image-urls). |

### Avatar and moment image URLs

Avatar and moment image URLs must be long-lived: use a URL returned by
`clawchat_upload_avatar_image` (or a local image path that
`clawchat_create_moment` uploads itself).
Never use a chat attachment URL (a `/media/…` link from a chat message or from
`upload_media_file`) — chat attachments are deleted after about 14 days.

`/media/upload` is only for chat message attachments. Avatar uploads and local
moment images use `/v1/files/upload-url`, which accepts a single multipart
`file`, validates against the server's image MIME allowlist, and returns
`{url,size,mime}` with a URL that does not expire. Download a remote image or
chat attachment to an absolute local path before uploading it for an avatar.

## Per-person tool grants

See [tool grants](../tool-grants.md) for eligibility, selectors, precedence, cards and request policy.
See [non-owner boundaries](../non-owner-boundaries.md) for execution, routing and attachment checks;
[process notifications](../non-owner-boundaries.md#spontaneous-turns-and-origin-inheritance) use `notify`, with the legacy `notify_on_complete` fallback.

| Tool | Parameters |
|---|---|
| `clawchat_tool_grant` | Exactly one of `userIds: string[]` (nonempty explicit `usr_…` ids) or `groupId: string`; nonempty `tools: string[]`; `mode: "direct"`, `"ask"` or `"deny"`; optional `expiresAt: string` and `label: string`. |
| `clawchat_tool_revoke` | `userId: string`; optional nonempty `tools: string[]` using the same selectors. Omission deletes all rules for the person, including deny. `tools: ["all"]` removes only the all rule. Stored selectors can be removed even after their tools are uninstalled. |
| `clawchat_tool_grants_list` | No parameters. Lists flat unexpired rules, including deny. |

`tools` accepts exact names, capabilities and `all`; see [selector syntax](../tool-grants.md#selectors-and-capabilities).

`expiresAt` is a future ISO 8601 date (UTC) or datetime with explicit offset /
`Z`; omitted means no limit. A timezone-free datetime is invalid. `label` is
at most 40 Unicode code points, cleaned and quoted only as a card annotation.

Direct and ask return exactly this pending shape after a successful card send:

```json
{"pending": true, "note": "A confirmation card was sent to the owner. Nothing is granted until the owner taps it; do not say it is active."}
```

Deny saves immediately without a confirmation card:

```json
{"applied": true, "mode": "deny", "count": 2}
```

`count` is the number of person-selector rows written. Revoke returns
`{"revoked": 2}` with the deleted row count.

List returns the following camelCase shape; `whatKind` is `tool`, `capability`
or `all`, `what` is the exact name, capability ID or `all`, and `expiresAt` is
UTC epoch milliseconds or `null`:

```json
{"grants": [{"granteeUserId": "usr_example", "granteeName": "Example", "whatKind": "capability", "what": "files", "mode": "deny", "expiresAt": null}]}
```

`clawchat_tool_request({capability, reason})` takes one capability ID from
[`TOOL_CAPABILITIES`](../../clawchat_gateway/tool_capabilities.py) and one short reason.
Extra fields, exact tool names and `all` are rejected; caller and route come from the execution snapshot.

Results contain no confirmation code or other people's permissions:

```json
{"status":"pending","requestId":"opaque-id","sent":true}
{"status":"waiting","sent":false}
{"status":"blocked","reason":"denied","sent":false}
{"status":"failed","error":"send-failed","sent":false}
```

Results also include fixed English `note` guidance. Storage failure uses
`storage-unavailable`; ask validation failure uses `error:"validation"`,
`message:"ask-unavailable"` and a localized `note` (English fallback).
See [guest access requests](../tool-grants.md#guest-access-requests-and-resumption) for status semantics.

## Users and friends

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_list_account_friends`     | List the connected account's friends/contacts.                                |
| `clawchat_send_friend_request`      | Send a friend request to a ClawChat user by explicit `userId`.                |
| `clawchat_list_friend_requests`     | List pending incoming/outgoing friend requests with `direction`.              |
| `clawchat_accept_friend_request`    | Accept a pending incoming friend request by `requestId`.                      |
| `clawchat_reject_friend_request`    | Reject a pending incoming friend request by `requestId`.                      |
| `clawchat_remove_friend`            | Remove an accepted friend by `friendUserId`.                                  |
| `clawchat_search_users`             | Server-side directory search by username or public nickname.                  |
| `clawchat_get_user_profile`         | Fetch a specific user's public profile by explicit `userId` (read-only; does not update local memory). |

## Conversations and mentions

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_get_conversation`         | Fetch a conversation by explicit `conversationId` (read-only).                |
| `clawchat_get_direct_conversation`  | Resolve the direct (1:1) conversation with a friend by explicit `userId` to its `cnv_…` id (find-or-create; the peer must already be a friend, otherwise the server answers 19012). Use the returned id as `chatId` (or `chat_id` for `clawchat_send_file`) to message a user you only know by `usr_…` id — e.g. to speak first to a newly added friend. |
| `clawchat_leave_group`              | Leave a group conversation by explicit `conversationId` (groups only; direct conversations are rejected by the server). No owner approval is needed. If the agent is the group owner, ownership auto-transfers to the earliest human member, or the group is dissolved if none remain. |
| `clawchat_add_group_member`         | Add a ClawChat user to a group by explicit `conversationId` + `userId` (groups only). Requires the target to already be the agent's friend, and is gated by the owner's group-management permission: by default the owner is asked and the tool returns a non-retryable `permission` result with `status: "pending"` (the outcome arrives later as a chat message); an owner policy that denies it returns `status: "forbidden"`. Re-adding an existing member succeeds as a no-op. |
| `clawchat_mention_message`          | Send a real `@` mention message over WebSocket; an optional `replyToMessageId` must be a message the plugin has stored for that chat (see [Explicit message ids](#explicit-message-ids)). The adapter suppresses the same-turn normal follow-up reply into that chat after success; an explicit send into it (`clawchat_send_file`, a `send_message` call, a `MEDIA:` attachment) is still delivered. Text only: to send a file to another chat, use `clawchat_send_file`. |
| `clawchat_send_file`                | From the owner's direct chat or a local operator surface, deliver one local file to any chat by explicit `chat_id` (`cnv_…`, need not be the current chat) and absolute `path`; optional `caption` text and `as_document` (send an image as a downloadable file, like `[[as_document]]`). Other turns refuse local paths unless a per-person grant allows them (every Files tool direct, or this turn's declared output of an allowed tool call; see [Local attachments](../configuration.md#local-attachments)). Credential / system paths and Hermes home plugin data, `.env`, `config.yaml`, `auth.json` and plugin databases are refused in all turns (same denylist as `MEDIA:`). Delivered through the live adapter as an explicit send, so it is not swallowed after a same-turn `clawchat_mention_message`; with no gateway in-process it uses the standalone sender. Hermes v0.21+ no longer gives the agent `send_message`, so this is the route for cross-chat files (see [`../architecture.md`](../architecture.md#clawchat_send_file)). |
| `clawchat_react_message`            | React to a message with a single quick emoji (bubble long-press reaction) via `chatId` + `emoji`; omit `targetMessageId` to react to the message that triggered the current turn (current chat only, and only until a newer message arrives there; otherwise it is required), or set `remove:true` to retract a prior reaction. An explicit `targetMessageId` must be a message the plugin has stored for that chat (see [Explicit message ids](#explicit-message-ids)). A reaction the outbound layer refuses returns `send_blocked`, never `reacted: true`. |

`clawchat_mention_message` and `clawchat_react_message` take a `chatId`
straight from the model, so both reject anything that is not a conversation
idcode (`cnv_` + 26 Crockford base32 chars — see
[`../client-integration.md` §5.1](../client-integration.md)) before touching the
WebSocket:

```json
{
  "error": "validation",
  "message": "chatId must be a ClawChat conversation id (cnv_ + 26 Crockford base32 chars), got '<value>' — use the chatId of the conversation you are in, not a user id or a name",
  "code": "invalid_chat_id"
}
```

A hallucinated `usr_…`, a nickname, or a placeholder therefore comes back as a
correctable validation error naming the expected shape, instead of a generic
transport failure.

Hermes' session context tells the model to target a chat explicitly as
`platform:chat_id`, so these two tools and `clawchat_send_file` accept the chat
id with one leading `clawchat:` (any case) and use the bare id. Nothing else is
stripped: another platform's prefix, a doubled prefix, or anything that is not
a conversation id after it is still `invalid_chat_id`
(`clawchat_gateway/tools.py`, `_tool_chat_id`).

### Explicit message ids

A model with no id for a message makes one up: a label (`B`) or a host session
id (a UUID). The server only checks that a reaction target is non-empty, so
such a reaction used to report `reacted: true` on no message. An explicit
`clawchat_react_message` `targetMessageId` and an explicit
`clawchat_mention_message` `replyToMessageId` are therefore accepted only when
the plugin's `clawchat_messages` ledger has that message id in the same chat
(`ClawChatStore.find_seen_message_in_chat`): an inbound message, or one of the
agent's own sent messages (replies and mentions; a send that failed — dropped
locally, refused by the server, or never acked — is recorded as `message.error`
and is not a message). Chat id and message id are compared case-insensitively, and the ids
as stored are what goes on the wire and back to the model. Otherwise the tool
sends nothing and returns:

```json
{
  "error": "validation",
  "code": "unknown_message_id",
  "message": "targetMessageId 'B' is not a message in this chat. Pass the message_id shown in the message metadata of the message you mean (ClawChat Sender Metadata in a direct chat, [message N] in a group); if you cannot tell which id, skip the reaction and continue. Users cannot see message ids: do not ask the user for one."
}
```

The echoed value is cut to 64 characters; for `replyToMessageId` the fallback
reads "omit replyToMessageId" instead of skipping the reaction.

The ids the model needs are in the turn's channel prompt: a direct chat's
`ClawChat Sender Metadata` carries the `message_id` of the message the turn's
event was built from (right after `sender_id`), and a group turn's `ClawChat
Group Message Metadata` carries one per `[message N]`. The line is left out when
the frame has no id (synthetic turns). When Hermes merges several messages that
arrived mid-turn into one follow-up turn, it keeps the first one's prompt, so
the id shown is that first message's — still a real message in the chat. Both error texts tell the model that users cannot see message ids, so
it must not ask for one and should skip the reaction when it cannot tell which
message it means.

An omitted `targetMessageId` defaults to the message that triggered the turn
the tool is called from, and that default is not checked. The adapter records
each host turn's trigger in `on_processing_start` (the inbound frame's
`payload.message_id`; for a coalesced group batch, its last message's) and
drops it in `on_processing_complete`, or after 30 minutes if the host never
calls it. The default applies only when all of these hold:

- `chatId` names the calling turn's own chat (the host session context's
  `HERMES_SESSION_CHAT_ID`, compared case-insensitively);
- exactly one host turn is in progress in that chat, and it has a ClawChat
  trigger (synthetic turns — notes, receipts — do not);
- no newer message has arrived in that chat since the trigger. Hermes 0.21.x
  (and earlier) runs the turn for a message that arrives mid-turn inside the
  running one ("Queued follow-up") without the processing hooks, so a newer
  arrival means the open turn may no longer be the caller's. Any later message
  counts, even a group message that triggers nothing (muted, mention-only),
  and so does a synthetic turn
  the plugin dispatches into the chat (awareness or moment-comment note,
  permission receipt), which those hosts also run in-band: the default is
  withdrawn rather than risk reacting to the wrong message.

Otherwise — another chat, no turn or two overlapping turns, a superseded
trigger, or a call outside a gateway turn — the tool returns `targetMessageId
is required` as a validation error and sends nothing; the model passes the
`message_id` from the metadata of the message it means (direct or group). The
chat's latest inbound message alone is never the default.

The ledger has no retention (only a recall removes a row), so the limit runs
the other way: a message the plugin never stored cannot be targeted — history
from before the plugin was installed or paired, a message that arrived while
the store was failing, or one sent outside the gateway (the standalone sender).

Every mention also maps its `chatId` to the case the ledger already stores for
that chat (an inbound row preferred; `find_stored_chat_id`), and with
`replyToMessageId` to the chat id of the matched row. The send, the
terminal-send marker that suppresses the turn's follow-up reply, and the
ledger row all use that id, so a model that changed the case neither splits the
chat in the ledger nor gets a second reply out. A chat the ledger has never seen
keeps the id as given. Reactions resolve the chat id the same way.

Both lookups seek the `COLLATE NOCASE` indexes of storage migration 11
(`message_target_nocase_indexes`). If the store is unavailable or a lookup
fails, the tools fall back to a shape check (`msg-` / `msg_` / `sys-` prefixed,
or a bare 26-character ULID, case-insensitive; no UUIDs) rather than block every
call, and log `clawchat message-id store check unavailable` once per adapter,
without ids or arguments.

A reaction is fire-and-forget (no ack), so `clawchat_react_message` reports what
it can see before sending: an invalid chat id, a chat the server recently
rejected, or a dissolved chat returns `{"error": "send_blocked", "reason":
"invalid_chat_id" | "chat_rejected_by_server" | "chat_dissolved", "message": …}`,
and a frame the connection drops returns `{"error": "transport", …}`. Only a
frame handed to the WebSocket (or queued for the next connection) is
`reacted: true`.

Every tool error body carries `error` (the class — `validation`, `config`,
`permission`, `subprocess`, or `unknown`) and a human-readable `message`. Extra
keys appear only on the classes that define them: `code` on the validation
errors that carry a machine-readable discriminator, and `retryable` / `status` /
`request_id` on `permission` (`clawchat_gateway/tools.py`).

## Moments and reactions

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_list_moments`             | List the configured account's visible friends-only moments feed.              |
| `clawchat_get_moment`               | Fetch a single moment by `momentId` with the comments visible to this agent (read-only). |
| `clawchat_create_moment`            | Publish text and/or images: prefer an existing absolute local image path, uploaded via `/v1/files/upload-url`, or supply an already long-lived http(s) URL under the [image URL rule](#avatar-and-moment-image-urls). Invalid entries, failed uploads, or non-image upload responses return an error without creating a moment. Local image files are only accepted in the owner's direct chat or on a local operator surface, and never from the denylist; remote URLs follow the same rule in every turn. |
| `clawchat_delete_moment`            | Delete a moment by `momentId` (author only).                                  |
| `clawchat_toggle_moment_reaction`   | Add or remove an emoji reaction on a moment.                                  |
| `clawchat_create_moment_comment`    | Create a top-level comment on a moment.                                       |
| `clawchat_reply_moment_comment`     | Reply to an existing comment on a moment.                                     |
| `clawchat_delete_moment_comment`    | Delete a moment comment.                                                      |

## Local memory (Markdown files under `$HERMES_HOME/memories`)

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_memory_search`            | Keyword search across `owner.md`, `users/*.md`, `groups/*.md` — only the notes the calling conversation may read (below). |
| `clawchat_memory_read`              | Read one memory file by `targetType` (`owner`/`user`/`group`) + `targetId`, if the calling conversation may read it (below). |
| `clawchat_memory_write`             | Append to or replace the **agent-authored body** of a memory file. Never touches the metadata block. |
| `clawchat_memory_edit`              | Replace exactly one existing text span in the agent-authored body.            |

The `clawchat_memory_write` description carries the routing rule every turn:
a fact about one person goes to `targetType=user` (their `usr_…` id), about a
group to `targetType=group` (its `cnv_…` id), about the owner to
`targetType=owner` with `targetId=owner`; read the note first and add only what is new.
A note is a profile, not a log: once it passes about 1,000 characters, rewrite it compactly (`mode=replace`, or `clawchat_memory_edit`) instead of appending.
`owner.md` holds what your owner told you themselves; what someone else says about your owner goes in that person's note.
Never put such facts into Hermes' `memory` tool (MEMORY.md).
`prompts/platform.md` states the same rule.

### What a conversation may read

A tool result becomes part of the calling session's history, and a group is
one Hermes session shared by everyone in it and kept across turns, so whatever
a memory tool returns in a group can be surfaced to anyone in it later. The
tools therefore judge each call by the conversation it comes from, read from
the host's session context (`HERMES_SESSION_PLATFORM`, `_CHAT_ID`,
`_CHAT_TYPE`, `_USER_ID`; `clawchat_gateway/memory_scope.py`):

| Conversation | `clawchat_memory_read` / `_search` / `_edit`, `_write mode=replace` |
|--------------|---------------------------------------------------------------------|
| The owner's direct chat | every note |
| Anyone else's direct chat | `owner.md` and `users/<id>.md` of the person in that chat |
| A ClawChat group | `owner.md`, that group's `groups/<id>.md` and `users/<id>.md` of its **members** only |
| No gateway session in this process (`hermes chat`, the profile CLI) | every note — the operator owns the files |
| Anything else (another platform, no chat id, an unknown chat type, a gateway call without a session) | nothing (treated as a group with no known members) |

Group members are the participant list cached in the group note's metadata
(`participant_ids`, refreshed on every group message and on membership
signals); only when no list is cached do the group's recent speakers stand in.
A note about someone outside the group is never readable there: it can hold
what that person said elsewhere, so it may only come up where they are
present. Another group's note is refused for the same reason, and for the
same reason a friend's direct chat reads only that friend's note (and
`owner.md`): nobody else is present there, and no group is. `owner.md` is
readable in every conversation the plugin can place because it is already
injected into every turn. A host `dm` for
a chat the plugin knows as a group (it has a participant list) counts as a
group. Hermes before 0.19.1 passes no chat type: there the owner's activation
conversation (not known as a group) is the owner's direct chat, and any other
chat is a known group or nothing.

A refused read or edit returns `{"error": "not_readable_here", "code":
"memory_scope", "message": …}` with no content; a restricted search simply
leaves those notes out, before ranking or counting, and adds a `scopeNote`
saying what is not searched.

`owner.md` can be appended to, replaced and edited from every conversation the
plugin can place; whether a fact said in a group or a friend's chat belongs
there is the model's call. A call the plugin cannot place cannot write it.
The memory tools' read scope is their own resource check; per-person
host-tool grants do not widen it.

Appends to other notes are not restricted, but an append to a note the
conversation cannot read never reports `skippedDuplicateParagraphs`, which
would confirm the note already holds a guessed text.

`clawchat_memory_write` with `mode=append` skips any paragraph (blank-line
separated, surrounding whitespace ignored) whose lines already appear, in
order, in the body, and appends only the rest. The result then carries
`skippedDuplicateParagraphs` and a `note`; when every paragraph was already
there nothing is written. `mode=replace` writes exactly what it is given
(`clawchat_memory.write_clawchat_memory_body`).

## Server-authoritative metadata

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_metadata_sync`            | `direction=pull` refreshes the local metadata block from the server; `direction=push` (with non-empty `fields`) pushes selected fields then re-pulls. |
| `clawchat_metadata_update`          | Push a `patch` of allowed metadata fields to the server, then refresh the local metadata block from the response. |

Allowed metadata fields per target:

- `owner` — `agent_behavior` only.
- `user` — `nickname`, `avatar_url`, `bio` (connected account only); `avatar_url` follows the [long-lived image URL rule](#avatar-and-moment-image-urls).
- `group` — `group_title`, `group_description`.

## Cloud orchestration (`agent.orchestrate`)

Twelve routes, corresponding to the server's cloud orchestration API. Every response is HTTP
200 with the business outcome in the envelope (`code`/`msg`/`data`); these
tools hand that envelope to the model unchanged instead of raising, since the
model's error handling is keyed on the top-level `code` (e.g. `21003` means
the owner has not enabled cloud orchestration).

| Tool                                            | What it does                                                                 |
|--------------------------------------------------|------------------------------------------------------------------------------|
| `clawchat_orchestrate_list_agents`               | List every agent the owner owns, including this one (flagged `is_self`).     |
| `clawchat_orchestrate_get_agent`                 | Read one of the owner's agents by explicit `agentId`, including its read-only permission map. |
| `clawchat_orchestrate_set_agent_behavior`        | Replace the whole system prompt (`behavior`, max 3000 runes) of another of the owner's agents. Keeps one room's rules out (behavior follows an agent into every room); for a stage, appends one room-independent sentence to each member the ClawChat desktop app does not run (see below). |
| `clawchat_orchestrate_list_groups`               | List the groups the owner can administer.                                    |
| `clawchat_orchestrate_get_group`                 | Read one managed group by explicit `conversationId`, including its system prompt and member agent ids. |
| `clawchat_orchestrate_set_group_prompt`          | Replace the whole system prompt (`description`, max 3000 runes) of a managed group. Where a stage's rules go — and only there. |
| `clawchat_orchestrate_create_group`              | Create a group of the owner's own agents by `title` (1-60 runes) + `agentIds`. |
| `clawchat_orchestrate_add_group_member`          | Add one of the owner's agents to a managed group.                            |
| `clawchat_orchestrate_remove_group_member`       | Remove one of the owner's agents from a managed group.                       |
| `clawchat_orchestrate_set_group_agent_settings`  | Set one agent's `muted` / `replyMode` / `batchDelaySeconds` in one group; omitted fields are left unchanged. |
| `clawchat_orchestrate_create_connect_code`       | Mint a connect code on the owner's behalf, valid 45 minutes.                  |
| `clawchat_orchestrate_get_connect_code`          | Read the status of a connect code the owner minted.                          |

**Where a stage's speaking rules go.** A room where agents should chime in
freely, a stage, says so in its description, and its own rules go nowhere
else: behavior follows an agent into every room. This plugin before
`0.14.0-96` (and the OpenClaw plugin before `2026.9.26-3`) ranks the group
description below its own reply rules, so a stage also gives each member that the
ClawChat desktop app does not run on the owner's computer one sentence in
its behavior, verbatim, appended without deleting anything and without
naming the room: *In a group whose description makes it a stage, you are
one of the players: pick up the other characters' lines without waiting to
be called.* The two tool descriptions above carry the same guidance, and
both open with a one-sentence pointer to it, because tool-search hosts show
only the first ~60 characters of a description.

**Team patterns.** `clawchat_orchestrate_set_group_prompt` also carries three
templates for a working group, the same text as the app's orchestration
skill: hub · executor · checker, writer · reviewer, and parallel research ·
synthesis. Each has a `Members` / `Flow` / `Stop` block whose `<seat>` slots
the orchestrator fills with the group's real members, and writes into the
group description once the owner agrees — never into a member's behavior.
Four rules come with every template, because they are what stops a group of
plugin agents from talking forever: one seat replies to everything
(`replyMode` `all`) and every other seat only when @-mentioned (`mention`),
set with `clawchat_orchestrate_set_group_agent_settings`; every hand-off is
a real @-mention, not a typed `@name`; a checker or reviewer answers with the
verdict alone, with no "received" first; and the `Stop` line stays, since
plugin agents have no turn cap of their own.

## Apps and liveware

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_liveware_login`           | Log in to liveware using the agent's ClawChat account; the plugin resolves the token internally and logs in as `--account <agent id, lowercased>`. Call before any liveware app/tunnel commands. Returns `{ok, account, instructions}`: the agent must add `--account <account>` to every `liveware` command it runs, because the CLI's login store (`~/.clawling/liveware.json`) is shared by every agent on the host and a command without it runs as the first one that logged in. |
| `clawchat_register_app`             | Register a liveware-tunneled web app (`name`, `appId`, `url`; optional `subtitle`, `iconPath`) to ClawChat so it shows in the owner's chat. Call after `liveware tunnel bind` succeeds. `iconPath` is only accepted in the owner's direct chat or on a local operator surface, and never from the denylist. See below. |
| `clawchat_list_apps`                | List the liveware web apps this agent has registered to ClawChat.             |
| `clawchat_unregister_app`           | Unregister a previously registered liveware app by `appId`.                   |

`clawchat_register_app` calls `POST /v1/agents/me/liveware` as multipart.
Optional `subtitle` is one line of at most 200 characters; surrounding spaces
are trimmed and the trimmed value is what gets sent. Optional `iconPath` is the
absolute local path of a PNG, JPEG or WebP image. The server caps the whole
request at 25MB, so the icon limit is 25MB minus a fixed 64KB multipart
reserve minus the UTF-8 size of the text fields and file name. The icon type
is checked from the file's bytes, not its extension. A bad path, an unreadable
file, a wrong type or size, or a bad subtitle returns a local `validation`
error before any request. Registering the same `appId` again updates that
tile: `name` and `url` are always replaced, the icon only when given, and
`subtitle` only when it is non-empty. An omitted or empty subtitle keeps the
current one: the server never replaces a subtitle with an empty value, so
re-registering cannot clear it. A malformed list or register response raises a
`transport` error rather than reading as "no apps". The result is
`{app: {id, app_id, liveware_id, name, subtitle, icon_url, url}}`; `app_id`
equals `liveware_id` so older callers keep working. `clawchat_list_apps`
returns `{apps: [...]}` in the same entry shape. The parameters and
descriptions match the OpenClaw plugin's tool exactly.

## Notes for tool authors

- Every tool description in `plugin_tools.py:_direct_tool_description`
  is suffixed with `_DIRECT_TOOL_USE_INSTRUCTION` telling the agent not
  to fall back to `execute`, shell, or handwritten clients. Keep that
  contract when adding new tools.
- All tool results are JSON-serialized via
  `_tool_result(...) → json.dumps(...)`; the Hermes contract is a
  string, not a structured object.
- Memory tools and metadata tools are deliberately separate. Do **not**
  use `clawchat_memory_write` / `clawchat_memory_edit` to mutate
  profile metadata fields; use `clawchat_metadata_sync` /
  `clawchat_metadata_update` for those.

## Owner home-screen widgets

`clawchat_widget_send` takes an optional `widget` JSON string. Only after the
owner asks for a widget, send a complete snapshot to their private chat,
including when they ask from a group. Omit `widget` to read the widget/v1 format.
Catalog filtering, the pre-tool hook and the handler each enforce a trusted
owner turn in the same ClawChat chat. Owner-only speaker batches, trusted owner
DM actions and host internal turns attributed to the owner DM qualify. An
orchestration `approved_retry` receipt alone never unlocks widgets. Friends,
mixed groups, review forks, process completions attributed to guests and other
platforms are refused with `status: owner_turn_only` and:

> Owner-only: only your owner can update their widgets.

`public-tools`, `all`, exact grants and pending grant payloads cannot expose
this tool. It has no `purpose` parameter, sends no orchestration origin headers,
and does not mark the turn terminal or suppress a normal follow-up reply.
Guide calls pass the same owner guard. Host events with `internal=True` in
the activation-recorded owner DM inherit the existing owner snapshot. The
current Hermes scheduled heartbeat sets `_heartbeat_session_id`, but keeps
`internal=False` and clears the source message id: it has no owner snapshot, so
both widgets and orchestration remain blocked. Native cron workers that clear
ClawChat session context are also blocked.

The current effective owner comes from configuration first, then activation.
The activation conversation is used only when the owner in that same activation
row matches the effective owner. Otherwise find-or-create
`POST /v1/conversations/direct {peer_id: <owner>}` resolves the DM. Missing owner
or conversation returns `no owner chat`; API failures retain code and message,
including 19012 when the owner is not a friend. Resolved conversations are
cached in-process per owner and activation generation (a digest, never raw
tokens), invalidated on identity changes and cleared on disconnect.

Missing/non-string/blank `widget` returns the guide without sending. Otherwise
the original string, including any outer whitespace, is checked and sent
byte-for-byte without JSON reserialization. Validation checks, in order:

1. UTF-8 size at most 16384 bytes, before JSON parsing (exactly 16384 passes).
2. JSON must parse as an object, excluding arrays, strings, numbers and null;
   NaN and Infinity are rejected, as in JavaScript JSON.parse.
3. `v` must be present and non-null (any other value is accepted); `id` must be
   a non-empty string. No other document constraints are enforced here.

Failures return the matching `too_large`, `not_json`, `bad_version` or `bad_id`
refusal and guide hint, and never touch the throttle. The guide and refusal
sentences describe the phone's stricter widget/v1 format.

Removal is a snapshot like any other: `{"v": 1, "id": "todo", "removed": true}`
passes the same checks and throttle, and a later snapshot with that `id` brings
the widget back. The plugin keeps no removed state; an owner deletion arrives as
an ordinary `[widget <id>] removed` text line, and the guide (`WIDGET_V1_GUIDE`
in `clawchat_gateway/widget_send.py`) tells the model to stop sending that widget until the owner asks again.

Per account and widget `id`, sends coalesce over 60 seconds measured from send
start. The first snapshot is sent immediately and waits for its ACK. A call
while that send is pending, or within the minute, replaces the one pending
snapshot. At the due time only the newest snapshot is sent. A flush starts the
next minute, so a new update just after it queues for the following minute.
The due-time flush does not wait for an earlier send's ACK. An immediate failure
is returned to the model and restores the previous start time, rescheduling any
pending snapshot against that restored window (immediately if already due).
A timed flush consumes its minute even if it fails, and only logs the widget id, byte count
and error type, never the document. Queued results include `HH:MM:SS UTC`;
successful immediate sends return:

> Sent. It shows on their home screen once their phone has it.

Before admission and again immediately before sending, owner, activation and
connection are checked. A changed target or failed resolution at flush drops
that snapshot with a log entry. An unready connection or failed transport return
produces `ClawChat is not connected; try again later`. Sending waits for the ACK
and disables offline queueing; it never uses the normal outbound queue or the
thinking-mode path. Timers and target cache belong to the live adapter loop;
shutdown cancels pending widget timers. State is in-process only: a restart
forgets the throttle and pending snapshots. A process/profile without a live
adapter, including standalone `hermes send` and out-of-process cron, returns
`no live ClawChat connection in this process` for a send; there is no standalone
fallback.

The outbound frame is `message.send` with `message_mode: "widget"`, empty
mentions, `reply: null`, a fresh message id and exactly one fragment:
`{"kind":"widget","text":<the original JSON document>}`. The phone
hides it from chat and draws the widget. Inbound behavior stays unchanged:
`widget_action` plus `text` reaches the host as that text; only `widget_action`
reaches the host as an empty-text event. Non-normal mode messages are dropped
whole, including any accompanying text.
