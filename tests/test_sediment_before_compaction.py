"""The sediment turn runs before Hermes compresses, on small windows too.

Hermes compresses a session at the LOWER of two points: its ratio threshold
(``compression.threshold``, factory 0.50, raised to 0.75 for windows under
512K, applied to the window minus the output reservation) and
``compression.threshold_tokens`` (the plugin fills it from
``session-cap-tokens``, factory 150000). The sediment point used to be fixed at
``threshold_tokens - sediment-margin-tokens`` = 140000. On a 128K-window model
Hermes compresses at 0.75 x 128000 = 96000, so the conversation was compressed
long before the sediment turn could keep anything.

The plugin now records each conversation's window (``post_api_request`` passes
``context_length``) and output reservation (``pre_api_request`` passes
``max_tokens``) and puts the sediment point ``sediment-margin-tokens`` before
the point Hermes will actually compress at.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway import sediment
from clawchat_gateway.adapter import ClawChatAdapter

DM = "cnv_dm"
FRIEND = "usr_friend"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sediment.reset_usage_registry()
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a._host_compression_cap = 150_000
    a._host_compression_ratio = None
    a._active_sessions = {}
    a.dispatched = []

    async def handle_message(event):
        a.dispatched.append(event)
        raw = event.raw_message.get("clawchat_raw") or {}
        if raw.get("clawchat_sediment"):
            await a.on_processing_complete(event, None)

    async def send_frame(frame, **_kw):
        return True

    monkeypatch.setattr(a, "handle_message", handle_message, raising=False)
    monkeypatch.setattr(a, "build_source", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._group_settings_ready.set()
    yield a
    sediment.reset_usage_registry()


def _request(monkeypatch, *, prompt_tokens, context_length=None, max_tokens=None, session_id="s1"):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    sediment.clawchat_pre_api_request(platform="clawchat", session_id=session_id, max_tokens=max_tokens)
    kwargs = {"platform": "clawchat", "session_id": session_id, "usage": {"prompt_tokens": prompt_tokens}}
    if context_length is not None:
        kwargs["context_length"] = context_length
    sediment.clawchat_post_api_request(**kwargs)


async def _turn_completes(adapter):
    event = SimpleNamespace(source=SimpleNamespace(chat_id=DM, chat_type="dm", user_id=FRIEND), raw_message={})
    await adapter.on_processing_complete(event, None)
    for _ in range(20):
        await asyncio.sleep(0)


def _sediments(adapter):
    return [e for e in adapter.dispatched if (e.raw_message.get("clawchat_raw") or {}).get("clawchat_sediment")]


@pytest.mark.parametrize("use_host_formula", [True, False])
def test_compaction_point_on_a_128k_window(monkeypatch, use_host_formula):
    if use_host_formula:
        if sediment._host_threshold_helpers() is None:
            pytest.skip("Hermes' agent.context_compressor is not importable here")
    else:
        monkeypatch.setattr(sediment, "_host_threshold_helpers", lambda: None)
    # 0.50 is raised to 0.75 under 512K: 0.75 x 128000 = 96000, below the 150000 cap.
    assert sediment.compaction_point(
        context_length=128_000, max_tokens=None, threshold_percent=None, threshold_tokens=150_000
    ) == 96_000


def test_compaction_point_leaves_room_for_the_output_reservation():
    # (128000 - 32000) x 0.75 = 72000.
    assert sediment.compaction_point(
        context_length=128_000, max_tokens=32_000, threshold_percent=None, threshold_tokens=150_000
    ) == 72_000


def test_compaction_point_on_a_large_window_is_the_cap():
    # 0.50 x 1000000 = 500000; the 150000 cap is lower.
    assert sediment.compaction_point(
        context_length=1_000_000, max_tokens=None, threshold_percent=None, threshold_tokens=150_000
    ) == 150_000


def test_sediment_point_is_before_compaction_on_a_128k_window(adapter, monkeypatch):
    _request(monkeypatch, prompt_tokens=1_000, context_length=128_000)
    threshold = adapter._compact_sediment_threshold(DM)
    assert threshold == 86_000  # 96000 - 10000
    assert threshold < 96_000


@pytest.mark.asyncio
async def test_a_128k_conversation_gets_its_sediment_turn_before_hermes_compresses(adapter, monkeypatch):
    _request(monkeypatch, prompt_tokens=90_000, context_length=128_000)
    await _turn_completes(adapter)
    assert len(_sediments(adapter)) == 1


@pytest.mark.asyncio
async def test_below_the_128k_sediment_point_nothing_runs(adapter, monkeypatch):
    _request(monkeypatch, prompt_tokens=80_000, context_length=128_000)
    await _turn_completes(adapter)
    assert _sediments(adapter) == []


@pytest.mark.asyncio
async def test_without_a_known_window_the_cap_still_decides(adapter, monkeypatch):
    _request(monkeypatch, prompt_tokens=120_000)
    await _turn_completes(adapter)
    assert _sediments(adapter) == []
    assert adapter._compact_sediment_threshold(DM) == 140_000


def test_an_operator_ratio_is_honoured(adapter, monkeypatch):
    adapter._host_compression_ratio = 0.9
    _request(monkeypatch, prompt_tokens=1_000, context_length=128_000)
    # 0.9 x 128000 = 115200 (above the 0.75 floor), under the cap.
    assert adapter._compact_sediment_threshold(DM) == 105_200
