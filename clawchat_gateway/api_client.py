"""Shared HTTP client for ClawChat REST APIs used by tools and media uploads."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from clawchat_gateway.device_id import get_device_id

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://app.clawling.com"
DEFAULT_WEBSOCKET_URL = "wss://app.clawling.com/ws"
AGENTS_CONNECT_PLATFORM = "hermes"
AGENTS_CONNECT_TYPE = "clawbot"
DEFAULT_REQUEST_TIMEOUT = 30.0

# Activation talks to a single-use connect code, so a request that may have
# reached the server must NOT be retried. We use a shorter timeout than the
# default API client (fail fast on a dead network) and retry only failures that
# provably never reached the server (DNS / connection refused).
ACTIVATION_TIMEOUT_SECONDS = 15.0
ACTIVATION_CONNECT_RETRIES = 2
ACTIVATION_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# Hard per-attempt wall-clock ceiling. urlopen's timeout does NOT bound DNS
# resolution (getaddrinfo), so a stalled resolver could otherwise hang far past
# ACTIVATION_TIMEOUT_SECONDS and then be retried. This ceiling guarantees each
# attempt returns; a hit is ambiguous, so it is surfaced and NOT retried.
ACTIVATION_ATTEMPT_CEILING_SECONDS = ACTIVATION_TIMEOUT_SECONDS + 5.0

# POST /v1/auth/refresh envelope codes (token-refresh spec §0). The endpoint
# ALWAYS returns HTTP 200; branch on the envelope `code`, never on HTTP status.
REFRESH_CODE_SUCCESS = 0
REFRESH_CODE_INVALID_REFRESH = 10003  # not found / revoked / expired / device mismatch
REFRESH_CODE_BAD_REQUEST = 400  # bad body / missing or oversized device id
REFRESH_CODE_INTERNAL = 1  # server internal error (no rotation committed)

# Transient-refresh backoff (spec §B): min(30s, 1s * 2^(n-1)) ± jitter, cap 30s.
REFRESH_RETRY_BACKOFF_CAP_SECONDS = 30.0
REFRESH_RETRY_BASE_SECONDS = 1.0
REFRESH_REQUEST_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of POST /v1/auth/refresh, classified by envelope `code`."""

    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class ClawChatApiError(Exception):
    kind: str
    message: str
    status: int | None = None
    path: str | None = None
    code: int | None = None
    # True only when the request provably never reached the server (DNS failure
    # or connection refused). Such failures are safe to retry; ambiguous ones
    # (read timeout, connection reset mid-flight) are NOT, because the server
    # may already have processed a single-use request such as activation.
    connect_failed: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class UploadResult:
    url: str
    size: int
    mime: str
    kind: str | None = None
    name: str | None = None


class ClawChatApiClient:
    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        user_id: str = "",
        device_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ClawChatApiError(
                "validation", f'base_url must start with http:// or https:// (got "{base_url}")'
            )
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._user_id = user_id
        self._device_id = device_id or get_device_id()
        self._timeout = timeout if timeout and timeout > 0 else DEFAULT_REQUEST_TIMEOUT

    async def get_my_profile(self) -> dict:
        return await self._call_json("GET", "/v1/users/me")

    async def get_user_info(self, user_id: str) -> dict:
        if not user_id.strip():
            raise ClawChatApiError("validation", "user_id is required")
        return await self._call_json("GET", f"/v1/users/{user_id}")

    async def list_friends(self, *, page: int = 1, page_size: int = 20) -> dict:
        return await self._call_json("GET", "/v1/friendships")

    async def send_friend_request(self, *, user_id: str, greeting: str | None = None) -> dict:
        if not user_id.strip():
            raise ClawChatApiError("validation", "user_id is required")
        payload = {"user_id": user_id}
        if greeting is not None:
            payload["greeting"] = greeting
        return await self._call_json(
            "POST",
            "/v1/friendships",
            body=json.dumps(payload).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def list_friend_requests(self, *, direction: str = "incoming") -> dict:
        if direction not in {"incoming", "outgoing"}:
            raise ClawChatApiError(
                "validation",
                "direction must be incoming or outgoing",
            )
        return await self._call_json("GET", f"/v1/friendships/requests/{direction}")

    async def accept_friend_request(self, request_id: int) -> dict:
        return await self._call_json("POST", f"/v1/friendships/requests/{request_id}/accept")

    async def reject_friend_request(self, request_id: int) -> dict:
        return await self._call_json("POST", f"/v1/friendships/requests/{request_id}/reject")

    async def remove_friend(self, friend_user_id: str) -> dict:
        if not friend_user_id.strip():
            raise ClawChatApiError("validation", "friend_user_id is required")
        return await self._call_json("DELETE", f"/v1/friendships/{friend_user_id}")

    async def search_users(self, *, q: str = "", limit: int | None = None) -> dict:
        params: dict[str, str | int] = {}
        if q:
            params["q"] = q
        if limit is not None:
            params["limit"] = limit
        query = urlencode(params)
        path = f"/v1/users/search?{query}" if query else "/v1/users/search"
        return await self._call_json("GET", path)

    async def list_moments(self, *, before: int | None = None, limit: int | None = None) -> dict:
        params: dict[str, int] = {}
        if before is not None:
            params["before"] = before
        if limit is not None:
            params["limit"] = limit
        query = urlencode(params)
        path = f"/v1/moments?{query}" if query else "/v1/moments"
        return await self._call_json("GET", path)

    async def get_conversation(self, conversation_id: str) -> dict:
        if not conversation_id.strip():
            raise ClawChatApiError("validation", "conversation_id is required")
        return await self._call_json("GET", f"/v1/conversations/{conversation_id}")

    async def get_agent_detail(self, agent_id: str) -> dict:
        if not agent_id.strip():
            raise ClawChatApiError("validation", "agent_id is required")
        return await self._call_json("GET", f"/v1/agents/{agent_id}")

    async def get_agent(self, agent_id: str) -> dict:
        return await self.get_agent_detail(agent_id)

    async def patch_agent(
        self,
        agent_id: str,
        *,
        nickname: str | None = None,
        avatar_url: str | None = None,
        bio: str | None = None,
    ) -> dict:
        if not agent_id.strip():
            raise ClawChatApiError("validation", "agent_id is required")
        patch = {}
        if nickname is not None:
            patch["nickname"] = nickname
        if avatar_url is not None:
            patch["avatar_url"] = avatar_url
        if bio is not None:
            patch["bio"] = bio
        if not patch:
            raise ClawChatApiError(
                "validation",
                "at least one of nickname/avatar_url/bio is required",
            )
        return await self._call_json(
            "PATCH",
            f"/v1/agents/{agent_id}",
            body=json.dumps(patch).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def update_agent_behavior(self, behavior: str) -> dict:
        return await self._call_json(
            "PATCH",
            "/v1/agents/me/behavior",
            body=json.dumps({"behavior": behavior}).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def patch_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        description: str | None = None,
    ) -> dict:
        if not conversation_id.strip():
            raise ClawChatApiError("validation", "conversation_id is required")
        patch = {}
        if title is not None:
            patch["title"] = title
        if description is not None:
            patch["description"] = description
        if not patch:
            raise ClawChatApiError(
                "validation",
                "at least one of title/description is required",
            )
        return await self._call_json(
            "PATCH",
            f"/v1/conversations/{conversation_id}",
            body=json.dumps(patch).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def create_moment(
        self,
        *,
        text: str | None = None,
        images: list[str] | None = None,
    ) -> dict:
        payload = {}
        if text is not None:
            payload["text"] = text
        if images is not None:
            payload["images"] = images
        return await self._call_json(
            "POST",
            "/v1/moments",
            body=json.dumps(payload).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def delete_moment(self, moment_id: int) -> dict:
        return await self._call_json("DELETE", f"/v1/moments/{moment_id}")

    async def toggle_moment_reaction(self, *, moment_id: int, emoji: str) -> dict:
        return await self._call_json(
            "POST",
            f"/v1/moments/{moment_id}/reactions",
            body=json.dumps({"emoji": emoji}).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def create_moment_comment(self, *, moment_id: int, text: str) -> dict:
        return await self._call_json(
            "POST",
            f"/v1/moments/{moment_id}/comments",
            body=json.dumps({"text": text}).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def reply_moment_comment(
        self,
        *,
        moment_id: int,
        reply_to_comment_id: int,
        text: str,
    ) -> dict:
        return await self._call_json(
            "POST",
            f"/v1/moments/{moment_id}/comments",
            body=json.dumps(
                {"text": text, "reply_to_comment_id": reply_to_comment_id}
            ).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def delete_moment_comment(self, *, moment_id: int, comment_id: int) -> dict:
        return await self._call_json("DELETE", f"/v1/moments/{moment_id}/comments/{comment_id}")

    async def update_my_profile(
        self,
        *,
        nickname: str | None = None,
        avatar_url: str | None = None,
        bio: str | None = None,
    ) -> dict:
        patch = {}
        if nickname is not None:
            patch["nickname"] = nickname
        if avatar_url is not None:
            patch["avatar_url"] = avatar_url
        if bio is not None:
            patch["bio"] = bio
        if not patch:
            raise ClawChatApiError("validation", "at least one of nickname/avatar_url/bio is required")
        return await self._call_json(
            "PATCH",
            "/v1/users/me",
            body=json.dumps(patch).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def agents_connect(
        self,
        *,
        code: str,
        user_id: str | None = None,
    ) -> dict:
        if not code.strip():
            raise ClawChatApiError("validation", "invite code is required")
        payload = {
            "code": code.strip(),
            "platform": AGENTS_CONNECT_PLATFORM,
            "type": AGENTS_CONNECT_TYPE,
        }
        if user_id and user_id.strip():
            payload["user_id"] = user_id.strip()
        body = json.dumps(payload).encode("utf-8")
        return await self._call_json(
            "POST",
            "/v1/agents/connect",
            body=body,
            extra_headers={"content-type": "application/json"},
        )

    async def auth_refresh(
        self,
        *,
        refresh_token: str,
        device_id: str,
    ) -> RefreshResult:
        """Exchange a refresh token for a rotated ``{access_token, refresh_token}``.

        Token-refresh spec §0: ``POST /v1/auth/refresh`` is UNAUTHENTICATED — it
        sends NO Authorization header; the refresh token in the body is the
        credential, and ``X-Device-Id`` must equal the connect-time device id.
        The endpoint always returns HTTP 200; we branch on the envelope ``code``:

        - ``0`` → success (rotated tokens).
        - ``10003`` → PERMANENT (kind ``auth``): not found / revoked / expired /
          device mismatch → caller auto-logs-out.
        - ``400`` → PERMANENT client bug (kind ``validation``) → auto-logout.
        - ``1`` → TRANSIENT (kind ``api``, retryable) → server internal error.
        - any non-200 / network error → TRANSIENT (kind ``transport``, retryable).
        """
        return await asyncio.to_thread(
            self._auth_refresh_sync,
            refresh_token,
            device_id,
        )

    def _auth_refresh_sync(
        self,
        refresh_token: str,
        device_id: str,
    ) -> RefreshResult:
        if not refresh_token or not refresh_token.strip():
            raise ClawChatApiError(
                "validation",
                "refresh_token is required",
                path="/v1/auth/refresh",
                code=REFRESH_CODE_BAD_REQUEST,
            )
        if not device_id or not device_id.strip():
            raise ClawChatApiError(
                "validation",
                "device_id is required",
                path="/v1/auth/refresh",
                code=REFRESH_CODE_BAD_REQUEST,
            )
        body = json.dumps({"refresh_token": refresh_token.strip()}).encode("utf-8")
        # NO authorization header — the refresh token in the body is the credential.
        request = Request(
            f"{self._base_url}/v1/auth/refresh",
            method="POST",
            data=body,
            headers={
                "content-type": "application/json",
                "content-length": str(len(body)),
                "x-device-id": device_id.strip(),
            },
        )
        timeout = REFRESH_REQUEST_TIMEOUT_SECONDS
        try:
            with urlopen(request, timeout=timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            # Non-200 (500 / LB / transport) → TRANSIENT, retryable.
            try:
                detail = exc.read().decode("utf-8")
            except Exception:
                detail = ""
            raise ClawChatApiError(
                "transport",
                f"refresh HTTP {exc.code}: {detail or exc.reason}",
                status=exc.code,
                path="/v1/auth/refresh",
            ) from exc
        except URLError as exc:
            reason = exc.reason
            connect_failed = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise ClawChatApiError(
                "transport",
                str(reason or exc),
                path="/v1/auth/refresh",
                connect_failed=connect_failed,
            ) from exc
        except TimeoutError as exc:
            raise ClawChatApiError(
                "transport",
                str(exc) or "refresh request timed out",
                path="/v1/auth/refresh",
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise ClawChatApiError("transport", str(exc), path="/v1/auth/refresh") from exc

        if status != 200:
            # Defensive: any non-200 is TRANSIENT (the contract says always 200).
            raise ClawChatApiError(
                "transport",
                f"refresh non-200 status={status}",
                status=status,
                path="/v1/auth/refresh",
            )
        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise ClawChatApiError(
                "transport",
                "refresh non-JSON response",
                status=status,
                path="/v1/auth/refresh",
            ) from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("msg") or payload.get("message") or "")
        if code == REFRESH_CODE_SUCCESS:
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ClawChatApiError(
                    "transport",
                    "refresh invalid envelope: missing object data",
                    status=status,
                    path="/v1/auth/refresh",
                    code=code,
                )
            access_token = str(data.get("access_token") or "").strip()
            new_refresh_token = str(data.get("refresh_token") or "").strip()
            if not access_token or not new_refresh_token:
                raise ClawChatApiError(
                    "transport",
                    "refresh invalid envelope: missing rotated tokens",
                    status=status,
                    path="/v1/auth/refresh",
                    code=code,
                )
            return RefreshResult(access_token=access_token, refresh_token=new_refresh_token)
        if code == REFRESH_CODE_INVALID_REFRESH:
            # PERMANENT: invalid refresh token (revoked / expired / device mismatch).
            raise ClawChatApiError(
                "auth",
                msg or "refresh token invalid",
                status=status,
                path="/v1/auth/refresh",
                code=code,
            )
        if code == REFRESH_CODE_BAD_REQUEST:
            # PERMANENT (client bug): bad body / missing or oversized device id.
            raise ClawChatApiError(
                "validation",
                msg or "refresh bad request",
                status=status,
                path="/v1/auth/refresh",
                code=code,
            )
        if code == REFRESH_CODE_INTERNAL:
            # TRANSIENT: server internal error, no rotation committed.
            raise ClawChatApiError(
                "api",
                msg or "refresh server internal error",
                status=status,
                path="/v1/auth/refresh",
                code=code,
            )
        # Unknown code → treat as TRANSIENT (do not auto-logout on the unexpected).
        raise ClawChatApiError(
            "transport",
            msg or f"refresh unexpected code={code}",
            status=status,
            path="/v1/auth/refresh",
            code=code,
        )

    async def upload_media(
        self,
        *,
        buffer: bytes,
        filename: str,
        mime: str = "application/octet-stream",
    ) -> UploadResult:
        return await self._upload(
            "/media/upload",
            buffer=buffer,
            filename=filename,
            mime=mime,
            required_fields=("kind", "url", "name", "mime", "size"),
        )

    async def upload_avatar(
        self,
        *,
        buffer: bytes,
        filename: str,
        mime: str = "application/octet-stream",
    ) -> UploadResult:
        return await self._upload(
            "/v1/files/upload-url",
            buffer=buffer,
            filename=filename,
            mime=mime,
            required_fields=("url", "mime", "size"),
        )

    async def _upload(
        self,
        path: str,
        *,
        buffer: bytes,
        filename: str,
        mime: str,
        required_fields: tuple[str, ...],
    ) -> UploadResult:
        boundary = f"----clawchat-{uuid.uuid4().hex}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode("utf-8") + buffer + f"\r\n--{boundary}--\r\n".encode("utf-8")
        payload = await self._call_json(
            "POST",
            path,
            body=body,
            extra_headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        for field in required_fields:
            if field not in payload:
                raise ClawChatApiError(
                    "transport",
                    f"invalid upload response: missing {field}",
                    path=path,
                )
        return UploadResult(
            url=str(payload["url"]),
            size=int(payload["size"]),
            mime=str(payload["mime"]),
            kind=str(payload["kind"]) if "kind" in payload else None,
            name=str(payload["name"]) if "name" in payload else None,
        )

    async def _call_json(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict:
        return await asyncio.to_thread(
            self._call_json_sync,
            method,
            path,
            body,
            extra_headers or {},
        )

    def _call_json_sync(
        self,
        method: str,
        path: str,
        body: bytes | None,
        extra_headers: dict[str, str],
    ) -> dict:
        request = Request(
            f"{self._base_url}{path}",
            method=method,
            data=body,
            headers=self._headers(extra_headers, body),
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            status = exc.code
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                code = payload.get("code")
                message = str(payload.get("msg") or payload.get("message") or exc.reason)
            else:
                code = None
                message = str(exc.reason or exc)
            kind = "auth" if status in (401, 403) else "api"
            raise ClawChatApiError(kind, message, status=status, path=path, code=code) from exc
        except URLError as exc:
            # connection-refused / DNS arrive here and prove the request never
            # reached the server, so they are safe to retry (connect_failed).
            # NOTE: read/connect timeouts do NOT reach this branch — they are
            # raised as a bare TimeoutError (see below) and left non-retryable.
            reason = exc.reason
            connect_failed = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise ClawChatApiError(
                "transport", str(reason or exc), path=path, connect_failed=connect_failed
            ) from exc
        except TimeoutError as exc:
            # A timeout is ambiguous (the server may already have processed a
            # single-use request), so it is explicitly NOT connect_failed and
            # will not be retried.
            raise ClawChatApiError(
                "transport", str(exc) or "request timed out", path=path, connect_failed=False
            ) from exc
        except Exception as exc:
            raise ClawChatApiError("transport", str(exc), path=path) from exc

        try:
            payload = json.loads(raw)
        except Exception as exc:
            raise ClawChatApiError("transport", "non-JSON response", status=status, path=path) from exc

        code = payload.get("code") if isinstance(payload, dict) else None
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("msg") or payload.get("message") or "")
        if code != 0:
            kind = "auth" if status in (401, 403) else "api"
            raise ClawChatApiError(kind, msg or f"code={code}", status=status, path=path, code=code)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ClawChatApiError("transport", "invalid envelope: missing object data", status=status, path=path)
        return data

    def _headers(self, extra_headers: dict[str, str], body: bytes | None) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._token}",
            "x-device-id": self._device_id,
        }
        if body is not None:
            headers["content-length"] = str(len(body))
        headers.update(extra_headers)
        return headers


def is_permanent_refresh_error(exc: ClawChatApiError) -> bool:
    """Classify a refresh failure as PERMANENT (auto-logout) vs TRANSIENT (retry).

    Per spec §B, only ``code == 10003`` (invalid refresh) and ``code == 400``
    (client bug) are permanent. ``code == 1`` (internal), non-200, and any
    network error are transient and must keep retrying — a transient failure
    never auto-logs-out, because no rotation was committed and the old refresh
    token is still valid.
    """
    return exc.code in (REFRESH_CODE_INVALID_REFRESH, REFRESH_CODE_BAD_REQUEST)


def _refresh_backoff_delay(attempt: int) -> float:
    base = min(
        REFRESH_RETRY_BACKOFF_CAP_SECONDS,
        REFRESH_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
    )
    jitter = random.uniform(-base * 0.25, base * 0.25)
    return max(0.0, min(REFRESH_RETRY_BACKOFF_CAP_SECONDS, base + jitter))


async def auth_refresh_with_retry(
    client: ClawChatApiClient,
    *,
    refresh_token: str,
    device_id: str,
    max_transient_retries: int | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> RefreshResult:
    """Call ``auth_refresh`` and retry ONLY transient failures with exp backoff.

    Transient failures (``code:1``, non-200, network) retry effectively
    unbounded but rate-limited (mirroring the WS supervisor that retries
    forever); ``max_transient_retries`` bounds it only for tests. PERMANENT
    failures (``code:10003`` / ``code:400``) propagate immediately so the caller
    can auto-logout. (Spec §B.)
    """
    sleeper = sleep if sleep is not None else asyncio.sleep
    attempt = 0
    while True:
        try:
            return await client.auth_refresh(
                refresh_token=refresh_token,
                device_id=device_id,
            )
        except ClawChatApiError as exc:
            if is_permanent_refresh_error(exc):
                raise
            attempt += 1
            if max_transient_retries is not None and attempt > max_transient_retries:
                raise
            delay = _refresh_backoff_delay(attempt)
            logger.warning(
                "clawchat token refresh transient failure (attempt %d), retrying in %.1fs: %s",
                attempt,
                delay,
                exc,
            )
            if delay:
                await sleeper(delay)


async def agents_connect_with_retry(
    client: ClawChatApiClient,
    *,
    code: str,
    user_id: str | None = None,
    retries: int = ACTIVATION_CONNECT_RETRIES,
    backoff: tuple[float, ...] = ACTIVATION_RETRY_BACKOFF_SECONDS,
    attempt_ceiling: float | None = ACTIVATION_ATTEMPT_CEILING_SECONDS,
) -> dict:
    """Call ``agents_connect`` for a single-use code, retrying ONLY failures that
    provably never reached the server (``connect_failed``). Ambiguous failures
    (timeout, reset) are surfaced immediately so the code is never double-spent.

    ``attempt_ceiling`` bounds each attempt's total wall clock (covering DNS
    resolution, which urlopen's timeout does not); a ceiling hit is ambiguous
    and therefore not retried.
    """
    connect_kwargs: dict[str, str] = {"code": code}
    if user_id and user_id.strip():
        connect_kwargs["user_id"] = user_id
    attempt = 0
    while True:
        try:
            if attempt_ceiling and attempt_ceiling > 0:
                return await asyncio.wait_for(
                    client.agents_connect(**connect_kwargs),
                    timeout=attempt_ceiling,
                )
            return await client.agents_connect(**connect_kwargs)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            # Ceiling hit (e.g. DNS stall): ambiguous, never retry a single-use code.
            raise ClawChatApiError(
                "transport", "activation request timed out", connect_failed=False
            ) from exc
        except ClawChatApiError as exc:
            if not exc.connect_failed or attempt >= retries:
                raise
            delay = backoff[min(attempt, len(backoff) - 1)] if backoff else 0
            logger.warning(
                "clawchat activation connection failed (attempt %d), retrying in %.0fs: %s",
                attempt + 1,
                delay,
                exc,
            )
            attempt += 1
            if delay:
                await asyncio.sleep(delay)
