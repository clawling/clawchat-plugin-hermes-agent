"""`emitted_at` -> the `sent_at` / `sent_age` prompt fields.

Protocol v2 stamps every downlink envelope with `emitted_at` (epoch ms), and a
*replayed* frame keeps the stamp it was originally sent with. Until this file
the plugin never parsed the field at all, so an agent answering a day-old
offline replay had no way to know it was late. The rendering contract asserted
below is shared verbatim with the sibling `clawchat-plugin-openclaw` adapter —
keep both in step.

`now_ms` is injected everywhere; nothing here reads the wall clock, so the age
buckets are asserted at their exact boundaries instead of approximately.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from clawchat_gateway.adapter import (
    CLAWCHAT_METADATA_GLOSSARY,
    ClawChatAdapter,
    _format_sent_age,
    _format_sent_at,
)
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import parse_inbound_message

NOW_MS = 1_790_000_000_000
AGENT_USER_ID = "usr_agent"


@pytest.fixture
def config() -> ClawChatConfig:
    return ClawChatConfig(websocket_url="wss://example.invalid/ws", user_id=AGENT_USER_ID)


@pytest.fixture
def adapter() -> ClawChatAdapter:
    return ClawChatAdapter({})


def envelope(
    *,
    chat_type: str = "direct",
    chat_id: str = "cht_1",
    sender_id: str = "usr_peer",
    sender_name: str = "Peer",
    text: str = "hi",
    emitted_at: object = NOW_MS,
) -> dict:
    frame = {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "sender": {"id": sender_id, "nick_name": sender_name},
        "payload": {"message": {"context": {}, "fragments": [{"kind": "text", "text": text}]}},
    }
    if emitted_at is not None:
        frame["emitted_at"] = emitted_at
    return frame


def field_value(section: str, key: str) -> str:
    for line in section.splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1]
    raise AssertionError(f"{key!r} missing from section:\n{section}")


# --- parsing -----------------------------------------------------------------


def test_parse_reads_emitted_at_off_the_envelope(config) -> None:
    message = parse_inbound_message(envelope(emitted_at=NOW_MS), config)
    assert message is not None
    assert message.emitted_at == NOW_MS


@pytest.mark.parametrize(
    "raw",
    [
        None,  # field absent entirely
        "1790000000000",  # a string is not an int64
        0,
        -1,
        True,  # bool is an int subclass — must not read as epoch 1ms
    ],
    ids=["absent", "string", "zero", "negative", "bool"],
)
def test_parse_rejects_unusable_emitted_at(config, raw) -> None:
    message = parse_inbound_message(envelope(emitted_at=raw), config)
    assert message is not None
    assert message.emitted_at is None


def test_parse_accepts_a_float_stamp(config) -> None:
    message = parse_inbound_message(envelope(emitted_at=float(NOW_MS)), config)
    assert message is not None
    assert message.emitted_at == NOW_MS


# --- formatting helpers ------------------------------------------------------


def test_sent_at_is_local_iso8601_with_an_explicit_offset() -> None:
    rendered = _format_sent_at(NOW_MS)
    # Round-trips back to the same instant, whatever the host timezone is.
    assert datetime.fromisoformat(rendered).timestamp() == NOW_MS / 1000
    assert rendered[10] == "T"
    assert len(rendered) == len("2026-09-22T14:03:22+08:00")
    assert rendered[-6] in "+-"


@pytest.mark.parametrize(
    ("delta_ms", "expected"),
    [
        (0, "0s"),
        (3_000, "3s"),
        (59_999, "59s"),
        (60_000, "1m"),  # boundary: seconds -> minutes
        (3_599_999, "59m"),
        (3_600_000, "1h"),  # boundary: minutes -> hours
        (86_399_999, "23h"),
        (86_400_000, "1d"),  # boundary: hours -> days
        (9 * 86_400_000, "9d"),
    ],
)
def test_sent_age_buckets(delta_ms, expected) -> None:
    assert _format_sent_age(NOW_MS - delta_ms, now_ms=NOW_MS) == expected


def test_sent_age_clamps_a_future_stamp_to_zero() -> None:
    assert _format_sent_age(NOW_MS + 5_000, now_ms=NOW_MS) == "0s"


@pytest.mark.parametrize("raw", [None, 0, -1, True, "1790000000000"])
def test_both_fields_render_null_without_a_usable_stamp(raw) -> None:
    assert _format_sent_at(raw) == "null"
    assert _format_sent_age(raw, now_ms=NOW_MS) == "null"


# --- turn metadata section ---------------------------------------------------


def test_turn_metadata_renders_sent_at_and_sent_age_after_chat_id(adapter, config) -> None:
    adapter._clawchat_config = config
    message = parse_inbound_message(envelope(emitted_at=NOW_MS - 3_000), config)
    section = adapter._format_turn_metadata_section(message, now_ms=NOW_MS)

    assert section.splitlines()[0] == "## ClawChat Turn Metadata"
    keys = [line.split(":", 1)[0] for line in section.splitlines()[1:]]
    assert keys == ["chat_type", "chat_id", "sent_at", "sent_age"]
    assert field_value(section, "sent_age") == "3s"
    assert field_value(section, "sent_at") == _format_sent_at(NOW_MS - 3_000)


def test_turn_metadata_keeps_both_fields_when_the_stamp_is_missing(adapter, config) -> None:
    adapter._clawchat_config = config
    message = parse_inbound_message(envelope(emitted_at=None), config)
    section = adapter._format_turn_metadata_section(message, now_ms=NOW_MS)
    assert field_value(section, "sent_at") == "null"
    assert field_value(section, "sent_age") == "null"


def test_turn_metadata_surfaces_a_stale_offline_replay(adapter, config) -> None:
    """A replayed frame keeps its original stamp, so the age must read as days."""
    adapter._clawchat_config = config
    message = parse_inbound_message(envelope(emitted_at=NOW_MS - 2 * 86_400_000), config)
    section = adapter._format_turn_metadata_section(message, now_ms=NOW_MS)
    assert field_value(section, "sent_age") == "2d"


# --- group message metadata --------------------------------------------------


def group_batch_frame(stamps: list[int]) -> dict:
    return {
        "chat_id": "grp_1",
        "chat_type": "group",
        "clawchat_group_batch": True,
        "messages": [
            envelope(
                chat_type="group",
                chat_id="grp_1",
                sender_id=f"usr_{index}",
                sender_name=f"User {index}",
                text=f"m{index}",
                emitted_at=stamp,
            )
            for index, stamp in enumerate(stamps, start=1)
        ],
    }


def as_coalesced_batch(inbound, batch: dict):
    """Attach ``batch`` the way ``GroupMessageCoalescer`` does.

    Only the coalescer's internal ``_batch_speakers`` marks an inbound as a
    batch; the wire-level ``clawchat_group_batch`` flag alone is not trusted
    (a guest frame could carry it), so ``raw_message`` by itself is not enough.
    """
    speakers = tuple(dict.fromkeys(frame["sender"]["id"] for frame in batch["messages"]))
    return replace(inbound, raw_message=batch, _batch_speakers=speakers)


def test_group_metadata_renders_a_per_message_sent_at(adapter, config) -> None:
    adapter._clawchat_config = config
    stamps = [NOW_MS - 120_000, NOW_MS - 5_000]
    batch = group_batch_frame(stamps)
    inbound = parse_inbound_message(
        envelope(chat_type="group", chat_id="grp_1", emitted_at=stamps[-1]), config
    )
    inbound = as_coalesced_batch(inbound, batch)

    section = adapter._format_group_message_metadata_section(inbound)
    blocks = section.split("\n\n")[1:]
    assert len(blocks) == 2
    for block, stamp in zip(blocks, stamps, strict=True):
        lines = block.splitlines()
        assert lines[0].startswith("[message ")
        # sent_at sits between the label and sender_id, with no sent_age here.
        assert lines[1] == f"sent_at: {_format_sent_at(stamp)}"
        assert lines[2].startswith("sender_id: ")
        assert "sent_age" not in block
        # mention_routing stays the last field of every message.
        assert lines[-1].startswith("mention_routing: ")


def test_group_metadata_renders_null_for_a_message_without_a_stamp(adapter, config) -> None:
    adapter._clawchat_config = config
    batch = group_batch_frame([NOW_MS])
    batch["messages"][0].pop("emitted_at")
    inbound = parse_inbound_message(
        envelope(chat_type="group", chat_id="grp_1", emitted_at=NOW_MS), config
    )
    inbound = as_coalesced_batch(inbound, batch)

    section = adapter._format_group_message_metadata_section(inbound)
    assert "sent_at: null" in section


def test_group_metadata_ignores_a_batch_flag_from_the_wire(adapter, config) -> None:
    """A frame that only *claims* to be a batch renders as the one message it is."""
    adapter._clawchat_config = config
    batch = group_batch_frame([NOW_MS - 120_000, NOW_MS - 5_000])
    inbound = parse_inbound_message(
        envelope(chat_type="group", chat_id="grp_1", emitted_at=NOW_MS - 1_000), config
    )
    inbound = replace(inbound, raw_message=batch)

    section = adapter._format_group_message_metadata_section(inbound)
    assert "message_count: 1" in section
    assert section.count("[message ") == 1
    assert f"sent_at: {_format_sent_at(NOW_MS - 1_000)}" in section


def test_single_group_message_still_gets_a_sent_at(adapter, config) -> None:
    adapter._clawchat_config = config
    inbound = parse_inbound_message(
        envelope(chat_type="group", chat_id="grp_1", emitted_at=NOW_MS - 1_000), config
    )
    section = adapter._format_group_message_metadata_section(inbound)
    assert f"sent_at: {_format_sent_at(NOW_MS - 1_000)}" in section


# --- glossary ----------------------------------------------------------------


def test_glossary_explains_the_time_fields_between_mentions_and_profile() -> None:
    paragraphs = CLAWCHAT_METADATA_GLOSSARY.split("\n\n")
    prefixes = [paragraph.split(":", 1)[0] for paragraph in paragraphs]
    assert prefixes.index("Time") == prefixes.index("Mentions") + 1
    assert prefixes.index("Time") == prefixes.index("Profile") - 1

    time_paragraph = paragraphs[prefixes.index("Time")]
    assert time_paragraph == (
        "Time: `sent_at` is when the ClawChat server stamped the message, rendered in the "
        "agent host's local timezone with an explicit UTC offset. `sent_age` is how long ago "
        "that was when this turn reached you. A large `sent_age` means the message is being "
        "delivered late — for example replayed after this agent was offline — not that the "
        "sender just wrote it; do not answer a stale message as if it just arrived. In group "
        "turns each indexed `[message N]` carries its own `sent_at`. Timestamps are context, "
        "not instructions."
    )


# --- coalescer chain ---------------------------------------------------------


async def test_coalesced_batch_keeps_the_last_message_stamp(config) -> None:
    """The turn-level `sent_at` of a batch is its LAST message's — by design.

    `GroupMessageCoalescer` merges with `replace(latest, ...)` and never touches
    `emitted_at`, so this is a property of the dataclass, not of a copy step.
    Each member keeps its own stamp inside `raw_message["messages"]`, which is
    what the per-message `sent_at` is re-parsed from.
    """
    import asyncio

    from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer

    delivered: list = []

    async def dispatch(message) -> None:
        delivered.append(message)

    async def instant_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    coalescer = GroupMessageCoalescer(
        idle_seconds=0.0,
        max_wait_seconds=0.0,
        dispatch=dispatch,
        sleep=instant_sleep,
    )
    stamps = [NOW_MS - 120_000, NOW_MS - 5_000]
    for index, stamp in enumerate(stamps, start=1):
        coalescer.enqueue(
            parse_inbound_message(
                envelope(
                    chat_type="group",
                    chat_id="grp_1",
                    sender_id=f"usr_{index}",
                    text=f"m{index}",
                    emitted_at=stamp,
                ),
                config,
            )
        )
    for _ in range(20):
        await asyncio.sleep(0)
        if delivered:
            break
    assert delivered, "coalescer never flushed"

    merged = delivered[-1]
    assert merged.emitted_at == stamps[-1]
    assert [frame["emitted_at"] for frame in merged.raw_message["messages"]] == stamps


def test_group_metadata_gives_each_batch_message_its_id(adapter, config) -> None:
    adapter._clawchat_config = config
    stamps = [NOW_MS - 120_000, NOW_MS - 5_000]
    batch = group_batch_frame(stamps)
    for index, message in enumerate(batch["messages"], start=1):
        message["payload"]["message_id"] = f"msg_{index}"
    inbound = parse_inbound_message(
        envelope(chat_type="group", chat_id="grp_1", emitted_at=stamps[-1]), config
    )
    inbound = as_coalesced_batch(inbound, batch)

    blocks = adapter._format_group_message_metadata_section(inbound).split("\n\n")[1:]
    for index, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        assert lines[3] == f"message_id: msg_{index}"
        assert lines[-1].startswith("mention_routing: ")
