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
        adapter._group_settings_cache.apply_fetched(group_settings)

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
