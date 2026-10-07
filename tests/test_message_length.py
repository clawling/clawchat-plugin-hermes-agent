"""A long reply goes out as one message, and that message always fits the hub.

Hermes splits a reply longer than the adapter's ``MAX_MESSAGE_LENGTH`` into
several messages before handing them to ``send``. At 4000 characters a long
answer arrived as a run of separate bubbles. The hub's real limit is in bytes:
it refuses a ``message.send`` / ``message.reply`` whose marshaled downlink
envelope exceeds its per-message produce cap (1 000 000 bytes by default)
with a terminal ``message_too_large``
(docs/client-integration.md §14.3), and the WebSocket frame cap (8 MiB) is
larger than that. 32000 characters stays under the produce cap with a wide
margin even in the worst case: a character the hub re-encodes as a 6-byte JSON
escape (Go escapes ``<``, ``>`` and ``&``), plus the envelope and a reply
preview.
"""

from __future__ import annotations

import json

from clawchat_gateway.adapter import REPLY_PREVIEW_TEXT_MAX, ClawChatAdapter
from clawchat_gateway.protocol import build_message_reply_event

HUB_PRODUCE_CAP_BYTES = 1_000_000


def _go_marshaled_size(frame: dict) -> int:
    # Go's encoding/json writes UTF-8 and escapes <, > and & as \\u003c etc.
    text = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
    extra = 5 * sum(text.count(c) for c in "<>&")
    return len(text.encode("utf-8")) + extra


def test_single_message_limit_is_raised_past_the_old_4000():
    assert ClawChatAdapter.MAX_MESSAGE_LENGTH == 32000


def test_worst_case_full_length_reply_fits_the_hub_produce_cap():
    limit = ClawChatAdapter.MAX_MESSAGE_LENGTH
    for filler in ("<", "😀", "字"):
        frame = build_message_reply_event(
            chat_id="cnv_" + "x" * 26,
            chat_type="group",
            message_id="msg-" + "0" * 26,
            fragments=[{"kind": "text", "text": filler * limit}],
            reply_to_message_id="msg-" + "1" * 26,
            reply_preview={
                "id": "usr_" + "y" * 26,
                "nick_name": "n" * 64,
                "fragments": [{"kind": "text", "text": filler * REPLY_PREVIEW_TEXT_MAX}],
            },
            include_message_id=True,
        )
        # Room for the server-stamped envelope fields (sender, to, seq, ...).
        assert _go_marshaled_size(frame) + 4096 < HUB_PRODUCE_CAP_BYTES / 4
