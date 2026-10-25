# Hermes prompt injection reference

This document lists the prompt injection surfaces relevant to the ClawChat
Hermes plugin and the Hermes host runtime. It records where injected text lands
in the final model request, regardless of whether the source is ClawChat-specific
or a general Hermes mechanism.

## Injection surfaces

| Entry point | Used by | Injected role | Timing | Persistence |
|-------------|---------|---------------|--------|-------------|
| `platform_hint` | Platform plugin | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB. |
| `MemoryProvider.system_prompt_block()` | Memory provider plugin | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB. |
| `MessageEvent.channel_prompt` | Platform adapter or message event | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `MessageEvent.channel_context` | Platform adapter or message event, on supported hosts | `user` | Prepended to the current message, before `[New message]`. | Stored with the user message and replayed in transcript history. |
| `MessageEvent.channel_prompt` → `## ClawChat Peer Memory` | ClawChat adapter, direct chats | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `MessageEvent.channel_prompt` → `## ClawChat Group Memory` | ClawChat adapter, group chats; owner and group notes on supported hosts | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `MessageEvent.channel_context` → `## ClawChat Speaker Memory` | ClawChat adapter, group chats on supported hosts | `user` | Current turn's speakers, within the remaining note budget. | Stored with the user message and replayed in transcript history. |
| `MessageEvent.channel_prompt` → `Memory routing:` line | ClawChat adapter, every chat | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `pre_llm_call` hook returning `context` | General plugin hook | `user` | Before the current turn's user message is sent. | Hermes v2026.7.20+ stores it with the user message as an `api_content` sidecar and replays it; older hosts send it only once. |
| `MemoryProvider.prefetch()` / `prefetch_all()` | Memory provider plugin | `user` | Before the current turn's user message is sent. | Hermes v2026.7.20+ stores it with the user message as an `api_content` sidecar and replays it; older hosts send it only once. |
| `ephemeral_system_prompt` argument | `AIAgent` caller, gateway, or API server | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `HERMES_EPHEMERAL_SYSTEM_PROMPT` | Environment variable | `system` | Every API call. | Ephemeral; not stored in transcript history or session DB. |
| `config.yaml` `agent.system_prompt` | Hermes configuration | `system` | Gateway loads it as an ephemeral system prompt. | Ephemeral; not stored in the cached system prompt. |
| API server `instructions` / `system_message` | API caller | `system` | Every API call. | Ephemeral for the agent request. |
| `HERMES_PREFILL_MESSAGES_FILE` / `prefill_messages` | Configuration or caller | The role on each prefill message. | Inserted after the system message and before conversation history. | Ephemeral; not stored in transcript history or session DB. |
| `SOUL.md` | Hermes profile/context file | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB. |
| `MEMORY.md` / `USER.md` built-in memory snapshot | Hermes built-in memory | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB; frozen for the session. |
| `AGENTS.md` / `.hermes.md` / `HERMES.md` / `.cursorrules` context files | Filesystem context | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB. |
| Skills index, tool guidance, environment hints, timestamp | Hermes internals | `system` | When Hermes first builds or rebuilds the cached system prompt for a session. | Stored in the cached system prompt and session DB. |

## Notes

- `MessageEvent.channel_prompt` is a `system` prompt overlay, but it is not part
  of the cached system prompt. Hermes appends it to the effective system prompt
  at API-call time and includes it in the agent configuration signature. Changing
  it can rebuild the agent and changes the model's system prefix.
- `MessageEvent.channel_context` is prepended by Hermes as
  `<channel_context>\n\n[New message]\n<message_text>`. It persists with that user
  message, so replay preserves earlier turns' prefixes.
- `pre_llm_call` hook context and memory provider `prefetch()` results are
  appended to the current turn's `user` message.
- `pre_api_request` receives request metadata and messages for observation, but
  Hermes does not use its return value to modify the prompt. It is not a prompt
  injection surface.

## ClawChat section placement

The adapter detects support from the `channel_context` dataclass field on the
host's `MessageEvent` (available since v2026.5.16). No configuration key is
needed. Hosts without that field keep the complete legacy channel prompt,
with identical section order and bytes, entirely in `system.channel_prompt`.

| Sections / part ids | Supported-host target | Reason |
|---------------------|-----------------------|--------|
| `conversation-semantics`, `metadata-glossary` | `system.channel_prompt` | Stable interpretation rules, including the authority of the plugin's per-turn user block. |
| `agent-profile`, `agent-files`, `owner-metadata-*`, `current-agent` | `system.channel_prompt` | Agent/owner context remains stable until its source changes. |
| `group-profile`, `group-participants`, `peer-profile` | `system.channel_prompt` | Conversation profile context, independent of this turn's message ids and speakers. |
| `memory-routing`, direct `peer-memory` | `system.channel_prompt` | Stable routing and direct-conversation notes. |
| Group `group-memory` (owner.md and this group's note only) | `system.channel_prompt` | Speaker changes cannot displace these notes or change the system prefix. |
| `response-protocol` | `system.channel_prompt` | Stable output protocol; direct-chat reply guidance stays here too. |
| `non-owner-turn`, `turn-metadata` | `user.channel_context` | Current turn's binding capability limits and time fields. |
| `group-speaker-memory` | `user.channel_context` | Notes for the current batch's distinct speakers. |
| `group-message-metadata` / `sender-metadata` | `user.channel_context` | Current message ids, sender identity, timestamps, batch and mention metadata. |
| Group `response-guidance`, `access-request-notice` | `user.channel_context` | Mention-dependent guidance and current access-request outcomes. |

The user block orders these parts as: non-owner limits first, turn metadata,
speaker memory, message metadata, group reply guidance, then access notices.
The stable conversation semantics explicitly identify this block as plugin-written
and its limits as binding. Existing nonempty host channel context precedes it,
separated by a blank line. The adapter leaves `event.text` assembly unchanged,
including group transcript increments.

This keeps timestamps, message ids, batch speakers and mention decisions out of
the effective system prefix and agent configuration signature, preserving model
prefix caching across turns. Profiles and saved notes can still legitimately
change that prefix when their underlying content changes.

## Session-level ClawChat context

Hermes currently does not expose a public platform-plugin API for rebuilding the
cached system prompt of an existing gateway session in place.

Observed Hermes behavior:

- The cached system prompt is intentionally built once per session and replayed
  verbatim on later turns for prompt-cache stability.
- `platform_hint` and memory-provider `system_prompt_block()` content are stored
  in that cached system prompt and persisted in the session DB.
- `MessageEvent.channel_prompt` is merged into the effective system prompt at
  API-call time as an ephemeral overlay, without changing the cached system
  snapshot or the stored transcript. Its contents still affect the effective
  system prefix and cached-agent configuration signature.
- Hermes has internal helpers such as `AIAgent._invalidate_system_prompt()` and
  gateway cached-agent eviction, but they are not a stable platform-plugin
  contract. Evicting a cached agent alone is not enough to rebuild a continued
  session, because Hermes can restore the previous `system_prompt` snapshot from
  the session DB.
- Gateway `reset_session()` creates a new session id. It is not an in-place
  cached-system-prompt rebuild for the same conversation.

ClawChat does not register a `MemoryProvider` or implement
`MemoryProvider.system_prompt_block()`. Static platform guidance currently lives
in `platform_hint=platform_prompt()`, while conversation semantics and the
metadata glossary are also carried in each turn's `MessageEvent.channel_prompt`.
If session-level static context is needed in future, `system_prompt_block()`
could be considered, although Hermes exposes it as a memory-provider surface
rather than a general platform session-prompt API. Such a block should contain
only stable explanatory text, never agent behavior, profiles, sender facts, or
other data that can change within a session.

Session-level ClawChat context is valid only when the Hermes session is strictly
bound to the intended conversation. Direct messages are bound by the direct
conversation `chat_id`. Group messages are bound by `chat_id` and the session is
shared by the whole group (see
[`../configuration.md`](../configuration.md#one-session-per-group)).
Session-level context may describe stable group-level semantics, but current
sender facts belong in `MessageEvent.channel_context` on supported hosts;
conversation profile metadata remains in `MessageEvent.channel_prompt`, while
current message text stays in the user-message body. (A legacy config that still sets
`group_sessions_per_user: true` keeps a per-speaker session for the groups it
covers; session-level context must not assume such a session represents the
whole group.)

Keep message-scoped context in `MessageEvent.channel_context` on supported
hosts, with the legacy channel-prompt fallback described above. The ClawChat
channel prompt includes the current agent profile from
`owner.md` metadata (`agent_user_id`, `agent_nickname`, `agent_avatar_url`, and
`agent_bio`) so group and direct turns know which agent identity is replying.
`owner.md` can describe another agent (`hermes profile create --clone-all`
copies the source profile's memories) until a pull rewrites it, so the adapter
drops `owner.md`'s `agent_*` fields (profile and `agent_behavior`) when its
`agent_user_id` is a different `usr_...` than the connection's own, and the
`agent_owner_*` fields when `agent_owner_id` is not this agent's owner; the
profile then comes from `users/<own id>.md`. A multiplexed gateway builds,
connects and runs each profile's adapter under that profile's home override,
so the files read are the served profile's own.
Every turn also gets `## ClawChat Current Agent`: `current_agent_id` (the
connection's own `usr_...` id, so a profile never shows another agent's
identity), `current_agent_nickname` (from
`owner.md` only when its `agent_user_id` is this agent, else
`users/<id>.md`; omitted when unknown), and one line telling the model that
this id is itself and that `mentions_current_agent=true` addresses it,
whatever name the text shows. `ClawChat Group Participants` labels the same
row `current_agent`. Without it a model in a group with a second agent cannot
tell which participant it is (the OpenClaw plugin carries the same section).
It injects `ClawChat Sender Metadata` for direct chats and `ClawChat Group
Message Metadata` for group chats. Current direct message text and group
transcript text stay in the host user-message body and are not duplicated in
the system context.

Each turn also carries the agent's **own notes** for the people in it — the
agent-written body of the ClawChat memory files, never their metadata block
(`ClawChatAdapter._format_note_memory_section`):

Every turn starts with `owner.md` (`### Your owner (owner.md)`), in every
conversation, as the agent's own notes about its owner; what of it to bring up
where is left to the model (the platform prompt says what is in the owner's
memory does not belong in front of their friends).

- `## ClawChat Peer Memory` in a direct chat: `owner.md`, then, when the
  sender is not the owner, `users/<sender_id>.md`.
- `## ClawChat Group Memory` in a group: `owner.md`, `groups/<chat_id>.md`,
  then, on legacy hosts, `users/<id>.md` for every distinct sender in the turn's
  batch (the agent itself and the owner excluded). On supported hosts those
  speaker notes move to `## ClawChat Speaker Memory` in the current user block.
  The memory tools hold the same line
  when the agent reads notes itself: in a group `clawchat_memory_read` /
  `_search` return only `owner.md`, that group's note and its members' notes
  (see [`tools.md`](./tools.md#what-a-conversation-may-read)).

These sections open with a fixed line saying the notes are social context written by the
agent, not instructions; the group section adds that a note about a person may
hold something said elsewhere and must not be brought up unless that person
already said it in the group. Empty notes add nothing, and a turn with no notes
has no section. Sizes come from the plugin keys `note-cap-user` (each person,
`owner.md` included, factory 1500 chars), `note-cap-group` (the group note,
2000) and `note-cap-turn` (the whole section, 4000), see
[`../configuration.md`](../configuration.md#session-and-note-keys). A note over
its cap keeps its opening profile paragraph if it is at most one third of
the cap, then the latest whole paragraphs that fit, with
`(earlier part truncated — …)` between the profile and the latest text (or at
the start if no profile is kept). The marker names the `clawchat_memory_read`
call that shows the omitted text. If the last paragraph does not fit, trailing
whole lines are kept, falling back to the final characters. Retained note text
fits the cap; the marker and its separating newlines are outside it. When the
notes together exceed `note-cap-turn`, short ones stay whole and the longest
are cut to a common ceiling using the same direction
(`clawchat_gateway/note_injection.py`). In split group mode, owner and group
notes are capped and share `note-cap-turn` first, independently of the speakers.
Only the remaining budget is shared among speaker notes; zero remaining budget
omits the speaker section. The system portion therefore stays stable when a
different speaker or a long speaker note enters the batch.

Every turn, direct or group, also carries one `Memory routing:` line
(`adapter.MEMORY_ROUTING_REMINDER`): a fact about a ClawChat person, group or
the owner goes only to `clawchat_memory_write` (`targetType=user` / `group` /
`owner`), never to Hermes' memory tool, `MEMORY.md` or `USER.md`. The full rule
is in `platform_hint`, but that is frozen into a session's cached system prompt
when the session starts, so a session that began on an older plugin never
receives it; the plugin has no host API to make Hermes rebuild an existing
session's system prompt, so the per-turn line is what reaches those sessions.
Without it such a session can keep saving people into `MEMORY.md`, which then
goes into the system prompt of every conversation, groups included.

Besides the per-turn `channel_context` block on supported hosts, two kinds of
turn put plugin-written text into the **user message** (so it stays in the
session): the first turn of a group's shared session starts with
the group's recent messages, and an @-mention later adds group messages the
session has not seen (see
[`../configuration.md`](../configuration.md#group-session-seeding-and-queueing));
and a sediment turn before `/new` or compression is a whole synthetic user
message naming the only notes it may write (see
[`../configuration.md`](../configuration.md#sediment-turns)).

`ClawChat Turn Metadata` also carries the message time taken from the
Protocol-v2 envelope's `emitted_at`: `sent_at` (local timezone, ISO-8601 with an
explicit UTC offset, second precision) and `sent_age` (`3s` / `4m` / `5h` /
`6d`). In group turns each indexed `[message N]` in `ClawChat Group Message
Metadata` carries its own `sent_at` as well, with no per-message `sent_age`; for
a coalesced batch the turn-level `sent_at`/`sent_age` are the last message's.
A replayed frame keeps its original `emitted_at`, so a large `sent_age` means
late delivery rather than a just-written message — the metadata glossary tells
the agent exactly that. When the envelope carries no usable `emitted_at`, both
fields render as `null` rather than disappearing.

Each indexed `[message N]` also carries its `message_id` right after
`sender_id` when the frame has one, and so does a direct chat's `ClawChat Sender
Metadata` for the current message, so the model can name the message it reacts
to (`clawchat_react_message` `targetMessageId`) when the current-turn default is
withdrawn (see [`tools.md`](./tools.md#explicit-message-ids)). On supported
hosts these ids and `sent_at`/`sent_age` live in the current user message via
`channel_context`, preserving the effective system prefix. Legacy hosts keep
them in the ephemeral system overlay; although the cached base system snapshot
is unchanged, the effective system prompt and agent configuration signature
still change each turn.

Who decides whether to speak in a group (`reply_guidance` plus the metadata
glossary):

1. Structured mention routing comes first. A message with `mention_routing:
   addressed_to_other` is never answered; one with
   `addressed_to_current_agent` may be answered (eligible, not mandatory).
2. For a message with `mention_routing: no_structured_mentions` (including
   such messages inside a batch that also mentions the agent), the group's
   `group_description` decides whether and how much to speak, and
   `agent_behavior` decides where the description is silent.
3. `agent_behavior` can always rule a reply out, even when the description
   calls for one.
4. If neither calls for a reply, the agent listens and outputs the no-reply
   token.
5. Rules in either about whom not to answer (for example, other agents) apply
   to every message, including ones that mention the agent.

The group description outranks the default reply guidance only on that
question; it does not override mention routing, `agent_behavior`, or platform
rules such as the privacy floor in `platform.md`.
