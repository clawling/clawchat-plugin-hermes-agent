"""Shared HTTP client for ClawChat REST APIs used by tools and media uploads."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import uuid
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
        tools: list[str] | None = None,
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
        if tools:
            payload["tools"] = [tool for tool in tools if isinstance(tool, str) and tool.strip()]
        body = json.dumps(payload).encode("utf-8")
        return await self._call_json(
            "POST",
            "/v1/agents/connect",
            body=body,
            extra_headers={"content-type": "application/json"},
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
