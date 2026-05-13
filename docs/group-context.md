# Group Context — `clawchat_gateway/group_context.py`

Formats the default ClawChat group covenant for Hermes' per-event `MessageEvent.channel_prompt` field.

`channel_prompt` is a Hermes gateway adapter field, not part of the ClawChat WebSocket protocol and not a plugin hook. Hermes applies it as an ephemeral system prompt at API-call time and does not persist it into transcript history.

## Constants

| Name | Purpose |
|---|---|
| `DEFAULT_GROUP_COVENANT` | Default product copy applied to group conversations. Tests may monkeypatch this with fixture text. |

## Functions

| Function | Signature | Purpose |
|---|---|---|
| `format_group_covenant_prompt` | `(covenant: str) -> str \| None` | Strip the covenant text and wrap non-empty content under `ClawChat group covenant:`. Returns `None` for blank input. |
| `build_group_channel_prompt` | `() -> str \| None` | Format `DEFAULT_GROUP_COVENANT` for adapter use. |

The adapter only calls this helper for inbound group messages. Direct messages do not receive group covenant text.
