"""The "typing" indicator stays lit for the whole reply, and goes out at the end.

A receiver lights the indicator for a few seconds after each ``is_typing:true``
(the reference client: 6 s) and clears it on ``false`` or when that window ends
(docs/client-integration.md §9.1). Hermes refreshes typing every 2 s while a
turn runs. The adapter used to throttle repeated ``true`` frames to one per
10 s, so the indicator lit for 6 s, went dark for 4 s, and flashed for the
whole reply. Now every host refresh goes out (at most one per 1.5 s, so a
refresh every 2 s is never thinned out), ``false`` follows at the end of the
turn and when the gateway shuts down, and a silent sediment turn sends no
typing at all.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter

DM = "cnv_dm"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(adapter_mod.time, "monotonic", c)
    return c


@pytest.fixture
def adapter(monkeypatch, tmp_path, clock):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a.frames = []

    async def send_frame(frame, **_kw):
        a.frames.append(frame)
        return True

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    return a


def _typing(frames):
    return [f["payload"]["is_typing"] for f in frames if f.get("event") == "typing.update"]


async def test_every_host_refresh_reaches_the_wire(adapter, clock):
    # Hermes' _keep_typing: send_typing, then sleep 2 s, until the turn ends.
    for _ in range(6):
        await adapter.send_typing(DM)
        clock.now += 2.0
    assert _typing(adapter.frames) == [True] * 6


async def test_refresh_interval_stays_within_half_the_receiver_window():
    # The contract: refresh at least every 3 s (half the 6 s receiver window).
    # A throttle at or above the host's 2 s refresh would drop every other
    # refresh and stretch the real interval to 4 s.
    assert adapter_mod.TYPING_REFRESH_SECONDS < 2.0


async def test_back_to_back_calls_are_still_collapsed(adapter, clock):
    # Hermes also re-sends typing 0.3 s after each tool-progress edit.
    await adapter.send_typing(DM)
    clock.now += 0.3
    await adapter.send_typing(DM)
    assert _typing(adapter.frames) == [True]


async def test_false_follows_the_end_of_the_turn_once(adapter, clock):
    await adapter.send_typing(DM)
    clock.now += 2.0
    await adapter.stop_typing(DM)
    await adapter.stop_typing(DM)  # the host stops twice (_stop_typing_refresh)
    assert _typing(adapter.frames) == [True, False]


async def test_continuous_typing_cap_still_holds(adapter, clock):
    adapter._clawchat_config = replace(adapter._clawchat_config, typing_max_continuous_seconds=10.0)
    for _ in range(10):
        await adapter.send_typing(DM)
        clock.now += 2.0
    sent = _typing(adapter.frames)
    assert sent and all(sent)
    assert len(sent) == 6  # t=0,2,4,6,8,10; nothing after the cap


async def test_graceful_shutdown_clears_a_lit_indicator(adapter, clock, monkeypatch):
    await adapter.send_typing(DM)
    await adapter.send_typing("cnv_other")
    await adapter.stop_typing("cnv_other")

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(adapter._connection, "stop", _noop)
    monkeypatch.setattr(adapter._group_message_coalescer, "cancel", _noop)
    await adapter.disconnect()
    by_chat = [(f["chat_id"], f["payload"]["is_typing"]) for f in adapter.frames]
    assert by_chat == [(DM, True), ("cnv_other", True), ("cnv_other", False), (DM, False)]


async def test_sediment_turn_sends_no_typing(adapter, clock):
    adapter._sediment_chats.add(DM)
    await adapter.send_typing(DM)
    clock.now += 2.0
    await adapter.stop_typing(DM)
    assert adapter.frames == []


async def test_sediment_turn_still_clears_an_indicator_left_lit(adapter, clock):
    # A sediment turn never lights the indicator, but must not pin one that a
    # reply left lit either.
    await adapter.send_typing(DM)
    adapter._sediment_chats.add(DM)
    await adapter.stop_typing(DM)
    assert _typing(adapter.frames) == [True, False]
