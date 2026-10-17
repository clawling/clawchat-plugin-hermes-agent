"""Plugin admission prevents the host seeing cross-snapshot steer input."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from clawchat_gateway import adapter as adapter_mod, host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage
from clawchat_gateway.tool_grants import NON_OWNER_GRANTS_HINT

TOOL = "mcp__docs__read"
CHAT = "cnv_chat"
OWNER_CHAT = "cnv_owner"


def message(sender="usr_a", text="hello", chat=CHAT, kind="direct", synthetic=False, mid=None):
    raw = {"event": "message.send", "chat_id": chat, "chat_type": kind,
           "sender": {"id": sender, "nick_name": sender},
           "payload": {"message_id": mid or f"msg-{sender}-{text}",
                       "message": {"fragments": [{"kind": "text", "text": text}]}}}
    if synthetic:
        raw["synthetic"] = True
    constructor = InboundMessage.internal_synthetic if synthetic else InboundMessage
    return constructor(chat_id=chat, chat_type=kind, sender_id=sender, sender_name=sender,
                       text=text, raw_message=raw, was_mentioned=kind == "group")


async def coalesced(messages):
    from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
    dispatched = []
    async def dispatch(inbound):
        dispatched.append(inbound)
    queue = GroupMessageCoalescer(idle_seconds=3600, max_wait_seconds=3600, dispatch=dispatch)
    try:
        for incoming in messages:
            queue.enqueue(incoming)
        await queue.flush_now(messages[0].chat_id)
        return dispatched[0]
    finally:
        await queue.cancel()


def grant(user, tool=TOOL, mode="direct"):
    return {"grantee_user_id": user, "grantee_name": user, "tool": tool, "mode": mode, "expires_at": None}



@pytest_asyncio.fixture
async def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from clawchat_gateway.tool_capabilities import CAPABILITY_TOOL_NAMES, AUTHORIZATION_TOOLS
    catalog = frozenset(CAPABILITY_TOOL_NAMES) | {TOOL, *AUTHORIZATION_TOOLS}
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (catalog, catalog))
    monkeypatch.setattr(guard, "public_tool_names", lambda _: frozenset())
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner",
                               sediment_on_reset=False, sediment_on_compact=False)
    rows, events = [], []
    a._store = SimpleNamespace(load_grants_for_users=lambda **_: list(rows))
    monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: OWNER_CHAT)
    monkeypatch.setattr(a, "_is_owner_turn", lambda inbound: inbound.chat_type == "direct" and
                        inbound.chat_id == OWNER_CHAT and inbound.sender_id == "usr_owner")
    monkeypatch.setattr(a, "_refuses_turn", lambda _: False)
    monkeypatch.setattr(a, "_maybe_consume_skill_update_consent", AsyncMock(return_value=False))
    monkeypatch.setattr(a, "_ensure_group_participants_metadata", AsyncMock())
    monkeypatch.setattr(a, "_download_inbound_media", AsyncMock(return_value=[]))
    monkeypatch.setattr(a, "_refresh_group_batch_sender_context", lambda inbound: inbound)
    monkeypatch.setattr(a, "_session_names", lambda _: ("Conversation", "Speaker"))
    monkeypatch.setattr(a, "_group_context_for_turn", lambda _: "")
    monkeypatch.setattr(a, "_compose_channel_prompt_parts", lambda _: [])
    monkeypatch.setattr(a, "build_source", lambda **kwargs: SimpleNamespace(platform="clawchat", **kwargs), raising=False)
    monkeypatch.setattr(adapter_mod, "write_llm_context_snapshot", lambda **_: None)
    monkeypatch.setattr(a, "_after_host_turn", lambda _: None)
    monkeypatch.setattr(a, "_settle_held_status_sends", AsyncMock())
    monkeypatch.setattr(a, "_effective_group_settings", lambda _: SimpleNamespace(muted=False, reply_mode="all", batch_delay_seconds=3600))
    monkeypatch.setattr(a, "_claim_message_once", lambda **_: True)
    monkeypatch.setattr(a, "_note_inbound_rate", lambda _: None)
    monkeypatch.setattr(a, "_schedule_profile_sync", lambda coro: coro.close())
    monkeypatch.setattr(a, "_resolve_inbound_sender_context", lambda inbound: inbound)
    monkeypatch.setattr(a, "_direct_text_allowed_on_old_host", lambda _: True)
    for name in ("_answer_owner_group_slash_confirm", "_handle_owner_forwarded_approval", "_answer_non_owner_slash_confirm"):
        monkeypatch.setattr(a, name, AsyncMock(return_value=False))
    a._group_settings_ready.set()
    a._active_sessions = {}
    a._grant_admission_queue._busy_poll_seconds = 0.005
    a._group_message_coalescer._busy_poll_seconds = 0.005

    async def handle(event):
        events.append(event)
        key = f"agent:main:clawchat:{event.source.chat_type}:{event.source.chat_id}"
        if event.source.chat_type != "dm" and a._clawchat_config.group_sessions_per_user:
            key += ":" + event.source.user_id
        a._active_sessions[key] = object()
        await a.on_processing_start(event)
    monkeypatch.setattr(a, "handle_message", handle, raising=False)
    yield SimpleNamespace(adapter=a, rows=rows, events=events, monkeypatch=monkeypatch)
    await a._grant_admission_queue.cancel()
    await a._group_message_coalescer.cancel()
    for event in events:
        turns.release_turn(getattr(event.source, "message_id", ""))
    for key in list(a._grant_sessions):
        a._expire_grant_session(key)


async def wait_for_events(harness, count):
    for _ in range(100):
        if len(harness.events) >= count:
            return
        await asyncio.sleep(0.005)
    assert len(harness.events) >= count


@pytest.mark.asyncio
async def test_steer_input_stays_in_plugin_until_new_snapshot(harness):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    await a._handle_inbound(message(kind="group"))
    first = harness.events[0]
    original = turns.get_snapshot(first.source.message_id)
    assert original.speakers == ("usr_a",) and original.grants == {TOOL: "direct"}
    await a._on_message(message("usr_b", "B", kind="group").raw_message)
    await a._on_message(message("usr_c", "C", kind="group").raw_message)
    await asyncio.sleep(0.02)
    assert harness.events == [first], "B and C must never reach the host's steer/redirect path"
    a._active_sessions.clear()
    await a.on_processing_complete(first, "SUCCESS")
    await wait_for_events(harness, 2)
    second = harness.events[1]
    current = turns.get_snapshot(second.source.message_id)
    assert second.source.message_id != first.source.message_id
    assert current.speakers == ("usr_b", "usr_c") and current.grants == {}
    assert turns.get_snapshot(first.source.message_id) is None
    await a.on_processing_complete(first, "SUCCESS")
    assert turns.get_snapshot(second.source.message_id) is current


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,per_user", [("direct", False), ("group", True)])
async def test_new_grant_waits_behind_ungranted_turn_and_slash(harness, kind, per_user):
    a = harness.adapter
    a._clawchat_config = replace(a._clawchat_config, group_sessions_per_user=per_user)
    await a._handle_inbound(message(kind=kind))
    first = harness.events[0]
    harness.rows.append(grant("usr_a"))
    await a._on_message(message(text="/status", kind=kind).raw_message)
    await asyncio.sleep(0.02)
    assert harness.events == [first]
    a._active_sessions.clear()
    # Polling alone notices idle and revokes the old snapshot.
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(first.source.message_id) is None
    s = turns.get_snapshot(harness.events[1].source.message_id)
    assert s.speakers == ("usr_a",) and s.grants == {TOOL: "direct"}


@pytest.mark.asyncio
async def test_active_direct_grant_holds_ungranted_input(harness):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    await a._handle_inbound(message())
    first = harness.events[0]
    harness.rows.clear()
    await a._on_message(message(text="B").raw_message)
    assert harness.events == [first]
    a._active_sessions.clear()
    await a.on_processing_complete(first, "SUCCESS")
    await wait_for_events(harness, 2)
    s = turns.get_snapshot(harness.events[1].source.message_id)
    assert s.speakers == ("usr_a",) and s.grants == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("off", [False, True])
async def test_empty_or_off_preserves_immediate_dispatch_source_and_toolsets(harness, off):
    a = harness.adapter
    if off:
        harness.rows.append(grant("usr_a"))
        a._clawchat_config = replace(a._clawchat_config, tool_grants="off")
    incoming = message()
    await a._handle_inbound(incoming)
    await a._on_message(message(text="second").raw_message)
    assert len(harness.events) == 2
    expected = dict(platform="clawchat", chat_id=CHAT, user_id="usr_a", chat_name="Conversation",
                    user_name="Speaker", chat_type=a._map_source_chat_type("direct"))
    for event in harness.events:
        source = vars(event.source).copy()
        nonce = source.pop("message_id")
        assert source == expected
        assert turns.get_snapshot(nonce).grants == {}
    harness.monkeypatch.setattr(guard, "_is_known_group", lambda _: False)
    harness.monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off" if off else "on"})
    harness.monkeypatch.setattr(guard, "_narrowed_toolsets", lambda: ["web", guard.NO_MCP])
    assert a.toolsets_for_source(harness.events[0].source) == ["web", guard.NO_MCP]
    assert a.toolsets_for_source(SimpleNamespace(**expected)) == ["web", guard.NO_MCP]


@pytest.mark.asyncio
async def test_dispatch_in_progress_blocks_before_snapshot_creation(harness):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def download(_):
        entered.set()
        await proceed.wait()
        return []
    harness.monkeypatch.setattr(a, "_download_inbound_media", download)
    task = asyncio.create_task(a._handle_inbound(message()))
    await entered.wait()
    harness.rows.clear()
    await a._handle_inbound(message(text="B"))
    assert harness.events == []
    assert a._grant_admission_queue._pending
    proceed.set()
    await task
    assert len(harness.events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "group"])
async def test_admission_timeout_revokes_before_dispatch(harness, kind):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    queue = a._group_message_coalescer if kind == "group" else a._grant_admission_queue
    queue._busy_timeout_seconds = 0.01
    await a._handle_inbound(message(kind=kind))
    first = harness.events[0]
    await a._handle_inbound(message(text="second", kind=kind))
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(first.source.message_id) is None
    assert harness.events[1].source.message_id != first.source.message_id
    assert len(harness.events) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [{"synthetic": True}, {"messages": [{"sender": {"id": "usr_a"}}, {}]}])
async def test_synthetic_or_missing_batch_speaker_has_no_grants(harness, raw):
    harness.rows.append(grant("usr_a"))
    incoming = await coalesced([message(kind="group"), replace(message("", kind="group"), raw_message=raw)])
    await harness.adapter._handle_inbound(incoming)
    s = turns.get_snapshot(harness.events[0].source.message_id)
    assert s.grants == {}


@pytest.mark.asyncio
async def test_storage_read_failure_has_no_grants(harness):
    harness.adapter._store.load_grants_for_users = lambda **_: (_ for _ in ()).throw(RuntimeError())
    await harness.adapter._handle_inbound(message())
    assert turns.get_snapshot(harness.events[0].source.message_id).grants == {}


@pytest.mark.asyncio
async def test_owner_direct_has_verified_empty_snapshot_and_prompt_switch(harness):
    a = harness.adapter
    incoming = message("usr_owner", chat=OWNER_CHAT)
    await a._handle_inbound(incoming)
    s = turns.get_snapshot(harness.events[0].source.message_id)
    assert s.owner_verified and s.grants == {}
    assert a._non_owner_turn_section(incoming) is None
    assert NON_OWNER_GRANTS_HINT in a._non_owner_turn_section(message())
    a._clawchat_config = replace(a._clawchat_config, tool_grants="off")
    assert NON_OWNER_GRANTS_HINT not in a._non_owner_turn_section(message())


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/approve", "/deny", "/new", "/approve ABC123", "/deny ABC123",
                                  "ABC123", "ordinary text"])
async def test_busy_owner_direct_messages_reach_host_without_admission_delay(harness, text):
    a = harness.adapter
    await a._handle_inbound(message("usr_owner", chat=OWNER_CHAT))
    first = turns.get_snapshot(harness.events[0].source.message_id)
    assert first.owner_verified and not first.grants and a._active_sessions
    await a._on_message(message("usr_owner", text, chat=OWNER_CHAT).raw_message)
    assert len(harness.events) == 2
    assert harness.events[-1].text == text
    assert not a._grant_admission_queue._pending
    assert turns.get_snapshot(first.nonce).owner_verified
    assert turns.get_snapshot(harness.events[-1].source.message_id).owner_verified


@pytest.mark.asyncio
async def test_nonce_reply_anchor_is_restored_after_release(harness):
    a = harness.adapter
    incoming = message(mid="real-message-id")
    await a._handle_inbound(incoming)
    event = harness.events[0]
    await a.on_processing_complete(event, "SUCCESS")
    captured = []
    harness.monkeypatch.setattr(a, "_consume_terminal_send", lambda *args, **kwargs: False)
    harness.monkeypatch.setattr(a, "_should_suppress_runtime_status_message", lambda _: False)
    harness.monkeypatch.setattr(a, "_is_runtime_status_text", lambda _: False)
    def hold(chat_id, content, reply_to, *args, **kwargs):
        captured.append(reply_to)
        return True
    harness.monkeypatch.setattr(a, "_hold_unlisted_status", hold)
    await a.send(CHAT, "response", reply_to=event.source.message_id)
    assert captured == ["real-message-id"]
    assert a._grant_reply_anchors[event.source.message_id] == "real-message-id"


@pytest.mark.asyncio
async def test_grant_added_during_preparation_cannot_enter_running_host_turn(harness):
    a = harness.adapter
    await a._handle_inbound(message())
    entered, proceed = asyncio.Event(), asyncio.Event()
    async def download(_):
        entered.set()
        await proceed.wait()
        return []
    harness.monkeypatch.setattr(a, "_download_inbound_media", download)
    task = asyncio.create_task(a._handle_inbound(message(text="B")))
    await entered.wait()
    harness.rows.append(grant("usr_a"))
    proceed.set()
    await task
    assert len(harness.events) == 1
    assert a._grant_admission_queue._pending


@pytest.mark.asyncio
async def test_nested_batches_keep_full_speaker_intersection(harness):
    harness.rows.extend([grant("usr_a"), grant("usr_b"), grant("usr_b", tool="memory")])
    nested = await coalesced([message(kind="group")])
    incoming = await coalesced([nested, message("usr_b", kind="group")])
    await harness.adapter._handle_inbound(incoming)
    s = turns.get_snapshot(harness.events[0].source.message_id)
    assert s.speakers == ("usr_a", "usr_b") and s.grants == {TOOL: "direct"}


@pytest.mark.asyncio
async def test_direct_session_holds_synthetic_or_different_sender(harness):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    await a._handle_inbound(message())
    first = harness.events[0]
    await a._handle_inbound(message("clawchat-awareness", "wake", synthetic=True))
    await a._on_message(message("usr_b", "B").raw_message)
    assert harness.events == [first]
    assert turns.get_snapshot(first.source.message_id).grants == {TOOL: "direct"}
    a._active_sessions.clear()
    await a.on_processing_complete(first, "SUCCESS")
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(harness.events[1].source.message_id).grants == {}


@pytest.mark.asyncio
async def test_idle_without_processing_start_revokes_before_next_dispatch(harness):
    a = harness.adapter
    harness.rows.append(grant("usr_a"))
    async def deferred(event):
        harness.events.append(event)
    harness.monkeypatch.setattr(a, "handle_message", deferred)
    await a._handle_inbound(message())
    old_nonce = harness.events[0].source.message_id
    await a._handle_inbound(message(text="B"))
    assert turns.get_snapshot(old_nonce) is None
    assert len(harness.events) == 2


@pytest.mark.asyncio
async def test_direct_steer_next_snapshot_contains_only_next_speaker_grants(harness):
    a = harness.adapter
    harness.rows.extend([grant("usr_a"), grant("usr_b", tool="memory")])
    await a._handle_inbound(message())
    first = harness.events[0]
    await a._on_message(message("usr_b", "B").raw_message)
    assert harness.events == [first]
    a._active_sessions.clear()
    await a.on_processing_complete(first, "SUCCESS")
    await wait_for_events(harness, 2)
    s = turns.get_snapshot(harness.events[1].source.message_id)
    assert s.nonce != first.source.message_id
    assert s.speakers == ("usr_b",) and s.grants == {"memory": "direct"}


@pytest.mark.asyncio
@pytest.mark.parametrize("off", [False, True])
async def test_empty_or_off_shared_group_keeps_original_busy_wait(harness, off):
    a = harness.adapter
    if off:
        a._clawchat_config = replace(a._clawchat_config, tool_grants="off")
        harness.rows.append(grant("usr_a"))
    a._group_message_coalescer._busy_timeout_seconds = 0.01
    await a._handle_inbound(message(kind="group"))
    first = harness.events[0]
    a._group_message_coalescer.enqueue(message(text="B", kind="group"))
    await a._group_message_coalescer.flush_now(CHAT)
    await asyncio.sleep(0.03)
    assert harness.events == [first]
    a._active_sessions.clear()
    await wait_for_events(harness, 2)
    assert turns.get_snapshot(harness.events[1].source.message_id).grants == {}
