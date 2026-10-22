"""Size limits for the agent's own notes shown in a turn's channel prompt.

Pure functions only; the adapter decides which notes belong in a turn.

* ``cap_note`` keeps a short opening profile paragraph and the newest whole
  paragraphs that fit, with a marker between them. If the last paragraph is too
  long, it keeps trailing whole lines, then falls back to a hard cut of the end.
  The marker and its separating newlines are outside the note-text limit.
* ``fit_turn_budget`` shares one per-turn budget between several notes: short
  notes are kept whole and the longest give way first (each is cut to a common
  ceiling chosen so the total fits).
"""

from __future__ import annotations

import re

_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n")


def _truncation_marker(read_hint: str) -> str:
    if read_hint:
        return f"(earlier part truncated — {read_hint} for the rest)"
    return "(earlier part truncated)"


def cap_note(body: str, limit: int, *, read_hint: str = "") -> str:
    """Keep a short profile and the latest text within *limit* characters.

    The marker and its separating newlines do not count toward the limit;
    paragraph and line separators within the retained tail do count.
    """
    text = (body or "").strip()
    if len(text) <= limit:
        return text
    paragraphs = _PARAGRAPH_SPLIT_RE.split(text)
    head = ""
    if len(paragraphs) >= 2 and len(paragraphs[0]) <= limit // 3:
        head = paragraphs.pop(0)
    remaining = limit - len(head)
    tail = ""
    for paragraph in reversed(paragraphs):
        candidate = f"{paragraph}\n\n{tail}" if tail else paragraph
        if len(candidate) > remaining:
            break
        tail = candidate
    if not tail:
        for line in reversed(paragraphs[-1].split("\n")):
            candidate = f"{line}\n{tail}" if tail else line
            if len(candidate) > remaining:
                break
            tail = candidate
    if not tail:
        tail = text[-remaining:] if remaining > 0 else ""
    marker = _truncation_marker(read_hint)
    return f"{head}\n{marker}\n{tail}" if head else f"{marker}\n{tail}"


def fit_turn_budget(lengths: list[int], budget: int) -> list[int]:
    """Per-note ceilings so ``sum(min(length, ceiling)) <= budget``.

    Notes no longer than the common ceiling keep their full length; only the
    longer ones are reduced, all to the same ceiling.
    """
    if sum(lengths) <= budget:
        return list(lengths)
    remaining = budget
    ceilings = list(lengths)
    order = sorted(range(len(lengths)), key=lambda index: lengths[index])
    for position, index in enumerate(order):
        share = remaining // (len(order) - position)
        if lengths[index] <= share:
            remaining -= lengths[index]
            continue
        for rest in order[position:]:
            ceilings[rest] = share
        break
    return ceilings
