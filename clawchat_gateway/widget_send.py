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
    "the format before your first one. It needs ClawChat app 1.0.29 or later on "
    "their phone; older apps show it as unsupported content."
)
NOT_CONNECTED = "ClawChat is not connected; try again later"

WIDGET_V1_GUIDE = """## Home-screen widgets (widget/v1)

A widget is a small card on your partner's phone home screen. You write it with `clawchat_widget_send`; their taps come back to you as ordinary messages. It only ever goes to your private chat with them.

### When to send

- Send one only after your partner asked for a widget — a recipe card, or their own words.
- If they keep asking you for the same thing (a price, a list, a count), you may offer a widget once. Make it only after they say yes. Never offer the same one twice.

### When to update

Send a new snapshot in the same turn whenever what it shows has changed:

- they tapped or typed on it (a `[widget <id>] …` message);
- they changed the same data in chat: "remind me to buy milk tomorrow" also goes onto the to-do widget;
- a new day began: "today", "tomorrow" or "N days left" in your last snapshot are now wrong. Send it again in your first turn of the day. If it counts days, schedule yourself a turn early every morning for it.

Nothing changed: send nothing. Every message is the full snapshot, never a diff; the latest one per `id` wins. At most one per `id` a minute. Data that changes by itself (prices, weather) belongs in `source`, not in repeated snapshots.

### Design

- One widget answers one question. `title`: a few words. Never repeat it, the `badge` or the big number elsewhere; no "tap + to add" hints.
- Write `small`, `medium` and `large`, each from one layout below, with the same main figure. A missing size is stretched from another and looks broken.
- Name things in every size: a figure alone says nothing. Every text is one line. Per line: small about 7 Chinese characters (12 Latin), medium and large about 17 (30). Write it short; never rely on "…".
- Height is tight. `small` and `medium` fit exactly one of: `[caption; number title; caption]`, 4 lines of text, or a `list` of 3. A `button` counts as 2 lines; with a `source`, one line less (its footer). Anything taller is not drawn.
- `large` is twice as tall as `medium`: fill it with what medium had no room for (more items, a breakdown, the next ones). Never the medium tree alone, never a figure twice.
- One big `number` (`style: title`) per size; in `small` at most 4 characters ("81737" is 5: `style: body` there). `unit` is a unit only ("km", "kg", "%"); labels go in a `caption`.
- Supporting lines: `caption` with `tone: secondary`. Columns side by side get the same lines, so their tops line up. `tone: accent` in one place at most, only for what is urgent. An `icon` only when it tells items apart.
- A `list` holds things of one kind: write all of them, we show what fits and "N more" — only when the `list` is the whole size, not inside a `column`. Buttons and sentences never go in a `list`; only its first row may have a big number.
- `action: capture` only when they add items by typing (to-dos, expenses); then no `button` doing the same. A `button` only for one tap that changes something (check in, add a cup, draw again).
- `badge`: only the number of open items, and then no big number saying the same.
- Nothing that goes stale: dates ("10/24") and clock times ("last 8:20"), never "today", "tomorrow" or "50 min ago" (not in the `title` either), even if they ask "how long since": a widget has no running clock. A count for one day carries its date.
- Nothing left: an empty `list`; we draw the empty state.
- Every text in your partner's language, their items in their own words.

### Layouts

Pick by what it shows. `[a; b]` is a `column`, `(a | b)` a `row`.

1. One figure (spent this month, cups of water, km this week):
   small `[caption label; number title + unit; caption context]` (with a `source`: drop the last caption), or with a tap `[number title + unit; button]`;
   medium `([caption label; number title + unit; caption context] | X)`, X is one thing: a `ring` for a goal, a `button`, or `[caption; number body]`. Never two things stacked beside it;
   large `[the medium tree; list of up to 5 (text grow | number body)]`: a breakdown or the last entries.
2. Several things, each with a figure or a time (prices, rates, books, a schedule, countdowns): every size is a `list`, one row per thing, the nearest or most important first.
   first row `(text name grow | number title + unit)`, the rest `(text name grow | number body + unit)` or `(text name grow | caption time)`;
   small: the same rows with short names. A `list` grows with taller widgets and counts "N more"; a `column` does not.
3. Things to tick (to-dos, shopping, habits): see the document below.
4. Words (a fortune, a word of the day): one `column`, one text per line, never side by side in a `row`.
   small `[text title; caption; caption]`; medium `[text title; text body; caption; caption]`, or `[(text title grow | button); text body; caption]` with a tap; large: up to 4 more `caption` lines. Split long sentences.

### Removing one

Asked to remove a widget: send `{"v": 1, "id": "todo", "removed": true}` with its `id`. It leaves their widget list; a copy on the home screen shows "deleted" until they long-press it away. `[widget todo] removed` means they deleted it themselves. Either way, stop sending it. A later snapshot with that `id` brings it back, so send one only if they ask again.

### Document

```json
{"v": 1, "id": "todo", "title": "To-do", "badge": 2,
 "action": {"do": "capture"},
 "small": {"type": "list", "items": [
   {"type": "row", "children": [
     {"type": "check", "id": "t1", "label": "Pay rent", "checked": false},
     {"type": "text", "text": "Pay rent", "grow": true}]},
   {"type": "row", "children": [
     {"type": "check", "id": "t2", "label": "Call mom", "checked": false},
     {"type": "text", "text": "Call mom", "grow": true}]}]},
 "medium": {"type": "list", "items": [
   {"type": "row", "children": [
     {"type": "check", "id": "t1", "label": "Pay rent", "checked": false},
     {"type": "text", "text": "Pay rent", "grow": true},
     {"type": "text", "text": "10/24", "style": "caption", "tone": "accent"}]},
   {"type": "row", "children": [
     {"type": "check", "id": "t2", "label": "Call mom", "checked": false},
     {"type": "text", "text": "Call mom", "grow": true}]}]}}
```

`large`: the same `list` as `medium`. Always list every item; we show what fits and "N more".

- `v`: 1. `id`: `^[a-z0-9][a-z0-9_-]{0,31}$`, the same across updates. `title`: up to 40 characters.
- `badge` (optional): an integer 0–9999, or up to 12 characters.
- `action` (optional, top right): `capture`, `chat` or `liveware`.
- `small` / `medium` / `large`: one block tree per size, at least one. A missing size borrows `medium`, then `large`, then `small`. A medium list shows about 3 rows.
- At most 16 KiB of UTF-8. Unknown fields and block types are skipped.
- Colours, fonts and spacing are ours; you choose the structure.

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
- `capture`: opens your chat with "<title>: " already typed; what they add reaches you as an ordinary message.
- `chat`: opens your chat. Text without `tap` does this.
- `liveware`: `{"do": "liveware", "url": "https://<app>.apps.clawling.io/…"}` opens that page.

### Data that changes by itself

```json
"source": {"url": "https://…", "every": "30m", "fields": {"price": "data.price"}}
```

The phone fetches it itself: HTTPS GET, no credentials, JSON up to 64 KiB, every 15m to 24h. A field is a dot path into that JSON, array items by number (`data.items.0.price`); each step is letters, digits or `_` only, so a key like `xnet-1` cannot be read. Field names: lowercase letters, digits and `_`; every `{name}` you write must be one of them. Write `{price}` in `text` or `number` where the value goes: a value you type in yourself goes stale. Your partner sees the domain.

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
[widget todo] removed
```

They can arrive late and several at once: apply each in order, they are not duplicates. Then send one new full snapshot, except after `removed`: send nothing for that widget (see Removing one). `resend` means a device lost the widget: send the latest snapshot again.
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

    # Preserve numeric shape without Python's integer digit limit or float conversion.
    number = object()
    try:
        value = json.loads(doc, parse_int=lambda _: number, parse_float=lambda _: number,
                           parse_constant=reject_constant)
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
    if not isinstance(widget, str) or not widget.strip():
        return {"message": WIDGET_V1_GUIDE}
    # The original string is checked and sent byte-for-byte; whitespace counts toward the size.
    doc = widget
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

        due = state.last_start + 60 if state.last_start is not None else self.now()
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
                        if state.pending is not None:
                            self._cancel_timer(state)
                            self._arm(key, state)
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
