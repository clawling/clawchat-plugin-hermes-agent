"""Refresh attempt deadline + retry cadence for the backend refresh grace window.

token-refresh.md §B: every refresh attempt has a 20s total deadline (a hit is
TRANSIENT), and after a transient whose outcome is unknown the next attempt with
the same refresh token starts 30-45s after the previous attempt began. The
backend tolerates a replay of an already-rotated refresh token only inside a
short window (90s, minimum replay age 2s, at most 2 replays), so the cadence
must put the first two replays inside it and a third outside. A 10003 after a
timeout still logs out — fixing the lost-response case is the server's job.
"""

from __future__ import annotations

import threading
import time

import pytest

from clawchat_gateway import api_client
from clawchat_gateway.api_client import (
    ClawChatApiClient,
    ClawChatApiError,
    RefreshResult,
    auth_refresh_with_retry,
    is_permanent_refresh_error,
)
from clawchat_gateway.token_refresh import MIN_REFRESH_INTERVAL_SECONDS, RefreshManager

GRACE_WINDOW_SECONDS = 90.0
GRACE_MIN_AGE_SECONDS = 2.0


def _client() -> ClawChatApiClient:
    return ClawChatApiClient(base_url="https://api.example.invalid", token="", user_id="usr_1")


def _timeout_error() -> ClawChatApiError:
    return ClawChatApiError("transport", "refresh request timed out", path="/v1/auth/refresh")


def _invalid_refresh_error() -> ClawChatApiError:
    return ClawChatApiError("auth", "refresh token invalid", code=10003, path="/v1/auth/refresh")


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.t += delay


class _ScriptedClient:
    """Fake client: each call consumes one scripted step (seconds spent, result)."""

    def __init__(self, clock: _Clock, steps: list[tuple[float, object]]) -> None:
        self._clock = clock
        self._steps = list(steps)
        self.started_at: list[float] = []

    async def auth_refresh(self, *, refresh_token: str, device_id: str) -> RefreshResult:
        self.started_at.append(self._clock.t)
        spent, result = self._steps.pop(0)
        self._clock.t += spent
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]


def test_attempt_deadline_is_20s_total_and_below_the_min_interval_floor() -> None:
    assert api_client.REFRESH_ATTEMPT_CEILING_SECONDS == 20.0
    # urlopen's timeout stays a per-socket-operation bound inside the ceiling.
    assert api_client.REFRESH_REQUEST_TIMEOUT_SECONDS <= api_client.REFRESH_ATTEMPT_CEILING_SECONDS
    assert api_client.REFRESH_ATTEMPT_CEILING_SECONDS < MIN_REFRESH_INTERVAL_SECONDS


async def test_hung_urlopen_is_cut_at_the_total_deadline_as_transient(monkeypatch) -> None:
    release = threading.Event()

    def hung_urlopen(request, timeout=None):  # noqa: ANN001
        release.wait(5.0)
        raise TimeoutError("released")

    monkeypatch.setattr(api_client, "urlopen", hung_urlopen)
    monkeypatch.setattr(api_client, "REFRESH_ATTEMPT_CEILING_SECONDS", 0.05)
    started = time.monotonic()
    try:
        with pytest.raises(ClawChatApiError) as info:
            await _client().auth_refresh(refresh_token="r" * 64, device_id="hermes-dev")
    finally:
        release.set()
    assert time.monotonic() - started < 2.0
    exc = info.value
    assert exc.kind == "transport"
    assert "timed out" in exc.message
    assert exc.connect_failed is False
    assert not is_permanent_refresh_error(exc)


async def test_timeout_retry_starts_31_to_35s_after_the_previous_attempt_began() -> None:
    for _ in range(50):  # the jitter is random; the bound must hold on every draw
        clock = _Clock()
        client = _ScriptedClient(
            clock,
            [(20.0, _timeout_error()), (0.1, RefreshResult("a1", "r1"))],
        )
        result = await auth_refresh_with_retry(
            client,  # type: ignore[arg-type]
            refresh_token="r0",
            device_id="d",
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        assert result == RefreshResult("a1", "r1")
        gap = client.started_at[1] - client.started_at[0]
        assert 31.0 <= gap <= 35.0


async def test_all_timeouts_keep_two_replays_inside_the_window_and_the_third_outside() -> None:
    for _ in range(50):
        clock = _Clock()
        client = _ScriptedClient(clock, [(20.0, _timeout_error())] * 4)
        with pytest.raises(ClawChatApiError):
            await auth_refresh_with_retry(
                client,  # type: ignore[arg-type]
                refresh_token="r0",
                device_id="d",
                max_transient_retries=3,
                sleep=clock.sleep,
                monotonic=clock.monotonic,
            )
        ages = [s - client.started_at[0] for s in client.started_at[1:]]
        assert len(ages) == 3
        # Replays 1 and 2: past the minimum age, inside the window with margin.
        assert GRACE_MIN_AGE_SECONDS < ages[0] <= 35.0
        assert ages[1] <= 70.0
        # Replay 3 would exceed the per-token replay cap, so it must fall outside.
        assert ages[2] > GRACE_WINDOW_SECONDS


@pytest.mark.parametrize(
    "error",
    [
        ClawChatApiError("transport", "refused", path="/v1/auth/refresh", connect_failed=True),
        ClawChatApiError("api", "internal", code=1, path="/v1/auth/refresh"),
    ],
)
async def test_failures_that_never_rotated_keep_the_fast_backoff(error) -> None:  # noqa: ANN001
    clock = _Clock()
    client = _ScriptedClient(clock, [(0.1, error), (0.1, RefreshResult("a1", "r1"))])
    await auth_refresh_with_retry(
        client,  # type: ignore[arg-type]
        refresh_token="r0",
        device_id="d",
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert len(clock.sleeps) == 1
    assert clock.sleeps[0] <= 1.25


def _manager(clock: _Clock, client: _ScriptedClient, calls: dict) -> RefreshManager:
    async def persist_tokens(access: str, refresh: str) -> bool:
        calls.setdefault("persist", []).append((access, refresh))
        return True

    async def persist_logout(reason: str) -> None:
        calls.setdefault("logout", []).append(reason)

    return RefreshManager(
        build_client=lambda: client,  # type: ignore[arg-type,return-value]
        persist_tokens=persist_tokens,
        persist_logout=persist_logout,
        device_id_provider=lambda: "hermes-dev",
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


async def test_timeout_then_10003_still_escalates_to_logout() -> None:
    clock = _Clock()
    client = _ScriptedClient(clock, [(20.0, _timeout_error()), (0.1, _invalid_refresh_error())])
    calls: dict = {}
    outcome = await _manager(clock, client, calls).refresh(access_token="a0", refresh_token="r0")
    assert outcome.status == "permanent"
    assert len(client.started_at) == 2
    assert len(calls.get("logout", [])) == 1
    assert "persist" not in calls


async def test_timeout_then_grace_replay_success_persists_the_rotated_pair() -> None:
    clock = _Clock()
    client = _ScriptedClient(clock, [(20.0, _timeout_error()), (0.1, RefreshResult("a2", "r2"))])
    calls: dict = {}
    outcome = await _manager(clock, client, calls).refresh(access_token="a0", refresh_token="r0")
    assert outcome.status == "success"
    assert calls.get("persist") == [("a2", "r2")]
    assert "logout" not in calls
