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

- Expose `/clawchat-output minimal|normal|full` for the ClawChat platform.
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

The command updates the ClawChat platform visibility preset:

```text
/clawchat-output minimal
/clawchat-output normal
/clawchat-output full
```

There is no reset command. `/clawchat-output normal` returns ClawChat to normal
platform behavior.

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
| `minimal` | `tool_progress: off`; `show_reasoning: false`; `streaming: false`; `interim_assistant_messages: false`; long-running/progress/runtime notices disabled for ClawChat. |
| `normal` | `tool_progress: off`; `show_reasoning: false`; `streaming: false`; `interim_assistant_messages: true`; long-running/progress/runtime notices disabled for ClawChat. |
| `full` | `tool_progress: verbose`; `show_reasoning: true`; `streaming: false`; `interim_assistant_messages: true`; runtime/tool/progress/command output enabled for ClawChat. |

This is a ClawChat visibility preset, not a replacement for every Hermes
display knob. Hermes operators may still use finer settings as advanced
runtime-specific controls.

The shared ClawChat preset config name is:

```yaml
platforms:
  clawchat:
    extra:
      output_visibility: normal
```

`output_visibility` is the single semantic source for the ClawChat preset in
Hermes. `runtime_status_messages` remains the Hermes adapter/runtime-status
delivery switch, but it is controlled by `/clawchat-output` instead of being a
separate user-facing visibility mode.

## Effective Visibility Resolution

Resolve visibility in this order:

1. `platforms.clawchat.extra.output_visibility`.
2. Default `normal`.

Visibility is user-visible display configuration. It should live in Hermes
configuration or a host-supported conversation preference surface, not in the
ClawChat message, tool-call, or audit database.

Implementation rules:

- `/clawchat-output` updates the ClawChat platform preset in
  `$HERMES_HOME/config.yaml`.
- The command writes `platforms.clawchat.extra.output_visibility` and the
  derived Hermes settings under `display.platforms.clawchat.*` and `agent.*`.
- The command also writes the derived
  `platforms.clawchat.extra.runtime_status_messages` value:
  `false` for `minimal` and `normal`, `true` for `full`.
- Do not implement a per-chat or per-session override unless Hermes exposes a
  supported visible conversation-preference API.
- Do not rewrite unrelated global `$HERMES_HOME/config.yaml` display settings
  on every command.
- Do not store visibility overrides in message history, tool-call records, or
  hidden adapter audit state.

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

Recent Hermes runtime/status guards, including the hardcoded
`_HERMES_RUNTIME_STATUS_PREFIXES` send/final-response suppression, are controlled
by one derived policy variable:

```text
allow_runtime_status_output = output_visibility == "full"
```

`minimal` and `normal` keep those Hermes lifecycle/provider/fallback/retry
messages out of ClawChat. `full` allows them through. Existing
`runtime_status_messages` config is the adapter-facing boolean controlled by the
command:

```text
runtime_status_messages = allow_runtime_status_output
```

If `output_visibility` is absent, existing `runtime_status_messages` values may
be used as a legacy fallback, but the command path keeps both fields aligned.

The adapter must not redact or rewrite content. Secret filtering and tool
output safety remain Hermes runtime/tool responsibilities.

## Source Entry Points

Expected files:

- `clawchat_gateway/config.py`: config model and resolved display policy.
- `clawchat_gateway/adapter.py`: output routing and defensive guards.
- Hermes config writer for the ClawChat platform display preset.
- `__init__.py`: command registration if slash commands are registered there.
- `clawchat_gateway/activate.py`: activation defaults if the default ClawChat
  display preset changes from quiet to `normal`.
- `docs/configuration.md`: user-facing docs after implementation.

## Tests

Add focused tests before or with implementation:

- Default resolved visibility is `normal`.
- Command override updates the ClawChat platform preset.
- `minimal` sends only final assistant-visible output.
- `normal` sends interim assistant messages and final output, but no
  reasoning, tool progress, tool result, command output, or runtime progress
  notices.
- `full` forwards reasoning, tool progress, tool output, command output,
  interim assistant messages, Hermes runtime/status notices, and final output
  categories that Hermes provides.
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

- Activation defaults should stay aligned with the `normal` preset, including
  `interim_assistant_messages: true`.
- Hermes has both global and platform display settings. The implementation must
  update only the known ClawChat visibility keys and agent heartbeat/warning
  keys required by the selected preset.
- Existing adapter tests may assume group-output restrictions from older plans.
  Those assumptions must be revisited because the shared contract now says
  group chats follow resolved visibility.
- The root shared contract and OpenClaw design still describe current-chat
  overrides. Hermes intentionally remains platform-global until a supported
  Hermes conversation-preference API exists.
