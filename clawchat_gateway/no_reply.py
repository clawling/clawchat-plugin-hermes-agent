import re

# Whitespace is tolerated ONLY at delimiter positions: around the optional
# opening / closing brackets, around the ``:`` between ``clawchat`` and the
# verb, and around the optional trailing ``/``.  It is *never* tolerated inside
# the literal token words ``clawchat`` / ``no-reply`` / ``silent`` (so model
# output like ``<claw chat:no-reply/>``, ``<clawchat:si lent/>`` or
# ``<clawchat:no - reply/>`` is NOT suppressed).  ``no-reply`` keeps its hyphen
# glued: ``no\s*-\s*reply`` would over-match, so we leave it as a literal.
_RE = re.compile(
    r"^\s*[<\[{]?\s*clawchat\s*:\s*(no-reply|silent)\s*/?\s*[>\]}]?\s*$",
    re.IGNORECASE,
)

# Canonical complete forms (lowercase, no surrounding/delimiter whitespace) of
# every accepted no-reply / silent variant.  A streaming first chunk is
# suppressed when it is a strict prefix of any of these.  Built from the same
# bracket / token alphabet as ``_RE`` so the streaming guard and the final
# detection stay in lockstep.
_OPEN = ["", "<", "[", "{"]
_CLOSE = ["", "/", ">", "]", "}", "/>", "/]", "/}"]
_TOKENS = ["clawchat:no-reply", "clawchat:silent"]
_COMPLETE_FORMS = frozenset(
    f"{o}{tok}{c}" for o in _OPEN for tok in _TOKENS for c in _CLOSE
)


def _normalize(text: str) -> str:
    """Lowercase, strip, and collapse whitespace ONLY around delimiters.

    Delimiters are the optional brackets ``<[{``/``>]}``, the ``:`` separator,
    and the trailing ``/``.  Whitespace inside the literal token words
    (``clawchat`` / ``no-reply`` / ``silent``) is preserved so it cannot be
    collapsed into a spurious match.  Used by the streaming prefix guard so it
    stays consistent with ``_RE``.
    """
    s = text.strip().lower()
    # Drop whitespace that sits adjacent to a delimiter char only.
    return re.sub(r"\s*([<\[{>\]}:/])\s*", r"\1", s)


def is_no_reply_token(text: str) -> bool:
    # ``_RE`` is anchored (``^...$``, case-insensitive) and only allows
    # whitespace around the delimiters, never inside the literal token words.
    # So spacing variants like ``<clawchat: no-reply/>`` are suppressed while
    # word-internal spaces (``<claw chat:...>``, ``<clawchat:si lent/>``) and
    # trailing chat text (``clawchat: no-reply please``) are not.
    return bool(_RE.match(text))


def is_no_reply_token_prefix(text: str) -> bool:
    """True when *text* is a non-empty, strict prefix of some accepted token.

    Used by the streaming output path: the first partial chunk of a no-reply /
    silent token (in any bracket / case / spacing variant) must be held back so
    the final-detection suppression guard still fires.  A complete token returns
    ``False`` here — that case is owned by :func:`is_no_reply_token`.
    """
    norm = _normalize(text)
    if not norm:
        return False
    return any(
        form.startswith(norm) and form != norm for form in _COMPLETE_FORMS
    )
