# Output visibility

Hermes ClawChat supports three platform-level output visibility presets:

```text
/clawchat-output minimal
/clawchat-output normal
/clawchat-output full
```

The command updates the ClawChat platform settings in `$HERMES_HOME/config.yaml`.
It is not a per-chat or per-session preference. `streaming` stays `false` in all
three presets, so choosing a preset also turns Hermes reply streaming off again.
ClawChat reply streaming is a separate, experimental opt-in
(`extra.stream_replies`, default `false`) that needs host `streaming: true` as
well; see
[`./configuration.md`](./configuration.md#reply-streaming-experimental).

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
marker, so a new Hermes notice needs a new entry there. (Hermes' own markers do not
help on ClawChat: `non_conversational` is set for Discord only, `_interim_send`
marks every mid-turn send including the agent's interim text, and `notify` marks
only the turn-final reply.)

Until a new notice is listed, a fallback limits the damage: while a turn runs, a
send that opens with a status glyph (`⚠ ❌ ℹ 🔄 ↻ ⏳ ⏱ 🗜 📦 🔌 💾 ♻ ⛔ 🚫 🛑`) and
is neither the turn-final reply nor an explicit tool send is held. When the turn
ends in a no-reply the held notices are dropped; otherwise they are sent at the
end of the turn. The agent's own interim text (no glyph) goes out at once, and
in `full` nothing is held.
Required approval/action controls are still delivered in every preset.

Independently of the preset, Hermes CLI session-status lines that lead a
turn (`◐ Session automatically reset …` with its paragraph, and the
`◆ Model:` / `◆ Provider:` / `◆ Context:` lines) are cut out of every
outbound text before delivery — they are advice for a terminal user
(`/resume`, `config.yaml`) and are never the agent's words. The match is
narrow (exact line prefixes); a message that consisted only of those lines is
not sent at all. See `clawchat_gateway/hermes_session_status.py`.

## Host redelivery after a restart

Hermes keeps a delivery ledger of final text replies (`gateway/delivery_ledger.py`)
and marks a row delivered only when `send()` returns success. A send that did not
— the ClawChat ack never came back (a stalled read loop, a reconnect), or the
gateway was stopped mid-send — is sent again on the next gateway boot (or after a
reconnect / rate-limit wait), up to 24 hours later, prefixed with
`♻️ Recovered reply — … may be a duplicate:` and a blank line. When only the ack
had been lost, that showed the old reply in the chat a second time.

The adapter recognises that prefix (`_strip_host_recovered_marker`), drops it, and
looks up the earlier attempt in its own `clawchat_messages` ledger by chat and
text. Found, the reply is resent under the earlier attempt's `message_id`: the ClawChat server
upserts the inbox row per (recipient, `message_id`) and clients dedupe by
`message_id`, so a reply that already arrived is not shown again and one that
never arrived is delivered. Not found, it goes out as a new message, still without
the prefix. A ledger row that was never attempted is resent by Hermes without a
prefix; it never reached the chat, so it is delivered like any reply.
