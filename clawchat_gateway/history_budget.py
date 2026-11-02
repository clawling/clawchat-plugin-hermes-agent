"""Pure history budgeting rules shared with the ClawChat desktop agent.

Inputs are in ledger order, oldest first; history_maintenance orchestrates them.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import re
from typing import Literal


@dataclass(frozen=True)
class Entry:
    id: str
    ts: str
    role: Literal["self", "owner", "other"]
    name: str
    text: str


@dataclass(frozen=True)
class BlockItem(Entry):
    truncated: bool = False


def estimate_tokens(text: str) -> int:
    units = text.encode("utf-16-le", errors="surrogatepass")
    cjk = 0
    for offset in range(0, len(units), 2):
        unit = units[offset] | units[offset + 1] << 8
        if (0x2E80 <= unit <= 0x9FFF or 0xAC00 <= unit <= 0xD7AF
                or 0xF900 <= unit <= 0xFAFF or 0xFF00 <= unit <= 0xFFEF):
            cjk += 1
    return cjk + (len(units) // 2 - cjk + 3) // 4


def estimate_entry_tokens(entry: Entry) -> int:
    return estimate_tokens(f"{entry.name} {entry.text}") + 8


def history_tokens(
    open_history: int | None, open_context: int | None, context_last: int | None,
) -> int | None:
    if context_last is None or open_context is None:
        return None
    return (open_history or 0) + max(0, context_last - open_context)


_ISO_TIMESTAMP = re.compile(
    r"([+-]?[0-9]{4,6})-?([0-9]{2})-?([0-9]{2})"
    r"(?:[ T]([0-9]{2})(?::?([0-9]{2})(?::?([0-9]{2})(?:[.,]([0-9]+))?)?)?"
    r"( ?[zZ]| ?([-+])([0-9]{2})(?::?([0-9]{2}))?)?)?\Z"
)


def _entry_time(text: str) -> datetime | None:
    """Dart's ISO timestamp grammar, overflow normalization and local-time rule.

    Ledger dates must fit Python's datetime year range (1 through 9999).
    """
    match = _ISO_TIMESTAMP.fullmatch(text)
    if match is None:
        return None
    try:
        year, month, day = (int(match[i]) for i in (1, 2, 3))
        year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
        hour, minute, second = (int(match[i] or 0) for i in (4, 5, 6))
        micros = int((match[7] or "")[:6].ljust(6, "0"))
        if match[9]:
            offset = int(match[10]) * 60 + int(match[11] or 0)
            minute -= offset if match[9] == "+" else -offset
        ts = datetime(year, month, 1, tzinfo=timezone.utc if match[8] else None)
        ts += timedelta(days=day - 1, hours=hour, minutes=minute,
                        seconds=second, microseconds=micros)
        return ts.astimezone()
    except (ValueError, OverflowError, OSError):
        return None


def plan_compaction(
    entries: list[Entry], now: datetime, recent_window: timedelta,
    keep_recent_turns: int, window_cap_tokens: int, fold_all: bool,
) -> tuple[list[Entry], list[Entry]]:
    """Port of planCompaction; window_cap_tokens is already the window's share."""
    start = len(entries)
    if not fold_all:
        threshold = now.astimezone() - recent_window
        for index, entry in enumerate(entries):
            ts = _entry_time(entry.ts)
            if ts is not None and ts >= threshold:
                start = index
                break
        inbound = sum(entry.role != "self" for entry in entries[start:])
        while start > 0 and inbound < keep_recent_turns:
            start -= 1
            if entries[start].role != "self":
                inbound += 1
        spent = sum(estimate_entry_tokens(entry) for entry in entries[start:])
        last_inbound = next(
            (index for index in range(len(entries) - 1, -1, -1)
             if entries[index].role != "self"), -1,
        )
        stop = len(entries) if last_inbound < 0 else last_inbound
        while start < stop and spent > window_cap_tokens:
            spent -= estimate_entry_tokens(entries[start])
            start += 1
        while start < stop and entries[start].role == "self":
            start += 1
    return entries[:start], entries[start:]


def split_blocks(fold: list[Entry], block_cap_tokens: int = 24000) -> list[list[BlockItem]]:
    """Keep whole messages together; only an oversized message loses its middle.

    For that exception use a conservative character allowance (one character per
    token), reserving space for the label and omission marker. The mandatory
    marker itself may exceed an artificially tiny cap.
    """
    if block_cap_tokens <= 0:
        raise ValueError("block_cap_tokens must be positive")
    blocks: list[list[BlockItem]] = []
    block: list[BlockItem] = []
    spent = 0
    for entry in fold:
        cost = estimate_entry_tokens(entry)
        item = BlockItem(entry.id, entry.ts, entry.role, entry.name, entry.text)
        if cost > block_cap_tokens:
            if block:
                blocks.append(block)
                block, spent = [], 0
            marker = f"[middle omitted — chat_history_read around={entry.id}]"
            allowance = max(0, block_cap_tokens - estimate_entry_tokens(replace(entry, text=marker)))
            head = (allowance + 1) // 2
            tail = allowance // 2
            text = entry.text[:head] + marker + (entry.text[-tail:] if tail else "")
            blocks.append([replace(item, text=text, truncated=True)])
            continue
        if block and spent + cost > block_cap_tokens:
            blocks.append(block)
            block, spent = [], 0
        block.append(item)
        spent += cost
    if block:
        blocks.append(block)
    return blocks


def recent_window_for_injection(
    entries: list[Entry], budget_tokens: int, *, has_summary: bool = False,
    now: datetime, recent_window: timedelta = timedelta(days=3),
    keep_recent_turns: int = 8,
) -> list[Entry]:
    """With a summary, entries must already be after through and boundary.

    Without a summary apply the desktop agent's recent-window planning rules.
    A summary's tail is a contiguous suffix and keeps at least its newest entry.
    """
    if not has_summary:
        return plan_compaction(
            entries, now, recent_window, keep_recent_turns, budget_tokens, False,
        )[1]
    start = len(entries)
    spent = 0
    for index in range(len(entries) - 1, -1, -1):
        cost = estimate_entry_tokens(entries[index])
        if start != len(entries) and spent + cost > budget_tokens:
            break
        spent += cost
        start = index
    return entries[start:]
