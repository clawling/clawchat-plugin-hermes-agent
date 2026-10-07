"""Rule-B silence markers: suppressed only as the whole (trimmed) reply.

Mirrors the OpenClaw plugin's ``src/no-reply.test.ts`` rule-B cases; the two
``no_reply`` modules must stay literal mirrors of each other.
"""

import pytest

from clawchat_gateway.no_reply import contains_no_reply_token, is_host_silence_marker

RULE_B_ACCEPT = [
    "NO_REPLY",
    "[SILENT]",
    "SILENT",
    "  no reply  ",
    ".NO_REPLY",
    "*NO_REPLY*",
    # The OpenClaw host's heartbeat ack; a run that answers only this must not
    # reach the chat as a message.
    "HEARTBEAT_OK",
    "  heartbeat_ok\n",
    "HEARTBEAT_OK.",
]
RULE_B_REJECT = [
    "好的,NO_REPLY",
    "HEARTBEAT_OK — all checks passed, nothing to report",
    "The host answered HEARTBEAT_OK",
    "there is no reply from the server",
    "[SILENT",
    "",
    f"NO_REPLY {'x' * 64}",
]


@pytest.mark.parametrize("text", RULE_B_ACCEPT)
def test_rule_b_accepts(text):
    assert is_host_silence_marker(text)
    assert contains_no_reply_token(text)


@pytest.mark.parametrize("text", RULE_B_REJECT)
def test_rule_b_rejects(text):
    assert not is_host_silence_marker(text)
