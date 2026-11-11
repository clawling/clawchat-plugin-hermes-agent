"""Opt-in: a direct-chat reply streams as it is written, and ends as one message.

Streaming is experimental and off by default (``extra.stream_replies``), because
not every ClawChat client renders §8 streams yet. While it is off the adapter
never sends a streaming frame, even when an operator turned Hermes' own
``display.platforms.clawchat.streaming`` on: the host's send-then-edit drafts
are buffered and the reply goes out once as a plain ``message.reply``, and a
reply the host never finalizes is dropped, as before reply streaming existed.
Activation and every ``/clawchat-output`` preset write host streaming ``false``.

With the opt-in and host streaming on, Hermes sends a reply's first chunk with a cursor and
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
  whole under the same id. ``message.failed`` only withdraws a preview, so a run
  that fails mid-stream is followed by a plain notice that the reply broke off,
  and a reply already sent under the stream's id is never withdrawn.
* With the opt-in on, a reply the host never finalizes (``/stop``, ``/new``, a
  bubble left behind for a mid-turn commentary) is sent with the text it had
  once the turn is over — streamed or not.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.connection import ConnectionState

AGENT = "usr_agent"
OWNER = "usr_owner"
DM = "cnv_dm"
GROUP = "cnv_group"
CURSOR = " ▉"
STREAM_EVENTS = {"message.created", "message.add", "message.done", "message.failed"}


def _make_adapter(monkeypatch, tmp_path, *, stream_replies):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    assert a._clawchat_config.stream_replies is False  # the default
    a._clawchat_config = replace(
        a._clawchat_config, user_id=AGENT, owner_user_id=OWNER, stream_replies=stream_replies
    )
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


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    """Streaming opted in (``extra.stream_replies: true``)."""
    return _make_adapter(monkeypatch, tmp_path, stream_replies=True)


@pytest.fixture
def plain_adapter(monkeypatch, tmp_path):
    """The default: streaming not opted in."""
    return _make_adapter(monkeypatch, tmp_path, stream_replies=False)


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


@pytest.mark.parametrize("event", ["message.created", "message.add"])
@pytest.mark.parametrize("payload", [
    {"code": "agent_turn_limit", "retryable": False},
    {"code": "agent_muted", "retryable": False},
    {"code": "future_code", "retryable": True},
    {"code": "send_rate_limited", "retryable": False},
    {"code": "send_rate_limited"},
    {"code": "send_rate_limited", "retryable": "true"},
])
async def test_terminal_stream_rejection_stops_reply_and_host_recovery(
    adapter, monkeypatch, event, payload, caplog,
):
    send_frame = adapter._connection.send_frame
    records = []
    monkeypatch.setattr(adapter, "_update_message_record", lambda **record: records.append(record))

    async def reject(frame, **kwargs):
        sent = await send_frame(frame, **kwargs)
        if frame["event"] == event:
            await adapter._on_message_error({
                "chat_id": DM, "payload": dict(payload, message_id=frame["payload"]["message_id"]),
            })
        return sent

    monkeypatch.setattr(adapter._connection, "send_frame", reject)
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    run = adapter._active_runs_by_id[first.message_id]
    assert run.rejected_code == payload["code"]
    assert run.stream_failed and not run.stream_open
    frames = list(adapter.frames)
    assert _events(frames) == (["message.created"] if event == "message.created"
                               else ["message.created", "message.add"])
    await adapter._stream_update(run, "Hello again")
    result = await adapter.edit_message(DM, first.message_id, "Hello again", finalize=True)
    assert not result.success and result.error
    assert adapter.frames == frames  # No add, done, failed or final reply.
    assert first.message_id not in adapter._active_runs_by_id
    assert first.message_id in adapter._completed_run_ids
    assert records[-1]["event_type"] == "message.error"
    assert records[-1]["message_id"] == first.message_id
    assert "clawchat reply rejected" in caplog.text
    assert not adapter._server_rejected_chats
    key = adapter._reply_content_key(DM, "Hello again")
    assert key in adapter._rejected_reply_cache() and adapter._rejected_reply_cache()[key] is None
    monkeypatch.setattr(adapter, "_earlier_attempt_message_id", lambda *_: None)
    recovered = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
    result = await adapter.send(DM, recovered + "Hello again")
    assert not result.success
    assert adapter.frames == frames


@pytest.mark.parametrize("event", ["message.created", "message.add"])
@pytest.mark.parametrize("hint, wait", [(1500, 1.5), (30000, 30), (None, 0)])
async def test_retryable_stream_rejection_waits_before_final_reply(
    adapter, monkeypatch, event, hint, wait,
):
    clock, sleeps = [100.0], []
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock[0])

    async def wait_message_retry(delay):
        sleeps.append(delay)
        assert adapter.frames[-1]["event"] == event
        clock[0] += delay
        return True

    monkeypatch.setattr(adapter._connection, "wait_message_retry", wait_message_retry)
    send_frame = adapter._connection.send_frame

    async def reject(frame, **kwargs):
        if frame["event"] == "message.reply":
            assert clock[0] >= 100.0 + wait
        sent = await send_frame(frame, **kwargs)
        if frame["event"] == event:
            await adapter._on_message_error({
                "chat_id": DM, "payload": {
                    "message_id": frame["payload"]["message_id"], "code": "send_rate_limited",
                    "retryable": True, "retry_after_ms": hint,
                },
            })
        return sent

    monkeypatch.setattr(adapter._connection, "send_frame", reject)
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    run = adapter._active_runs_by_id[first.message_id]
    assert run.stream_failed and not run.stream_open and run.rejected_code is None
    frames = list(adapter.frames)
    await adapter._stream_update(run, "Hello again")
    assert adapter.frames == frames
    result = await adapter.edit_message(DM, first.message_id, "Hello again", finalize=True)
    assert result.success
    assert sleeps == ([wait] if wait else [])
    assert adapter.frames[:-1] == frames
    assert adapter.frames[-1]["event"] == "message.reply"
    assert adapter.acked[-1] is True
    assert not adapter._rejected_reply_cache()


@pytest.mark.parametrize("hint", [30001, 120000])
async def test_stream_retry_hint_above_wait_cap_discards_reply(adapter, monkeypatch, hint):
    clock = [100.0]
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock[0])
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    await adapter._on_message_error({"chat_id": DM, "payload": {
        "message_id": first.message_id, "code": "send_rate_limited", "retryable": True,
        "retry_after_ms": hint,
    }})

    async def unexpected_wait(_delay):
        pytest.fail("a server minimum above the cap must not be shortened")

    monkeypatch.setattr(adapter._connection, "wait_message_retry", unexpected_wait)
    frames = list(adapter.frames)
    result = await adapter.edit_message(DM, first.message_id, "Hello again", finalize=True)
    assert not result.success
    assert adapter.frames == frames
    assert adapter._reply_content_rejected(DM, "Hello again")
    key = adapter._reply_content_key(DM, "Hello again")
    deadline = 100.0 + max(hint / 1000.0, adapter_mod.MESSAGE_ERROR_RETRY_TOTAL_WAIT_SECONDS)
    assert adapter._rejected_reply_cache()[key] == deadline
    monkeypatch.setattr(adapter, "_earlier_attempt_message_id", lambda *_: None)
    recovered = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
    clock[0] = deadline - 0.001
    assert not (await adapter.send(DM, recovered + "Hello again")).success
    assert adapter.frames == frames
    clock[0] = deadline
    assert (await adapter.send(DM, recovered + "Hello again")).success
    assert key not in adapter._rejected_reply_cache()
    assert _events(adapter.frames[len(frames):]) == ["message.reply"]


@pytest.mark.parametrize("mismatch", ["message_id", "chat_id", "discarded"])
async def test_unmatched_stream_error_does_not_change_run(adapter, mismatch):
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    run = adapter._active_runs_by_id[first.message_id]
    # No connection or other adapter initialization is required by this callback.
    bare = ClawChatAdapter.__new__(ClawChatAdapter)
    bare._active_runs_by_id = {} if mismatch == "discarded" else {first.message_id: run}
    await bare._on_message_error({
        "chat_id": GROUP if mismatch == "chat_id" else DM,
        "payload": {"message_id": "other-reply" if mismatch == "message_id" else first.message_id,
                    "code": "agent_turn_limit", "retryable": False},
    })
    assert run.stream_open and not run.stream_failed
    assert run.rejected_code is None and run.retry_not_before is None


async def test_stream_run_discarded_while_created_is_sent_stays_closed(adapter, monkeypatch):
    send_frame = adapter._connection.send_frame

    async def discard(frame, **kwargs):
        sent = await send_frame(frame, **kwargs)
        run = adapter._active_runs_by_id[frame["payload"]["message_id"]]
        adapter._discard_run(run)
        return sent

    monkeypatch.setattr(adapter._connection, "send_frame", discard)
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    assert first.message_id not in adapter._active_runs_by_id
    assert _events(adapter.frames) == ["message.created"]


@pytest.mark.parametrize("interruption", ["discard", "terminal"])
async def test_stream_retry_wait_rechecks_run(adapter, monkeypatch, interruption):
    clock = [100.0]
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock[0])
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    run = adapter._active_runs_by_id[first.message_id]
    await adapter._on_message_error({"chat_id": DM, "payload": {
        "message_id": first.message_id, "code": "send_rate_limited", "retryable": True,
        "retry_after_ms": 1500,
    }})

    async def wait_message_retry(delay):
        clock[0] += delay
        if interruption == "discard":
            adapter._discard_run(run)
        else:
            await adapter._on_message_error({"chat_id": DM, "payload": {
                "message_id": first.message_id, "code": "agent_turn_limit", "retryable": False,
            }})
        return True

    monkeypatch.setattr(adapter._connection, "wait_message_retry", wait_message_retry)
    frames = list(adapter.frames)
    result = await adapter.edit_message(DM, first.message_id, "Hello again", finalize=True)
    assert not result.success
    assert adapter.frames == frames
    assert first.message_id not in adapter._active_runs_by_id


@pytest.mark.parametrize("interruption", ["generation", "disconnect"])
async def test_stream_retry_wait_abandoned_on_connection_change(adapter, monkeypatch, interruption):
    clock = [100.0]
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock[0])
    conn = adapter._connection
    conn._state = ConnectionState.READY
    conn._ws = object()
    entered, resume = adapter_mod.asyncio.Event(), adapter_mod.asyncio.Event()

    async def sleep(delay):
        assert delay == 1.5
        entered.set()
        await resume.wait()

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", sleep)
    first = await adapter.send(DM, "Hello" + CURSOR, reply_to="msg-in")
    await adapter._on_message_error({"chat_id": DM, "payload": {
        "message_id": first.message_id, "code": "send_rate_limited", "retryable": True,
        "retry_after_ms": 1500,
    }})
    frames = list(adapter.frames)
    task = adapter_mod.asyncio.create_task(
        adapter.edit_message(DM, first.message_id, "Hello again", finalize=True),
    )
    await entered.wait()
    assert first.message_id in adapter._active_runs_by_id
    assert len(conn._message_retry_waiters) == 1
    clock[0] = 100.5
    if interruption == "generation":
        conn._delivery_generation += 1
        resume.set()
    else:
        conn._state = ConnectionState.DISCONNECTED
        conn._reject_pending_acks(RuntimeError("connection disconnected"))
    result = await task
    assert not result.success and result.error
    assert adapter.frames == frames
    assert not conn._message_retry_waiters
    assert not conn._send_queue
    assert first.message_id not in adapter._active_runs_by_id
    key = adapter._reply_content_key(DM, "Hello again")
    assert adapter._rejected_reply_cache()[key] == 100.5 + adapter_mod.MESSAGE_ERROR_RETRY_TOTAL_WAIT_SECONDS
    recovered = "♻️ Recovered reply — the gateway restarted during delivery, so this may be a duplicate:\n\n"
    assert not (await adapter.send(DM, recovered + "Hello again")).success
    assert adapter.frames == frames


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


async def test_a_run_that_fails_mid_stream_withdraws_the_preview_and_says_so(adapter):
    # message.failed only withdraws the preview (it leaves nothing behind), so a
    # real failure must not end on it alone: the owner gets a plain message
    # saying the reply broke off. The host's error text stays out of the chat.
    message_id = await _stream(adapter, DM, "Half an ans", "Half an answer")
    await adapter.on_run_failed(DM, "provider exploded: 500", message_id=message_id)
    assert _events(adapter.frames) == [
        "message.created",
        "message.add",
        "message.add",
        "message.failed",
        "message.reply",
    ]
    notice = adapter.frames[-1]["payload"]
    assert notice["message_id"] != message_id
    assert notice["message_mode"] == "normal"
    text = notice["message"]["body"]["fragments"][0]["text"]
    assert "provider exploded" not in text
    assert text == adapter_mod.STREAM_INTERRUPTED_NOTICE["en"]


async def test_the_interrupted_notice_follows_the_owner_language(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_owner_locale", lambda: "zh-CN")
    message_id = await _stream(adapter, DM, "Half an ans", "Half an answer")
    await adapter.on_run_failed(DM, "boom", message_id=message_id)
    text = adapter.frames[-1]["payload"]["message"]["body"]["fragments"][0]["text"]
    assert text == adapter_mod.STREAM_INTERRUPTED_NOTICE["zh"]


async def test_a_run_that_fails_before_anything_was_shown_sends_nothing(adapter):
    # Nothing was on screen, so there is nothing to withdraw or explain; the
    # error stays out of the chat as before.
    first = await adapter.send(DM, "[claw" + CURSOR, reply_to="msg-in")
    await adapter.on_run_failed(DM, "boom", message_id=first.message_id)
    assert adapter.frames == []


async def test_an_already_sent_reply_is_never_withdrawn(adapter):
    # If the reply under this id already went out, failing the stream would
    # make the client delete a delivered message: close it as done instead.
    message_id = await _stream(adapter, DM, "Hello", "Hello, wor")
    adapter._claim_outbound_message = lambda **_kw: False
    await adapter.edit_message(DM, message_id, "Hello, world", finalize=True)
    assert "message.failed" not in _events(adapter.frames)
    assert _events(adapter.frames)[-1] == "message.done"


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


# --- streaming off (the default) ---------------------------------------------


async def test_without_the_opt_in_a_host_stream_goes_out_as_one_reply(plain_adapter):
    # Hermes streaming was turned on by hand: it sends a cursor draft and edits.
    message_id = await _stream(plain_adapter, DM, "Hello", "Hello, wor", final="Hello, world")
    assert _events(plain_adapter.frames) == ["message.reply"]
    assert not STREAM_EVENTS & set(_events(plain_adapter.frames))
    reply = plain_adapter.frames[0]["payload"]
    assert reply["message_id"] == message_id
    assert reply["message_mode"] == "normal"
    assert reply["message"]["body"]["fragments"] == [{"kind": "text", "text": "Hello, world"}]
    assert plain_adapter.acked == [True]


async def test_without_the_opt_in_a_suppressed_reply_sends_nothing(plain_adapter):
    await _stream(plain_adapter, DM, "Let me think", final="NO_REPLY")
    assert plain_adapter.frames == []


async def test_without_the_opt_in_a_rewritten_reply_still_arrives_once(plain_adapter):
    await _stream(plain_adapter, DM, "Hello", "Goodbye", final="Goodbye all")
    assert _events(plain_adapter.frames) == ["message.reply"]


async def test_with_host_streaming_off_a_reply_is_one_message(plain_adapter):
    # Host streaming off: Hermes sends the finished reply once, no cursor, no edits.
    await plain_adapter.send(DM, "Hello, world", reply_to="msg-in")
    assert _events(plain_adapter.frames) == ["message.reply"]


async def test_without_the_opt_in_an_unfinished_reply_is_not_sent_after_the_turn(
    plain_adapter, monkeypatch
):
    # /stop or /new: Hermes abandons the draft rather than deliver stale text,
    # and the owner never saw any of it.
    monkeypatch.setattr(adapter_mod, "STREAM_ABANDON_GRACE_SECONDS", 0.0)
    await _stream(plain_adapter, DM, "Partial ans", "Partial answer")
    await _stream(plain_adapter, GROUP, "Partial ans", "Partial answer")
    await plain_adapter.stop_typing(DM)
    await plain_adapter.stop_typing(GROUP)
    await plain_adapter._drain_stream_sweeps()
    assert [e for e in _events(plain_adapter.frames) if e != "typing.update"] == []


# --- configuration --------------------------------------------------------------

from clawchat_gateway import activate  # noqa: E402
from clawchat_gateway.config import ClawChatConfig  # noqa: E402
from clawchat_gateway.output_visibility import DISPLAY_PRESETS  # noqa: E402


class _PlatformConfig:
    def __init__(self, extra):
        self.extra = extra


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, False),
        ({"stream_replies": False}, False),
        ({"stream_replies": "off"}, False),
        ({"stream_replies": "false"}, False),
        ({"stream_replies": True}, True),
        ({"stream_replies": "on"}, True),
    ],
)
def test_stream_replies_is_an_explicit_opt_in(extra, expected):
    config = ClawChatConfig.from_platform_config(_PlatformConfig(extra))
    assert config.stream_replies is expected


def test_activation_and_every_output_preset_keep_host_streaming_off():
    assert all(preset["streaming"] is False for preset in DISPLAY_PRESETS.values())
    assert activate.CLAWCHAT_DISPLAY_DEFAULTS["streaming"] is False


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


@pytest.mark.parametrize("streaming", [False, True])
def test_load_never_changes_host_streaming(fake_config, streaming):
    fake_config["config"] = {
        "platforms": {"clawchat": {"extra": {"user_id": AGENT}}},
        "display": {"platforms": {"clawchat": {"streaming": streaming}}},
        "memory": {"user_profile_enabled": False, "nudge_interval": 0},
        "compression": {"threshold_tokens": 150000},
    }
    activate.ensure_clawchat_host_defaults_on_load()
    config = fake_config["config"]
    assert config["display"]["platforms"]["clawchat"]["streaming"] is streaming
    assert config["platforms"]["clawchat"]["extra"] == {"user_id": AGENT}
    assert fake_config["writes"] == 0
