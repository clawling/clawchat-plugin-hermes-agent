"""Stable system overlays and persisted per-turn user context, with legacy parity."""

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from clawchat_gateway import adapter as adapter_mod, host_tools_guard as guard
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.llm_context_hooks import build_placement_checks
from clawchat_gateway.note_injection import cap_note, fit_turn_budget
from clawchat_gateway.tool_requests import build_request_notice
from test_tool_grants_admission import OWNER_CHAT, coalesced, harness, message

NOW = 1_800_000_000_000


@pytest.fixture
def prompt_env(harness, monkeypatch):
    a = harness.adapter
    a._clawchat_config = replace(a._clawchat_config, tool_grants="off")
    monkeypatch.setattr(a, "_compose_channel_prompt_parts",
                        ClawChatAdapter._compose_channel_prompt_parts.__get__(a))
    metadata = {
        ("owner", "owner"): {"agent_user_id": "usr_agent", "agent_nickname": "Test Agent",
                              "agent_owner_id": "usr_owner", "agent_owner_nickname": "Owner"},
        ("group", "cnv_group"): {"title": "Test Group", "group_owner_id": "usr_owner",
                                  "participant_ids": "usr_agent,usr_owner,usr_a,usr_b"},
        ("user", "usr_a"): {"nickname": "Speaker A"},
        ("user", "usr_b"): {"nickname": "Speaker B"},
    }
    notes = {
        ("owner", "owner"): "Owner prefers brief replies.",
        ("group", "cnv_group"): "This group discusses test fixtures.",
        ("user", "usr_a"): "Speaker A note: likes examples.",
        ("user", "usr_b"): "Speaker B note: likes diagrams.",
    }
    monkeypatch.setattr(a, "_read_memory_metadata", lambda kind, key: dict(metadata.get((kind, key), {})))
    monkeypatch.setattr(a, "_read_memory_body", lambda kind, key: notes.get((kind, key), ""))
    monkeypatch.setattr(a, "_format_agent_files_section", lambda: "## ClawChat Agent Files\n/test/agent-files")
    monkeypatch.setattr(guard, "blocked_tools_hint", lambda **_: "Non-owner tool limits apply.")
    monkeypatch.setattr(guard, "limited_tools_hint", lambda **_: None)
    monkeypatch.setattr(adapter_mod.time, "time", lambda: NOW / 1000)
    # Pin local-time rendering for the baseline byte snapshots on any test host.
    monkeypatch.setattr(adapter_mod, "_format_sent_at", lambda stamp:
                        datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat(timespec="seconds"))
    harness.notes = notes
    harness.snapshots = []
    harness.remembered = []
    monkeypatch.setattr(adapter_mod, "write_llm_context_snapshot", lambda **kw: harness.snapshots.append(kw))
    monkeypatch.setattr(adapter_mod, "remember_injection_parts", lambda **kw: harness.remembered.append(kw))
    return harness


@pytest.fixture
def split_env(prompt_env, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_host_supports_channel_context", lambda: True)
    return prompt_env


def incoming(sender="usr_a", *, kind="direct", mid="msg-first", stamp=NOW, mentioned=False):
    chat = "cnv_group" if kind == "group" else OWNER_CHAT if sender == "usr_owner" else "cnv_peer"
    msg = message(sender, chat=chat, kind=kind, mid=mid)
    raw = dict(msg.raw_message, emitted_at=stamp)
    raw["payload"] = dict(raw["payload"])
    if mentioned:
        raw["payload"]["message"] = dict(raw["payload"]["message"], context={
            "mentions": [{"id": "usr_agent", "display": "Test Agent"}],
        })
    return replace(msg, raw_message=raw, emitted_at=stamp, was_mentioned=mentioned)


async def dispatch(h, inbound):
    h.adapter._active_sessions.clear()
    if h.events:
        await h.adapter.on_processing_complete(h.events[-1], "SUCCESS")
    await h.adapter._handle_inbound_now(inbound)
    return h.events[-1]


def user_ids(h):
    return [p["id"] for p in h.remembered[-1]["parts"] if p["target"] == "user.channel_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["usr_a", "usr_owner"])
async def test_direct_system_stable_across_message_ids_and_times(split_env, sender):
    h = split_env
    first_inbound = incoming(sender)
    second_inbound = incoming(sender, mid="msg-second", stamp=NOW - 120_000)
    first = await dispatch(h, first_inbound)
    second = await dispatch(h, second_inbound)
    assert first.channel_prompt.encode() == second.channel_prompt.encode()
    for event, inbound, mid in ((first, first_inbound, "msg-first"), (second, second_inbound, "msg-second")):
        assert f"message_id: {mid}" in event.channel_context
        assert f"sent_at: {adapter_mod._format_sent_at(inbound.emitted_at)}" in event.channel_context
        assert mid not in event.channel_prompt
        assert "sent_at:" not in event.channel_prompt
        assert "sent_age:" not in event.channel_prompt
        assert event.text == inbound.text
        assert "written by the plugin, not by the sender; its limits are binding." in event.channel_prompt
    limits = h.adapter._non_owner_turn_section(second_inbound)
    if sender == "usr_owner":
        assert limits is None
        assert user_ids(h) == ["turn-metadata", "sender-metadata"]
        assert "Non-owner tool limits" not in second.channel_context
    else:
        assert second.channel_context.startswith(limits + "\n\n")
        assert limits not in second.channel_prompt
        assert user_ids(h) == ["non-owner-turn", "turn-metadata", "sender-metadata"]
    assert "reply_guidance:" in second.channel_prompt  # Direct protocol stays intact.
    assert h.snapshots[-1]["input"]["channelContext"] == second.channel_context
    checks = build_placement_checks(parts=h.remembered[-1]["parts"], request_messages=[
        {"role": "system", "content": second.channel_prompt},
        {"role": "user", "content": f"{second.channel_context}\n\n[New message]\n{second.text}"},
    ])
    assert all(c["found"] and c["role"] == c["target"].split(".")[0] for c in checks)


@pytest.mark.asyncio
async def test_group_system_stable_across_batches_mentions_and_speakers(split_env):
    h = split_env
    first_batch = await coalesced([
        incoming(kind="group", mentioned=True),
        incoming(kind="group", mid="msg-batch-tail", stamp=NOW + 1_000),
    ])
    second_batch = await coalesced([
        incoming("usr_b", kind="group", mid="msg-second", stamp=NOW - 120_000),
    ])
    first = await dispatch(h, first_batch)
    second = await dispatch(h, second_batch)
    assert first.channel_prompt.encode() == second.channel_prompt.encode()
    assert "message_count: 2" in first.channel_context
    assert "message_count: 1" in second.channel_context
    assert "mentions_current_agent: true" in first.channel_context
    assert "mentions_current_agent: true" not in second.channel_context
    for event, sender, other in ((first, "usr_a", "usr_b"), (second, "usr_b", "usr_a")):
        assert h.notes[("user", sender)] in event.channel_context
        assert event.channel_context.count(h.notes[("user", sender)]) == 1
        assert h.notes[("user", other)] not in event.channel_context
        assert "reply_guidance:" in event.channel_context
        assert "reply_guidance:" not in event.channel_prompt
        assert "Speaker Memory" not in event.channel_prompt
        assert h.notes[("user", sender)] not in event.channel_prompt
        assert h.notes[("owner", "owner")] in event.channel_prompt
        assert h.notes[("group", "cnv_group")] in event.channel_prompt
    assert h.adapter._group_reply_guidance(first_batch) != h.adapter._group_reply_guidance(second_batch)
    assert h.adapter._group_reply_guidance(first_batch) in first.channel_context
    assert h.adapter._group_reply_guidance(second_batch) in second.channel_context
    assert user_ids(h) == ["non-owner-turn", "turn-metadata", "group-speaker-memory",
                           "group-message-metadata", "response-guidance"]


@pytest.mark.asyncio
@pytest.mark.parametrize("budget", [100, 500])
async def test_speaker_notes_only_use_budget_left_after_stable_notes(split_env, budget):
    h = split_env
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, note_cap_turn=budget,
                                        note_cap_user=200, note_cap_group=200)
    h.notes[("owner", "owner")] = "Owner profile.\n\n" + "o" * 180
    h.notes[("group", "cnv_group")] = "Group profile.\n\n" + "g" * 180
    h.notes[("user", "usr_a")] = "A profile.\n\n" + "a" * 10_000
    h.notes[("user", "usr_b")] = "B profile.\n\n" + "b" * 20_000
    first = await dispatch(h, incoming(kind="group"))
    second = await dispatch(h, incoming("usr_b", kind="group", mid="msg-second"))
    assert first.channel_prompt == second.channel_prompt
    lengths = [len(h.notes[key]) for key in (("owner", "owner"), ("group", "cnv_group"))]
    ceilings = fit_turn_budget(lengths, budget)
    for key, ceiling in zip((("owner", "owner"), ("group", "cnv_group")), ceilings):
        kind, target = key
        hint = f"clawchat_memory_read targetType={kind} targetId={target}"
        assert cap_note(h.notes[key], ceiling, read_hint=hint) in first.channel_prompt
    remaining = max(0, budget - sum(ceilings))
    if not remaining:
        assert "group-speaker-memory" not in user_ids(h)
        assert "ClawChat Speaker Memory" not in first.channel_context
    else:
        assert "group-speaker-memory" in user_ids(h)
        assert cap_note(h.notes[("user", "usr_b")], remaining,
                        read_hint="clawchat_memory_read targetType=user targetId=usr_b") in second.channel_context


def test_speaker_budget_uses_rendered_stable_note_lengths(split_env):
    h = split_env
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, note_cap_turn=600)
    h.notes[("owner", "owner")] = "Owner profile.\n\n" + "\n\n".join(["o" * 150] * 5)
    h.notes[("group", "cnv_group")] = "Group profile.\n\n" + "\n\n".join(["g" * 150] * 5)
    stable_lengths = []
    for kind, target in (("owner", "owner"), ("group", "cnv_group")):
        rendered = cap_note(h.notes[(kind, target)], 300,
                            read_hint=f"clawchat_memory_read targetType={kind} targetId={target}")
        stable_lengths.append(len(rendered))
    remaining = 600 - sum(stable_lengths)
    assert remaining > 0
    h.notes[("user", "usr_a")] = "Speaker tail.\n\n" + "x" * 5_000
    parts = {p["id"]: p for p in h.adapter._compose_channel_prompt_parts(incoming(kind="group"), split=True)}
    expected = cap_note(h.notes[("user", "usr_a")], remaining,
                        read_hint="clawchat_memory_read targetType=user targetId=usr_a")
    assert expected in parts["group-speaker-memory"]["content"]


@pytest.mark.parametrize("has_speaker_note", [False, True])
def test_empty_stable_notes_leave_full_budget_for_speakers(split_env, has_speaker_note):
    h = split_env
    h.notes.pop(("owner", "owner"))
    h.notes.pop(("group", "cnv_group"))
    if not has_speaker_note:
        h.notes.pop(("user", "usr_a"))
    parts = {p["id"]: p for p in h.adapter._compose_channel_prompt_parts(incoming(kind="group"), split=True)}
    assert "group-memory" not in parts
    assert ("group-speaker-memory" in parts) is has_speaker_note


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "system-render", "user-render", "host-reject"])
async def test_access_notice_user_placement_and_failed_render_rollback(split_env, monkeypatch, failure):
    h = split_env
    h.adapter._clawchat_config = replace(h.adapter._clawchat_config, tool_grants="on")
    notice = {"capability_id": "terminal", "decision_mode": "direct", "notice_claim_key": "test-claim"}
    h.adapter._store.take_pending_notice = Mock(return_value=[notice])
    h.adapter._store.finish_pending_notice = Mock()
    original_render = h.adapter._render_channel_prompt_parts
    if failure in {"system-render", "user-render"}:
        def render(parts):
            if parts and parts[0]["target"] == ("system.channel_prompt" if failure == "system-render" else "user.channel_context"):
                raise RuntimeError("render failed")
            return original_render(parts)
        monkeypatch.setattr(h.adapter, "_render_channel_prompt_parts", render)
        with pytest.raises(RuntimeError, match="render failed"):
            await dispatch(h, incoming())
        assert not h.events
    else:
        if failure == "host-reject":
            original_handle = h.adapter.handle_message
            async def reject(event):
                await original_handle(event)
                event._gateway_accepted = False
            monkeypatch.setattr(h.adapter, "handle_message", reject)
        event = await dispatch(h, incoming())
        text = build_request_notice(notice)
        assert event.channel_context.endswith(text)
        assert text not in event.channel_prompt
        assert user_ids(h)[-1] == "access-request-notice"
    h.adapter._store.finish_pending_notice.assert_called_with(
        account_id="usr_agent", claim_key="test-claim", accepted=failure is None,
    )


@pytest.mark.asyncio
async def test_existing_context_is_preserved_and_debugged(split_env, monkeypatch, caplog):
    h = split_env
    original_event = adapter_mod.MessageEvent
    def with_context(**kwargs):
        return original_event(channel_context="Existing host context", **kwargs)
    monkeypatch.setattr(adapter_mod, "MessageEvent", with_context)
    monkeypatch.setattr(adapter_mod, "_debug_prompt_injection_enabled", lambda: True)
    event = await dispatch(h, incoming())
    assert event.channel_context.startswith("Existing host context\n\n" + h.adapter._non_owner_turn_section(incoming()))
    assert adapter_mod.DEBUG_CHANNEL_CONTEXT_BEGIN in caplog.text
    assert event.channel_context in caplog.text
    assert adapter_mod.DEBUG_CHANNEL_CONTEXT_END in caplog.text


def test_channel_context_support_requires_a_declared_dataclass_field(monkeypatch):
    @dataclass
    class Supported:
        channel_context: str | None = None
    @dataclass
    class Old:
        text: str = ""
    for event_type, expected in ((Supported, True), (Old, False), (lambda **kw: None, False)):
        monkeypatch.setattr(adapter_mod, "MessageEvent", event_type)
        assert adapter_mod._host_supports_channel_context() is expected


# SHA-256 of the full rendered bytes from commit 92b977f, using prompt_env and
# incoming() above. These pin content as well as the legacy part order/targets.
LEGACY_DIGESTS = {"direct": "874bf65024341e917b1b3f42256af388dd953acc2e075e6401db38cbeb0d3995", "group": "508c0c947dc59b9b5a47932034d4dd3e722970d74ff1a69a648a146a4e59f11c"}
LEGACY_IDS = {
    "direct": ["conversation-semantics", "metadata-glossary", "turn-metadata", "agent-profile",
               "owner-metadata-1", "owner-metadata-2", "current-agent", "peer-profile", "memory-routing", "non-owner-turn",
               "peer-memory", "sender-metadata", "response-protocol"],
    "group": ["conversation-semantics", "metadata-glossary", "turn-metadata", "agent-profile",
              "owner-metadata-1", "owner-metadata-2", "current-agent", "group-profile", "group-participants", "memory-routing",
              "non-owner-turn", "group-memory", "group-message-metadata", "response-protocol"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "group"])
async def test_old_host_preserves_full_legacy_bytes_and_part_order(prompt_env, kind):
    h = prompt_env
    inbound = incoming(kind=kind, mentioned=kind == "group")
    assert adapter_mod._host_supports_channel_context() is False  # Unmodified conftest double.
    parts = h.adapter._compose_channel_prompt_parts(inbound, split=False)
    assert [p["id"] for p in parts] == LEGACY_IDS[kind]
    assert all(p["target"] == "system.channel_prompt" for p in parts)
    event = await dispatch(h, inbound)
    assert event.channel_prompt == h.adapter._render_channel_prompt_parts(parts)
    assert hashlib.sha256(event.channel_prompt.encode()).hexdigest() == LEGACY_DIGESTS[kind]
    assert not getattr(event, "channel_context", None)


@pytest.mark.asyncio
async def test_old_host_debug_log_keeps_the_legacy_format(prompt_env, monkeypatch, caplog):
    monkeypatch.setattr(adapter_mod, "_debug_prompt_injection_enabled", lambda: True)
    await dispatch(prompt_env, incoming())
    assert adapter_mod.DEBUG_PROMPT_INJECTION_BEGIN in caplog.text
    assert adapter_mod.DEBUG_CHANNEL_CONTEXT_BEGIN not in caplog.text


def test_snapshot_writer_keeps_channel_context(tmp_path):
    import json
    from clawchat_gateway.llm_context_debug import ClawChatLlmContextDebug

    writer = ClawChatLlmContextDebug(env={
        "CLAWCHAT_LLM_CONTEXT_DEBUG": "1",
        "CLAWCHAT_LLM_CONTEXT_SNAPSHOT_DIR": str(tmp_path),
    })
    path = writer.write_snapshot(
        visibility="host_event",
        trace={"messageId": "msg-1"},
        input={"injectedPrompt": "stable", "channelContext": "per-turn", "eventText": "hi"},
    )
    assert json.loads(open(path).read())["input"]["channelContext"] == "per-turn"
