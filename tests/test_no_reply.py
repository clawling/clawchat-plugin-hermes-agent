import pytest
from clawchat_gateway.no_reply import is_no_reply_token

ACCEPT = [
    "<clawchat:no-reply/>",
    "<clawchat:no-reply>",
    "[clawchat:no-reply]",
    "{clawchat:no-reply}",
    "  <clawchat:no-reply/>  ",
    "<CLAWCHAT:NO-REPLY/>",
    "clawchat:no-reply",
    "<clawchat:silent/>",
    "[clawchat:silent]",
]

REJECT = [
    "sure, replying",
    "<clawchat:no-reply/> and here is more",
    "the no-reply token is <clawchat:no-reply/>",
    "clawchat: no-reply please",
]


@pytest.mark.parametrize("s", ACCEPT)
def test_accept(s):
    assert is_no_reply_token(s) is True


@pytest.mark.parametrize("s", REJECT)
def test_reject(s):
    assert is_no_reply_token(s) is False
