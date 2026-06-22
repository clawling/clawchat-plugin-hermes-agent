import re

_RE = re.compile(r"^[<\[{]?\s*clawchat:(no-reply|silent)\s*/?\s*[>\]}]?$")

# Canonical complete forms (whitespace-collapsed, lowercase) of every accepted
# no-reply / silent variant.  A streaming first chunk is suppressed when it is a
# strict prefix of any of these.  Built from the same bracket / token alphabet as
# ``_RE`` so the streaming guard and the final detection stay in lockstep.
_OPEN = ["", "<", "[", "{"]
_CLOSE = ["", "/", ">", "]", "}", "/>", "/]", "/}"]
_TOKENS = ["clawchat:no-reply", "clawchat:silent"]
_COMPLETE_FORMS = frozenset(
    f"{o}{tok}{c}" for o in _OPEN for tok in _TOKENS for c in _CLOSE
)


def _normalize(text: str) -> str:
    """Lowercase, strip, and collapse all internal whitespace (mirrors ``_RE``)."""
    return re.sub(r"\s+", "", text.strip().lower())


def is_no_reply_token(text: str) -> bool:
    # Normalize first (collapse all internal whitespace, lowercase, strip) so
    # spacing variants like ``<clawchat: no-reply/>`` or ``<clawchat :no-reply/>``
    # are recognized and suppressed.  Collapsing whitespace cannot turn real chat
    # text into a match: ``_RE`` is anchored (``^...$``), so anything with extra
    # words outside the bracketed token (e.g. ``clawchat: no-reply please``) still
    # fails to match after the collapse.
    return bool(_RE.match(_normalize(text)))


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
