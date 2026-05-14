from __future__ import annotations

import logging
from types import SimpleNamespace

import clawchat_gateway.adapter as adapter_module
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage
from clawchat_gateway.stream_buffer import compute_delta


class FakeConnection:
    def __init__(self) -> None:
        self.sent_frames: list[dict] = []
        self.wait_for_ack: list[bool] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_frame(self, frame: dict, *, wait_for_ack: bool = False) -> None:
        self.sent_frames.append(frame)
        self.wait_for_ack.append(wait_for_ack)


def _make_adapter(**extra) -> ClawChatAdapter:
    adapter = ClawChatAdapter(
        SimpleNamespace(
            extra={
                "websocket_url": "ws://x",
                "token": "t",
                "user_id": "bot",
                **extra,
            }
        )
    )
    adapter._connection = FakeConnection()
    return adapter


def test_adapter_accepts_dynamic_clawchat_platform(monkeypatch):
    class DynamicPlatform:
        def __init__(self, value):
            if value != "clawchat":
                raise ValueError(value)
            self.value = value

    monkeypatch.setattr(adapter_module, "Platform", DynamicPlatform)

    adapter = ClawChatAdapter(SimpleNamespace(extra={"websocket_url": "ws://x", "token": "t"}))

    assert adapter.platform.value == "clawchat"


def test_compute_delta_uses_suffix_when_content_extends():
    full, delta = compute_delta("abc", "abcdef")
    assert full == "abcdef"
    assert delta == "def"


def test_compute_delta_falls_back_to_full_on_prefix_reset():
    full, delta = compute_delta("abcdef", "xyz")
    assert full == "xyz"
    assert delta == "xyz"


async def test_on_message_builds_message_event():
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="hello",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    assert len(adapter.handled) == 1
    assert adapter.handled[0].text == "hello"
    assert adapter.handled[0].source.user_id == "u1"
    assert adapter.handled[0].source.chat_type == "dm"
    assert adapter.handled[0].raw_message == {
        "clawchat_chat_type": "direct",
        "clawchat_reply": None,
        "clawchat_raw": {"x": 1},
    }


async def test_on_message_attaches_clawchat_skill_for_activation_intent():
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="clawchat 的激活码是 R4E1IW",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert event.auto_skill == "clawchat"
    assert "/clawchat-activate CODE" in event.channel_prompt


async def test_group_message_attaches_group_covenant_channel_prompt(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="room1",
        chat_type="group",
        sender_id="u1",
        sender_name="alice",
        text="hello group",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert event.channel_prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )


async def test_direct_message_does_not_attach_group_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="hello dm",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    assert adapter.handled[0].channel_prompt is None


async def test_group_activation_prompt_appends_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.adapter.build_group_channel_prompt",
        lambda: "ClawChat group covenant:\n群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="room1",
        chat_type="group",
        sender_id="u1",
        sender_name="alice",
        text="clawchat 的激活码是 R4E1IW",
        raw_message={"x": 1},
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert "ClawChat group covenant:" in event.channel_prompt
    assert "群里陈平安是落魄山共和国的国王" in event.channel_prompt
    assert "/clawchat-activate CODE" in event.channel_prompt


async def test_on_message_downloads_media_before_dispatch(monkeypatch, tmp_path):
    adapter = _make_adapter(
        base_url="http://company.newbaselab.com:10086",
        websocket_url="ws://company.newbaselab.com:10086/ws",
    )
    local_path = tmp_path / "img.png"
    local_path.write_bytes(b"png-bytes")
    calls = []

    async def fake_download(urls, **kwargs):
        calls.append({"urls": urls, "kwargs": kwargs})
        return [
            SimpleNamespace(
                local_path=local_path,
                mime="image/png",
                size=len(b"png-bytes"),
                source_url="http://company.newbaselab.com:10086/media/img.png",
            )
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.download_inbound_media", fake_download)

    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="![img.png](/media/img.png)",
        raw_message={"x": 1},
        media_urls=["/media/img.png"],
        media_types=["image"],
    )

    await adapter._handle_inbound(inbound)

    assert calls[0]["urls"] == ["/media/img.png"]
    assert calls[0]["kwargs"]["base_url"] == "http://company.newbaselab.com:10086"
    assert adapter.handled[0].media_urls == [str(local_path)]
    assert adapter.handled[0].media_types == ["image/png"]


async def test_on_message_logs_inbound_and_dispatch(caplog):
    adapter = _make_adapter()
    frame = {
        "event": "message.send",
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "context": {},
                "fragments": [{"kind": "text", "text": "hello"}],
            }
        },
    }

    with caplog.at_level(logging.INFO, logger="clawchat_gateway.adapter"):
        await adapter._on_message(frame)

    messages = [record.getMessage() for record in caplog.records]
    assert any("clawchat inbound parsed chat_id=u1" in message for message in messages)
    assert any("clawchat dispatch to hermes chat_id=u1" in message for message in messages)
    assert any("clawchat dispatch accepted by hermes chat_id=u1" in message for message in messages)


async def test_on_message_logs_parse_drop(caplog):
    adapter = _make_adapter(group_mode="mention")
    frame = {
        "event": "message.send",
        "chat_id": "room1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "context": {"mentions": []},
                "fragments": [{"kind": "text", "text": "hello"}],
            }
        },
    }

    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.adapter"):
        await adapter._on_message(frame)

    assert any(
        "clawchat inbound dropped event=message.send chat_id=room1" in record.getMessage()
        for record in caplog.records
    )


async def test_on_message_maps_reply_preview_to_message_event_fields():
    adapter = _make_adapter()
    inbound = InboundMessage(
        chat_id="u1",
        chat_type="direct",
        sender_id="u1",
        sender_name="alice",
        text="hello",
        raw_message={"x": 1},
        reply_preview={
            "id": "msg-42",
            "fragments": [
                {"kind": "text", "text": "older"},
                {"kind": "image", "url": "https://example.com/p.png"},
                {"kind": "text", "text": " message"},
            ],
        },
    )

    await adapter._handle_inbound(inbound)

    event = adapter.handled[0]
    assert event.reply_to_message_id == "msg-42"
    assert event.reply_to_text == "older message"


async def test_on_message_maps_interaction_submit_approve_to_text_command():
    adapter = _make_adapter()
    frame = {
        "event": "interaction.submit",
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message_id": "msg-1",
            "fragment_index": 0,
            "fragment_kind": "approval_request",
            "action_id": "approve",
            "action_payload": {"decision": "approve"},
        },
    }

    await adapter._on_message(frame)

    assert adapter.handled[0].text == "/approve"
    assert adapter.handled[0].raw_message["clawchat_interaction_submit"] == frame["payload"]


async def test_on_message_maps_interaction_submit_deny_to_text_command():
    adapter = _make_adapter()
    frame = {
        "event": "interaction.submit",
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message_id": "msg-1",
            "fragment_index": 0,
            "fragment_kind": "approval_request",
            "action_id": "deny",
            "action_payload": {"decision": "deny"},
        },
    }

    await adapter._on_message(frame)

    assert adapter.handled[0].text == "/deny"


async def test_send_emits_message_reply_for_static_mode():
    adapter = _make_adapter(reply_mode="static")

    result = await adapter.send(chat_id="u1", content="hi")

    assert result.success is True
    assert adapter._connection.sent_frames[0]["event"] == "message.reply"
    assert adapter._connection.wait_for_ack == [True]
    assert adapter._connection.sent_frames[0]["version"] == "2"
    assert "message_id" not in adapter._connection.sent_frames[0]["payload"]
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "hi"}
    ]


async def test_send_filters_think_and_tool_output_by_default():
    adapter = _make_adapter(reply_mode="static")

    await adapter.send(
        chat_id="u1",
        content=(
            "<think>private reasoning</think>"
            "visible"
            "<tool_call>{\"name\":\"x\"}</tool_call>"
            "<tool_result>secret result</tool_result>"
        ),
    )

    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "visible"}
    ]


async def test_send_preserves_think_and_tool_output_when_enabled():
    adapter = _make_adapter(
        reply_mode="static",
        show_tools_output=True,
        show_think_output=True,
    )
    content = "<think>private reasoning</think>visible<tool_call>{}</tool_call>"

    await adapter.send(chat_id="u1", content=content)

    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": content}
    ]


async def test_send_suppresses_gateway_tool_progress_by_default():
    adapter = _make_adapter(reply_mode="stream")

    result = await adapter.send(
        chat_id="u1",
        content='🧭 browser_navigate: "https://example.com/image.png"',
    )

    assert result.success is True
    assert result.message_id is None
    assert adapter._connection.sent_frames == []


async def test_send_preserves_gateway_tool_progress_when_enabled():
    adapter = _make_adapter(reply_mode="stream", show_tool_progress=True)

    await adapter.send(
        chat_id="u1",
        content='🧭 browser_navigate: "https://example.com/image.png"',
    )

    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.created",
        "message.add",
    ]


async def test_send_suppresses_gateway_tool_progress_when_raw_tools_visible_but_progress_hidden():
    adapter = _make_adapter(
        reply_mode="stream",
        show_tools_output=True,
        show_tool_progress=False,
    )

    result = await adapter.send(
        chat_id="u1",
        content='🧭 browser_navigate: "https://example.com/image.png"',
    )

    assert result.success is True
    assert adapter._connection.sent_frames == []


async def test_send_shows_tool_progress_while_filtering_raw_tool_output():
    adapter = _make_adapter(
        reply_mode="stream",
        show_tools_output=False,
        show_tool_progress=True,
    )

    await adapter.send(
        chat_id="u1",
        content='🧭 browser_navigate: "https://example.com/image.png"',
    )
    await adapter.send(
        chat_id="u1",
        content='<tool_result>{"secret": true}</tool_result>visible',
    )

    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.created",
        "message.add",
        "message.created",
        "message.add",
    ]
    assert adapter._connection.sent_frames[3]["payload"]["fragments"][0]["text"] == "visible"


async def test_edit_message_suppresses_gateway_tool_progress_by_default():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="visible")
    adapter._connection.sent_frames.clear()

    result = await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content='🧭 browser_navigate: "https://example.com/image.png"',
    )

    assert result.success is True
    assert adapter._connection.sent_frames == []


async def test_send_emits_rich_approval_fragment_when_enabled():
    adapter = _make_adapter(reply_mode="static", enable_rich_interactions=True)
    fallback = "Tool wants to continue. Reply /approve to proceed or /deny to cancel."

    await adapter.send(chat_id="u1", content=fallback)

    fragments = adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"]
    assert fragments == [
        {
            "kind": "approval_request",
            "title": "Approval required",
            "fallback_text": fallback,
            "state": "pending",
            "actions": [
                {
                    "id": "approve",
                    "label": "Approve",
                    "style": "primary",
                    "payload": {"decision": "approve"},
                },
                {
                    "id": "deny",
                    "label": "Deny",
                    "style": "danger",
                    "payload": {"decision": "deny"},
                },
            ],
        }
    ]


async def test_send_keeps_approval_text_fallback_when_rich_interactions_disabled():
    adapter = _make_adapter(reply_mode="static", enable_rich_interactions=False)
    fallback = "Tool wants to continue. Reply /approve to proceed or /deny to cancel."

    await adapter.send(chat_id="u1", content=fallback)

    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": fallback}
    ]


async def test_send_emits_metadata_action_card_when_enabled():
    adapter = _make_adapter(reply_mode="static", enable_rich_interactions=True)

    await adapter.send(
        chat_id="u1",
        content="Run cleanup?",
        metadata={
            "clawchat_interaction": {
                "kind": "action_card",
                "title": "Cleanup",
                "fallback_text": "Run cleanup? Reply /approve or /deny.",
                "state": "pending",
                "actions": [
                    {"id": "approve", "label": "Run", "style": "primary"},
                    {"id": "deny", "label": "Cancel", "style": "secondary"},
                ],
            }
        },
    )

    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {
            "kind": "action_card",
            "title": "Cleanup",
            "fallback_text": "Run cleanup? Reply /approve or /deny.",
            "state": "pending",
            "actions": [
                {"id": "approve", "label": "Run", "style": "primary"},
                {"id": "deny", "label": "Cancel", "style": "secondary"},
            ],
        }
    ]


async def test_send_logs_static_reply(caplog):
    adapter = _make_adapter(reply_mode="static")

    with caplog.at_level(logging.INFO, logger="clawchat_gateway.adapter"):
        await adapter.send(chat_id="u1", content="hi")

    messages = [record.getMessage() for record in caplog.records]
    assert any("clawchat send start chat_id=u1" in message for message in messages)
    assert any("clawchat send static reply queued chat_id=u1" in message for message in messages)


async def test_send_typing_emits_typing_update():
    adapter = _make_adapter()

    await adapter.send_typing("u1")

    assert adapter._connection.sent_frames[0]["event"] == "typing.update"
    assert adapter._connection.sent_frames[0]["version"] == "2"
    assert adapter._connection.sent_frames[0]["payload"]["is_typing"] is True


async def test_stop_typing_emits_inactive_typing_update():
    adapter = _make_adapter()

    await adapter.send_typing("u1")
    await adapter.stop_typing("u1")

    assert adapter._connection.sent_frames[1]["event"] == "typing.update"
    assert adapter._connection.sent_frames[1]["payload"]["is_typing"] is False


async def test_send_typing_is_deduped_until_stop():
    adapter = _make_adapter()

    await adapter.send_typing("u1")
    await adapter.send_typing("u1")
    await adapter.stop_typing("u1")
    await adapter.stop_typing("u1")

    assert [frame["payload"]["is_typing"] for frame in adapter._connection.sent_frames] == [
        True,
        False,
    ]


async def test_send_emits_message_created_then_add_for_stream_mode():
    adapter = _make_adapter(reply_mode="stream")

    result = await adapter.send(chat_id="u1", content="hello")

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.created",
        "message.add",
    ]
    message_id = result.message_id
    assert adapter._connection.sent_frames[0]["version"] == "2"
    assert adapter._connection.sent_frames[0]["payload"]["message_id"] == message_id
    assert adapter._connection.sent_frames[1]["payload"]["message_id"] == message_id
    assert adapter._connection.sent_frames[1]["payload"]["sequence"] == 0
    assert (
        adapter._connection.sent_frames[1]["payload"]["fragments"][0]["delta"]
        == "hello"
    )


async def test_stream_filters_incomplete_think_and_tool_output_before_delta():
    adapter = _make_adapter(reply_mode="stream")

    first = await adapter.send(chat_id="u1", content="<think>hidden")
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="<think>hidden</think>visible<tool_call>{}",
    )
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="<think>hidden</think>visible<tool_call>{}</tool_call> done",
    )

    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.created",
        "message.add",
        "message.add",
    ]
    assert adapter._connection.sent_frames[1]["payload"]["fragments"][0]["delta"] == "visible"
    assert adapter._connection.sent_frames[2]["payload"]["fragments"][0]["delta"] == " done"


async def test_stream_strips_trailing_typing_cursor_before_diffing():
    adapter = _make_adapter(reply_mode="stream")

    first = await adapter.send(chat_id="u1", content="hello ▉")
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world ▉",
    )
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world!",
        finalize=True,
    )

    add_frames = [f for f in adapter._connection.sent_frames if f["event"] == "message.add"]
    assert [f["payload"]["fragments"][0]["delta"] for f in add_frames] == [
        "hello",
        " world",
        "!",
    ]
    assert all(
        "▉" not in f["payload"]["fragments"][0]["text"]
        and "▉" not in f["payload"]["fragments"][0]["delta"]
        for f in add_frames
    )


async def test_edit_message_emits_message_add_for_stream_mode():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    adapter._connection.sent_frames.clear()

    result = await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world",
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.add"]
    assert adapter._connection.sent_frames[0]["payload"]["message_id"] == first.message_id
    assert adapter._connection.sent_frames[0]["payload"]["sequence"] == 1
    fragment = adapter._connection.sent_frames[0]["payload"]["fragments"][0]
    assert fragment["text"] == "hello world"
    assert fragment["delta"] == " world"


async def test_edit_message_targets_run_by_message_id_with_overlapping_streams():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="first")
    second = await adapter.send(chat_id="u1", content="second")
    adapter._connection.sent_frames.clear()

    result = await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="first expanded",
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.add"]
    add_frame = adapter._connection.sent_frames[0]
    assert add_frame["payload"]["message_id"] == first.message_id
    assert add_frame["payload"]["fragments"][0]["delta"] == " expanded"
    assert adapter._active_chat_runs["u1"] == second.message_id
    assert adapter._active_runs_by_id[first.message_id].last_text == "first expanded"
    assert adapter._active_runs_by_id[second.message_id].last_text == "second"


async def test_edit_message_with_finalize_emits_done_only():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    adapter._connection.sent_frames.clear()

    result = await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world",
        finalize=True,
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.add",
        "message.done",
    ]
    assert adapter._connection.sent_frames[0]["payload"]["fragments"][0]["delta"] == " world"
    assert adapter._connection.sent_frames[1]["payload"]["fragments"] == [
        {"kind": "text", "text": "hello world"}
    ]
    assert first.message_id not in adapter._active_runs_by_id


async def test_edit_message_after_finalize_is_idempotent_success(caplog):
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world",
        finalize=True,
    )
    adapter._connection.sent_frames.clear()

    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.adapter"):
        result = await adapter.edit_message(
            chat_id="u1",
            message_id=first.message_id or "",
            content="hello world",
        )

    assert result.success is True
    assert result.message_id == first.message_id
    assert adapter._connection.sent_frames == []
    assert caplog.records == []


async def test_duplicate_on_run_complete_after_finalize_is_idempotent():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hello world",
        finalize=True,
    )
    adapter._connection.sent_frames.clear()

    await adapter.on_run_complete(
        chat_id="u1",
        final_text="hello world",
        message_id=first.message_id,
    )

    assert adapter._connection.sent_frames == []


async def test_v012_stream_edits_strip_cursor_and_finalize_lifecycle():
    adapter = _make_adapter(reply_mode="stream")

    result = await adapter.send(chat_id="u1", content="Hey! I'm ▉")
    await adapter.edit_message(
        chat_id="u1",
        message_id=result.message_id or "",
        content="Hey! I'm doing well ▉",
    )
    await adapter.edit_message(
        chat_id="u1",
        message_id=result.message_id or "",
        content="Hey! I'm doing well",
        finalize=True,
    )

    assert adapter.REQUIRES_EDIT_FINALIZE is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.created",
        "message.add",
        "message.add",
        "message.done",
    ]
    first_add = adapter._connection.sent_frames[1]["payload"]["fragments"][0]
    second_add = adapter._connection.sent_frames[2]["payload"]["fragments"][0]
    done = adapter._connection.sent_frames[3]
    assert first_add == {"kind": "text", "text": "Hey! I'm", "delta": "Hey! I'm"}
    assert second_add == {
        "kind": "text",
        "text": "Hey! I'm doing well",
        "delta": " doing well",
    }
    assert done["payload"]["fragments"] == [{"kind": "text", "text": "Hey! I'm doing well"}]
    assert all(
        "▉" not in str(frame)
        for frame in adapter._connection.sent_frames
    )


async def test_edit_message_ignores_unknown_kwargs():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hi")
    adapter._connection.sent_frames.clear()

    result = await adapter.edit_message(
        chat_id="u1",
        message_id=first.message_id or "",
        content="hi there",
        stream_id="abc",
        chunk_index=3,
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.add"]


async def test_on_run_complete_emits_message_done_without_trailing_reply():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    adapter._connection.sent_frames.clear()

    await adapter.on_run_complete(chat_id="u1", final_text="hello world")

    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.add",
        "message.done",
    ]
    assert adapter._connection.sent_frames[1]["payload"]["message_id"] == first.message_id
    assert adapter._connection.sent_frames[1]["payload"]["fragments"] == [
        {"kind": "text", "text": "hello world"}
    ]
    assert first.message_id not in adapter._active_runs_by_id


async def test_on_run_failed_emits_message_failed_and_cleans_run():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="hello")
    adapter._connection.sent_frames.clear()

    await adapter.on_run_failed(chat_id="u1", error="boom", message_id=first.message_id)

    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.failed"]
    failed = adapter._connection.sent_frames[0]
    assert failed["payload"]["message_id"] == first.message_id
    assert failed["payload"]["sequence"] == 0
    assert failed["payload"]["streaming"]["status"] == "failed"
    assert first.message_id not in adapter._active_runs_by_id


async def test_on_run_complete_finalizes_requested_run_during_overlap():
    adapter = _make_adapter(reply_mode="stream")
    first = await adapter.send(chat_id="u1", content="first")
    second = await adapter.send(chat_id="u1", content="second")
    adapter._connection.sent_frames.clear()

    await adapter.on_run_complete(
        chat_id="u1",
        final_text="first final",
        message_id=first.message_id,
    )

    assert [frame["event"] for frame in adapter._connection.sent_frames] == [
        "message.add",
        "message.done",
    ]
    assert adapter._connection.sent_frames[1]["payload"]["message_id"] == first.message_id
    assert first.message_id not in adapter._active_runs_by_id
    assert adapter._active_chat_runs["u1"] == second.message_id


async def test_send_forces_static_mode_when_outbound_media_exists(monkeypatch):
    adapter = _make_adapter(reply_mode="stream")

    async def fake_upload(urls, **kwargs):
        assert urls == ["https://example.com/a.png"]
        return [
            {
                "kind": "image",
                "url": "https://cdn.example.com/a.png",
                "mime": "image/png",
                "size": 9,
                "name": "a.png",
            }
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.upload_outbound_media", fake_upload)

    result = await adapter.send(
        chat_id="u1",
        content="look",
        metadata={"media_urls": ["https://example.com/a.png"]},
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.reply"]
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "look"},
        {"kind": "image", "url": "https://cdn.example.com/a.png", "mime": "image/png", "size": 9, "name": "a.png"},
    ]


async def test_send_classifies_non_image_media_in_static_fallback(monkeypatch):
    adapter = _make_adapter(reply_mode="stream")

    async def fake_upload(urls, **kwargs):
        assert urls == [
            "https://example.com/report.pdf",
            "https://example.com/voice.mp3",
            "https://example.com/clip.unknown",
        ]
        return [
            {"kind": "file", "url": "https://cdn.example.com/report.pdf", "mime": "application/pdf", "size": 10, "name": "report.pdf"},
            {"kind": "audio", "url": "https://cdn.example.com/voice.mp3", "mime": "audio/mpeg", "size": 11, "name": "voice.mp3"},
            {"kind": "video", "url": "https://cdn.example.com/clip.mp4", "mime": "video/mp4", "size": 12, "name": "clip.unknown"},
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.upload_outbound_media", fake_upload)

    result = await adapter.send(
        chat_id="u1",
        content="files",
        metadata={
            "media_urls": [
                "https://example.com/report.pdf",
                "https://example.com/voice.mp3",
                "https://example.com/clip.unknown",
            ],
            "media_content_types": {
                "https://example.com/clip.unknown": "video/mp4",
            },
        },
    )

    assert result.success is True
    assert [frame["event"] for frame in adapter._connection.sent_frames] == ["message.reply"]
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "files"},
        {"kind": "file", "url": "https://cdn.example.com/report.pdf", "mime": "application/pdf", "size": 10, "name": "report.pdf"},
        {"kind": "audio", "url": "https://cdn.example.com/voice.mp3", "mime": "audio/mpeg", "size": 11, "name": "voice.mp3"},
        {"kind": "video", "url": "https://cdn.example.com/clip.mp4", "mime": "video/mp4", "size": 12, "name": "clip.unknown"},
    ]


async def test_send_uploads_local_media_before_static_reply(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "generated.png"
    image_path.write_bytes(b"png-bytes")

    adapter = _make_adapter(reply_mode="stream", media_local_roots=[str(allowed)])
    uploaded = []

    async def fake_upload(urls, **kwargs):
        uploaded.append({"urls": urls, "kwargs": kwargs})
        return [
            {
                "kind": "image",
                "url": "https://cdn.example.com/generated.png",
                "mime": "image/png",
                "size": 9,
                "name": "generated.png",
            }
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.upload_outbound_media", fake_upload)

    result = await adapter.send(
        chat_id="u1",
        content="look",
        metadata={"media_urls": [str(image_path)]},
    )

    assert result.success is True
    assert uploaded[0]["urls"] == [str(image_path)]
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "look"},
        {
            "kind": "image",
            "url": "https://cdn.example.com/generated.png",
            "mime": "image/png",
            "size": 9,
            "name": "generated.png",
        },
    ]


async def test_send_image_file_uploads_local_image(monkeypatch, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    image_path = allowed / "reply.png"
    image_path.write_bytes(b"png-bytes")

    adapter = _make_adapter(media_local_roots=[str(allowed)])

    async def fake_upload(urls, **kwargs):
        assert urls == [str(image_path)]
        return [
            {
                "kind": "image",
                "url": "https://cdn.example.com/reply.png",
                "mime": "image/png",
                "size": 9,
                "name": "reply.png",
            }
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.upload_outbound_media", fake_upload)

    result = await adapter.send_image_file(
        chat_id="u1",
        image_path=str(image_path),
        caption="generated",
        reply_to="msg-1",
    )

    assert result.success is True
    assert adapter._connection.sent_frames[0]["event"] == "message.reply"
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {"kind": "text", "text": "generated"},
        {
            "kind": "image",
            "url": "https://cdn.example.com/reply.png",
            "mime": "image/png",
            "size": 9,
            "name": "reply.png",
        },
    ]


async def test_send_image_file_treats_hermes_file_wrapped_remote_url_as_remote(
    monkeypatch,
):
    adapter = _make_adapter()
    uploaded = []

    async def fake_upload(urls, **kwargs):
        uploaded.append({"urls": urls, "kwargs": kwargs})
        return [
            {
                "kind": "image",
                "url": "https://cdn.example.com/reply.png",
                "mime": "image/png",
                "size": 9,
                "name": "reply.png",
            }
        ]

    monkeypatch.setattr("clawchat_gateway.adapter.upload_outbound_media", fake_upload)

    result = await adapter.send_image_file(
        chat_id="u1",
        image_path="file://https%3A//clawchat.example.com/media/reply.png",
    )

    assert result.success is True
    assert uploaded[0]["urls"] == ["https://clawchat.example.com/media/reply.png"]
    assert adapter._connection.sent_frames[0]["payload"]["message"]["body"]["fragments"] == [
        {
            "kind": "image",
            "url": "https://cdn.example.com/reply.png",
            "mime": "image/png",
            "size": 9,
            "name": "reply.png",
        },
    ]
