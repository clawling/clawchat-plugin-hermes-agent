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
| `pre_llm_call` hook returning `context` | General plugin hook | `user` | Before the current turn's user message is sent. | Ephemeral; not stored in transcript history or session DB. |
| `MemoryProvider.prefetch()` / `prefetch_all()` | Memory provider plugin | `user` | Before the current turn's user message is sent. | Ephemeral; not stored in transcript history or session DB. |
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
  at API-call time.
- `pre_llm_call` hook context and memory provider `prefetch()` results are
  appended to the current turn's `user` message.
- `pre_api_request` receives request metadata and messages for observation, but
  Hermes does not use its return value to modify the prompt. It is not a prompt
  injection surface.

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
  prompt or the stored transcript.
- Hermes has internal helpers such as `AIAgent._invalidate_system_prompt()` and
  gateway cached-agent eviction, but they are not a stable platform-plugin
  contract. Evicting a cached agent alone is not enough to rebuild a continued
  session, because Hermes can restore the previous `system_prompt` snapshot from
  the session DB.
- Gateway `reset_session()` creates a new session id. It is not an in-place
  cached-system-prompt rebuild for the same conversation.

For ClawChat, use `MemoryProvider.system_prompt_block()` as the current
session-level home for static ClawChat context semantics that should not be
repeated on every message, even though this is a responsibility compromise:
Hermes currently exposes this as a memory-provider surface, not as a general
platform session-prompt surface. Keep this block limited to stable explanatory
text, such as the merged ClawChat conversation semantics and metadata glossary.
Do not include `agent_behavior`, agent-owner metadata, peer profile metadata,
group profile metadata, current `[message]` blocks, or other ClawChat data that
can change within a session.

Session-level ClawChat context is valid only when the Hermes session is strictly
bound to the intended conversation. Direct messages are bound by the direct
conversation `chat_id`. Group messages are bound by `chat_id`, with the official
Hermes-compatible `group_sessions_per_user` setting determining whether the
session is per participant or shared by the whole group:

- `group_sessions_per_user=true`: group sessions include the participant id.
  Session-level context must not assume it represents the whole group.
- `group_sessions_per_user=false`: group sessions are shared by the group
  conversation. Session-level context may describe stable group-level semantics,
  but current sender facts and mutable group/user metadata still belong in
  `MessageEvent.channel_prompt` or the current `[message]` block.

Keep message-scoped or metadata-scoped context in `MessageEvent.channel_prompt`
unless Hermes adds an explicit session prompt invalidation API for platform
plugins.
