"""A direct-chat reply streams as it is written, and ends as one message.

With host streaming on, Hermes sends a reply's first chunk with a cursor and
then edits it with the cumulative text, finalizing at the end. The adapter used
to buffer all of that and send only the final message. Now, in a direct chat,
it streams per docs/client-integration.md §8: ``message.created``, then one
``message.add`` per edit (cumulative ``text`` plus the new ``delta``, sequence
from 0), then ``message.done`` and a ``message.reply`` that reuses the stream's
``message_id`` (§8.4), so the stored conversation keeps one message.

* Streaming frames are not acked; the final ``message.reply`` still is.
* Only the reply text streams. Tool progress, notices and reasoning are sent
  whole as "thinking" messages; a sediment turn never streams.
* Group replies are not streamed: the server's merged copy of a stream carries
  no mentions, and another agent would read that copy first.
* Text that might still become a no-reply token is held back. A stream whose
  reply turns out to be suppressed is closed with ``message.failed``; one whose
  text stops extending what was streamed is failed and the reply goes out
  whole under the same id.
* A reply the host never finalizes (``/stop``, ``/new``, a bubble left behind
  for a mid-turn commentary) is sent with the text it had once the turn is
  over — streamed or not.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter

AGENT = "usr_agent"
OWNER = "usr_owner"
DM = "cnv_dm"
GROUP = "cnv_group"
CURSOR = " ▉"
STREAM_EVENTS = {"message.created", "message.add", "message.done", "message.failed"}


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a.frames = []
    a.acked = []

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        a.frames.append(frame)
        a.acked.append(wait_for_ack)
        return True

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._known_chat_types[DM] = "direct"
    a._known_chat_types[GROUP] = "group"
    return a


def _events(frames):
    return [f["event"] for f in frames]


async def _stream(adapter, chat_id, *chunks, final=None):
    first = await adapter.send(chat_id, chunks[0] + CURSOR, reply_to="msg-in")
    for chunk in chunks[1:]:
        await adapter.edit_message(chat_id, first.message_id, chunk + CURSOR)
    if final is not None:
        await adapter.edit_message(chat_id, first.message_id, final, finalize=True)
        # Hermes sends the finalizing edit twice for REQUIRES_EDIT_FINALIZE.
        await adapter.edit_message(chat_id, first.message_id, final, finalize=True)
    return first.message_id


async def test_a_direct_reply_streams_then_ends_as_one_message(adapter):
    message_id = await _stream(adapter, DM, "Hello", "Hello, wor", final="Hello, world")
    assert _events(adapter.frames) == [
        "message.created",
        "message.add",
        "message.add",
        "message.done",
        "message.reply",
    ]
    assert {f["chat_id"] for f in adapter.frames} == {DM}
    assert {f["payload"]["message_id"] for f in adapter.frames} == {message_id}
    created, add0, add1, done, reply = adapter.frames
    assert created["payload"]["message_mode"] == "normal"
    assert add0["payload"]["sequence"] == 0 and add1["payload"]["sequence"] == 1
    assert add0["payload"]["fragments"] == [{"kind": "text", "text": "Hello", "delta": "Hello"}]
    assert add1["payload"]["fragments"] == [{"kind": "text", "text": "Hello, wor", "delta": ", wor"}]
    assert add1["payload"]["mutation"] == {"type": "append", "target_fragment_index": 0}
    assert add1["payload"]["streaming"]["status"] == "streaming"
    assert add1["payload"]["streaming"]["mutation_policy"] == "append_text_only"
    assert done["payload"]["fragments"] == [{"kind": "text", "text": "Hello, world"}]
    assert done["payload"]["streaming"]["status"] == "done"
    assert done["payload"]["streaming"]["sequence"] == 1
    assert reply["payload"]["message"]["body"]["fragments"] == [{"kind": "text", "text": "Hello, world"}]
    assert reply["payload"]["message_mode"] == "normal"
    assert reply["payload"]["message"]["context"]["reply"]["reply_to_msg_id"] == "msg-in"
    # Only the materialized reply waits for an ack (§8.5).
    assert adapter.acked == [False, False, False, False, True]


async def test_unchanged_edits_add_nothing(adapter):
    await _stream(adapter, DM, "Hello", "Hello", "Hello there")
    assert _events(adapter.frames) == ["message.created", "message.add", "message.add"]


async def test_group_replies_are_not_streamed(adapter):
    await _stream(adapter, GROUP, "Hello", "Hello, wor", final="Hello, world")
    assert _events(adapter.frames) == ["message.reply"]


async def test_a_possible_no_reply_token_is_held_back(adapter):
    # The plugin's own token; Hermes holds back its bare markers itself.
    await _stream(adapter, DM, "[claw", "[clawchat:no-re", final="[clawchat:no-reply]")
    assert adapter.frames == []


async def test_a_stream_whose_reply_is_suppressed_is_failed(adapter):
    message_id = await _stream(adapter, DM, "Let me think", final="NO_REPLY")
    assert _events(adapter.frames) == ["message.created", "message.add", "message.failed"]
    failed = adapter.frames[-1]["payload"]
    assert failed["message_id"] == message_id
    assert failed["streaming"]["status"] == "failed"


async def test_rewritten_text_fails_the_stream_and_the_reply_still_arrives(adapter):
    message_id = await _stream(adapter, DM, "Hello", "Goodbye", "Goodbye all", final="Goodbye all")
    assert _events(adapter.frames) == [
        "message.created",
        "message.add",
        "message.failed",
        "message.reply",
    ]
    assert adapter.frames[-1]["payload"]["message_id"] == message_id
    assert adapter.frames[-1]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "Goodbye all"}
    ]


async def test_a_sediment_turn_never_streams(adapter):
    adapter._sediment_chats.add(DM)
    await _stream(adapter, DM, "Saving notes", final="Saved")
    assert adapter.frames == []


async def test_a_stream_left_open_is_closed_after_the_turn(adapter, monkeypatch):
    monkeypatch.setattr(adapter_mod, "STREAM_ABANDON_GRACE_SECONDS", 0.0)
    message_id = await _stream(adapter, DM, "Partial ans", "Partial answer")
    await adapter.stop_typing(DM)
    await adapter._drain_stream_sweeps()
    stream = [f for f in adapter.frames if f["event"] != "typing.update"]
    assert _events(stream) == [
        "message.created",
        "message.add",
        "message.add",
        "message.done",
        "message.reply",
    ]
    assert stream[-1]["payload"]["message_id"] == message_id
    assert stream[-1]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "Partial answer"}
    ]


async def test_an_unfinished_group_reply_is_sent_after_the_turn(adapter, monkeypatch):
    monkeypatch.setattr(adapter_mod, "STREAM_ABANDON_GRACE_SECONDS", 0.0)
    await _stream(adapter, GROUP, "Partial ans", "Partial answer")
    await adapter.stop_typing(GROUP)
    await adapter._drain_stream_sweeps()
    replies = [f for f in adapter.frames if f["event"] == "message.reply"]
    assert [r["payload"]["message"]["body"]["fragments"] for r in replies] == [
        [{"kind": "text", "text": "Partial answer"}]
    ]


async def test_a_later_turn_is_not_swept(adapter, monkeypatch):
    monkeypatch.setattr(adapter_mod, "STREAM_ABANDON_GRACE_SECONDS", 0.0)
    await adapter.stop_typing(DM)
    await _stream(adapter, DM, "New turn")
    await adapter._drain_stream_sweeps()
    assert "message.reply" not in _events(adapter.frames)


async def test_a_reply_that_becomes_an_approval_card_is_not_streamed(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, enable_rich_interactions=True)
    await _stream(adapter, DM, "Run it? Reply /approve or /deny", final="Run it? Reply /approve or /deny")
    assert "message.created" not in _events(adapter.frames)


async def test_a_finalized_stream_is_left_alone_after_the_turn(adapter, monkeypatch):
    monkeypatch.setattr(adapter_mod, "STREAM_ABANDON_GRACE_SECONDS", 0.0)
    await _stream(adapter, DM, "Hello", final="Hello!")
    await adapter.stop_typing(DM)
    await adapter._drain_stream_sweeps()
    assert [e for e in _events(adapter.frames) if e != "typing.update"] == [
        "message.created",
        "message.add",
        "message.done",
        "message.reply",
    ]


async def test_reasoning_split_off_a_streamed_reply_goes_first(adapter):
    final = "💭 **Reasoning:**\n```\nsum\n```\n\nThe sum is 4."
    await _stream(adapter, DM, "The sum", final=final)
    events = _events(adapter.frames)
    assert events[:2] == ["message.created", "message.add"]
    thinking = [f for f in adapter.frames if f["event"] == "message.reply" and f["payload"]["message_mode"] == "thinking"]
    assert len(thinking) == 1
    assert events[-2:] == ["message.done", "message.reply"]
    assert adapter.frames[-1]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "The sum is 4."}
    ]


# --- host streaming switch ----------------------------------------------------
#
# Hermes only drives the send-then-edit stream when
# display.platforms.clawchat.streaming is on. Activation and every
# /clawchat-output preset now write true; plugin load turns the false that
# earlier releases wrote into true once (marker extra.reply_streaming_enabled),
# so an operator who sets it back to false afterwards keeps false.

from clawchat_gateway import activate  # noqa: E402
from clawchat_gateway.output_visibility import DISPLAY_PRESETS  # noqa: E402


@pytest.fixture
def fake_config(monkeypatch, tmp_path):
    state: dict = {"config": {}, "writes": 0}

    def load():
        return tmp_path / "config.yaml", state["config"]

    def write(_path, config):
        state["config"] = config
        state["writes"] += 1

    monkeypatch.setattr(activate, "_load_config", load)
    monkeypatch.setattr(activate, "_write_config", write)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return state


def test_every_output_preset_and_activation_turn_host_streaming_on():
    assert all(preset["streaming"] is True for preset in DISPLAY_PRESETS.values())
    assert activate.CLAWCHAT_DISPLAY_DEFAULTS["streaming"] is True


def _installed(streaming, **extra):
    return {
        "platforms": {"clawchat": {"extra": dict(extra)}},
        "display": {"platforms": {"clawchat": {"streaming": streaming}}},
        "memory": {"user_profile_enabled": False, "nudge_interval": 0},
        "compression": {"threshold_tokens": 150000},
    }


def test_load_turns_the_old_false_on_once(fake_config):
    fake_config["config"] = _installed(False)
    activate.ensure_clawchat_host_defaults_on_load()
    config = fake_config["config"]
    assert config["display"]["platforms"]["clawchat"]["streaming"] is True
    assert config["platforms"]["clawchat"]["extra"]["reply_streaming_enabled"] is True
    assert fake_config["writes"] == 1

    # The operator turns it off again: later loads keep their choice.
    config["display"]["platforms"]["clawchat"]["streaming"] = False
    activate.ensure_clawchat_host_defaults_on_load()
    assert fake_config["config"]["display"]["platforms"]["clawchat"]["streaming"] is False
    assert fake_config["writes"] == 1


def test_load_leaves_an_unactivated_config_alone(fake_config):
    fake_config["config"] = {
        "display": {"platforms": {"clawchat": {"streaming": False}}},
        "memory": {"user_profile_enabled": False, "nudge_interval": 0},
        "compression": {"threshold_tokens": 150000},
    }
    activate.ensure_clawchat_host_defaults_on_load()
    assert fake_config["config"]["display"]["platforms"]["clawchat"]["streaming"] is False
    assert fake_config["writes"] == 0
