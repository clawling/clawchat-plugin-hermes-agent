"""Conversation-scoped queries over the plugin's original-message ledger."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit

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


_HISTORY_URLS = re.compile(
    r"\b[a-z][a-z0-9+.-]*://(?:(?!\b[a-z][a-z0-9+.-]*://)[^" + _WHITESPACE + r"\"'<>])+",
    re.IGNORECASE | re.ASCII,
)


def _url_host(host):
    if ":" in host:
        if "%" in host:
            raise ValueError("invalid URL host")
        return f"[{ipaddress.IPv6Address(host).compressed}]"
    host = unicodedata.normalize("NFKC", unquote(host, errors="strict")).lower()
    if not host or re.search(r"[\x00-\x20\x7f #%/:<>?@\[\]\\^|]", host):
        raise ValueError("invalid URL host")
    host = ".".join(label if label.isascii() else "xn--" + label.encode("punycode").decode("ascii")
                    for label in host.split("."))
    parts = host.removesuffix(".").split(".")
    if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", parts[-1]):
        numbers = []
        for part in parts:
            if part.startswith("0x"):
                numbers.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        if len(numbers) > 4 or any(n > 255 for n in numbers[:-1]) or numbers[-1] >= 256 ** (5 - len(numbers)):
            raise ValueError("invalid URL host")
        value = numbers[-1] + sum(n * 256 ** (3 - i) for i, n in enumerate(numbers[:-1]))
        return str(ipaddress.IPv4Address(value))
    return host


def _redact_history_url(source):
    """Match the media-source URL redactor's origin + path, failing closed."""
    try:
        source = source.strip("".join(map(chr, range(33))))
        source = source.replace("\t", "").replace("\r", "").replace("\n", "")
        scheme, rest = source.split(":", 1)
        scheme = scheme.lower()
        defaults = {"http": 80, "https": 443, "ftp": 21, "ws": 80, "wss": 443}
        if scheme not in defaults:
            return "[redacted URL]"
        # WHATWG special URLs normalize backslashes and repeated opening slashes.
        rest = re.split(r"[?#]", rest, maxsplit=1)[0].replace("\\", "/")
        parsed = urlsplit(scheme + "://" + rest.lstrip("/"))
        host = _url_host(parsed.hostname or "")
        port = parsed.port
        authority = host + (f":{port}" if port is not None and port != defaults[scheme] else "")
        parts = []
        segments = (parsed.path or "/").split("/")
        for i, part in enumerate(segments):
            dot = part.lower().replace("%2e", ".")
            if dot in (".", ".."):
                if dot == ".." and len(parts) > 1:
                    parts.pop()
                if i == len(segments) - 1:
                    parts.append("")
            else:
                parts.append(part)
        path = quote("/".join(parts), safe="/!$&'()*+,-.:;=@_~%[]|^")
        return f"{scheme}://{authority}{path}"
    except (ValueError, UnicodeError):
        return "[redacted URL]"


def _redact_history_urls(value):
    def replace(match):
        source = match.group(0)
        closing = re.search(r"[)\]}]+$", source)
        suffix = closing.group(0) if closing else ""
        return _redact_history_url(source[:len(source) - len(suffix)]) + suffix
    return _HISTORY_URLS.sub(replace, value)


_CREDENTIAL_KEYS = re.compile(
    r"(?:\\*[\"'])?\b(proxy-authorization|authorization|access[_-]?token|refresh[_-]?token|token|"
    r"api[_-]?key|apikey|password|passwd|secret|signature|x-amz-signature|x-amz-credential)"
    r"\b(?:\\*[\"'])?[" + _WHITESPACE + r"]*[:=][" + _WHITESPACE + r"]*",
    re.IGNORECASE | re.ASCII,
)
_MASK = '"[redacted]"'
_MASK_BOUNDARY = re.compile("[" + _WHITESPACE + r"\,;&}\])\"']")
_USERINFO_SCHEME = re.compile(r"(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,31}://", re.IGNORECASE | re.ASCII)


def _strip_history_userinfo(value):
    parts = []
    copied = 0
    for match in _USERINFO_SCHEME.finditer(value):
        if match.start() < copied:
            continue
        start = match.end()
        boundary = re.search("[" + _WHITESPACE + r"/?#]", value[start:])
        end = start + boundary.start() if boundary else len(value)
        at = value.rfind("@", start, end)
        if at < 0:
            continue
        boundary = re.search("[" + _WHITESPACE + r"\"'<>`]", value[at + 1:end])
        host_end = at + 1 + boundary.start() if boundary else end
        source = value[match.start():host_end]
        try:
            parsed = urlsplit(source)
            if parsed.scheme in {"http", "https", "ftp", "ws", "wss"}:
                safe = _redact_history_url(source)
                authority = urlsplit(safe).netloc
            else:
                authority = parsed.netloc.rsplit("@", 1)[-1]
                parsed.port  # Reject invalid ports, as new URL does.
            safe = match.group(0) + authority if authority else "[redacted-url]"
        except (ValueError, UnicodeError):
            safe = "[redacted-url]"
        parts.append(value[copied:match.start()] + safe)
        copied = host_end
    return "".join(parts) + value[copied:]


def _quoted_history_value_end(value, start, level, quote):
    for at in range(start, len(value)):
        if value[at] != quote:
            continue
        run = 0
        while at - run - 1 >= start and value[at - run - 1] == "\\":
            run += 1
        if level == 0:
            if run % 2 == 0:
                return at + 1
        elif run % 4 == 1:
            return at + 1
        elif run % 2 == 0:
            break
    raise ValueError("ambiguous credential value")


def _mask_history_credentials(value):
    out = ""
    pos = 0
    mask_ends = []
    while (match := _CREDENTIAL_KEYS.search(value, pos)) is not None:
        start = match.end()
        level = 0
        while start + level < len(value) and value[start + level] == "\\":
            level += 1
        quote = value[start + level:start + level + 1]
        if quote in ('"', "'"):
            if level > 1:
                raise ValueError("ambiguous credential value")
            end = _quoted_history_value_end(value, start + level + 1, level, quote)
        else:
            delimiter = r"[\r\n]" if "authorization" in match[1].lower() else r"[\r\n\"',;&}]"
            stop = re.search(delimiter, value[start:])
            end = start + stop.start() if stop else len(value)
        out += value[pos:start] + _MASK
        mask_ends.append(len(out))
        pos = end
    out += value[pos:]
    if any(at < len(out) and not _MASK_BOUNDARY.fullmatch(out[at]) for at in mask_ends):
        raise ValueError("ambiguous credential value")
    pos = 0
    while (match := _CREDENTIAL_KEYS.search(out, pos)) is not None:
        if not out.startswith(_MASK, match.end()):
            raise ValueError("ambiguous credential value")
        pos = match.end() + len(_MASK)
    return out


def _redact_history_text(value):
    """URL redaction followed by the shared reference diagnostic credential rules."""
    try:
        safe = _mask_history_credentials(_strip_history_userinfo(_redact_history_urls(value)))
        safe = re.sub(r"\b(bearer)[" + _WHITESPACE + r"]+[A-Za-z0-9._~+/=-]+",
                      lambda m: m[1] + " [redacted]", safe, flags=re.IGNORECASE | re.ASCII)
        safe = re.sub(r"\b([Bb][Aa][Ss][Ii][Cc])[" + _WHITESPACE
                      + r"]+(?=[A-Za-z0-9+/]*[A-Z0-9+/=])[A-Za-z0-9+/]+={0,2}(?![A-Za-z0-9+/=])",
                      lambda m: m[1] + " [redacted]", safe, flags=re.ASCII)
        safe = re.sub(r"\beyJ[A-Za-z0-9._~+/=-]{10,}", "[redacted]", safe, flags=re.ASCII)
        if safe in {"(diagnostic text withheld: base URL contains credentials)",
                    "(diagnostic text withheld: redaction failed)",
                    "(diagnostic text withheld: ambiguous credential value)"}:
            return "[redacted]"
        return safe
    except Exception:  # noqa: BLE001 - ambiguous/failed redaction withholds the whole string
        return "[redacted]"


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
    body = row.get("text") or ""
    if self and isinstance(raw.get("attachmentNames"), list):
        files = [v if isinstance(v, str) else "" for v in raw["attachmentNames"]]
    elif self and type(raw.get("mediaCount")) in (int, float) and 0 < raw["mediaCount"] <= 9007199254740991 and int(raw["mediaCount"]) == raw["mediaCount"]:
        files = [""] * int(raw["mediaCount"])
    else:
        # Hermes persists the full outbound frame as well as inbound frames.
        fragments = _object(_object(_object(raw.get("payload")).get("message")).get("body")).get("fragments")
        if isinstance(fragments, list):
            media = [f for f in map(_object, fragments) if f.get("kind") in ("image", "file", "audio", "video")]
            files = [f.get("name") if isinstance(f.get("name"), str) else "" for f in media]
            # Replace full envelope URLs first, including malformed ones or spaces.
            for fragment in media:
                source = fragment.get("url")
                if isinstance(source, str) and source:
                    body = body.replace(source, _redact_history_url(source))
    at = _parse_iso(ts)
    return _Entry(row.get("protocol_message_id") or row["message_id"], ts, at,
                  at if math.isfinite(at) else row["created_at"], _redact_history_text(body), from_id, _redact_history_text(name),
                  self, not self and bool(scope.get("owner_user_id")) and from_id == scope["owner_user_id"], [_redact_history_text(f) for f in files], row["id"])


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


def chat_history_read_query(args: dict, exclude_ids) -> dict | None:
    """Latest/range SQL plan; render still validates after storage resolution."""
    if _text(args.get("around")):
        return None
    try:
        return {"limit": _limit(args.get("limit"), 20, 60),
                "from": _time(args.get("from"), "from"), "to": _time(args.get("to"), "to", True),
                "exclude_ids": frozenset(exclude_ids)}
    except ValueError:
        return None


_FIELD_CHARS = 256
_FILE_MARKERS_CHARS = 2048
_ENTRY_CHARS = 8000


def _clip(value, cap):
    """A cap includes its deterministic ellipsis, measured in UTF-16 units."""
    return value if _length(value) <= cap else _slice(value, 0, cap - 1) + "…"


def _file_markers(names):
    tail = ""
    for i, name in enumerate(names):
        marker = f"  [file {_clip(_escape(name), _FIELD_CHARS)}]" if name else "  [file]"
        # Keep complete markers and reserve the final ellipsis when files remain.
        reserve = int(i + 1 < len(names))
        if _length(tail) + _length(marker) + reserve > _FILE_MARKERS_CHARS:
            return tail + "…"
        tail += marker
    return tail


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
    head = f"[{_clip(e.ts, _FIELD_CHARS)}] {_clip(_speaker(e), _FIELD_CHARS)} (id {e.id}):"
    tail = _file_markers(e.files)
    return _clip(head + ("\n" if "\n" in body else " ") + body + tail, _ENTRY_CHARS)


def _pack(header, lines, more, force_more):
    # Query echoes can consume most of a search reply. Bound each entry to
    # what one line can actually use after reserving header/newlines/footer.
    longest = max(_length(more(None)), *(_length(more(i)) for i in range(len(lines))))
    entry_budget = max(1, 12000 - _length(header(len(lines))) - longest - 2)
    lines = [_clip(line, min(_ENTRY_CHARS, entry_budget)) for line in lines]

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
        safe_query = _redact_history_text(raw)
        display = _slice(safe_query, 0, 11000) + "…" if _length(safe_query) > 11000 else safe_query
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
    continuation = False
    if around:
        at = next((i for i, e in enumerate(all_entries) if e.id == around), -1)
        if at < 0:
            raise ValueError("INVALID: no message with that id in this conversation; use an id from chat_history_search.")
        continuation = start is not None and start == all_entries[at].chronological_at
        if continuation:
            entries = [e for e in all_entries[at:] if end is None or e.chronological_at <= end][:cap + 1]
        else:
            offset = max(0, min(at - (cap - 1) // 2, len(all_entries) - cap))
            entries = all_entries[offset:offset + cap]
    else:
        entries = ranged[:cap + 1] if start is not None or end is not None else all_entries[-cap:]
    if not entries:
        return "No messages in this conversation for that range."
    next_entry = entries[cap] if len(entries) > cap else None
    entries = entries[:cap]

    def more(cut):
        omitted = next_entry if cut is None else entries[cut]
        if omitted is None:
            return "More messages follow — narrow it down with from / to."
        shown = entries if cut is None else entries[:cut]
        same_time = any(e.chronological_at == omitted.chronological_at for e in shown)
        if continuation or same_time or not math.isfinite(omitted.at):
            stamp = omitted.ts if math.isfinite(omitted.at) else _stamp(omitted.chronological_at)
            encode = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            until = "" if end is None else f", to: {encode(_text(args.get('to')))}"
            return (f"More messages follow — call again with around: {encode(omitted.id)}, "
                    f"from: {encode(stamp)}{until}, limit: {cap}.")
        until = "" if end is None else f", to: {json.dumps(_text(args.get('to')), ensure_ascii=False)}"
        return f'More messages follow — call again with from: "{omitted.ts}"{until}.'

    return _pack(lambda n: f"{n} message(s), oldest first:", [_line(e) for e in entries], more, next_entry is not None)



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
    if snapshot is None:
        try:
            from clawchat_gateway.terminal_send import get_clawchat_sender
            sender = get_clawchat_sender()
            if sender is not None:
                trigger = sender._current_turn_trigger(chat)
                if trigger:
                    excluded |= frozenset({trigger})
                    open_turns = sender._open_turns(chat)
                    if len(open_turns) == 1 and open_turns[0][1] == trigger:
                        raw = _object(getattr(open_turns[0][0], "raw_message", None)).get("clawchat_raw")
                        excluded |= frozenset(sender._batch_message_ids(SimpleNamespace(raw_message=raw)))
        except (AttributeError, OSError, ValueError):
            pass
    store = get_clawchat_store()
    # Message ingestion and outbound persistence both use this ledger account;
    # the turn-grants account is instead the paired user's identity.
    query = chat_history_read_query(args, excluded) if tool == "chat_history_read" else None
    rows = (store.list_chat_history_messages("default", chat, query) if query is not None
            else store.list_chat_history_messages("default", chat))
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
    except (AttributeError, OSError, ValueError):
        # Display metadata is optional; it cannot widen the conversation scope.
        pass
    return run_chat_history_tool(tool, args, rows, {
        "exclude_ids": excluded, "owner_user_id": _owner_user_id(),
        "self_user_id": self_id, "self_name": self_name,
    })
