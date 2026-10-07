# Output visibility

Hermes ClawChat supports three platform-level output visibility presets:

```text
/clawchat-output minimal
/clawchat-output normal
/clawchat-output full
```

The command updates the ClawChat platform settings in `$HERMES_HOME/config.yaml`.
It is not a per-chat or per-session preference. `streaming` stays `false` in all
three presets.

> **Quote `tool_progress: "off"`.** `tool_progress` is a *string* enum
> (`off` / `new` / `all` / `verbose`), and the plugin writes and compares the
> string `"off"` (`clawchat_gateway/output_visibility.py`, `DISPLAY_PRESETS`).
> A bare `off` is parsed as the boolean `false` by YAML 1.1 loaders, which is
> not the same value — always quote it when hand-editing `config.yaml`. The
> same applies to `display.background_process_notifications`.

`output_visibility` is the semantic preset. `runtime_status_messages` is the
adapter-facing boolean used by the existing Hermes runtime/status suppression
path, and `/clawchat-output` keeps it aligned with the selected preset.

## `minimal`

Only final assistant-visible output is sent to ClawChat. Hermes runtime,
provider, fallback, retry, tool progress, reasoning, and interim assistant
messages are suppressed.

```yaml
platforms:
  clawchat:
    extra:
      output_visibility: minimal
      runtime_status_messages: false

display:
  platforms:
    clawchat:
      tool_progress: "off"
      show_reasoning: false
      streaming: false
      interim_assistant_messages: false
      long_running_notifications: false
      busy_ack_detail: false
      cleanup_progress: false

agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
```

## `normal`

Final assistant-visible output and natural interim assistant messages are sent
to ClawChat. Hermes runtime, provider, fallback, retry, tool progress, and
reasoning messages are suppressed.

```yaml
platforms:
  clawchat:
    extra:
      output_visibility: normal
      runtime_status_messages: false

display:
  platforms:
    clawchat:
      tool_progress: "off"
      show_reasoning: false
      streaming: false
      interim_assistant_messages: true
      long_running_notifications: false
      busy_ack_detail: false
      cleanup_progress: false

agent:
  gateway_notify_interval: 0
  gateway_timeout_warning: 0
```

## `full`

Hermes forwards the runtime categories the ClawChat adapter can deliver,
including reasoning, tool progress, command output, interim assistant messages,
and runtime/status notices.

```yaml
platforms:
  clawchat:
    extra:
      output_visibility: full
      runtime_status_messages: true

display:
  platforms:
    clawchat:
      tool_progress: verbose
      show_reasoning: true
      streaming: false
      interim_assistant_messages: true
      long_running_notifications: true
      busy_ack_detail: true
      cleanup_progress: false

agent:
  gateway_notify_interval: 180
  gateway_timeout_warning: 900
```

## Process messages are marked `thinking`

Everything a preset lets through that is not the agent's reply — tool progress,
the `💭 Reasoning` block (sent as its own message just before the reply it
belongs to), status updates, the long-running heartbeat, operational notices and
the runtime notices listed below — goes out with `payload.message_mode:
"thinking"` (protocol §7.5). A client may fold these; another agent in the chat
does not read them as input. The reply and interim assistant messages stay
`"normal"`. Hermes sends all of these through the adapter's `send()` without a
marker, so the adapter recognises them by the Hermes sender they come from
(`_HOST_PROCESS_SENDERS` in `clawchat_gateway/adapter.py`), by
`send_or_update_status`, and by the runtime-notice table.

Tool previews are rendered as inline code (the adapter's `format_tool_preview`,
called by Hermes builds that have the hook) and terminal commands as code
blocks (`supports_code_blocks`), so a `*` in a command is shown, not read as
emphasis. In the `full` preset (`tool_progress: verbose`) a non-terminal tool's
raw argument JSON is still formatted by Hermes itself, with no adapter hook.

## Runtime/status suppression

The adapter derives runtime-status delivery from the selected preset —
`clawchat_gateway.output_visibility.runtime_status_messages_for_visibility`:

```text
runtime_status_messages_for_visibility(mode) == (mode == "full")
runtime_status_messages = runtime_status_messages_for_visibility(output_visibility)
```

When `runtime_status_messages` is `false`, the adapter suppresses Hermes
lifecycle/provider/fallback/retry notices that would otherwise be sent to the
ClawChat client, including empty-response and fallback-provider status text,
`ℹ️ Context compression deferred …`, the background `💾 Self-improvement
review: …` summary, and `⚠ Stream stalled mid tool-call (…); the action was not
executed. …`, and the `⚠️ Gateway shutting down — …` / `⚠️ Gateway restarting —
…` notice Hermes sends to every chat with a running agent when the gateway
stops or restarts. Hermes appends the stream-stalled warning to the partial reply text, so it
is also cut out of a message that carries a real reply around it. Prefixes are
matched with and without the U+FE0F emoji variation selector (Hermes uses both
`⚠` and `⚠️`). The list lives in `_HERMES_RUNTIME_STATUS_PREFIXES` /
`_HERMES_RUNTIME_STATUS_PATTERNS` in `clawchat_gateway/adapter.py`; Hermes
sends these through the same `send()` as replies, without a "this is status"
marker, so a new Hermes notice needs a new entry there.
Required approval/action controls are still delivered in every preset.

Independently of the preset, Hermes CLI session-status lines that lead a
turn (`◐ Session automatically reset …` with its paragraph, and the
`◆ Model:` / `◆ Provider:` / `◆ Context:` lines) are cut out of every
outbound text before delivery — they are advice for a terminal user
(`/resume`, `config.yaml`) and are never the agent's words. The match is
narrow (exact line prefixes); a message that consisted only of those lines is
not sent at all. See `clawchat_gateway/hermes_session_status.py`.
