"""Strip Hermes CLI session-status lines from outbound ClawChat text.

Hermes prints a few terminal-facing status lines at the top of a turn when
the session was reset or when it announces the active model, e.g.::

    ◐ Session automatically reset (inactive for 24h). Conversation history cleared.
    Use /resume to browse and restore a previous session.
    Adjust reset timing in config.yaml under session_reset.

    ◆ Model: `deepseek-v4-flash-vision-exp`
    ◆ Provider: deepseek
    ◆ Context: 1.0M tokens (detected)

Observed 2026-09-03 (W-9): a group @-mention made the agent post exactly this
block into the ClawChat group before its real reply.  These lines are advice
for a person sitting at the CLI (``/resume``, ``config.yaml``) and mean
nothing inside a chat conversation, so they are cut out of the delivered text
regardless of ``output_visibility``.

The rule is deliberately NARROW — it would rather leak a line than eat a word
of the owner's question or the agent's own prose:

* a line starting with ``◐ Session automatically reset`` is dropped together
  with the rest of its paragraph (the following non-blank lines, up to the
  first blank line);
* a line starting with ``◆ Model:`` / ``◆ Provider:`` / ``◆ Context:`` is
  dropped on its own;
* blank lines immediately following a dropped line are dropped too, so the
  reply does not start with a hole.

Everything else is passed through byte-for-byte.  When nothing matches, the
input string is returned unchanged (not even trimmed).
"""

from __future__ import annotations

# A line whose stripped form starts with this opens a paragraph to drop.
SESSION_RESET_LINE_PREFIX = "◐ Session automatically reset"

# A line whose stripped form starts with one of these is dropped by itself.
SESSION_INFO_LINE_PREFIXES: tuple[str, ...] = (
    "◆ Model:",
    "◆ Provider:",
    "◆ Context:",
)

# Cheap pre-check: every rule needs one of these glyphs at a line start.
_MARKER_GLYPHS = ("◐", "◆")


def _is_blank(line: str) -> bool:
    return not line.strip()


def strip_hermes_session_status(text: str) -> str:
    """Remove Hermes session-status lines from *text*; see module docstring."""
    if not text or not any(glyph in text for glyph in _MARKER_GLYPHS):
        return text

    lines = text.split("\n")
    kept: list[str] = []
    removed_any = False
    i = 0
    total = len(lines)
    while i < total:
        stripped = lines[i].strip()
        if stripped.startswith(SESSION_RESET_LINE_PREFIX):
            # Drop the whole paragraph: this line plus following non-blank lines.
            while i < total and not _is_blank(lines[i]):
                i += 1
            while i < total and _is_blank(lines[i]):
                i += 1
            removed_any = True
            continue
        if stripped.startswith(SESSION_INFO_LINE_PREFIXES):
            i += 1
            while i < total and _is_blank(lines[i]):
                i += 1
            removed_any = True
            continue
        kept.append(lines[i])
        i += 1

    if not removed_any:
        return text
    return "\n".join(kept)
