"""ClawChat access-token refresh state machine (token-refresh spec §A/§B/§C).

This module owns the *policy* of refreshing the ClawChat access token: when to
fire (proactive expiry margin), single-flight de-duplication, the rejected-token
latch, the minimum-interval floor, transient-vs-permanent classification, and
the strict persist-before-swap ordering mandated by the rotation hazard (§0).

It is intentionally decoupled from the WebSocket: the connection wires four
callbacks (build api client, persist rotated tokens, persist logout, on-success
reconnect) and the device id provider, so the routine is unit-testable without a
live socket.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from clawchat_gateway.api_client import (
    ClawChatApiClient,
    ClawChatApiError,
    RefreshResult,
    auth_refresh_with_retry,
    is_permanent_refresh_error,
)
from clawchat_gateway.config import _jwt_exp, _jwt_iat

logger = logging.getLogger("clawchat_gateway.token_refresh")

# §A.1 proactive margin. refresh_at = exp - max(30min, min(2h, 0.25*(exp-iat))).
PROACTIVE_MIN_MARGIN_SECONDS = 30 * 60
PROACTIVE_MAX_MARGIN_SECONDS = 2 * 60 * 60
PROACTIVE_MARGIN_RATIO = 0.25
PROACTIVE_JITTER_SECONDS = 5 * 60  # ±5min, avoids a fleet-wide synchronized storm.

# §A.3 minimum interval between refresh attempts of the same token.
MIN_REFRESH_INTERVAL_SECONDS = 30.0

# Fallback access-token TTL when exp/iat are unparseable (24h, §A.0).
DEFAULT_ACCESS_TOKEN_TTL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class RefreshOutcome:
    """Result of a single-flight refresh attempt."""

    status: str  # "success" | "permanent" | "transient" | "skipped"
    access_token: str = ""
    refresh_token: str = ""
    error: str | None = None


def proactive_margin_seconds(exp: int, iat: int | None) -> float:
    """§A.1 margin: max(30min, min(2h, 0.25 * (exp - iat)))."""
    if iat is not None and exp > iat:
        quarter = PROACTIVE_MARGIN_RATIO * (exp - iat)
    else:
        # No iat → assume a 24h token so the quarter-life term is well-defined.
        quarter = PROACTIVE_MARGIN_RATIO * DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    return max(
        PROACTIVE_MIN_MARGIN_SECONDS,
        min(PROACTIVE_MAX_MARGIN_SECONDS, quarter),
    )


def access_token_exp(token: str, activated_at_ms: int | None) -> int | None:
    """Best-effort access-token expiry (epoch seconds).

    Decode the JWT ``exp``; fall back to ``activated_at + 24h`` (§A.0). Returns
    ``None`` only when neither is available.
    """
    exp = _jwt_exp(token)
    if exp is not None:
        return exp
    if activated_at_ms is not None:
        return int(activated_at_ms / 1000) + DEFAULT_ACCESS_TOKEN_TTL_SECONDS
    return None


def refresh_at_epoch(token: str, activated_at_ms: int | None) -> int | None:
    """Compute the proactive ``refresh_at`` epoch-seconds for a token (± jitter)."""
    exp = access_token_exp(token, activated_at_ms)
    if exp is None:
        return None
    iat = _jwt_iat(token)
    margin = proactive_margin_seconds(exp, iat)
    jitter = random.uniform(-PROACTIVE_JITTER_SECONDS, PROACTIVE_JITTER_SECONDS)
    return int(exp - margin + jitter)


def is_token_near_expiry(
    token: str,
    activated_at_ms: int | None,
    *,
    now: float | None = None,
) -> bool:
    """True when ``now`` has reached/passed the proactive ``refresh_at`` (§A.1/§A.4)."""
    if not token:
        return False
    exp = access_token_exp(token, activated_at_ms)
    if exp is None:
        return False
    iat = _jwt_iat(token)
    margin = proactive_margin_seconds(exp, iat)
    current = now if now is not None else time.time()
    return current >= (exp - margin)


# Callback signatures wired by the connection.
BuildClient = Callable[[], ClawChatApiClient]
# persist(access_token, refresh_token) -> bool: durable write to BOTH stores;
# MUST return True only after both .env and SQLite are written (§0 ordering).
PersistTokens = Callable[[str, str], Awaitable[bool]]
# persist_logout(reason) -> None: clear creds in both stores, keep identity (§C.1).
PersistLogout = Callable[[str], Awaitable[None]]


class RefreshManager:
    """Single-flight, latched, rate-limited refresh executor (§A.3)."""

    def __init__(
        self,
        *,
        build_client: BuildClient,
        persist_tokens: PersistTokens,
        persist_logout: PersistLogout,
        device_id_provider: Callable[[], str],
        min_interval_seconds: float = MIN_REFRESH_INTERVAL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        max_transient_retries: int | None = None,
    ) -> None:
        self._build_client = build_client
        self._persist_tokens = persist_tokens
        self._persist_logout = persist_logout
        self._device_id_provider = device_id_provider
        self._min_interval = min_interval_seconds
        self._monotonic = monotonic or time.monotonic
        self._max_transient_retries = max_transient_retries
        self._lock = asyncio.Lock()
        self._in_flight: asyncio.Future[RefreshOutcome] | None = None
        # §A.3 rejected-token latch: the access token a *permanent* refresh was
        # already attempted for; never re-attempt for the same dead token.
        self._rejected_token: str | None = None
        # §A.3 minimum-interval floor: the access token of the LAST attempt and
        # the monotonic time it ran. The floor applies only between attempts of
        # the SAME token, so a first attempt on a newly-rotated/different token
        # is never blocked by a prior attempt's timestamp.
        self._last_attempt_token: str | None = None
        self._last_attempt_at: float | None = None
        self._logged_out = False

    @property
    def logged_out(self) -> bool:
        return self._logged_out

    def reset_latch(self) -> None:
        """Clear the rejected-token latch (call when a *new* token is loaded)."""
        self._rejected_token = None
        self._logged_out = False

    async def refresh(
        self,
        *,
        access_token: str,
        refresh_token: str | None,
    ) -> RefreshOutcome:
        """Run one single-flight refresh for ``access_token``.

        Concurrent callers (proactive timer + reactive 401 + hello-fail) await
        the same in-flight future rather than firing parallel HTTP refreshes.
        """
        if not refresh_token:
            return RefreshOutcome(status="skipped", error="no refresh token")
        # Rejected-token latch: do not re-attempt for a token already proven dead.
        if access_token and access_token == self._rejected_token:
            return RefreshOutcome(status="skipped", error="rejected token unchanged")
        # In-flight dedupe.
        in_flight = self._in_flight
        if in_flight is not None and not in_flight.done():
            return await asyncio.shield(in_flight)
        async with self._lock:
            in_flight = self._in_flight
            if in_flight is not None and not in_flight.done():
                return await asyncio.shield(in_flight)
            if access_token and access_token == self._rejected_token:
                return RefreshOutcome(status="skipped", error="rejected token unchanged")
            # Minimum-interval floor: a reconnect storm must not become a refresh
            # storm. Honor it only between attempts of the SAME token — a first
            # attempt on a newly-rotated/different access token is never blocked.
            now = self._monotonic()
            if (
                self._last_attempt_at is not None
                and access_token == self._last_attempt_token
                and (now - self._last_attempt_at) < self._min_interval
            ):
                return RefreshOutcome(
                    status="skipped",
                    error="min interval not elapsed",
                )
            future: asyncio.Future[RefreshOutcome] = asyncio.get_running_loop().create_future()
            self._in_flight = future
        try:
            outcome = await self._do_refresh(access_token, refresh_token)
            if not future.done():
                future.set_result(outcome)
            return outcome
        except BaseException as exc:  # noqa: BLE001
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._in_flight = None

    async def _do_refresh(
        self,
        access_token: str,
        refresh_token: str,
    ) -> RefreshOutcome:
        self._last_attempt_at = self._monotonic()
        self._last_attempt_token = access_token or None
        device_id = self._device_id_provider()
        client = self._build_client()
        try:
            result: RefreshResult = await auth_refresh_with_retry(
                client,
                refresh_token=refresh_token,
                device_id=device_id,
                max_transient_retries=self._max_transient_retries,
            )
        except ClawChatApiError as exc:
            if is_permanent_refresh_error(exc):
                # §B / §C: permanent → latch + auto-logout (keep identity).
                self._rejected_token = access_token or None
                self._logged_out = True
                logger.warning(
                    "clawchat token refresh permanent failure code=%s: %s",
                    exc.code,
                    exc,
                )
                try:
                    await self._persist_logout(str(exc))
                except Exception:  # noqa: BLE001
                    logger.warning("clawchat logout persistence failed", exc_info=True)
                return RefreshOutcome(status="permanent", error=str(exc))
            # Transient retries were exhausted (only possible under a test bound).
            logger.warning("clawchat token refresh transient failure: %s", exc)
            return RefreshOutcome(status="transient", error=str(exc))

        # §0 rotation hazard: persist the rotated pair durably BEFORE the caller
        # treats the refresh as complete / swaps the in-memory token.
        persisted = await self._persist_tokens(result.access_token, result.refresh_token)
        if not persisted:
            # A crash/failure to persist after the server rotated would brick the
            # agent. Surface as transient so we keep the dead-but-unswapped token
            # and retry (the next attempt with the OLD refresh token returns
            # 10003 → escalates to permanent, per §B transient→permanent rule).
            logger.error("clawchat token refresh persisted=False after rotation")
            return RefreshOutcome(status="transient", error="persist failed")
        self._rejected_token = None
        logger.info("clawchat token refresh success (rotated, persisted)")
        return RefreshOutcome(
            status="success",
            access_token=result.access_token,
            refresh_token=result.refresh_token,
        )
