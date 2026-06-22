"""Tests for adapter dispatch gates: mute, reply-mode, idle override (Task 11 TDD).

Mirrors the OpenClaw Task 7/11 parity tests.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from collections import OrderedDict
from types import ModuleType, SimpleNamespace

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
from clawchat_gateway.group_settings import EffectiveSettings, GroupSettings, GroupSettingsCache


# ---------------------------------------------------------------------------
# Shared fakes (mirror test_reply_mode_surface_removed.py)
# ---------------------------------------------------------------------------

class _FakeConnection:
    def __init__(self):
        self.frames = []
        self.send_results = []

    async def send_frame(self, frame, **kwargs):
        self.frames.append((frame, kwargs))
        if self.send_results:
            return self.send_results.pop(0)
        return True


class _FakeStore:
    def __init__(self):
        self.claimed = []

    def claim_message_once(self, **kwargs):
        self.claimed.append(kwargs)
        return True

    def update_message_by_identity(self, **kwargs):
        pass

    def insert_message(self, **kwargs):
        pass

    def get_activation_conversation(self, **_kwargs):
        return None


def _load_adapter_class(monkeypatch):
    gateway = ModuleType("gateway")
    gateway_config = ModuleType("gateway.config")
    gateway_platforms = ModuleType("gateway.platforms")
    gateway_base = ModuleType("gateway.platforms.base")

    class _Platform(str):
        CLAWCHAT = "clawchat"

    class _BasePlatformAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    class _MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MessageType:
        TEXT = "text"

    class _SendResult:
        def __init__(self, success, error=None, message_id=None):
            self.success = success
            self.error = error
            self.message_id = message_id

    gateway_config.Platform = _Platform
    gateway_base.BasePlatformAdapter = _BasePlatformAdapter
    gateway_base.MessageEvent = _MessageEvent
    gateway_base.MessageType = _MessageType
    gateway_base.SendResult = _SendResult
    gateway_platforms.base = gateway_base
    gateway.config = gateway_config
    gateway.platforms = gateway_platforms

    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "gateway.platforms", gateway_platforms)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", gateway_base)

    import clawchat_gateway.adapter as adapter_module
    return importlib.reload(adapter_module).ClawChatAdapter


def _make_adapter(monkeypatch, *, extra=None, group_settings: list[GroupSettings] | None = None):
    """Create a minimal adapter bypassing __init__, similar to test_reply_mode_surface_removed."""
    ClawChatAdapter = _load_adapter_class(monkeypatch)
    adapter = ClawChatAdapter.__new__(ClawChatAdapter)
    adapter._clawchat_config = ClawChatConfig.from_platform_config(
        SimpleNamespace(
            extra={
                "websocket_url": "wss://example.test/ws",
                "token": "token",
                "user_id": "usr_agent",
                **(extra or {}),
            }
        )
    )
    adapter._connection = _FakeConnection()
    adapter._store = _FakeStore()
    adapter._memory_root = None
    adapter._inbound_window = {}
    adapter._known_chat_types = {}
    adapter._owner_approval_routes = {}
    adapter._active_runs_by_id = {}
    adapter._active_chat_runs = {}
    adapter._completed_run_ids = set()
    adapter._completed_run_order = []
    adapter._recent_emits = OrderedDict()
    adapter._reply_preview_by_message_id = {}
    adapter._reply_preview_order = []
    adapter._conversation_metadata_versions = {}
    adapter._plugin_report_tasks = set()
    adapter._profile_sync_tasks = set()
    adapter._run_counter = 0

    # Task 11: these are normally created in __init__
    dispatched_inbound = []

    async def _fake_handle_inbound(inbound):
        dispatched_inbound.append(inbound)

    adapter._group_message_coalescer = GroupMessageCoalescer(
        idle_seconds=10.0,
        max_wait_seconds=30.0,
        dispatch=_fake_handle_inbound,
    )
    adapter._group_settings_cache = GroupSettingsCache()
    if group_settings:
        adapter._group_settings_cache.apply_fetched(group_settings, sequence=1)

    adapter._dispatched_inbound = dispatched_inbound
    return adapter


def _group_frame(
    *,
    chat_id: str = "cnv_group",
    sender_id: str = "usr_sender",
    text: str = "hello group",
    message_id: str = "msg_1",
    context_mentions: list | None = None,
) -> dict:
    return {
        "version": "2",
        "event": "message.send",
        "chat_id": chat_id,
        "chat_type": "group",
        "sender": {"id": sender_id, "nick_name": "Sender"},
        "payload": {
            "message_id": message_id,
            "message": {
                "body": {"fragments": [{"kind": "text", "text": text}]},
                "context": {"mentions": context_mentions or [], "reply": None},
            },
        },
    }


def _mention_frame(
    *,
    chat_id: str = "cnv_group",
    sender_id: str = "usr_sender",
    text: str = "@Agent hi",
    message_id: str = "msg_1",
) -> dict:
    return {
        "version": "2",
        "event": "message.send",
        "chat_id": chat_id,
        "chat_type": "group",
        "sender": {"id": sender_id, "nick_name": "Sender"},
        "payload": {
            "message_id": message_id,
            "message": {
                "body": {
                    "fragments": [
                        {"kind": "mention", "user_id": "usr_agent", "display": "Agent"},
                        {"kind": "text", "text": " hi"},
                    ]
                },
                "context": {
                    "mentions": [{"kind": "mention", "user_id": "usr_agent", "display": "Agent"}],
                    "reply": None,
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Mute gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_muted_group_persists_but_does_not_enqueue(monkeypatch):
    """Backend muted=True: message is persisted (claim_message_once called) but NOT enqueued."""
    adapter = _make_adapter(
        monkeypatch,
        group_settings=[GroupSettings("cnv_group", muted=True, reply_mode="all", batch_delay_seconds=10, version=1)],
    )
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    await adapter._on_message(_group_frame())

    assert len(adapter._store.claimed) == 1, "message must be persisted even when muted"
    assert enqueued == [], "muted message must NOT be enqueued"
    assert adapter._dispatched_inbound == []


@pytest.mark.asyncio
async def test_muted_group_stays_silent_even_when_mentioned(monkeypatch):
    """Muted overrides mention: @-mentioned in a muted group -> no enqueue, no flush."""
    adapter = _make_adapter(
        monkeypatch,
        group_settings=[GroupSettings("cnv_group", muted=True, reply_mode="all", batch_delay_seconds=10, version=1)],
    )
    enqueued = []
    flushed = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    original_flush = adapter._group_message_coalescer.flush_now
    async def _fake_flush(chat_id):
        flushed.append(chat_id)
    adapter._group_message_coalescer.flush_now = _fake_flush

    await adapter._on_message(_mention_frame())

    assert len(adapter._store.claimed) == 1
    assert enqueued == []
    assert flushed == []


@pytest.mark.asyncio
async def test_mention_mode_non_mention_persists_but_does_not_enqueue(monkeypatch):
    """Backend reply_mode='mention', not @-mentioned: persisted, not enqueued."""
    adapter = _make_adapter(
        monkeypatch,
        group_settings=[GroupSettings("cnv_group", muted=False, reply_mode="mention", batch_delay_seconds=10, version=1)],
    )
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    await adapter._on_message(_group_frame())

    assert len(adapter._store.claimed) == 1
    assert enqueued == []


@pytest.mark.asyncio
async def test_backend_reply_mode_all_overrides_static_mention(monkeypatch):
    """Backend reply_mode='all' overrides static group_mode='mention': non-mention dispatched."""
    adapter = _make_adapter(
        monkeypatch,
        extra={"group_mode": "mention"},  # static config says mention-only
        group_settings=[GroupSettings("cnv_group", muted=False, reply_mode="all", batch_delay_seconds=10, version=1)],
    )
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    # Non-mention group message
    await adapter._on_message(_group_frame())

    assert len(adapter._store.claimed) == 1
    assert len(enqueued) == 1, "backend reply_mode=all must override static mention filter"


@pytest.mark.asyncio
async def test_unknown_group_falls_back_to_static_all_mode_enqueues(monkeypatch):
    """No backend settings for chat -> static fallback (all) -> enqueued."""
    adapter = _make_adapter(monkeypatch)  # no group_settings
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    await adapter._on_message(_group_frame())

    assert len(adapter._store.claimed) == 1
    assert len(enqueued) == 1


@pytest.mark.asyncio
async def test_enqueue_passes_batch_delay_as_idle_override(monkeypatch):
    """enqueue is called with idle_seconds_override=batch_delay_seconds from backend."""
    adapter = _make_adapter(
        monkeypatch,
        group_settings=[GroupSettings("cnv_group", muted=False, reply_mode="all", batch_delay_seconds=5, version=1)],
    )
    calls = []
    adapter._group_message_coalescer.enqueue = lambda msg, **kw: calls.append(kw)

    await adapter._on_message(_group_frame())

    assert calls == [{"idle_seconds_override": 5.0}]


@pytest.mark.asyncio
async def test_static_mention_fallback_drops_non_mention_when_no_backend_row(monkeypatch):
    """No backend settings row + static group_mode='mention' + non-mention msg -> claimed but NOT enqueued."""
    adapter = _make_adapter(monkeypatch, extra={"group_mode": "mention"})  # no group_settings
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    await adapter._on_message(_group_frame())  # plain text, no @mention

    assert len(adapter._store.claimed) == 1, "message must be persisted even when static mention gate drops it"
    assert enqueued == [], "non-mention must NOT be enqueued when static group_mode='mention' and no backend row"
    assert adapter._dispatched_inbound == []


# ---------------------------------------------------------------------------
# Group-settings readiness gate (finding #1): a group message that arrives while
# the first per-(re)connect settings refresh is still in flight must WAIT for the
# refresh to land, so a muted group is honored instead of being answered via the
# static fallback before the GET completes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_dispatch_waits_for_first_settings_refresh(monkeypatch):
    """Muted row lands only after the message arrives -> message must NOT enqueue.

    Reproduces the reconnect race: cache starts empty + gate unset; a background
    task populates the muted row shortly after. Without the gate, _on_message
    would enqueue via static-all fallback before the refresh; with the gate it
    waits and honors the mute.
    """
    adapter = _make_adapter(monkeypatch)  # cache empty
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    # Simulate "post-reconnect, refresh in flight": gate unset.
    adapter._group_settings_ready = asyncio.Event()

    async def _land_muted_settings_then_release():
        # Yield so _on_message is parked on the gate before the cache is filled.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        adapter._group_settings_cache.apply_fetched(
            [GroupSettings("cnv_group", muted=True, reply_mode="all", batch_delay_seconds=10, version=1)],
            sequence=1,
        )
        adapter._group_settings_ready.set()

    refresh = asyncio.ensure_future(_land_muted_settings_then_release())
    await adapter._on_message(_group_frame())
    await refresh

    assert len(adapter._store.claimed) == 1, "message must still be persisted"
    assert enqueued == [], "muted (landed during gate wait) must be honored, not enqueued via static fallback"
    assert adapter._dispatched_inbound == []


@pytest.mark.asyncio
async def test_group_dispatch_gate_times_out_and_proceeds(monkeypatch):
    """If the refresh never releases the gate, group dispatch proceeds after timeout."""
    import clawchat_gateway.adapter as adapter_module

    adapter = _make_adapter(monkeypatch)
    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    adapter._group_settings_ready = asyncio.Event()  # never set
    monkeypatch.setattr(adapter_module, "GROUP_SETTINGS_READY_TIMEOUT_SECONDS", 0.01)

    await adapter._on_message(_group_frame())

    assert len(adapter._store.claimed) == 1
    assert len(enqueued) == 1, "after timeout, dispatch proceeds with cached (empty -> static all) settings"


@pytest.mark.asyncio
async def test_gate_timeout_releases_generation_so_later_waiters_do_not_re_pay(monkeypatch):
    """Finding A: when a signal-triggered refresh's REST HANGS, the cleared gate is
    never set by the (parked) refresh `finally`. The FIRST waiter times out and
    proceeds; subsequent group messages must NOT each re-pay the full timeout —
    the first timeout/fallback releases the gate for the CURRENT generation."""
    adapter = _make_adapter(monkeypatch)
    import clawchat_gateway.adapter as adapter_module

    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    # Post-signal: gate cleared, a refresh (seq 1) is "in flight" but its GET hangs,
    # so its finally never runs and the gate stays cleared.
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_fetch_seq = 1

    waits = {"n": 0}
    real_wait_for = asyncio.wait_for

    async def _counting_wait_for(aw, timeout):
        # Only the gate wait uses the (patched, tiny) timeout; count those that
        # actually have to block on the unset event.
        if not adapter._group_settings_ready.is_set():
            waits["n"] += 1
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(adapter_module, "GROUP_SETTINGS_READY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(adapter_module.asyncio, "wait_for", _counting_wait_for)

    # First group message: pays the timeout once, then proceeds (static fallback).
    await adapter._on_message(_group_frame(message_id="m1"))
    assert waits["n"] == 1, "first message pays the gate timeout exactly once"
    assert adapter._group_settings_ready.is_set(), (
        "after the first timeout/fallback the gate must be released for this generation"
    )

    # Subsequent group messages must NOT block on the gate again (gate already set).
    await adapter._on_message(_group_frame(message_id="m2"))
    await adapter._on_message(_group_frame(message_id="m3"))
    assert waits["n"] == 1, "later messages must not each re-pay the full gate timeout"
    assert len(enqueued) == 3, "all three group messages proceed (static fallback)"


@pytest.mark.asyncio
async def test_gate_timeout_release_is_superseded_by_newer_refresh(monkeypatch):
    """A timeout-driven release must be generation-scoped: if a NEWER refresh was
    spawned (incrementing the fetch seq and clearing the gate) after this waiter
    started, the stale waiter's timeout must NOT reopen/set the gate for the newer
    generation."""
    adapter = _make_adapter(monkeypatch)
    import clawchat_gateway.adapter as adapter_module

    adapter._group_settings_ready = asyncio.Event()
    # Waiter starts under generation 1.
    adapter._group_settings_fetch_seq = 1

    monkeypatch.setattr(adapter_module, "GROUP_SETTINGS_READY_TIMEOUT_SECONDS", 0.01)

    # A NEWER refresh (generation 2) is spawned while this generation-1 waiter is
    # parked on the gate: it bumps the fetch seq and (re-)clears the gate. When the
    # stale waiter times out it must NOT set the gate for generation 2.
    real_wait_for = asyncio.wait_for

    async def _bump_seq_during_wait(aw, timeout):
        adapter._group_settings_fetch_seq = 2  # newer refresh supersedes generation 1
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(adapter_module.asyncio, "wait_for", _bump_seq_during_wait)

    await adapter._await_group_settings_ready()

    assert not adapter._group_settings_ready.is_set(), (
        "a stale (generation-1) waiter's timeout must not set the gate for the newer generation 2"
    )


@pytest.mark.asyncio
async def test_non_group_dispatch_not_gated_by_settings(monkeypatch):
    """A direct (non-group) message must NOT block on the group-settings gate."""
    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()  # unset; would block group msgs

    direct_frame = dict(_group_frame())
    direct_frame["chat_type"] = "direct"

    # Stub the direct-path handler so we only assert the gate did not block.
    handled = []

    async def _fake_handle_inbound(inbound):
        handled.append(inbound)

    adapter._handle_inbound = _fake_handle_inbound

    # Must return promptly without waiting on the (never-set) gate.
    await asyncio.wait_for(adapter._on_message(direct_frame), timeout=1.0)

    assert handled, "direct message must be handled without waiting on the group-settings gate"


# ---------------------------------------------------------------------------
# Reconnect race (Issue 2): the settings-ready gate must be CLEARED before the
# first await in the READY/reconnect path, so an in-flight group frame dispatched
# while the state-change handler is parked on an await is gated (waits for the
# refresh) instead of using the stale cache / static fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reconnect_clears_gate_before_first_await(monkeypatch):
    """A group message arriving while _on_state_change(READY) is parked on its
    first await must be gated until the refresh settles (gate cleared up-front)."""
    import clawchat_gateway.adapter as adapter_module
    from clawchat_gateway.connection import ConnectionState

    adapter = _make_adapter(
        monkeypatch,
        # Pre-seed a stale muted row to simulate a cache that predates reconnect.
        group_settings=[
            GroupSettings("cnv_group", muted=True, reply_mode="all", batch_delay_seconds=10, version=1)
        ],
    )
    # Gate starts SET (as it would after a prior connect settled).
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.set()

    # Neutralize the sync bootstrap hooks the READY path calls.
    adapter._schedule_activation_bootstrap = lambda: None
    adapter._schedule_owner_metadata_refresh = lambda: None
    adapter._connection.config = adapter._clawchat_config

    # Block the first await so we can observe the gate state mid-handler.
    enter_await = asyncio.Event()
    release_await = asyncio.Event()

    async def _blocking_reconnect_refresh():
        enter_await.set()
        await release_await.wait()

    adapter._schedule_reconnect_conversation_refresh = _blocking_reconnect_refresh

    # Capture whether the refresh task was dispatched; do NOT let it set the gate
    # yet — we control the gate release manually to observe the in-flight window.
    spawned = []
    adapter._spawn_group_settings_refresh = lambda reason: spawned.append(reason)

    enqueued = []
    adapter._group_message_coalescer.enqueue = lambda msg, **_kw: enqueued.append(msg)

    state_task = asyncio.ensure_future(adapter._on_state_change(ConnectionState.READY))
    await asyncio.wait_for(enter_await.wait(), timeout=1.0)

    # The handler is now parked on its first await. The gate MUST already be
    # cleared, and the refresh MUST already have been dispatched.
    assert not adapter._group_settings_ready.is_set(), "gate must be cleared before the first await"
    assert spawned == ["reconnect"], "settings refresh must be dispatched before the awaited reconnect refresh"

    # A group message arriving during this window must be GATED, not enqueued via
    # the stale cache. Dispatch it and give it a moment to park on the gate.
    msg_task = asyncio.ensure_future(adapter._on_message(_group_frame()))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert enqueued == [], "in-flight group msg must wait on the gate, not use the stale cache"
    assert not msg_task.done(), "group dispatch must still be blocked on the gate"

    # Settle the refresh: land a fresh (un-muted) snapshot, set the gate, release.
    adapter._group_settings_cache.apply_fetched(
        [GroupSettings("cnv_group", muted=False, reply_mode="all", batch_delay_seconds=5, version=2)],
        sequence=2,
    )
    adapter._group_settings_ready.set()
    release_await.set()

    await asyncio.wait_for(state_task, timeout=1.0)
    await asyncio.wait_for(msg_task, timeout=1.0)

    # Now released, the message is processed against the FRESH snapshot.
    assert enqueued, "after the gate releases, the group msg dispatches against the fresh snapshot"


# ---------------------------------------------------------------------------
# FIX 2: group-settings fetch sequence is allocated SYNCHRONOUSLY at spawn time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_settings_sequence_allocated_at_spawn_not_in_task_body(monkeypatch):
    """Two refreshes spawned in order must receive sequences in spawn order,
    regardless of which task body runs first.

    The monotonic sequence must be captured synchronously inside
    ``_spawn_group_settings_refresh`` (before ``ensure_future``); if it were
    incremented inside the task body, a later-spawned task that runs first would
    claim the lower sequence and an older snapshot could overwrite newer state.
    """
    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_fetch_seq = 0

    captured: list[tuple[str, int]] = []

    async def _capture_refresh(*, reason: str, sequence: int) -> None:
        captured.append((reason, sequence))

    adapter._refresh_group_settings = _capture_refresh

    # Spawn "reconnect" first, then "signal" — sequences must follow spawn order
    # even though neither task body has run yet at this point.
    adapter._spawn_group_settings_refresh("reconnect")
    adapter._spawn_group_settings_refresh("signal")

    # Nothing has run yet, but the sequences are already pinned to spawn order.
    assert adapter._group_settings_fetch_seq == 2

    # Let the (out-of-order-schedulable) task bodies run.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    by_reason = dict(captured)
    assert by_reason["reconnect"] == 1, "first spawned refresh must get the lower sequence"
    assert by_reason["signal"] == 2, "later spawned refresh must get the higher sequence"


@pytest.mark.asyncio
async def test_group_settings_auth_error_drives_reactive_refresh(monkeypatch):
    """A 401/403 from the group-settings pull must drive the reactive token
    refresh-and-retry (not be swallowed), then apply the retried snapshot."""
    from clawchat_gateway.api_client import ClawChatApiError
    from clawchat_gateway.group_settings import GroupSettingsFetchResult

    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()
    # Latest spawned sequence == the sequence under test (1): the generation guard
    # releases the gate only for the latest refresh, and the spawn path always
    # allocates the running sequence as the latest.
    adapter._group_settings_fetch_seq = 1

    refresh_calls = {"n": 0}

    async def _reactive_refresh():
        refresh_calls["n"] += 1
        return SimpleNamespace(status="success")

    adapter._connection.reactive_refresh = _reactive_refresh
    adapter._connection.config = adapter._clawchat_config
    adapter._connection.session_device_id = lambda: "dev-1"

    attempts = {"n": 0}

    async def _fake_get_my_group_settings(self):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ClawChatApiError("auth", "token expired", status=401, path="/v1/agents/me/group-settings")
        return GroupSettingsFetchResult(
            authoritative=True,
            rows=[GroupSettings("cnv_group", muted=True, reply_mode="all", batch_delay_seconds=5, version=3)],
        )

    monkeypatch.setattr(
        "clawchat_gateway.api_client.ClawChatApiClient.get_my_group_settings",
        _fake_get_my_group_settings,
    )

    await adapter._refresh_group_settings(reason="signal", sequence=1)

    assert refresh_calls["n"] == 1, "auth error must drive exactly one reactive refresh"
    assert attempts["n"] == 2, "the pull must be retried once after a successful refresh"
    assert adapter._group_settings_ready.is_set(), "gate must be released after the refresh"


# ---------------------------------------------------------------------------
# Issue #2 item 1: gate release must be generation-scoped.
#
# A slow refresh from a previous signal/reconnect that is still running when a
# NEWER reconnect clears the gate must NOT set the gate in its `finally`: doing so
# would release the gate before the newer reconnect's GET lands, letting a group
# message in that window skip the intended wait. Only the LATEST spawned refresh
# (sequence == _group_settings_fetch_seq) may release the gate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_refresh_does_not_release_gate(monkeypatch):
    """An older refresh finishing after a newer one was spawned must leave the
    gate cleared (it is the newer refresh's job to release it)."""
    from clawchat_gateway.group_settings import GroupSettingsFetchResult

    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.clear()
    # Latest spawned sequence is 2 (a newer refresh is in flight); the stale
    # refresh below carries sequence 1.
    adapter._group_settings_fetch_seq = 2

    async def _fake_rest(_call):
        return GroupSettingsFetchResult(authoritative=True, rows=[])

    adapter._rest_with_auth_retry = _fake_rest
    adapter._connection.config = adapter._clawchat_config

    await adapter._refresh_group_settings(reason="reconnect", sequence=1)

    assert not adapter._group_settings_ready.is_set(), (
        "a superseded (stale) refresh must NOT set the gate; the latest refresh owns release"
    )


@pytest.mark.asyncio
async def test_latest_refresh_releases_gate(monkeypatch):
    """The latest spawned refresh (sequence == latest) releases the gate."""
    from clawchat_gateway.group_settings import GroupSettingsFetchResult

    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.clear()
    adapter._group_settings_fetch_seq = 3

    async def _fake_rest(_call):
        return GroupSettingsFetchResult(authoritative=True, rows=[])

    adapter._rest_with_auth_retry = _fake_rest
    adapter._connection.config = adapter._clawchat_config

    await adapter._refresh_group_settings(reason="reconnect", sequence=3)

    assert adapter._group_settings_ready.is_set(), "the latest refresh must release the gate"


@pytest.mark.asyncio
async def test_stale_refresh_no_credentials_does_not_release_gate(monkeypatch):
    """The no-token/base_url early return must also honor the generation guard:
    a superseded refresh with no creds must not release the gate."""
    adapter = _make_adapter(monkeypatch, extra={"token": ""})
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.clear()
    adapter._group_settings_fetch_seq = 2
    # config carries no token -> the early-return path is taken.
    adapter._clawchat_config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://x/ws", "token": "", "user_id": "usr_agent"})
    )

    await adapter._refresh_group_settings(reason="reconnect", sequence=1)

    assert not adapter._group_settings_ready.is_set(), (
        "superseded no-credentials refresh must not release the gate"
    )


@pytest.mark.asyncio
async def test_latest_refresh_no_credentials_releases_gate(monkeypatch):
    """The latest no-credentials refresh still releases the gate (fallback to
    static settings instead of blocking until timeout)."""
    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.clear()
    adapter._group_settings_fetch_seq = 1
    adapter._clawchat_config = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://x/ws", "token": "", "user_id": "usr_agent"})
    )

    await adapter._refresh_group_settings(reason="reconnect", sequence=1)

    assert adapter._group_settings_ready.is_set(), (
        "the latest no-credentials refresh must release the gate (static fallback)"
    )


# ---------------------------------------------------------------------------
# Issue #2 item 3: a signal-triggered settings refresh must CLEAR the gate before
# spawning, so a group message arriving right after a mute/reply-mode change is
# gated and re-evaluated against the fresh GET (not the stale cache).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signal_refresh_clears_gate_before_spawn(monkeypatch):
    """`agent.config.changed` must clear `_group_settings_ready` before spawning
    the refresh, mirroring the reconnect-path protection."""
    adapter = _make_adapter(monkeypatch)
    # Gate starts SET (a prior connect/refresh settled).
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.set()

    spawned: list[str] = []
    gate_state_at_spawn: list[bool] = []

    def _fake_spawn(reason):
        gate_state_at_spawn.append(adapter._group_settings_ready.is_set())
        spawned.append(reason)

    adapter._spawn_group_settings_refresh = _fake_spawn

    await adapter._on_notify_signal(
        {"payload": {"type": "agent.config.changed"}}
    )

    assert spawned == ["signal"], "config-change signal must spawn a settings refresh"
    assert gate_state_at_spawn == [False], (
        "gate must already be CLEARED at the moment the signal refresh is spawned"
    )
    assert not adapter._group_settings_ready.is_set(), (
        "gate stays cleared until the fresh refresh releases it"
    )


@pytest.mark.asyncio
async def test_non_config_signal_does_not_touch_gate(monkeypatch):
    """A non-config signal must NOT clear the gate or spawn a refresh."""
    adapter = _make_adapter(monkeypatch)
    adapter._group_settings_ready = asyncio.Event()
    adapter._group_settings_ready.set()

    spawned: list[str] = []
    adapter._spawn_group_settings_refresh = lambda reason: spawned.append(reason)

    await adapter._on_notify_signal({"payload": {"type": "something.else"}})

    assert spawned == [], "unrelated signals must not spawn a refresh"
    assert adapter._group_settings_ready.is_set(), "unrelated signals must not clear the gate"
