import re

# Two accepted shapes, with deliberately different whitespace rules:
#
# 1. BRACKETED form (``<...>`` / ``[...]`` / ``{...}``): whitespace is tolerated
#    ONLY at delimiter positions — around the opening / closing brackets, around
#    the ``:`` between ``clawchat`` and the verb, and around the optional
#    trailing ``/``.  The closing bracket is a TERMINATOR, so completing one of
#    these mid-stream is unambiguous and safe to suppress.
#
# 2. BARE, UNBRACKETED form: ONLY the byte-exact canonical token
#    (``clawchat:no-reply`` / ``clawchat:silent``, case-insensitive) with NO
#    internal or delimiter whitespace.  Surrounding whitespace (leading /
#    trailing) is still stripped, but NO space is allowed around the ``:`` and
#    no trailing ``/`` is allowed.  Rationale (P2): the bare form has no
#    terminator, so a spaced bare form like ``clawchat: no-reply`` is a strict
#    PREFIX of longer real text (``clawchat: no-reply please``); treating it as
#    a COMPLETE token would discard a streaming run whose later chunks carry
#    real content.  Only the exact glued form is unambiguous as a complete token.
#
# In neither shape is whitespace tolerated INSIDE the literal token words
# ``clawchat`` / ``no-reply`` / ``silent`` (so ``<claw chat:no-reply/>``,
# ``<clawchat:si lent/>`` and ``<clawchat:no - reply/>`` stay rejected — P3).
# ``no-reply`` keeps its hyphen glued: ``no\s*-\s*reply`` would over-match.
_RE = re.compile(
    r"^\s*(?:"
    # Bracketed: whitespace tolerated around the delimiters.
    r"[<\[{]\s*clawchat\s*:\s*(?:no-reply|silent)\s*/?\s*[>\]}]"
    r"|"
    # Bare: exact canonical token, no internal/delimiter whitespace.
    r"clawchat:(?:no-reply|silent)"
    r")\s*$",
    re.IGNORECASE,
)

# Canonical complete forms (lowercase, no surrounding/delimiter whitespace) of
# every accepted no-reply / silent variant.  A streaming first chunk is
# suppressed when it is a strict prefix of any of these.  This set MUST stay the
# EXACT image of what ``_RE`` accepts (after ``_normalize``) so the streaming
# guard and the final detection stay in lockstep (P3): if a form appears here
# that ``is_no_reply_token`` would reject, ``is_no_reply_token_prefix`` flags it
# (or strings ending in it) as suppressible and the finalize path DROPS real
# model text.  Built directly from the two accepted shapes:
#
#   * BRACKETED: any open bracket + ``clawchat:<verb>`` + optional ``/`` + any
#     close bracket (the brackets need not pair, matching ``_RE``).
#   * BARE: ONLY the byte-exact ``clawchat:no-reply`` / ``clawchat:silent`` — no
#     trailing ``/`` and no bracket (a bare trailing slash is rejected by ``_RE``
#     and is NOT a prefix of any accepted form, so it must not be held).
_TOKENS = ["clawchat:no-reply", "clawchat:silent"]
_BRACKET_OPEN = ["<", "[", "{"]
_BRACKET_CLOSE = [">", "]", "}"]
_BRACKET_SLASH = ["", "/"]
_COMPLETE_FORMS = frozenset(
    [f"{o}{tok}{s}{c}"
     for o in _BRACKET_OPEN
     for tok in _TOKENS
     for s in _BRACKET_SLASH
     for c in _BRACKET_CLOSE]
    + list(_TOKENS)  # bare, byte-exact only
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
