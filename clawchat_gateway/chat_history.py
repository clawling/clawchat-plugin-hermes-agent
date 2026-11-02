"""Conversation-scoped queries over the plugin's original-message ledger."""
from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

_TIMES = {
    "from": {"type": "string", "description": "Earliest time: YYYY-MM-DD or ISO 8601."},
    "to": {"type": "string", "description": "Latest time: YYYY-MM-DD (whole day) or ISO 8601."},
}
CHAT_HISTORY_TOOLS = [
    {
        "name": "chat_history_search",
        "description": 'Search the full history of this conversation, including messages no longer in front of you. Give words that would appear in the message itself (for a link, a word from the link or "http"). Matches any of the words, best and newest first; each hit has its time, speaker and message id. Only this conversation.',
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to look for, case-insensitive."},
            **_TIMES,
            "speaker": {"type": "string", "description": "owner, you, or a name or usr_ id."},
            "limit": {"type": "integer", "description": "Hits to return; default 10, at most 30."},
        }, "required": ["query"]},
    },
    {
        "name": "chat_history_read",
        "description": "Read messages of this conversation verbatim, oldest first: around a message id from chat_history_search, or a time range. With neither, the latest messages. Only this conversation.",
        "parameters": {"type": "object", "properties": {
            "around": {"type": "string", "description": "A message id to read around."},
            **_TIMES,
            "limit": {"type": "integer", "description": "Messages to return; default 20, at most 60."},
        }},
    },
]
_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
HISTORY_SCOPE_UNAVAILABLE = "This conversation could not be identified; no chat history was queried."
HISTORY_UNAVAILABLE = "Local chat history is unavailable."
_MORE = "More matches than shown — narrow it down with from / to / speaker, or more specific words."
_ISO = re.compile(r"^([+-]?\d{4,6})-?(\d\d)-?(\d\d)(?:[ T](\d\d)(?::?(\d\d)(?::?(\d\d)(?:[.,](\d+))?)?)? ?([zZ]|([+-])(\d\d)(?::?(\d\d))?)?)?$", re.ASCII)


def _object(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return (value.strip(_WHITESPACE) or None) if isinstance(value, str) else None


def _escape(value):
    return value.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


def _length(value):
    # JavaScript and Dart count UTF-16 code units, including at cut boundaries.
    return len(value.encode("utf-16-le", "surrogatepass")) // 2


def _slice(value, start, end):
    return value.encode("utf-16-le", "surrogatepass")[start * 2:end * 2].decode("utf-16-le", "surrogatepass")


def _parse_iso(value):
    m = _ISO.fullmatch(value)
    if not m:
        return math.nan
    year, month, day = map(int, m.group(1, 2, 3))
    hour, minute, second = (int(v or 0) for v in m.group(4, 5, 6))
    fraction = int((m[7] or "").ljust(6, "0")[:6]) / 1000
    try:
        # Normalize overflowing date fields just as DateTime.tryParse does.
        year_delta, month = divmod(month - 1, 12)
        year += year_delta
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        before = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334][month]
        y = year - 1
        days = 365 * y + y // 4 - y // 100 + y // 400 + before + (leap and month > 1) + day - 1 - 719162
        seconds = (days * 24 + hour) * 3600 + minute * 60 + second
        if m[8]:
            offset = (int(m[10] or 0) * 60 + int(m[11] or 0)) * (-1 if m[9] == "-" else 1)
            result = (seconds - offset * 60) * 1000
        else:
            fields = time.gmtime(seconds)[:6]
            if 1 <= fields[0] <= 9999:
                # fold=0 picks the earlier repeated time and advances skipped
                # local times, matching JavaScript rather than mktime's cache.
                result = datetime(*fields, fold=0).timestamp() * 1000
            else:
                result = time.mktime((*fields, 0, 0, -1)) * 1000
        return result + fraction if abs(result) <= 8.64e15 else math.nan
    except (ValueError, OverflowError, OSError):
        return math.nan


def _js_string(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dict):
        return "[object Object]"
    if isinstance(value, list):
        return ",".join("" if v is None else _js_string(v) for v in value)
    if type(value) in (int, float):
        number = float(value)
        if math.isnan(number):
            return "NaN"
        if math.isinf(number):
            return "Infinity" if number > 0 else "-Infinity"
        if number == 0:
            return "0"
        if 1e-6 <= abs(number) < 1e21:
            rendered = format(Decimal(repr(number)), "f")
            return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
        return re.sub(r"e([+-])0+", r"e\1", repr(number))
    return str(value)


def _stamp(value):
    if type(value) in (int, float) and math.isfinite(value) and abs(value) <= 8.64e15:
        ms = int(value)
        d = time.gmtime(ms // 1000)
        year = f"{d.tm_year:04d}" if 0 <= d.tm_year <= 9999 else f"{d.tm_year:+07d}"
        return f"{year}-{d.tm_mon:02d}-{d.tm_mday:02d}T{d.tm_hour:02d}:{d.tm_min:02d}:{d.tm_sec:02d}.{ms % 1000:03d}Z"
    return _js_string(value)


def _time(value, field, end=False):
    value = _text(value)
    if not value:
        return None
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        value += "T23:59:59.999" if end else "T00:00:00"
    at = _parse_iso(value)
    if not math.isfinite(at):
        raise ValueError(f"INVALID: {field} must be YYYY-MM-DD or an ISO 8601 time.")
    return at


def _limit(value, fallback, maximum):
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip(_WHITESPACE)):
        try:
            value = float(value.strip(_WHITESPACE))
        except ValueError:
            value = None
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and int(value) == value
    except OverflowError:
        valid = False
    if not valid:
        value = fallback
    return min(maximum, max(1, int(value)))


@dataclass
class _Entry:
    id: str
    ts: str
    at: float
    chronological_at: float
    text: str
    sender: str
    name: str
    self: bool
    owner: bool
    files: list[str]
    order: int


def _entry(row, scope):
    try:
        raw = _object(json.loads(row.get("raw_json") or "{}"))
    except (TypeError, ValueError):
        raw = {}
    sender = _object(raw.get("sender"))
    stamp = next((raw[k] for k in ("emitted_at", "timestamp") if raw.get(k) is not None), row["created_at"])
    ts = _stamp(stamp)
    self = row["direction"] == "outbound"
    from_id = scope.get("self_user_id", "") if self else sender.get("id", "")
    name = scope.get("self_name", "") if self else sender.get("nick_name", "")
    from_id = from_id if isinstance(from_id, str) else ""
    name = name if isinstance(name, str) else ""
    files = []
    if self and isinstance(raw.get("attachmentNames"), list):
        files = [v if isinstance(v, str) else "" for v in raw["attachmentNames"]]
    elif self and type(raw.get("mediaCount")) in (int, float) and 0 < raw["mediaCount"] <= 9007199254740991 and int(raw["mediaCount"]) == raw["mediaCount"]:
        files = [""] * int(raw["mediaCount"])
    else:
        # Hermes persists the full outbound frame as well as inbound frames.
        fragments = _object(_object(_object(raw.get("payload")).get("message")).get("body")).get("fragments")
        if isinstance(fragments, list):
            files = [f.get("name") if isinstance(f.get("name"), str) else "" for f in map(_object, fragments)
                     if f.get("kind") in ("image", "file", "audio", "video")]
    at = _parse_iso(ts)
    return _Entry(row.get("protocol_message_id") or row["message_id"], ts, at,
                  at if math.isfinite(at) else row["created_at"], row.get("text") or "", from_id, name,
                  self, not self and bool(scope.get("owner_user_id")) and from_id == scope["owner_user_id"], files, row["id"])


def _speaker(e):
    if e.self:
        return "you"
    named = bool(e.name) and e.name != e.sender
    name = _escape(e.name)
    if e.owner:
        return f"{name} (your owner)" if named else "your owner"
    if not named:
        return _escape(e.sender)
    lower = e.name.strip(_WHITESPACE).lower()
    return f"{name} ({_escape(e.sender)})" if lower == "you" or "your owner" in lower else name


def _speaker_matches(e, who):
    if who == "owner":
        return e.owner
    if who in ("you", "me", "self"):
        return e.self
    return e.sender.lower() == who if who.startswith("usr_") else who in e.name.lower()


def _line(e, terms=None):
    body = e.text if e.text.strip(_WHITESPACE) else "(empty message)"
    if terms is not None:
        body = body.replace("\r\n", " ").replace("\n", " ")
        if _length(body) > 280:
            lower = body.lower()
            at = next((_length(lower[:lower.index(t)]) for t in terms if t in lower), -1)
            start = max(0, at - 80)
            end = min(_length(body), start + 280)
            body = ("…" if start else "") + _slice(body, start, end) + ("…" if end < _length(body) else "")
    elif _length(body) > 4000:
        body = _slice(body, 0, 4000) + "… (cut)"
    head = f"[{e.ts}] {_speaker(e)} (id {e.id}):"
    tail = "".join(f"  [file {_escape(name)}]" if name else "  [file]" for name in e.files)
    return head + ("\n" if "\n" in body else " ") + body + tail


def _pack(header, lines, more, force_more):
    def fit(budget):
        spent = _length(header(len(lines)))
        shown = 0
        for line in lines:
            if spent + 1 + _length(line) > budget:
                break
            spent += 1 + _length(line)
            shown += 1
        return shown
    shown = -1 if force_more else fit(12000)
    if shown < 0 or shown < len(lines):
        longest = max(_length(more(None)), *(_length(more(i)) for i in range(len(lines))))
        shown = fit(12000 - 1 - longest)
    return "\n".join([header(shown), *lines[:shown],
                      *([more(shown if shown < len(lines) else None)] if force_more or shown < len(lines) else [])])


def run_chat_history_tool(tool: str, args: dict, rows: list[dict], scope: dict) -> str:
    """Pure query and formatting semantics; rows must already be conversation-scoped."""
    if tool not in ("chat_history_search", "chat_history_read"):
        raise ValueError(f'INVALID: unknown history tool "{tool}".')
    raw = _text(args.get("query")) or ""
    terms = list(dict.fromkeys(t for t in re.split(f"[{_WHITESPACE}]+", raw.lower()) if t))
    if tool == "chat_history_search" and not terms:
        raise ValueError("INVALID: query must contain at least one word.")
    excluded = set(scope.get("exclude_ids", ())) | {scope.get("exclude_id", "")}
    all_entries = sorted((_entry(row, scope) for row in rows if row.get("message_id")
                          and row["message_id"] not in excluded and row.get("protocol_message_id") not in excluded),
                         key=lambda e: (e.chronological_at, e.order))
    start, end = _time(args.get("from"), "from"), _time(args.get("to"), "to", True)
    ranged = [e for e in all_entries if (start is None and end is None) or
              (math.isfinite(e.at) and (start is None or e.at >= start) and (end is None or e.at <= end))]
    if tool == "chat_history_search":
        display = _slice(raw, 0, 11000) + "…" if _length(raw) > 11000 else raw
        who = (_text(args.get("speaker")) or "").lower()
        hits = []
        for e in ranged:
            if who and not _speaker_matches(e, who):
                continue
            haystack = "\n".join([e.text, *e.files]).lower()
            score = sum(t in haystack for t in terms)
            if score:
                hits.append((e, score))
        hits.sort(key=lambda h: (-h[1], -h[0].at if math.isfinite(h[0].at) else math.inf, -h[0].order))
        if not hits:
            return f'No message in this conversation matches "{display}". Try other words, or chat_history_read a time range.'
        shown = hits[:_limit(args.get("limit"), 10, 30)]
        return _pack(lambda n: f'Found {len(hits)} message(s) matching "{display}"; showing {n}, best match first.',
                     [_line(e, terms) for e, _ in shown], lambda _: _MORE, len(hits) > len(shown))
    cap = _limit(args.get("limit"), 20, 60)
    around = _text(args.get("around"))
    if around:
        at = next((i for i, e in enumerate(all_entries) if e.id == around), -1)
        if at < 0:
            raise ValueError(f"INVALID: no message with id {around} in this conversation.")
        offset = max(0, min(at - (cap - 1) // 2, len(all_entries) - cap))
        entries = all_entries[offset:offset + cap]
    else:
        entries = ranged[:cap + 1] if start is not None or end is not None else all_entries[-cap:]
    if not entries:
        return "No messages in this conversation for that range."
    next_ts = entries[cap].ts if len(entries) > cap else None
    entries = entries[:cap]
    return _pack(lambda n: f"{n} message(s), oldest first:", [_line(e) for e in entries],
                 lambda cut: f'More messages follow — call again with from: "{next_ts if cut is None else entries[cut].ts}".', next_ts is not None)


def history_for_session(tool: str, args: dict, *, session_id="", turn_id="") -> str:
    from clawchat_gateway.memory_scope import _session_value, _owner_user_id
    from clawchat_gateway.storage import get_clawchat_store
    from clawchat_gateway.turn_grants import effective_snapshot, lookup_snapshot_for_call

    if _session_value("HERMES_SESSION_PLATFORM").lower() != "clawchat":
        raise ValueError(HISTORY_SCOPE_UNAVAILABLE)
    chat = _session_value("HERMES_SESSION_CHAT_ID")
    if not chat:
        raise ValueError(HISTORY_SCOPE_UNAVAILABLE)
    nonce = _session_value("HERMES_SESSION_MESSAGE_ID")
    snapshot = (lookup_snapshot_for_call(nonce, chat, session_id, turn_id) if session_id and turn_id else None)
    snapshot = snapshot or effective_snapshot(nonce)
    if snapshot and snapshot.chat_id.lower() != chat.lower():
        raise ValueError(HISTORY_SCOPE_UNAVAILABLE)
    excluded = snapshot.history_exclude_ids if snapshot else frozenset({nonce})
    store = get_clawchat_store()
    # Message ingestion and outbound persistence both use this ledger account;
    # the turn-grants account is instead the paired user's identity.
    rows = store.list_chat_history_messages("default", chat)
    if rows is None:
        raise RuntimeError(HISTORY_UNAVAILABLE)
    if len({row.get("chat_id") for row in rows}) > 1:
        raise ValueError("The conversation's stored identity is ambiguous.")
    self_id = store.get_activation_user_id(platform="hermes", account_id="default") or ""
    self_name = ""
    try:
        from clawchat_gateway.terminal_send import get_clawchat_sender
        sender = get_clawchat_sender()
        if sender is not None:
            self_id, self_name = sender._current_agent_identity(sender._read_memory_metadata("owner", "owner"))
            if snapshot is None:
                trigger = sender._current_turn_trigger(chat)
                if trigger:
                    excluded |= frozenset({trigger})
                    open_turns = sender._open_turns(chat)
                    if len(open_turns) == 1 and open_turns[0][1] == trigger:
                        raw = _object(getattr(open_turns[0][0], "raw_message", None)).get("clawchat_raw")
                        excluded |= frozenset(sender._batch_message_ids(SimpleNamespace(raw_message=raw)))
    except (AttributeError, OSError, ValueError):
        # Display metadata is optional; it cannot widen the conversation scope.
        pass
    return run_chat_history_tool(tool, args, rows, {
        "exclude_ids": excluded, "owner_user_id": _owner_user_id(),
        "self_user_id": self_id, "self_name": self_name,
    })
