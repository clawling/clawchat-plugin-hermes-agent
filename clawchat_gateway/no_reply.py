"""ClawChat no-reply / silence token detection.

Two independent rules:

RULE A — the ``clawchat:`` family, matched as a LOOSE SUBSTRING.  Any
occurrence anywhere in the text suppresses the whole message.  Because the
pattern is NOT anchored, wrapping decorations (``<>``, ``[]``, ``{}``, ``/``,
backticks, ``*``) are tolerated for free and must not be written into the
pattern itself.

RULE B — the host's bare markers (``NO_REPLY`` / ``[SILENT]`` / ``SILENT`` /
``NO REPLY``), matched as a WHOLE STRING ONLY.  The canonicalization below is a
line-for-line port of the host runtime's own silence filter so the two never
drift.  These are ordinary English words; substring-matching them would swallow
prose such as "there is no reply from the server".

This module MUST stay a literal mirror of the OpenClaw plugin's equivalent
module.  When one side changes, change the other in the same breath.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Rule A — clawchat: family, loose substring
# ---------------------------------------------------------------------------

_CORE = r"clawchat\s*:\s*(?:no[-_ ]?reply|silent)\b"

_CONTAINS_RE = re.compile(_CORE, re.IGNORECASE)

# Decoration classes used only for stripping.  They deliberately EXCLUDE ``\s``:
# including whitespace would eat the spaces on either side of the token and glue
# neighbouring words together ("a <clawchat:silent/> b" -> "ab").
_STRIP_RE = re.compile(
    r"[<\[{`*_~]*" + _CORE + r"[/>\]}`*_~]*",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Rule B — host bare markers, whole-string exact
# ---------------------------------------------------------------------------

_HOST_MARKERS = frozenset({"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"})
_HOST_MARKER_MAX_LEN = 64


def _canonical_marker_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_marker_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Square brackets stay structural so a malformed ``[SILENT`` does not get
    laundered into a valid ``SILENT``.
    """
    start = 0
    end = len(text)
    while (
        start < end
        and text[start] not in "[]"
        and unicodedata.category(text[start]).startswith("P")
    ):
        start += 1
    while (
        end > start
        and text[end - 1] not in "[]"
        and unicodedata.category(text[end - 1]).startswith("P")
    ):
        end -= 1
    return text[start:end].strip()


def _canonical_marker_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_marker_candidate(text)
    stripped = _strip_edge_marker_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    return (exact, _canonical_marker_candidate(stripped))


def is_host_silence_marker(text: str) -> bool:
    """True when *text* as a whole is one of the host's bare silence markers."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _HOST_MARKER_MAX_LEN:
        return False
    return any(c in _HOST_MARKERS for c in _canonical_marker_candidates(stripped))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def contains_no_reply_token(text: str) -> bool:
    """True when *text* should suppress delivery under rule A or rule B."""
    if not text:
        return False
    return bool(_CONTAINS_RE.search(text)) or is_host_silence_marker(text)


def strip_no_reply_tokens(text: str) -> str:
    """Remove every rule-A hit (with its adjacent decorations) and trim the ends.

    Whitespace between surviving words is preserved as-is — never collapsed.
    A whole-string rule-B marker leaves nothing behind.
    """
    if not text:
        return ""
    if is_host_silence_marker(text):
        return ""
    return _STRIP_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
# Streaming prefix guard (rule A only)
# ---------------------------------------------------------------------------

# Canonical complete forms of every rule-A token, used by the streaming guard.
# Host bare markers are deliberately NOT included: the host runs its own
# streaming hold-back for those, and "NO" is far too common a prefix to hold.
_TOKEN_CORES = (
    "clawchat:no-reply",
    "clawchat:no_reply",
    "clawchat:noreply",
    "clawchat:no reply",
    "clawchat:silent",
)
_BRACKET_OPEN = ("", "<", "[", "{")
_BRACKET_CLOSE = ("", ">", "]", "}")
_SLASH = ("", "/")
_COMPLETE_FORMS = frozenset(
    f"{o}{core}{s}{c}"
    for o in _BRACKET_OPEN
    for core in _TOKEN_CORES
    for s in _SLASH
    for c in _BRACKET_CLOSE
)

_NORMALIZE_RE = re.compile(r"\s*([<\[{>\]}:/])\s*")


def _normalize(text: str) -> str:
    """Lowercase, strip, and drop whitespace adjacent to a delimiter only.

    Whitespace inside the literal token words is preserved so it can never be
    collapsed into a spurious match.
    """
    return _NORMALIZE_RE.sub(r"\1", text.strip().lower())


def is_no_reply_token_prefix(text: str) -> bool:
    """True when the whole of *text* is a strict prefix of some rule-A form.

    Used by the streaming output path: the first partial chunk of a token must
    be held back so the final-detection guard still gets to fire.  A text that
    already matches outright returns False — that case belongs to
    :func:`contains_no_reply_token`.
    """
    if contains_no_reply_token(text):
        return False
    norm = _normalize(text)
    if not norm:
        return False
    return any(form.startswith(norm) and form != norm for form in _COMPLETE_FORMS)
