"""Agent reply policy runs after recording and before host admission."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from clawchat_gateway import adapter as adapter_module
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.clawchat_memory import write_clawchat_metadata
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import parse_inbound_message

ME = "usr_self"
PEER = "usr_peer"
OWNER = "usr_owner"
CHAT = "cnv_test"


def frame(mid="msg_one", *, group=False, sender=PEER, mentions=(), fragment_mentions=False,
          text="hello"):
    fragments = [{"kind": "text", "text": text}]
    if fragment_mentions:
        fragments = [{"kind": "mention", "user_id": uid, "display": "member"}
                     for uid in mentions] + fragments
    return {
        "event": "message.send", "chat_id": CHAT,
        "chat_type": "group" if group else "direct",
        "sender": {"id": sender, "nick_name": "Peer"},
        "payload": {"message_id": mid, "message": {
            "fragments": fragments,
            "context": {"mentions": [] if fragment_mentions else list(mentions)},
        }},
    }


@pytest.fixture
async def adapter(monkeypatch, tmp_path):
    a = ClawChatAdapter({})
    # A REST endpoint and token are configured, so live profile lookups run.
    a._clawchat_config = replace(a._clawchat_config, user_id=ME, owner_user_id=OWNER,
                                 base_url="https://example.invalid", token="placeholder-token")
    a._memory_root = tmp_path / "memory"
    assert a._store is not None
    a._group_settings_ready.set()
    a._handle_inbound = AsyncMock()
    a.send_typing = AsyncMock()
    a._handle_owner_forwarded_approval = AsyncMock(return_value=False)
    monkeypatch.setattr(a, "_schedule_profile_sync", lambda coro: coro.close())
    api = SimpleNamespace(get_user_info=AsyncMock(return_value={"user": {"profile_type": "agent"}}))
    monkeypatch.setattr(a, "_new_api_client", lambda: api)
    yield a, api
    await a._group_message_coalescer.cancel()


def cache(a, profile_type, sender=PEER):
    write_clawchat_metadata(a._memory_root, "user", sender,
                           {"id": sender, "profile_type": profile_type})


def transcript(a):
    return a._store.list_recent_group_transcript("default", CHAT, 100)


async def flush(a):
    for key in list(a._group_message_coalescer._pending):
        await a._group_message_coalescer.flush_now(key)


async def test_default_direct_agent_is_recorded_without_host_admission(adapter, caplog):
    a, api = adapter
    cache(a, "agent")
    a._admit_owner_direct_inbound = Mock(side_effect=AssertionError("host admission"))
    a._hold_owner_inbound = Mock(side_effect=AssertionError("session queue"))
    with caplog.at_level(logging.INFO, logger="clawchat_gateway.adapter"):
        outcome = await a._on_message(frame(text="placeholder-message-body"))
    assert outcome is None  # Completed normally, so the transport can ack.
    a._handle_inbound.assert_not_awaited()
    a.send_typing.assert_not_awaited()
    api.get_user_info.assert_not_awaited()
    assert transcript(a)[0]["text"] == "placeholder-message-body"
    assert "auto reply suppressed" in caplog.text
    assert "placeholder-message-body" not in caplog.text
    assert not a._group_message_coalescer._pending


async def test_direct_human_dispatches(adapter):
    a, api = adapter
    cache(a, "user")
    await a._on_message(frame())
    a._handle_inbound.assert_awaited_once()
    api.get_user_info.assert_not_awaited()


@pytest.mark.parametrize("group", [False, True])
async def test_opt_in_restores_agent_replies(adapter, group):
    a, _ = adapter
    cache(a, "agent")
    a._clawchat_config = replace(a._clawchat_config, reply_to_agents=True)
    await a._on_message(frame(group=group))
    await flush(a)
    a._handle_inbound.assert_awaited_once()


@pytest.mark.parametrize("mentions", [(), ("all",), ("usr_other",)])
async def test_group_agent_requires_explicit_self_mention(adapter, mentions):
    a, _ = adapter
    cache(a, "agent")
    assert a._clawchat_config.group_mode == "all"
    await a._on_message(frame(group=True, mentions=mentions))
    await flush(a)
    a._handle_inbound.assert_not_awaited()
    a.send_typing.assert_not_awaited()
    assert len(transcript(a)) == 1


@pytest.mark.parametrize("fragment_mentions", [False, True])
@pytest.mark.parametrize("mode", ["all", "mention"])
async def test_group_agent_self_mention_dispatches(adapter, fragment_mentions, mode):
    a, _ = adapter
    cache(a, "agent")
    a._clawchat_config = replace(a._clawchat_config, group_mode=mode)
    await a._on_message(frame(group=True, mentions=(ME,), fragment_mentions=fragment_mentions))
    a._handle_inbound.assert_awaited_once()  # Mentions flush immediately.


@pytest.mark.parametrize(("mode", "mentions", "dispatch"), [
    ("all", (), True), ("mention", (), False),
    ("mention", ("all",), True), ("mention", (ME,), True),
])
async def test_group_human_keeps_existing_reply_modes(adapter, mode, mentions, dispatch):
    a, _ = adapter
    cache(a, "user")
    a._clawchat_config = replace(a._clawchat_config, group_mode=mode)
    await a._on_message(frame(group=True, mentions=mentions))
    await flush(a)
    assert a._handle_inbound.await_count == int(dispatch)


@pytest.mark.parametrize("legacy_type", [False, True])
async def test_uncached_rest_profile_is_reused(adapter, legacy_type):
    a, api = adapter
    api.get_user_info.return_value = {"user": {"type" if legacy_type else "profile_type": "agent"}}
    await a._on_message(frame())
    await a._on_message(frame("msg_two"))
    api.get_user_info.assert_awaited_once_with(PEER)
    a._handle_inbound.assert_not_awaited()
    assert len(transcript(a)) == 2


@pytest.mark.parametrize("failure", ["exception", "missing_type", "no_memory"])
async def test_unavailable_profile_fails_open_with_one_safe_warning(adapter, caplog, failure):
    a, api = adapter
    if failure == "exception":
        api.get_user_info.side_effect = RuntimeError("placeholder-profile-secret")
    elif failure == "missing_type":
        api.get_user_info.return_value = {"user": {}}
    else:
        a._memory_root = None
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.adapter"):
        await a._on_message(frame())
        await a._on_message(frame("msg_two"))
    assert a._handle_inbound.await_count == 2
    assert api.get_user_info.await_count == (0 if failure == "no_memory" else 1)
    warnings = [r for r in caplog.records if "treating as human" in r.getMessage()]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert "placeholder-profile-secret" not in caplog.text


async def test_concurrent_cache_misses_share_one_sender_lookup(adapter):
    a, api = adapter
    messages = [parse_inbound_message(frame(mid), a._clawchat_config) for mid in ("one", "two")]
    assert await asyncio.gather(*(a._suppress_agent_auto_reply(m) for m in messages)) == [True, True]
    api.get_user_info.assert_awaited_once_with(PEER)


async def test_profile_failure_retries_after_backoff(adapter):
    a, api = adapter
    api.get_user_info.side_effect = RuntimeError("profile unavailable")
    await a._on_message(frame())
    api.get_user_info.side_effect = None
    a._agent_profile_retry_after[PEER] = 0
    await a._on_message(frame("retry"))
    assert api.get_user_info.await_count == 2
    assert a._handle_inbound.await_count == 1


async def test_background_profile_refresh_redacts_error_at_source(adapter, caplog):
    a, api = adapter
    api.get_user_info.side_effect = RuntimeError("token=placeholder-profile-secret")
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.adapter"):
        assert not await a._refresh_user_profile(PEER)
    assert "profile refresh failed" in caplog.text
    assert "placeholder-profile-secret" not in caplog.text


async def test_suppressed_message_completes_receipt_and_is_acked(adapter, monkeypatch):
    from test_reliable_delivery_v2 import peer, rows, until

    a, _ = adapter
    cache(a, "agent")
    async with peer(monkeypatch, a._store, on_message=a._on_message) as transport:
        message = frame()
        message["dseq"] = 1
        transport.ws.feed(message)
        await until(lambda: bool(transport.ws.acks) and not transport.conn._inbound_lanes)
        assert transport.ws.acks[-1]["payload"]["dseq"] == 1
        assert rows(a._store)[0]["status"] == "done"
        a._handle_inbound.assert_not_awaited()
        assert len(transcript(a)) == 1


async def test_recovery_applies_policy_and_finishes_history_journal(adapter):
    a, _ = adapter
    cache(a, "agent")
    message = parse_inbound_message(frame(), a._clawchat_config)
    a._history = SimpleNamespace(no_reply=Mock())
    await a._continue_claimed_inbound_now(message)
    a._history.no_reply.assert_called_once_with(message)
    a._handle_inbound.assert_not_awaited()


async def test_recovered_mixed_batch_uses_original_human_trigger(adapter):
    a, _ = adapter
    cache(a, "agent")
    cache(a, "user", "usr_human")
    human = frame("human", group=True, sender="usr_human")
    agent = frame("agent", group=True)
    batch = replace(parse_inbound_message(agent, a._clawchat_config),
                    raw_message={"clawchat_group_batch": True, "messages": [human, agent]})
    assert not await a._suppress_agent_auto_reply(batch)
    batch = replace(batch, raw_message={"clawchat_group_batch": True, "messages": [agent]})
    assert await a._suppress_agent_auto_reply(batch)


async def test_suppression_precedes_history_maintenance(adapter):
    a, _ = adapter
    cache(a, "agent")
    history = SimpleNamespace(claim_options=Mock(return_value={}), no_reply=Mock(),
                              enabled=Mock(side_effect=AssertionError("maintenance admission")))
    a._history = history
    await a._on_message(frame())
    history.no_reply.assert_called_once()
    assert len(transcript(a)) == 1


async def test_suppression_durably_finishes_history_journal_without_opening_session(adapter):
    from clawchat_gateway.history_maintenance import HistoryMaintenance

    a, _ = adapter
    cache(a, "agent")
    a.build_source = lambda **kwargs: SimpleNamespace(**kwargs)
    history = HistoryMaintenance(a)
    history.available = True
    history.read_route = Mock(side_effect=AssertionError("host route read"))
    history.gateway = SimpleNamespace(
        session_store=SimpleNamespace(peek_session_id=Mock(side_effect=AssertionError("host session"))),
        _session_key_for_source=lambda source: "test-history-key",
    )
    a._history = history
    try:
        await a._on_message(frame())
        assert a._store.list_held(ME, "test-history-key") == []
        completed = a._store.list_held(ME, "test-history-key", states=("dispatched",))
        assert len(completed) == 1
        a._handle_inbound.assert_not_awaited()
    finally:
        await history.close()


@pytest.mark.parametrize("per_speaker", [False, True])
async def test_suppressed_group_message_is_context_for_later_human(adapter, per_speaker):
    a, _ = adapter
    a._clawchat_config = replace(a._clawchat_config, group_sessions_per_user=per_speaker)
    cache(a, "agent")
    cache(a, "user", "usr_human")
    # An existing session must catch up even when the human does not mention us.
    a._store.mark_group_shared_session("default", CHAT)
    await a._on_message(frame("agent_context", group=True, text="context from peer"))
    await a._on_message(frame("human_trigger", group=True, sender="usr_human", text="please reply"))
    await flush(a)
    a._handle_inbound.assert_awaited_once()
    trigger = a._handle_inbound.call_args.args[0]
    context = a._group_context_for_turn(trigger)
    assert "context from peer" in context
    assert "please reply" not in context
    assert a._group_context_for_turn(trigger) is None


@pytest.mark.parametrize("sender", [ME, OWNER])
async def test_self_still_skipped_and_owner_unaffected(adapter, sender):
    a, api = adapter
    await a._on_message(frame(sender=sender))
    assert a._handle_inbound.await_count == int(sender == OWNER)
    api.get_user_info.assert_not_awaited()


@pytest.mark.parametrize(("extra", "env", "expected"), [
    ({}, None, False), ({"reply_to_agents": False}, None, False),
    ({"reply_to_agents": True}, None, True), ({}, "true", True),
    ({}, "1", True), ({}, "on", True), ({}, "yes", True),
    ({"reply_to_agents": True}, "false", False),
    ({"reply_to_agents": False}, "true", True),
    ({"reply_to_agents": "true"}, None, False),
    ({"reply_to_agents": True}, "invalid", False),
    ({"groups": {CHAT: {"reply_to_agents": True}}}, None, False),
])
def test_configuration_parsing(monkeypatch, extra, env, expected):
    if env is not None:
        monkeypatch.setenv("CLAWCHAT_REPLY_TO_AGENTS", env)
    config = ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra))
    assert config.reply_to_agents is expected
    assert ClawChatConfig(websocket_url="wss://example.invalid/ws").reply_to_agents is False


async def test_recovery_reads_cache_only_and_treats_unknown_sender_as_human(adapter):
    a, api = adapter
    message = parse_inbound_message(frame(), a._clawchat_config)
    assert not await a._suppress_agent_auto_reply(message, fetch=False)
    api.get_user_info.assert_not_awaited()


async def test_slow_profile_lookup_times_out_to_human(adapter, monkeypatch):
    a, _ = adapter
    monkeypatch.setattr(adapter_module, "_AGENT_PROFILE_LOOKUP_TIMEOUT_SECONDS", 0.01)

    async def hang(*_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(a, "_refresh_user_profile", hang)
    message = parse_inbound_message(frame(), a._clawchat_config)
    assert not await a._suppress_agent_auto_reply(message)
    # The failed lookup backs off instead of stalling every later message.
    assert a._agent_profile_retry_after[PEER] > 0


async def test_failed_lookup_backoff_pauses_background_profile_refresh(adapter, monkeypatch):
    a, _ = adapter
    scheduled = []

    def schedule(coro):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(a, "_schedule_profile_sync", schedule)
    a._agent_profile_retry_after[PEER] = adapter_module.time.monotonic() + 60
    await a._on_message(frame())
    assert not scheduled
    a._agent_profile_retry_after[PEER] = 0
    await a._on_message(frame("second"))
    assert len(scheduled) == 1


async def test_recovery_does_not_wait_for_a_live_lookup_lock(adapter):
    a, _ = adapter
    cache(a, "agent")
    message = parse_inbound_message(frame(), a._clawchat_config)
    lock = a._agent_profile_locks.setdefault(PEER, asyncio.Lock())
    await lock.acquire()
    try:
        assert await asyncio.wait_for(a._suppress_agent_auto_reply(message, fetch=False), 1)
    finally:
        lock.release()


async def test_no_rest_endpoint_skips_live_lookup(adapter):
    a, api = adapter
    a._clawchat_config = replace(a._clawchat_config, base_url="")
    message = parse_inbound_message(frame(), a._clawchat_config)
    assert not await a._suppress_agent_auto_reply(message)
    api.get_user_info.assert_not_awaited()


def test_default_group_bio_does_not_forbid_replying_to_agents():
    from clawchat_gateway.plugin_prompts import default_group_bio_prompt

    bio = default_group_bio_prompt()
    assert "reply to other bots" not in bio.lower()
    assert "reply to other agents" not in bio.lower()
    assert "## What to be careful about here" in bio
    assert "Do not act publicly for the owner" in bio
