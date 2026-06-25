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
      tool_progress: off
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
      tool_progress: off
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

## Runtime/status suppression

The adapter derives runtime-status delivery from the selected preset:

```text
allow_runtime_status_output = output_visibility == "full"
runtime_status_messages = allow_runtime_status_output
```

When `runtime_status_messages` is `false`, the adapter suppresses Hermes
lifecycle/provider/fallback/retry notices that would otherwise be sent to the
ClawChat client, including empty-response and fallback-provider status text.
Required approval/action controls are still delivered in every preset.
