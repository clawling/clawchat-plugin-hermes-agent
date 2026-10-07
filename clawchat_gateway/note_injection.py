"""Size limits for the agent's own notes shown in a turn's channel prompt.

Pure functions only; the adapter decides which notes belong in a turn.

* ``cap_note`` keeps whole leading paragraphs that fit (falling back to whole
  lines, then to a hard cut of the first line) and marks the cut.
* ``fit_turn_budget`` shares one per-turn budget between several notes: short
  notes are kept whole and the longest give way first (each is cut to a common
  ceiling chosen so the total fits).
"""

from __future__ import annotations

import re

_PARAGRAPH_SPLIT_RE = re.compile(r"\n[ \t]*\n")


def _truncation_marker(read_hint: str) -> str:
    if read_hint:
        return f"(truncated — {read_hint} for the rest)"
    return "(truncated)"


def cap_note(body: str, limit: int, *, read_hint: str = "") -> str:
    """Return *body* cut to at most *limit* characters of note text."""
    text = (body or "").strip()
    if len(text) <= limit:
        return text
    kept = ""
    for paragraph in _PARAGRAPH_SPLIT_RE.split(text):
        candidate = f"{kept}\n\n{paragraph}" if kept else paragraph
        if len(candidate) > limit:
            break
        kept = candidate
    if not kept:
        for line in text.split("\n"):
            candidate = f"{kept}\n{line}" if kept else line
            if len(candidate) > limit:
                break
            kept = candidate
    if not kept:
        kept = text[:limit]
    return f"{kept.rstrip()}\n{_truncation_marker(read_hint)}"


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
