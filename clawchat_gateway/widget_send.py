"""Widget validation and snapshot coalescing, owned by one event loop."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import time
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

WIDGET_SENT = "Sent. It shows on their home screen once their phone has it."
WIDGET_OWNER_ONLY_MESSAGE = "Owner-only: only your owner can update their widgets."
WIDGET_DESCRIPTION = (
    "Update a widget on your partner's phone home screen (ClawChat widget/v1). "
    "Only after they asked for one. Each call is the whole widget, not a change, "
    "and goes to your private chat with them. Call it once with no widget to read "
    "the format before your first one."
)
NOT_CONNECTED = "ClawChat is not connected; try again later"

WIDGET_V1_GUIDE = """## Home-screen widgets (widget/v1)

A widget is a small card on your partner's phone home screen. You write it with `clawchat_widget_send`; their taps come back to you as ordinary messages.

- Send one only after your partner asked for a widget — a recipe card, or their own words.
- Every message is the full snapshot, never a diff. The latest one per `id` wins.
- At most one per `id` a minute; more calls in that minute collapse into the last one. Do not refresh on a timer: data that changes by itself belongs in `source`.
- It only ever goes to your private chat with your partner.

### Document

```json
{"v": 1, "id": "todo", "title": "To-do", "badge": 3,
 "action": {"do": "capture"},
 "medium": {"type": "list", "items": [
   {"type": "row", "children": [
     {"type": "check", "id": "t1", "label": "Pay rent", "checked": false},
     {"type": "text", "text": "Pay rent", "grow": true},
     {"type": "text", "text": "today", "style": "caption", "tone": "accent"}]}]}}
```

- `v`: 1. `id`: `^[a-z0-9][a-z0-9_-]{0,31}$`, the same across updates. `title`: up to 40 characters.
- `badge` (optional): an integer 0–9999, or up to 12 characters.
- `action` (optional, top right): `capture`, `chat` or `liveware`.
- `small` / `medium` / `large`: one block tree per size, at least one. A missing size borrows `medium`, then `large`, then `small`. A medium list shows about 3 rows.
- At most 16 KiB of UTF-8. Unknown fields and block types are skipped.
- Write the text in your partner's language. Colours, fonts and spacing are ours; you choose the structure.

### Blocks

| `type` | fields |
|---|---|
| `column` `row` `stack` | `children` |
| `list` | `items` — up to 20; the rest show as "N more" |
| `spacer` | — |
| `text` | `text` (up to 200), `style`: `title` `body` `caption`, `tone`: `normal` `accent` `secondary`, `grow`, `tap` |
| `number` | `value`: a string up to 16 or an integer (format decimals yourself), `unit` (up to 8), `style`, `tone` |
| `icon` | `name`: `plus` `check` `star` `heart` `fire` `clock` `calendar` `bell` `chart` `money` `sun` `cloud` `rain` `arrow_up` `arrow_down` `dot`; `tone` |
| `image` | `src`: a ClawChat media URL |
| `progress` `ring` | `value`: 0–1 |
| `check` | `id`, `label` (up to 80), `checked` |
| `toggle` | `id`, `label`, `on` |
| `button` | `id`, `label`, `tap` |

- `check` / `toggle` / `button` ids follow the `id` pattern and are unique within a size. The `label` is how their tap is reported to you.
- At most 6 levels deep and 120 blocks per size.
- A row with a checked `check` is drawn struck through.

### Actions: `{"do": …}`

- `send`: the default for `check` / `toggle` / `button`; the tap reaches you as a message.
- `capture`: opens a quick-entry box; what they type reaches you as a message.
- `chat`: opens your chat. Text without `tap` does this.
- `liveware`: `{"do": "liveware", "url": "https://<app>.apps.clawling.io/…"}` opens that page.

### Data that changes by itself

```json
"source": {"url": "https://…", "every": "30m", "fields": {"price": "data.price"}}
```

The phone fetches it itself: HTTPS GET, no credentials, JSON up to 64 KiB, every 15m to 24h. Use the field as `{price}` in `text` or `number`. Your partner sees the domain.

### What comes back

Each tap or entry arrives as an ordinary message with one line for you:

```
[widget todo] checked "Pay rent" (t1)
[widget todo] unchecked "Pay rent" (t1)
[widget todo] turned on "Focus" (f1)
[widget todo] turned off "Focus" (f1)
[widget todo] pressed "Refresh" (b1)
[widget todo] capture: buy milk
[widget todo] resend
```

They can arrive late and several at once: apply each in order, they are not duplicates. Then send one new full snapshot. `resend` means a device lost the widget: send the latest snapshot again.
"""


def widget_owner_only():
    return {"status": "owner_turn_only", "instruction": WIDGET_OWNER_ONLY_MESSAGE}


def check_widget_for_send(doc: str) -> dict:
    def refuse(code):
        why = {
            "not_json": "widget is not a JSON object",
            "too_large": "widget is over 16384 bytes of UTF-8",
            "bad_version": '"v" must be the integer 1',
            "bad_id": '"id" must match ^[a-z0-9][a-z0-9_-]{0,31}$ and stay the same across updates',
        }[code]
        return {"code": code, "error": f"{why} ({code}). Check it against the widget/v1 format before you retry."}

    if len(doc.encode("utf-8")) > 16384:
        return refuse("too_large")

    def reject_constant(value):
        raise ValueError(value)

    try:
        value = json.loads(doc, parse_constant=reject_constant)
    except (ValueError, RecursionError):
        return refuse("not_json")
    if not isinstance(value, dict):
        return refuse("not_json")
    if value.get("v") is None:
        return refuse("bad_version")
    if not isinstance(value.get("id"), str) or not value["id"]:
        return refuse("bad_id")
    return {"id": value["id"]}


def widget_queued(widget_id: str, due: float) -> str:
    at = datetime.fromtimestamp(due, timezone.utc).strftime("%H:%M:%S")
    return (f'Queued: "{widget_id}" was updated less than a minute ago. This snapshot '
            f'goes out at {at} UTC unless you send a newer one first.')


async def run_widget_tool(widget, send):
    doc = widget.strip() if isinstance(widget, str) else ""
    if not doc:
        return {"message": WIDGET_V1_GUIDE}
    checked = check_widget_for_send(doc)
    if "error" in checked:
        return {"error": "validation", "code": checked["code"], "message": checked["error"]}
    result = await send(doc, checked["id"])
    return dict(result, message=WIDGET_SENT if result.get("sent") else widget_queued(checked["id"], result["due"]))


@dataclass
class _Submission:
    account_id: str
    widget_id: str
    doc: str
    send: Callable[[], Awaitable[None]]


@dataclass
class _State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_start: float | None = None
    sending: int = 0
    sequence: int = 0
    pending: _Submission | None = None
    timer: object | None = None


class WidgetSendThrottle:
    """Admission and completion share a lock; an ACK never holds it."""

    def __init__(self, *, now=time.time, call_later=None):
        self.now = now
        self.call_later = call_later
        self.keys: dict[tuple[str, str], _State] = {}
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _cancel_timer(self, state):
        if state.timer is not None:
            state.timer.cancel()
        state.timer = None

    def _arm(self, key, state):
        if state.pending is None or state.timer is not None or self.keys.get(key) is not state:
            return

        def fire():
            self._spawn(self._flush(key, state, handle))

        due = (state.last_start if state.last_start is not None else self.now()) + 60
        schedule = self.call_later or asyncio.get_running_loop().call_later
        handle = schedule(max(0, due - self.now()), fire)
        state.timer = handle

    async def _flush(self, key, state, handle):
        async with state.lock:
            if state.timer is not handle:
                return
            state.timer = None
            if self.keys.get(key) is not state or state.pending is None:
                return
            item, state.pending = state.pending, None
            self._start(key, state, item, timed=True)

    def _start(self, key, state, item, *, timed):
        previous = state.last_start
        state.last_start = self.now()
        state.sequence += 1
        sequence = state.sequence
        state.sending += 1
        return self._spawn(self._deliver(key, state, item, timed, previous, sequence))

    async def _deliver(self, key, state, item, timed, previous, sequence):
        try:
            if self.keys.get(key) is not state:
                return
            await item.send()
        except Exception as exc:
            if timed:
                # Errors may echo the document, so log only the exception type.
                logger.warning("widget send failed id=%r bytes=%d error=%s", item.widget_id,
                               len(item.doc.encode("utf-8")), type(exc).__name__)
            else:
                async with state.lock:
                    if state.sequence == sequence:
                        state.last_start = previous
                raise
        finally:
            async with state.lock:
                state.sending -= 1
                self._arm(key, state)

    async def submit(self, account_id, widget_id, doc, send):
        key = (account_id, widget_id)
        state = self.keys.setdefault(key, _State())
        item = _Submission(account_id, widget_id, doc, send)
        async with state.lock:
            if self.keys.get(key) is not state:
                raise RuntimeError("Widget sender was disposed")
            if not state.sending and (state.last_start is None or self.now() - state.last_start >= 60):
                self._cancel_timer(state)
                state.pending = None
                delivery = self._start(key, state, item, timed=False)
            else:
                state.pending = item
                self._arm(key, state)
                return {"queued": True, "due": state.last_start + 60}
        await delivery
        return {"sent": True}

    def cancel_all(self):
        for state in self.keys.values():
            self._cancel_timer(state)
            state.pending = None
        self.keys.clear()
