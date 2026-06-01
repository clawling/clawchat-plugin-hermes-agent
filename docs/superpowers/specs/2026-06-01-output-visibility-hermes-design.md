# Hermes ClawChat Output Visibility Design

## Goal

Implement the shared ClawChat output visibility contract from
`../../../../docs/output-visibility.md` in the Hermes ClawChat plugin.

The Hermes plugin must support:

```text
minimal | normal | full
```

Default mode is `normal`.

## Scope

This spec covers only `clawchat-plugin-hermes-agent`.

The implementation should:

- Expose the same current-conversation `/clawchat-output
  minimal|normal|full` behavior as OpenClaw.
- Map the three shared modes to Hermes' existing display configuration and
  gateway adapter behavior.
- Prefer Hermes official display controls over adapter-side filtering.
- Keep existing advanced Hermes display settings available.

It should not:

- Remove Hermes' fine-grained display settings.
- Add content redaction, sanitization, or rewriting.
- Treat group chats as secretly stricter than direct chats.
- Change ClawChat Protocol v2 wire shapes.

## User-Facing Semantics

| Mode | ClawChat output |
|---|---|
| `minimal` | Only the final assistant-visible response from the agent runtime. |
| `normal` | Assistant-visible interim messages plus the final assistant-visible response. |
| `full` | All Hermes runtime output categories that the adapter can forward, including tool progress, tool results, command output, runtime/task progress, reasoning/thinking, interim assistant messages, and final response. |

The command override is scoped to the current direct chat or group
conversation:

```text
/clawchat-output minimal
/clawchat-output normal
/clawchat-output full
```

There is no reset command. `/clawchat-output normal` returns a conversation to
normal behavior.

Required approval/action controls are not optional visibility output. Hermes
must deliver them in every visibility mode because they can affect whether the
agent can continue. In direct chats, deliver them to the current conversation.
In group chats, route them to the agent owner's direct ClawChat conversation
instead of the group. Fine-grained display settings may affect presentation,
but they must not make required controls disappear.

## Hermes Display Mapping

Confirmed Hermes display controls:

- `display.tool_progress`
- `display.platforms.clawchat.tool_progress`
- `display.show_reasoning`
- `display.platforms.clawchat.show_reasoning`
- `display.streaming`
- `display.platforms.clawchat.streaming`
- `display.interim_assistant_messages`
- `display.platforms.clawchat.interim_assistant_messages`
- `display.platforms.clawchat.long_running_notifications`
- `display.platforms.clawchat.busy_ack_detail`
- `display.platforms.clawchat.cleanup_progress`

Policy mapping:

| Mode | Display preset |
|---|---|
| `minimal` | `tool_progress: off`; `show_reasoning: false`; `streaming: false`; `interim_assistant_messages: false`; long-running/progress notices disabled for ClawChat. |
| `normal` | `tool_progress: off`; `show_reasoning: false`; `interim_assistant_messages: true`; progressive token streaming remains disabled unless separately enabled by a supported gateway streaming setting. |
| `full` | `tool_progress: verbose`; `show_reasoning: true`; `interim_assistant_messages: true`; runtime/tool/progress/command output enabled for ClawChat. |

This is a ClawChat visibility preset, not a replacement for every Hermes
display knob. Hermes operators may still use finer settings as advanced
runtime-specific controls.

The shared ClawChat preset config name is:

```yaml
outputVisibility: normal
```

Hermes may place this under its ClawChat platform display/config section, but
the user-facing field name should match OpenClaw exactly.

## Effective Visibility Resolution

Resolve visibility in this order:

1. Current conversation command override.
2. Group-specific config override.
3. ClawChat platform display/config preset.
4. Default `normal`.

Conversation override storage should use Hermes plugin persistence rather than
rewriting global `$HERMES_HOME/config.yaml` on every command. The command is a
current-conversation preference, not a global display change.

## Adapter Behavior

The adapter should prefer Hermes display controls and host-provided display
state. Adapter-side filtering is acceptable only as a defensive guard when
Hermes still hands the adapter an output category outside the resolved
conversation visibility.

Expected behavior:

- `minimal`: send only final assistant-visible output.
- `normal`: send natural interim assistant messages and final assistant-visible
  output; suppress reasoning/thinking, tool progress, tool result, command
  output, and runtime progress notices.
- `full`: forward all output categories Hermes provides to the ClawChat
  adapter.
- Required approval/action controls: deliver in all modes; route group controls
  to the owner direct chat.

The adapter must not redact or rewrite content. Secret filtering and tool
output safety remain Hermes runtime/tool responsibilities.

## Source Entry Points

Expected files:

- `clawchat_gateway/config.py`: config model and resolved display policy.
- `clawchat_gateway/adapter.py`: output routing and defensive guards.
- `clawchat_gateway/storage.py`: current-conversation override persistence if
  needed.
- `__init__.py`: command registration if slash commands are registered there.
- `clawchat_gateway/activate.py`: activation defaults if the default ClawChat
  display preset changes from quiet to `normal`.
- `docs/configuration.md`: user-facing docs after implementation.

## Tests

Add focused tests before or with implementation:

- Default resolved visibility is `normal`.
- Command override affects only the current chat.
- `minimal` sends only final assistant-visible output.
- `normal` sends interim assistant messages and final output, but no
  reasoning, tool progress, tool result, command output, or runtime progress
  notices.
- `full` forwards reasoning, tool progress, tool output, command output,
  interim assistant messages, and final output categories that Hermes provides.
- Group chats follow resolved visibility rather than a hidden stricter policy.
- Required approval/action controls are delivered in all modes, with group
  controls routed to the owner direct chat.
- Existing fine-grained display settings still work as advanced controls.

Minimum verification:

```text
uv run pytest tests/test_adapter.py tests/test_config.py
```

Broaden to `uv run pytest` if command registration, activation, or gateway
startup behavior changes.

## Risks

- Current activation writes quiet ClawChat defaults, including
  `interim_assistant_messages: false`; default `normal` may require changing
  that default once code support lands.
- Hermes has both global and platform display settings. The implementation must
  avoid turning a current-conversation command into an accidental global config
  mutation.
- Existing adapter tests may assume group-output restrictions from older plans.
  Those assumptions must be revisited because the shared contract now says
  group chats follow resolved visibility.
