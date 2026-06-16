"""Reproduction for the production duplicate-message bug (pod cas-821f53a6...,
agent 维小会, group cnv_01KV7RN7ZSE0XANXJMBS03BAXB, 2026-06-16).

Prod logs showed a single inbound message + a single conversation turn
(``api_calls=1``, 12-char response) producing TWO ``message.reply`` frames with
distinct ``message_id``/``trace_id`` ~300ms apart:

    08:27:53,521 clawchat send start ...                       (send #1, immediate)
    08:27:53,657 send complete reply queued msg-...P0YV        (frame #1 emitted)
    08:27:53,657 edit skipped ... reason=no_active_run         (finalize finds no run)
    08:27:53,658 edit skipped ... reason=no_active_run
    08:27:53,830 [Clawchat] Sending response (12 chars)        (hermes core re-send)
    08:27:53,831 clawchat send start ...                       (send #2, immediate)
    08:27:53,967 send complete reply queued msg-...R00B        (frame #2 — DUPLICATE)

The root trigger (hermes core invoking the platform send twice) lives outside
this repo. This test reproduces the adapter-level defect that lets the duplicate
through: two ``send()`` calls with the same response text in one turn each mint a
fresh ``message_id`` (adapter.py:2260), so the message_id-keyed outbound claim
never dedups them and both frames go out.

Desired behaviour after the fix: the second identical emit within the turn is
suppressed -> exactly one ``message.reply`` frame reaches the connection.
"""

from __future__ import annotations

import importlib
import sys
from collections import OrderedDict, deque
from types import ModuleType

from clawchat_gateway.config import ClawChatConfig

BOT_USER_ID = "usr_bot"
OWNER_USER_ID = "usr_owner"
CHAT_ID = "cnv_01KV7RN7ZSE0XANXJMBS03BAXB"
REPLY_TEXT = "在呢，老大，有啥事吩咐？"


def _load_adapter_module(monkeypatch):
    # adapter.py imports the Hermes host `gateway` package, absent in the
    # plugin's own test env. Stub the minimal surface it touches.
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

    return importlib.reload(adapter_module)


class _FakeConnection:
    """Records every frame the adapter flushes; always acks success."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_frame(self, frame, wait_for_ack=False):  # noqa: ANN001
        self.sent.append(frame)
        return True


class _FakeStore:
    """Mirrors the real outbound claim: dedups by message_id, so two sends with
    distinct message_ids are BOTH claimed (exactly what prod did)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def claim_message_once(self, *, message_id=None, **_kwargs):  # noqa: ANN001
        if message_id in self._seen:
            return False
        if message_id is not None:
            self._seen.add(message_id)
        return True

    def update_message_by_identity(self, **_kwargs):  # noqa: ANN001
        return None


def _make_adapter(monkeypatch) -> tuple:
    adapter_module = _load_adapter_module(monkeypatch)
    # Terminal-tool-send global state must be inert for a clean reply path.
    monkeypatch.setattr(
        adapter_module, "consume_terminal_clawchat_send", lambda **_kwargs: None
    )

    adapter = adapter_module.ClawChatAdapter.__new__(adapter_module.ClawChatAdapter)
    adapter._clawchat_config = ClawChatConfig(
        websocket_url="wss://example.test/ws",
        user_id=BOT_USER_ID,
        owner_user_id=OWNER_USER_ID,
    )
    adapter._connection = _FakeConnection()
    adapter._store = _FakeStore()
    adapter._memory_root = None
    adapter._run_counter = 0
    adapter._active_runs_by_id = {}
    adapter._active_chat_runs = {}
    adapter._completed_run_ids = set()
    adapter._completed_run_order = deque()
    adapter._recent_emits = OrderedDict()
    return adapter_module, adapter


async def test_same_turn_response_emits_single_reply(monkeypatch) -> None:
    """Replays the prod turn: send -> finalize(no run) -> send. The adapter must
    emit the response exactly once; today it emits it twice (the bug)."""
    _adapter_module, adapter = _make_adapter(monkeypatch)

    # send #1 — the streaming/output path delivers the complete response.
    await adapter.send(CHAT_ID, REPLY_TEXT, chat_type="group")
    # finalize lands on a phantom id (send #1 went the immediate path, no run was
    # registered) -> harmless no_active_run no-op, as seen in prod logs.
    await adapter.edit_message(
        CHAT_ID, message_id="msg-PHANTOM", content=REPLY_TEXT, finalize=True
    )
    # send #2 — hermes core's platforms.base re-delivers the same final response.
    await adapter.send(CHAT_ID, REPLY_TEXT, chat_type="group")

    reply_frames = [
        f for f in adapter._connection.sent if f.get("event") == "message.reply"
    ]
    message_ids = [
        (f.get("payload") or {}).get("message_id") for f in reply_frames
    ]
    assert len(reply_frames) == 1, (
        f"expected the response to be sent once, got {len(reply_frames)} "
        f"message.reply frames (message_ids={message_ids}) — duplicate-send bug"
    )


async def test_distinct_responses_both_emitted(monkeypatch) -> None:
    """Guard against over-suppression: two different replies in one window must
    both go out."""
    _adapter_module, adapter = _make_adapter(monkeypatch)

    await adapter.send(CHAT_ID, "第一条", chat_type="group")
    await adapter.send(CHAT_ID, "第二条", chat_type="group")

    reply_frames = [
        f for f in adapter._connection.sent if f.get("event") == "message.reply"
    ]
    assert len(reply_frames) == 2


async def test_same_text_after_window_not_suppressed(monkeypatch) -> None:
    """A genuine repeat of the same text in a later turn (beyond the dedup
    window) must NOT be suppressed."""
    adapter_module, adapter = _make_adapter(monkeypatch)

    clock = {"now": 1000.0}
    monkeypatch.setattr(adapter_module.time, "monotonic", lambda: clock["now"])

    await adapter.send(CHAT_ID, REPLY_TEXT, chat_type="group")
    clock["now"] += adapter_module.DUPLICATE_EMIT_WINDOW_SECONDS + 1.0
    await adapter.send(CHAT_ID, REPLY_TEXT, chat_type="group")

    reply_frames = [
        f for f in adapter._connection.sent if f.get("event") == "message.reply"
    ]
    assert len(reply_frames) == 2
