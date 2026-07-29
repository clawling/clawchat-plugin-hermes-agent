import pytest

from clawchat_gateway.no_reply import (
    _COMPLETE_FORMS,
    _normalize,
    contains_no_reply_token,
    is_host_silence_marker,
    is_no_reply_token_prefix,
    strip_no_reply_tokens,
)

# --- 规则 A:子串,大小写不敏感 -------------------------------------------
RULE_A_ACCEPT = [
    "<clawchat:no-reply/>",
    "clawchat:no-reply",
    "[CLAWCHAT:SILENT]",
    "clawchat: no_reply",
    "`clawchat:noreply`",
    "**clawchat: no reply**",
    "<clawchat:no-reply/> and here is more",
    "the no-reply token is <clawchat:no-reply/>",
    "clawchat: no-reply please",
    "好的,我不打扰了 <clawchat:no-reply/>",
]

RULE_A_REJECT = [
    "claw chat:no-reply",
    "clawchat: no replying to that",
    "clawchat:silently",
    "clawchat 不回复",
    "sure, replying",
]


@pytest.mark.parametrize("s", RULE_A_ACCEPT)
def test_rule_a_accept(s):
    assert contains_no_reply_token(s) is True


@pytest.mark.parametrize("s", RULE_A_REJECT)
def test_rule_a_reject(s):
    assert contains_no_reply_token(s) is False


# --- 规则 B:整条精确 ------------------------------------------------------
RULE_B_ACCEPT = [
    "NO_REPLY",
    "[SILENT]",
    "SILENT",
    "  no reply  ",
    ".NO_REPLY",
    "*NO_REPLY*",
]

RULE_B_REJECT = [
    "好的,NO_REPLY",
    "there is no reply from the server",
    "[SILENT",
    "",
    "NO_REPLY " + "x" * 64,
]


@pytest.mark.parametrize("s", RULE_B_ACCEPT)
def test_rule_b_accept(s):
    assert is_host_silence_marker(s) is True
    assert contains_no_reply_token(s) is True


@pytest.mark.parametrize("s", RULE_B_REJECT)
def test_rule_b_reject(s):
    assert is_host_silence_marker(s) is False


def test_rule_b_length_cap_is_64():
    assert is_host_silence_marker("NO_REPLY".ljust(64)) is True
    assert is_host_silence_marker("[SILENT]" + "." * 60) is False


# --- 剥离 ------------------------------------------------------------------
def test_strip_removes_token_and_decorations():
    assert strip_no_reply_tokens("好的 <clawchat:no-reply/>") == "好的"


def test_strip_whole_token_yields_empty():
    assert strip_no_reply_tokens("<clawchat:no-reply/>") == ""


def test_strip_does_not_glue_neighbouring_words():
    assert strip_no_reply_tokens("a <clawchat:silent/> b") == "a  b"


def test_strip_host_marker_yields_empty():
    assert strip_no_reply_tokens("*NO_REPLY*") == ""


def test_strip_leaves_unrelated_text_untouched():
    assert strip_no_reply_tokens("there is no reply from the server") == (
        "there is no reply from the server"
    )


# --- 前缀守卫 --------------------------------------------------------------
PREFIX_ACCEPT = [
    "<",
    "<clawchat",
    "<clawchat:no-re",
    "[CLAWCHAT:SIL",
    "clawchat:no",
]

PREFIX_REJECT = [
    "",
    "<clawchat:no-reply/>",   # 完整 token 归 contains_no_reply_token 管
    "NO",                      # 宿主 marker 不参与前缀守卫
    "hello there",
]


@pytest.mark.parametrize("s", PREFIX_ACCEPT)
def test_prefix_accept(s):
    assert is_no_reply_token_prefix(s) is True


@pytest.mark.parametrize("s", PREFIX_REJECT)
def test_prefix_reject(s):
    assert is_no_reply_token_prefix(s) is False


# --- lockstep 不变式 -------------------------------------------------------
def test_every_complete_form_is_matched():
    for form in _COMPLETE_FORMS:
        assert contains_no_reply_token(form), f"complete form not matched: {form!r}"
        assert _normalize(form) == form, f"form is not its own normal form: {form!r}"


def test_complete_form_is_never_a_prefix():
    for form in _COMPLETE_FORMS:
        assert is_no_reply_token_prefix(form) is False
