"""Task 12 TDD: context-on-mention for group chats (Hermes).

Tests:
  1. list_recent_group_messages returns last N oldest-first (storage layer).
  2. A mention turn in a group prepends prior context (adapter layer).
  3. A multi-message coalesced batch where all batched messages also appear in
     list_recent_group_messages results → NONE duplicated in prepended context
     (the bug the OpenClaw task hit).
"""
from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from collections import OrderedDict
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
from clawchat_gateway.group_settings import EffectiveSettings, GroupSettings, GroupSettingsCache
from clawchat_gateway.storage import ClawChatStore


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

def _store(tmp_path) -> ClawChatStore:
    s = ClawChatStore(tmp_path / "clawchat.sqlite")
    s.initialize()
    return s


def _insert_msg(store: ClawChatStore, *, account_id: str, chat_id: str, message_id: str, text: str, created_at: int) -> None:
    """Insert a raw inbound message row (bypassing claim_message_once dedup)."""
    conn = sqlite3.connect(store.db_path)
    try:
        conn.execute(
            """
            INSERT INTO clawchat_messages(
              platform, account_id, kind, direction, event_type,
              chat_id, message_id, text, raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("hermes", account_id, "message", "inbound", "message.send",
             chat_id, message_id, text, None, created_at),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Storage test: list_recent_group_messages returns last N, oldest-first
# ---------------------------------------------------------------------------

def test_list_recent_group_messages_last_n_oldest_first(tmp_path):
    """Insert 15 messages with increasing created_at, expect last 10, [0]=='t5', [9]=='t14'."""
    store = _store(tmp_path)
    account_id = "usr_agent"
    chat_id = "cnv_group"
    for i in range(15):
        _insert_msg(
            store,
            account_id=account_id,
            chat_id=chat_id,
            message_id=f"msg_{i}",
            text=f"t{i}",
            created_at=1000 + i,
        )
    result = store.list_recent_group_messages(account_id, chat_id, 10)
    assert len(result) == 10, f"Expected 10, got {len(result)}"
    assert result[0]["text"] == "t5", f"Expected t5 at [0], got {result[0]['text']}"
    assert result[9]["text"] == "t14", f"Expected t14 at [9], got {result[9]['text']}"


def test_list_recent_group_messages_returns_fewer_when_not_enough(tmp_path):
    """When fewer than N messages exist, returns all of them."""
    store = _store(tmp_path)
    for i in range(3):
        _insert_msg(store, account_id="usr_a", chat_id="cnv_g", message_id=f"m{i}", text=f"t{i}", created_at=1000 + i)
    result = store.list_recent_group_messages("usr_a", "cnv_g", 10)
    assert len(result) == 3
    assert result[0]["text"] == "t0"
    assert result[2]["text"] == "t2"


def test_list_recent_group_messages_scoped_to_account_and_chat(tmp_path):
    """Messages from another account or chat must not appear."""
    store = _store(tmp_path)
    _insert_msg(store, account_id="usr_a", chat_id="cnv_g", message_id="m1", text="mine", created_at=1000)
    _insert_msg(store, account_id="usr_b", chat_id="cnv_g", message_id="m2", text="other_account", created_at=1001)
    _insert_msg(store, account_id="usr_a", chat_id="cnv_other", message_id="m3", text="other_chat", created_at=1002)
    result = store.list_recent_group_messages("usr_a", "cnv_g", 10)
    assert len(result) == 1
    assert result[0]["text"] == "mine"


def test_list_recent_group_messages_empty_when_no_rows(tmp_path):
    store = _store(tmp_path)
    result = store.list_recent_group_messages("usr_a", "cnv_g", 10)
    assert result == []


def test_list_recent_group_messages_row_shape(tmp_path):
    """Each row dict has at least message_id, text, created_at."""
    store = _store(tmp_path)
    _insert_msg(store, account_id="usr_a", chat_id="cnv_g", message_id="msg_shape", text="hello", created_at=9999)
    result = store.list_recent_group_messages("usr_a", "cnv_g", 10)
    assert len(result) == 1
    row = result[0]
    assert row["message_id"] == "msg_shape"
    assert row["text"] == "hello"
    assert row["created_at"] == 9999


# ---------------------------------------------------------------------------
# Adapter fakes (mirrored from test_adapter_dispatch.py)
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
    """Fake store that also supports list_recent_group_messages."""

    def __init__(self, recent_rows: list[dict] | None = None):
        self.claimed: list[dict] = []
        self._recent_rows = recent_rows or []

    def claim_message_once(self, **kwargs):
        self.claimed.append(kwargs)
        return True

    def update_message_by_identity(self, **kwargs):
        pass

    def insert_message(self, **kwargs):
        pass

    def get_activation_conversation(self, **_kwargs):
        return None

    def list_recent_group_messages(self, account_id: str, chat_id: str, limit: int) -> list[dict]:
        return list(self._recent_rows)


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


class _FakeSource:
    pass


def _make_adapter(monkeypatch, *, extra=None, recent_rows: list[dict] | None = None, group_settings: list[GroupSettings] | None = None):
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
    adapter._store = _FakeStore(recent_rows=recent_rows)
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

    # Stub out Hermes infrastructure methods that _handle_inbound needs
    adapter.build_source = lambda **_kw: _FakeSource()
    adapter._map_source_chat_type = lambda ct: ct
    adapter._extract_reply_fields = lambda reply: (None, None)
    adapter._session_user_id_for_inbound = lambda inbound: inbound.sender_id
    adapter._compose_channel_prompt_parts = lambda inbound: []
    adapter._render_channel_prompt_parts = lambda parts: None
    adapter.write_llm_context_snapshot = lambda **_kw: None

    async def _fake_download_inbound_media(inbound):
        return []

    adapter._download_inbound_media = _fake_download_inbound_media

    dispatched_events: list[Any] = []

    async def _fake_handle_message(event):
        dispatched_events.append(event)

    adapter.handle_message = _fake_handle_message

    dispatched_inbound: list[Any] = []

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
    adapter._dispatched_events = dispatched_events
    return adapter


def _mention_frame(
    *,
    chat_id: str = "cnv_group",
    sender_id: str = "usr_sender",
    text: str = "@Agent hi",
    message_id: str = "msg_mention",
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


def _group_frame(
    *,
    chat_id: str = "cnv_group",
    sender_id: str = "usr_other",
    text: str = "background msg",
    message_id: str = "msg_bg",
) -> dict:
    return {
        "version": "2",
        "event": "message.send",
        "chat_id": chat_id,
        "chat_type": "group",
        "sender": {"id": sender_id, "nick_name": "Other"},
        "payload": {
            "message_id": message_id,
            "message": {
                "body": {"fragments": [{"kind": "text", "text": text}]},
                "context": {"mentions": [], "reply": None},
            },
        },
    }


# ---------------------------------------------------------------------------
# Adapter test 1: mention turn prepends prior context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mention_turn_prepends_prior_context(monkeypatch):
    """A @-mention in a group turn causes prior context to be prepended to event.text."""
    recent_rows = [
        {"message_id": "msg_prior_1", "text": "first prior message", "created_at": 100},
        {"message_id": "msg_prior_2", "text": "second prior message", "created_at": 200},
    ]
    adapter = _make_adapter(monkeypatch, recent_rows=recent_rows)

    # Directly test _handle_inbound with a pre-built group batch (mention)
    from clawchat_gateway.inbound import InboundMessage
    inbound = InboundMessage(
        chat_id="cnv_group",
        chat_type="group",
        sender_id="usr_sender",
        sender_name="Sender",
        text="@Agent hi",
        raw_message={
            "clawchat_group_batch": True,
            "messages": [_mention_frame()],
        },
        was_mentioned=True,
    )

    await adapter._handle_inbound(inbound)

    assert len(adapter._dispatched_events) == 1, "Should dispatch exactly one event"
    event = adapter._dispatched_events[0]
    # The event text must contain the prior context before the current messages
    assert "first prior message" in event.text, "Prior context row 1 must appear in event text"
    assert "second prior message" in event.text, "Prior context row 2 must appear in event text"
    # Context must appear BEFORE the current turn text
    idx_prior = event.text.find("first prior message")
    idx_mention = event.text.find("@Agent")
    assert idx_prior < idx_mention, "Prior context must come before the current mention text"


# ---------------------------------------------------------------------------
# Adapter test 2: multi-message batch dedup — no duplicates from batched messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mention_batch_no_duplicate_batched_messages_in_context(monkeypatch):
    """
    Coalesced batch of 2 messages + mention: all batched message_ids are in
    list_recent_group_messages results → NONE of them duplicated in prior context.

    This is the bug the OpenClaw task hit: if dedupe only checked the triggering
    message_id (not ALL constituent IDs), the earlier batched messages would appear
    twice (once in the turn body, once in prior context).
    """
    # The batch will contain msg_bg (background) + msg_mention (mention)
    bg_id = "msg_bg"
    mention_id = "msg_mention"

    # Recent rows include BOTH batched message_ids (simulating they were stored before dispatch)
    recent_rows = [
        {"message_id": "msg_old_1", "text": "old message 1", "created_at": 50},
        {"message_id": bg_id, "text": "background msg", "created_at": 100},  # in batch → must be deduped
        {"message_id": mention_id, "text": "@Agent hi", "created_at": 200},  # in batch → must be deduped
    ]
    adapter = _make_adapter(monkeypatch, recent_rows=recent_rows)

    from clawchat_gateway.inbound import InboundMessage
    # Build a coalesced batch with two messages
    inbound = InboundMessage(
        chat_id="cnv_group",
        chat_type="group",
        sender_id="usr_sender",
        sender_name="Sender",
        text="ClawChat group messages:\n[message 1] Other: background msg\n[message 2] Sender: @Agent hi",
        raw_message={
            "clawchat_group_batch": True,
            "messages": [_group_frame(), _mention_frame()],
        },
        was_mentioned=True,
    )

    await adapter._handle_inbound(inbound)

    assert len(adapter._dispatched_events) == 1
    event = adapter._dispatched_events[0]

    # Old message must appear in context (not in batch)
    assert "old message 1" in event.text, "Non-batched prior context row must appear"

    # Count occurrences: "background msg" must appear exactly ONCE (in the turn body, not repeated in context)
    bg_count = event.text.count("background msg")
    assert bg_count == 1, (
        f"'background msg' appeared {bg_count} times — batched message must NOT be duplicated in prior context"
    )

    # Count occurrences: "@Agent hi" must appear exactly ONCE
    mention_count = event.text.count("@Agent hi")
    assert mention_count == 1, (
        f"'@Agent hi' appeared {mention_count} times — batched message must NOT be duplicated in prior context"
    )


# ---------------------------------------------------------------------------
# Regression: persist path and read path must agree on account_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mention_prior_context_uses_persist_account_id(monkeypatch, tmp_path):
    """Persist path writes account_id="default"; the mention read path must use the
    SAME account_id, not the paired ClawChat user_id.

    Regression for the bug where _build_mention_prior_context_text queried with
    self._account_id() (the ClawChat user_id) while _record_message /
    _claim_message_once persist with account_id="default", so the read matched 0
    rows in any paired adapter and prior context was never prepended.

    Uses the REAL ClawChatStore (not the account_id-ignoring fake) so the mismatch
    surfaces.
    """
    # Real store, populated exactly as the persist path does: account_id="default".
    store = _store(tmp_path)
    chat_id = "cnv_group"
    _insert_msg(store, account_id="default", chat_id=chat_id, message_id="msg_prior_1", text="first prior message", created_at=100)
    _insert_msg(store, account_id="default", chat_id=chat_id, message_id="msg_prior_2", text="second prior message", created_at=200)

    # Paired adapter: user_id is a real usr_* value, NOT "default".
    adapter = _make_adapter(monkeypatch)
    adapter._store = store
    assert adapter._account_id() == "usr_agent", "precondition: paired user_id != 'default'"

    from clawchat_gateway.inbound import InboundMessage
    inbound = InboundMessage(
        chat_id=chat_id,
        chat_type="group",
        sender_id="usr_sender",
        sender_name="Sender",
        text="@Agent hi",
        raw_message={
            "clawchat_group_batch": True,
            "messages": [_mention_frame()],
        },
        was_mentioned=True,
    )

    await adapter._handle_inbound(inbound)

    assert len(adapter._dispatched_events) == 1
    event = adapter._dispatched_events[0]
    assert "first prior message" in event.text, (
        "prior context (persisted under account_id='default') must be returned to the "
        "mention read path"
    )
    assert "second prior message" in event.text


# ---------------------------------------------------------------------------
# Adapter test 3: non-mention group turn does NOT prepend context
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_mention_group_turn_does_not_prepend_context(monkeypatch):
    """A non-mention group turn must NOT prepend prior context (no storage query)."""
    recent_rows = [
        {"message_id": "msg_prior", "text": "prior message", "created_at": 100},
    ]
    adapter = _make_adapter(monkeypatch, recent_rows=recent_rows)

    from clawchat_gateway.inbound import InboundMessage
    inbound = InboundMessage(
        chat_id="cnv_group",
        chat_type="group",
        sender_id="usr_sender",
        sender_name="Sender",
        text="just chatting",
        raw_message={
            "clawchat_group_batch": True,
            "messages": [_group_frame()],
        },
        was_mentioned=False,
    )

    await adapter._handle_inbound(inbound)

    assert len(adapter._dispatched_events) == 1
    event = adapter._dispatched_events[0]
    assert "prior message" not in event.text, "Non-mention must NOT inject prior context"
