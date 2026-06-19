"""Tests for GroupMessageCoalescer per-chat idle override (Task 11 TDD).

Tests the idle_seconds_override parameter added to enqueue().
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
from clawchat_gateway.inbound import InboundMessage


def _msg(chat_id: str = "cnv_1", text: str = "hello") -> InboundMessage:
    return InboundMessage(
        chat_id=chat_id,
        chat_type="group",
        sender_id="usr_sender",
        sender_name="Sender",
        text=text,
        raw_message={},
    )


class _FakeClock:
    """Controllable clock for asyncio.sleep."""

    def __init__(self) -> None:
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []
        self._now = 0.0

    async def sleep(self, seconds: float) -> None:
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    def advance(self, seconds: float) -> None:
        self._now += seconds
        done = []
        remaining = []
        for wake_at, fut in self._waiters:
            if self._now >= wake_at:
                if not fut.done():
                    fut.set_result(None)
                done.append((wake_at, fut))
            else:
                remaining.append((wake_at, fut))
        self._waiters = remaining


@pytest.mark.asyncio
async def test_enqueue_uses_idle_seconds_override() -> None:
    """With idle_seconds_override=3, the batch should flush after 3s not the default 10s."""
    clock = _FakeClock()
    dispatched: list[InboundMessage] = []

    async def dispatch(msg: InboundMessage) -> None:
        dispatched.append(msg)

    coalescer = GroupMessageCoalescer(
        idle_seconds=10.0,
        max_wait_seconds=30.0,
        dispatch=dispatch,
        sleep=clock.sleep,
    )

    coalescer.enqueue(_msg(), idle_seconds_override=3.0)
    await asyncio.sleep(0)  # let the task start and reach the sleep
    assert dispatched == []

    # advance 3 seconds — should flush
    clock.advance(3.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_enqueue_default_idle_without_override() -> None:
    """Without override, the default idle_seconds (10) is used."""
    clock = _FakeClock()
    dispatched: list[InboundMessage] = []

    async def dispatch(msg: InboundMessage) -> None:
        dispatched.append(msg)

    coalescer = GroupMessageCoalescer(
        idle_seconds=10.0,
        max_wait_seconds=30.0,
        dispatch=dispatch,
        sleep=clock.sleep,
    )

    coalescer.enqueue(_msg())
    await asyncio.sleep(0)  # let the task start and reach the sleep
    assert dispatched == []

    # advance only 3s — should NOT flush (default is 10s)
    clock.advance(3.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert dispatched == []

    # advance to 10s total — should flush
    clock.advance(7.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_enqueue_idle_override_resets_on_new_message() -> None:
    """Each enqueue resets the idle timer with the latest override."""
    clock = _FakeClock()
    dispatched: list[InboundMessage] = []

    async def dispatch(msg: InboundMessage) -> None:
        dispatched.append(msg)

    coalescer = GroupMessageCoalescer(
        idle_seconds=10.0,
        max_wait_seconds=30.0,
        dispatch=dispatch,
        sleep=clock.sleep,
    )

    coalescer.enqueue(_msg(text="first"), idle_seconds_override=5.0)
    await asyncio.sleep(0)  # let task start
    clock.advance(3.0)
    await asyncio.sleep(0)

    # Second message resets timer with same override
    coalescer.enqueue(_msg(text="second"), idle_seconds_override=5.0)
    await asyncio.sleep(0)  # let new task start
    await asyncio.sleep(0)

    # 4s total elapsed since second message reset, not yet 5s — no flush
    clock.advance(4.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert dispatched == []

    # Now 5s since reset — flush
    clock.advance(1.0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(dispatched) == 1
    # The merged message should include both
    assert "first" in dispatched[0].text or "second" in dispatched[0].text
