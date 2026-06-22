import pytest
from clawchat_gateway.no_reply import is_no_reply_token, is_no_reply_token_prefix

ACCEPT = [
    "<clawchat:no-reply/>",
    "<clawchat:no-reply>",
    "[clawchat:no-reply]",
    "{clawchat:no-reply}",
    "  <clawchat:no-reply/>  ",
    "<CLAWCHAT:NO-REPLY/>",
    "clawchat:no-reply",
    "clawchat:silent",
    "<clawchat:silent/>",
    "[clawchat:silent]",
    # Whitespace variants around the colon / token boundary must still suppress
    # for BRACKETED forms (issue #2: a space after the colon used to leak the
    # token as chat text).  Brackets provide a terminator, so completing them
    # mid-stream is unambiguous and safe.
    "<clawchat: no-reply/>",
    "<clawchat :no-reply/>",
    "<clawchat : no-reply />",
    "<clawchat: silent/>",
    "[clawchat: no-reply]",
    "<CLAWCHAT: NO-REPLY/>",
]

REJECT = [
    "sure, replying",
    "<clawchat:no-reply/> and here is more",
    "the no-reply token is <clawchat:no-reply/>",
    "clawchat: no-reply please",
    # Whitespace tolerance must not start matching real chat text.
    "i will reply, no-reply is just a clawchat token",
    "clawchat colon no-reply",
    # P2 regression: the BARE, BRACKETLESS spaced form has NO terminator and can
    # be a strict prefix of longer real text (e.g. ``clawchat: no-reply please``),
    # so it must NOT be accepted as a COMPLETE token.  Only the byte-exact
    # canonical bare form (no internal/delimiter whitespace) is accepted.
    "clawchat: no-reply",
    "clawchat : no-reply",
    "clawchat: silent",
    "clawchat :silent",
    # Whitespace must be tolerated only AROUND delimiters, never INSIDE the
    # literal token words (P3): a space inside ``clawchat`` / ``silent`` or
    # around the hyphen inside ``no-reply`` is real model output, not a token.
    "<claw chat:no-reply/>",
    "<clawchat:si lent/>",
    "<clawchat:no - reply/>",
]


@pytest.mark.parametrize("s", ACCEPT)
def test_accept(s):
    assert is_no_reply_token(s) is True


@pytest.mark.parametrize("s", REJECT)
def test_reject(s):
    assert is_no_reply_token(s) is False


# A streaming first chunk that is a strict prefix of *any* accepted variant
# (bracket / case / spacing / silent) must be recognized so the suppression
# guard holds it instead of leaking the token to chat.
PREFIX_ACCEPT = [
    "<",
    "<clawchat",
    "<clawchat:no",
    "[clawchat",
    "[clawchat:no-reply",  # missing closing bracket -> still a prefix
    "{clawchat:sil",
    "<CLAWCHAT:NO",
    "<CLAWCHAT:NO-REPLY",
    "  <clawchat:no-rep",  # surrounding whitespace tolerated like the matcher
    "clawchat:no",
    "clawchat:silen",
]

# Complete tokens are NOT prefixes (they are handled by is_no_reply_token),
# and obviously-normal text is not a prefix either.
PREFIX_REJECT = [
    "",
    "   ",
    "<clawchat:no-reply/>",  # complete -> not a (strict) prefix
    "[clawchat:silent]",  # complete -> not a (strict) prefix
    "sure",
    "hello there",
    "<div>",
    "<clawchat:something",  # diverges from both no-reply and silent
]


@pytest.mark.parametrize("s", PREFIX_ACCEPT)
def test_prefix_accept(s):
    assert is_no_reply_token_prefix(s) is True


@pytest.mark.parametrize("s", PREFIX_REJECT)
def test_prefix_reject(s):
    assert is_no_reply_token_prefix(s) is False
