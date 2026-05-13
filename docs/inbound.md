# Inbound — `clawchat_gateway/inbound.py`

Parses a ClawChat `message.send` frame into the `InboundMessage` shape used by the adapter.

## Data class

### `InboundMessage`

```python
@dataclass(frozen=True)
class InboundMessage:
    chat_id: str
    chat_type: str
    sender_id: str
    sender_name: str
    text: str
    raw_message: dict[str, Any]
    reply_preview: dict[str, Any] | None = None
    media_urls: list[str] = field(default_factory=list)
    media_types: list[str] = field(default_factory=list)
```

- `chat_id`, `chat_type` — pulled from the envelope; `chat_type` defaults to `"direct"`.
- `sender_id`, `sender_name` — from the envelope `sender` object.
- `text` — newline-joined concatenation of all text fragments, with media fragments rendered inline as Markdown.
- `raw_message` — the full original envelope (stored for downstream consumers).
- `reply_preview` — the `context.reply` dict if present.
- `media_urls` / `media_types` — parallel lists; `media_types[i]` is the declared fragment `kind` (`image`, `file`, `audio`, `video`).

## Private helpers

| Function | Signature | Purpose |
|---|---|---|
| `_as_dict` | `(value) -> dict \| None` | Return `value` if it's a dict, else `None`. |
| `_coerce_fragments` | `(message: dict) -> list` | Tries `message.fragments` → `message.body` (list/str/dict). For dict bodies, looks under `fragments`/`parts`/`items`, then falls back to `text`/`content`/`value`. For str bodies, wraps in `[{"kind": "text", "text": body}]`. Returns `[]` if nothing matches. |
| `_fragment_kind` | `(fragment: dict) -> str \| None` | Try `kind`, then `type`. Must be str. |
| `_fragment_text` | `(fragment: dict) -> str \| None` | Try `text`, `content`, `value`. Must be str. |

## `parse_inbound_message`

```python
parse_inbound_message(envelope: dict, config: ClawChatConfig) -> InboundMessage | None
```

Returns `None` (so the adapter drops the frame) when:

- `payload` is not a dict.
- `payload.message` is not a dict.
- `message.context` is not a dict.
- `chat_type == "group"` **and** `config.group_mode == "mention"` **and** the bot's `config.user_id` is not in `context.mentions`. The default `group_mode` is `"all"`, so mention filtering is opt-in.
- `envelope.sender` is not a dict.

Otherwise:

1. Coerce fragments via `_coerce_fragments(message)`.
2. Iterate fragments:
   - `kind in (None, "text")` with text → append to `text_parts`.
   - `kind in {"image", "file", "audio", "video"}` with `url: str` → append to `media_urls` / `media_types` and inline a Markdown label in the text (`![...]` for images, `[...]` for others).
3. Join non-empty text parts with `\n`.
4. Extract `sender.id` / `sender.nick_name`.
5. Pass `context.reply` through as `reply_preview`.

The resulting `InboundMessage` is the adapter's canonical representation; opt-in group-mention filtering and media extraction happen here so the adapter's `_handle_inbound` stays simple.
