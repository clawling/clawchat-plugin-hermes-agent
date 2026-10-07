"""Process messages go out as ``message_mode: "thinking"``; others' are not input.

Protocol §7.5: a producer marks producer-internal content (tool progress,
runtime notices, reasoning) with a non-normal ``message_mode``; a client that
ingests messages as conversational input (an agent) skips such frames, and a
human-facing client may fold them. The adapter used to send everything as
``"normal"`` and read every message as input, so its tool progress landed in
other agents' context and another agent's progress landed in its own.

What counts as a process message here:

* tool progress, reasoning lines and hints from Hermes' tool-progress sender;
* status updates (``send_or_update_status``), long-running heartbeats and
  operational notices (``_deliver_platform_notice``);
* a Hermes runtime notice from the suppression table, when the output preset
  lets it through;
* the reasoning block Hermes puts in front of a final reply
  (``show_reasoning``) — it is split off and sent as its own message first.

The reply itself, interim assistant messages, and anything the agent sends on
purpose stay ``"normal"``. Tool previews are rendered as inline code and
terminal commands as code blocks, so a ``*`` in a command is not read as
emphasis.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.protocol import build_message_reply_event, build_message_send_event

AGENT = "usr_agent"
OWNER = "usr_owner"
DM = "cnv_dm"
GROUP = "cnv_group"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a.frames = []

    async def send_frame(frame, **_kw):
        a.frames.append(frame)
        return True

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._known_chat_types[DM] = "direct"
    a._known_chat_types[GROUP] = "group"
    return a


@pytest.fixture
def runtime_status_shown(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_runtime_status_messages_enabled", lambda: True)
    return adapter


def _host_caller(name: str, filename: str = "/site-packages/gateway/run_turn_runner.py"):
    """An ``async def <name>(adapter, chat_id, text)`` that lives in a host file."""
    src = f"async def {name}(adapter, chat_id, text):\n    return await adapter.send(chat_id, text)\n"
    namespace: dict = {}
    exec(compile(src, filename, "exec"), namespace)  # noqa: S102 - test double of a host frame
    return namespace[name]


def _modes(frames):
    return [
        (f["payload"]["message_mode"], f["payload"]["message"]["body"]["fragments"][0]["text"])
        for f in frames
        if f.get("event") in {"message.send", "message.reply"}
    ]


# --- frame builders ----------------------------------------------------------


def test_frame_builders_default_to_normal_and_carry_thinking():
    common = dict(chat_id=DM, chat_type="direct", message_id="msg-1", fragments=[{"kind": "text", "text": "x"}])
    assert build_message_reply_event(**common)["payload"]["message_mode"] == "normal"
    assert build_message_send_event(**common)["payload"]["message_mode"] == "normal"
    assert build_message_reply_event(**common, message_mode="thinking")["payload"]["message_mode"] == "thinking"
    assert build_message_send_event(**common, message_mode="thinking")["payload"]["message_mode"] == "thinking"


# --- outbound tagging ---------------------------------------------------------


async def test_a_plain_reply_is_normal(adapter):
    await adapter.send(DM, "Here is the answer.")
    assert _modes(adapter.frames) == [("normal", "Here is the answer.")]


async def test_tool_progress_from_the_host_progress_sender_is_thinking(adapter):
    sender = _host_caller("send_progress_messages")
    await sender(adapter, DM, '⚙️ search_files: "notes"')
    assert _modes(adapter.frames) == [("thinking", '⚙️ search_files: "notes"')]


async def test_the_same_function_name_outside_the_host_is_not_trusted(adapter):
    sender = _host_caller("send_progress_messages", filename="/somewhere/else/tools.py")
    await sender(adapter, DM, "hello")
    assert _modes(adapter.frames) == [("normal", "hello")]


@pytest.mark.parametrize("name", ["_deliver_platform_notice", "_run_agent_notify_long_running"])
async def test_host_notices_and_heartbeats_are_thinking(adapter, name):
    sender = _host_caller(name, filename="/site-packages/gateway/run_notifications.py")
    await sender(adapter, DM, "⏳ Working — 3 min")
    assert _modes(adapter.frames) == [("thinking", "⏳ Working — 3 min")]


async def test_status_updates_are_thinking_when_shown(runtime_status_shown):
    await runtime_status_shown.send_or_update_status(DM, "lifecycle", "🗜️ Compressed 40 messages")
    assert _modes(runtime_status_shown.frames) == [("thinking", "🗜️ Compressed 40 messages")]


async def test_status_updates_stay_suppressed_by_default(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_runtime_status_messages_enabled", lambda: False)
    await adapter.send_or_update_status(DM, "lifecycle", "🗜️ Compressed 40 messages")
    assert adapter.frames == []


async def test_a_runtime_notice_let_through_by_the_preset_is_thinking(runtime_status_shown):
    await runtime_status_shown.send(DM, "⚠️ Rate limited — retrying in 5s")
    assert _modes(runtime_status_shown.frames) == [("thinking", "⚠️ Rate limited — retrying in 5s")]


async def test_reasoning_in_front_of_a_reply_goes_out_first_as_thinking(adapter):
    text = "💭 **Reasoning:**\n```\nThe user wants a sum.\n```\n\nThe sum is 4."
    await adapter.send(DM, text)
    assert _modes(adapter.frames) == [
        ("thinking", "💭 **Reasoning:**\n```\nThe user wants a sum.\n```"),
        ("normal", "The sum is 4."),
    ]


async def test_process_messages_do_not_count_as_a_visible_reply(adapter):
    sender = _host_caller("send_progress_messages")
    await sender(adapter, DM, "⚙️ terminal...")
    assert adapter._visible_send_count(DM) == 0
    await adapter.send(DM, "done")
    assert adapter._visible_send_count(DM) == 1


async def test_own_process_messages_stay_out_of_the_group_history(adapter):
    sender = _host_caller("send_progress_messages")
    await sender(adapter, GROUP, "⚙️ terminal...")
    await adapter.send(GROUP, "The answer.")
    rows = adapter._store.list_recent_group_transcript("default", GROUP, 10)
    assert [row["text"] for row in rows] == ["The answer."]
    legacy = adapter._store.list_recent_group_messages("default", GROUP, 10)
    assert [row["text"] for row in legacy] == ["The answer."]


# --- inbound skip -------------------------------------------------------------


def _inbound_frame(mode):
    payload = {
        "message_id": "msg-" + "A" * 26,
        "message": {"body": {"fragments": [{"kind": "text", "text": "progress"}]}, "context": {"mentions": [], "reply": None}},
    }
    if mode is not None:
        payload["message_mode"] = mode
    return {
        "version": "2",
        "event": "message.send",
        "chat_id": GROUP,
        "chat_type": "group",
        "sender": {"id": "usr_other_agent", "nick_name": "Other"},
        "payload": payload,
    }


@pytest.mark.parametrize("mode", ["thinking", "tool", " thinking "])
async def test_a_non_normal_message_is_not_conversational_input(adapter, monkeypatch, mode):
    parsed = []
    monkeypatch.setattr(adapter_mod, "parse_inbound_message", lambda frame, cfg: parsed.append(frame))
    await adapter._on_message(_inbound_frame(mode))
    assert parsed == []


@pytest.mark.parametrize("mode", ["normal", "", None, 7])
async def test_normal_and_unset_modes_are_input(adapter, monkeypatch, mode):
    parsed = []
    monkeypatch.setattr(adapter_mod, "parse_inbound_message", lambda frame, cfg: parsed.append(frame))
    await adapter._on_message(_inbound_frame(mode))
    assert len(parsed) == 1


# --- tool previews as code ----------------------------------------------------


def test_tool_previews_render_as_inline_code(adapter):
    assert adapter.format_tool_preview(SimpleNamespace(text="ls *.py")) == "`ls *.py`"
    assert adapter.format_tool_preview("rm a*b") == "`rm a*b`"
    assert adapter.format_tool_preview(SimpleNamespace(text="echo `date`")) == "`` echo `date` ``"
    assert adapter.format_tool_preview(SimpleNamespace(text="`x`")) == "`` `x` ``"
    assert adapter.format_tool_preview(SimpleNamespace(text="")) == ""


def test_terminal_commands_are_rendered_as_code_blocks():
    assert ClawChatAdapter.supports_code_blocks is True
