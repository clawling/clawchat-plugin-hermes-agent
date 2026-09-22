"""Shared HTTP client for ClawChat REST APIs used by tools and media uploads."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from clawchat_gateway import __version__
from clawchat_gateway.device_id import get_device_id
from clawchat_gateway.group_settings import GroupSettings, GroupSettingsFetchResult
from clawchat_gateway.permissions import PermissionPolicy

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

# POST /v1/agents/connect envelope code for "the supplied user_id matches no
# agent". Servers that predate the stale-user_id fallback return this instead of
# degrading to a fresh pairing; the client sheds the id and retries once.
AGENT_NOT_FOUND_CODE = 16001

# Transient-refresh backoff (spec §B): min(30s, 1s * 2^(n-1)) ± jitter, cap 30s.
REFRESH_RETRY_BACKOFF_CAP_SECONDS = 30.0
REFRESH_RETRY_BASE_SECONDS = 1.0
# urlopen's timeout: bounds each socket operation, NOT the whole attempt (a slow
# trickle of bytes, or a DNS stall, can outlast it).
REFRESH_REQUEST_TIMEOUT_SECONDS = 15.0
# Spec §B attempt deadline: hard total wall clock for one refresh attempt. A hit
# is TRANSIENT. The worker thread cannot be cancelled; if it still receives a
# rotation after the deadline the result is dropped, and the backend refresh
# grace window lets the retry below redeem the same refresh token again.
REFRESH_ATTEMPT_CEILING_SECONDS = 20.0
# Spec §B retry within the grace window: after a transient whose outcome is
# unknown (deadline hit, reset, non-200 — the server may have rotated), the next
# attempt with the same refresh token starts 30s + 1-5s jitter after the previous
# attempt BEGAN. That clears the backend's minimum replay age by a wide margin,
# lands the first two replays inside its 90s window (~31-35s, ~62-70s) and the
# third outside it (>= 93s), so normal retrying never exceeds its 2-replay cap.
REFRESH_UNKNOWN_OUTCOME_SPACING_SECONDS = 30.0
REFRESH_UNKNOWN_OUTCOME_JITTER_SECONDS = (1.0, 5.0)


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
    # The envelope ``data`` dict from the backend response, carried only when
    # code != 0 so that callers (e.g. gate-outcome mapping) can read fields
    # like request_id, operation, and expires_at without re-parsing the body.
    data: dict | None = None

    def __str__(self) -> str:
        return self.message


def build_plugin_report_payload(
    *,
    device_id: str,
    platform: str,
    plugin_version: str,
    agent_version: str,
    runtime_name: str,
    runtime_version: str,
    onboarding: dict[str, Any] | None = None,
) -> dict:
    """Pure builder for the plugin-report wire body (snake_case keys).

    ``onboarding`` carries the already-validated agent-written facts from
    ``~/clawchat/onboarding.json`` (see ``clawchat_gateway.onboarding_report``);
    only the four contract keys are ever merged in.
    """
    payload = {
        "device_id": device_id,
        "platform": platform,
        "plugin_version": plugin_version,
        "agent_version": agent_version,
        "runtime_name": runtime_name,
        "runtime_version": runtime_version,
    }
    for key in ("wiki_report_id", "capability_tier", "capability_ceiling", "capabilities"):
        if onboarding and onboarding.get(key) is not None:
            payload[key] = onboarding[key]
    return payload


@dataclass(frozen=True)
class UploadResult:
    url: str
    size: int
    mime: str
    kind: str | None = None
    name: str | None = None


_ORCH = "/v1/agents/me/orchestration"


def _orch_json(payload: dict) -> dict:
    """Body + content-type for an orchestration write, in one place.

    Every orchestration write is a small JSON object and the backend rejects a
    missing content-type, so the two always travel together.
    """
    return {"body": json.dumps(payload).encode("utf-8"), "extra_headers": {"content-type": "application/json"}}


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

    async def get_my_group_settings(self) -> GroupSettingsFetchResult:
        """Fetch per-group agent settings for this agent.

        Returns a :class:`GroupSettingsFetchResult` whose ``authoritative`` flag
        distinguishes an HTTP 200 (the complete override snapshot, possibly
        empty — ``authoritative=True``) from every NON-authoritative outcome
        (HTTP 404 / endpoint-absent / non-2xx / network error —
        ``authoritative=False``, empty rows). Only authoritative results may
        replace the cache; an HTTP 200 with an EMPTY list authoritatively means
        "zero overrides" and clears it, whereas a 404 (older backend without the
        endpoint) carries no information and must be a no-op.

        Auth (401/403) errors are NOT swallowed: they propagate (``kind ==
        "auth"``) so the caller's reactive token-refresh-and-retry path runs.
        """
        try:
            data = await self._call_json("GET", "/v1/agents/me/group-settings")
        except ClawChatApiError as exc:
            # Auth (401/403) errors MUST propagate so the caller's reactive
            # token-refresh-and-retry path runs (mirrors the OpenClaw plugin:
            # auth throws drive refresh). Swallowing them would leave a
            # mute/reply-mode change unloadable until some unrelated REST call
            # happened to refresh an expired access token.
            if exc.kind == "auth":
                raise
            # Every OTHER error (404 / non-2xx / network / non-JSON) is
            # NON-authoritative: it says nothing about the agent's actual
            # override set, so preserve the cache. Logged here for visibility;
            # callers treat it as a no-op.
            logger.debug("clawchat group-settings fetch non-authoritative", exc_info=True)
            return GroupSettingsFetchResult(authoritative=False)
        rows: list[GroupSettings] = []
        for item in data.get("settings") or []:
            if not isinstance(item, dict):
                continue
            try:
                rows.append(
                    GroupSettings(
                        conversation_id=str(item["conversation_id"]),
                        muted=bool(item.get("muted", False)),
                        reply_mode=str(item.get("reply_mode", "all")),
                        batch_delay_seconds=int(item.get("batch_delay_seconds", 0)),
                        version=int(item.get("version", 0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                logger.warning("clawchat group-settings: skipping malformed row %r", item)
        return GroupSettingsFetchResult(authoritative=True, rows=rows)

    async def get_my_permissions(self) -> PermissionPolicy:
        """Fetch the agent's permission policy from the backend.

        Returns a :class:`PermissionPolicy` parsed from the flat object
        returned by ``GET /v1/agents/me/permissions``.  Every key except
        ``"moment.visibility"`` is an operation→state mapping; the array key
        is placed in :attr:`PermissionPolicy.moment_visibility`.

        Non-200 / endpoint-absent / network errors are NOT swallowed here —
        callers are responsible for best-effort handling (log + keep stale
        cache).  Auth (401/403) errors propagate as ``kind == "auth"`` so the
        caller's reactive token-refresh-and-retry path can run.
        """
        data = await self._call_json("GET", "/v1/agents/me/permissions")
        by_op: dict[str, str] = {}
        vis: list[str] = []
        for k, v in (data or {}).items():
            if k == "moment.visibility":
                vis = v or []
            else:
                by_op[k] = v
        return PermissionPolicy(by_operation=by_op, moment_visibility=vis)

    async def get_my_profile(self) -> dict:
        return await self._call_json("GET", "/v1/users/me")

    async def get_user_info(self, user_id: str) -> dict:
        if not user_id.strip():
            raise ClawChatApiError("validation", "user_id is required")
        return await self._call_json("GET", f"/v1/users/{user_id}")

    async def get_agent_owner(self) -> dict:
        """Owner profile (incl. locale) of the calling agent, from the agent JWT's oid."""
        return await self._call_json("GET", "/v1/agents/me/owner")

    async def list_friends(self, *, page: int = 1, page_size: int = 20) -> dict:
        return await self._call_json("GET", "/v1/friendships")

    # --- cloud orchestration (`agent.orchestrate`) -------------------------
    # Twelve routes, 1:1 with docs/features/agentorch.md. Every response is
    # HTTP 200 with the business code in the envelope; these methods do not
    # interpret it, the caller does.

    async def orch_list_agents(self) -> dict:
        return await self._call_json("GET", f"{_ORCH}/agents")

    async def orch_get_agent(self, agent_id: str) -> dict:
        return await self._call_json("GET", f"{_ORCH}/agents/{quote(agent_id, safe='')}")

    async def orch_set_agent_behavior(self, agent_id: str, behavior: str) -> dict:
        return await self._call_json(
            "PATCH", f"{_ORCH}/agents/{quote(agent_id, safe='')}", **_orch_json({"behavior": behavior})
        )

    async def orch_list_groups(self) -> dict:
        return await self._call_json("GET", f"{_ORCH}/groups")

    async def orch_get_group(self, cid: str) -> dict:
        return await self._call_json("GET", f"{_ORCH}/groups/{quote(cid, safe='')}")

    async def orch_set_group_prompt(self, cid: str, description: str) -> dict:
        return await self._call_json(
            "PATCH", f"{_ORCH}/groups/{quote(cid, safe='')}", **_orch_json({"description": description})
        )

    async def orch_create_group(self, title: str, agent_ids: list[str]) -> dict:
        return await self._call_json(
            "POST", f"{_ORCH}/groups", **_orch_json({"title": title, "agent_ids": list(agent_ids)})
        )

    async def orch_add_group_member(self, cid: str, agent_id: str) -> dict:
        return await self._call_json(
            "POST", f"{_ORCH}/groups/{quote(cid, safe='')}/members", **_orch_json({"agent_id": agent_id})
        )

    async def orch_remove_group_member(self, cid: str, agent_id: str) -> dict:
        return await self._call_json(
            "DELETE", f"{_ORCH}/groups/{quote(cid, safe='')}/members/{quote(agent_id, safe='')}"
        )

    async def orch_set_group_agent_settings(
        self,
        cid: str,
        agent_id: str,
        *,
        muted: bool | None = None,
        reply_mode: str | None = None,
        batch_delay_seconds: int | None = None,
    ) -> dict:
        # `is not None` rather than truthiness: `muted=False` and
        # `batch_delay_seconds=0` are meaningful values the backend must see,
        # while an omitted field means "leave unchanged".
        payload: dict = {}
        if muted is not None:
            payload["muted"] = muted
        if reply_mode is not None:
            payload["reply_mode"] = reply_mode
        if batch_delay_seconds is not None:
            payload["batch_delay_seconds"] = batch_delay_seconds
        return await self._call_json(
            "PATCH",
            f"{_ORCH}/groups/{quote(cid, safe='')}/agents/{quote(agent_id, safe='')}",
            **_orch_json(payload),
        )

    async def orch_create_connect_code(self) -> dict:
        return await self._call_json("POST", f"{_ORCH}/connect-codes")

    async def orch_get_connect_code(self, code: str) -> dict:
        return await self._call_json("GET", f"{_ORCH}/connect-codes/{quote(code, safe='')}")

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

    async def register_app(self, *, name: str, app_id: str, url: str) -> dict:
        if not name.strip():
            raise ClawChatApiError("validation", "name is required")
        if not app_id.strip():
            raise ClawChatApiError("validation", "app_id is required")
        if not url.strip():
            raise ClawChatApiError("validation", "url is required")
        payload = {"name": name, "app_id": app_id, "url": url}
        return await self._call_json(
            "POST",
            "/v1/agents/me/apps",
            body=json.dumps(payload).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def list_apps(self) -> dict:
        return await self._call_json("GET", "/v1/agents/me/apps")

    async def unregister_app(self, app_id: str) -> dict:
        if not app_id.strip():
            raise ClawChatApiError("validation", "app_id is required")
        return await self._call_json("DELETE", f"/v1/agents/me/apps/{app_id}")

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

    async def get_moment(self, moment_id: int) -> dict:
        return await self._call_json("GET", f"/v1/moments/{moment_id}")

    async def get_direct_conversation(self, peer_id: str) -> dict:
        """Find-or-create the direct conversation with ``peer_id``.

        ``POST /v1/conversations/direct``; the peer must already be a friend
        (member-backend code 19012 otherwise). Returns
        ``{"conversation": {"id": "cnv_…", "type": "direct"}}``.
        """
        if not isinstance(peer_id, str) or not peer_id.strip():
            raise ClawChatApiError("validation", "peer_id is required")
        return await self._call_json(
            "POST",
            "/v1/conversations/direct",
            body=json.dumps({"peer_id": peer_id.strip()}).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

    async def get_conversation(self, conversation_id: str) -> dict:
        if not conversation_id.strip():
            raise ClawChatApiError("validation", "conversation_id is required")
        return await self._call_json("GET", f"/v1/conversations/{conversation_id}")

    async def leave_conversation(self, conversation_id: str) -> dict:
        if not conversation_id.strip():
            raise ClawChatApiError("validation", "conversation_id is required")
        return await self._call_json("POST", f"/v1/conversations/{conversation_id}/leave")

    async def add_conversation_member(self, *, conversation_id: str, user_id: str) -> dict:
        if not conversation_id.strip():
            raise ClawChatApiError("validation", "conversation_id is required")
        if not user_id.strip():
            raise ClawChatApiError("validation", "user_id is required")
        return await self._call_json(
            "POST",
            f"/v1/conversations/{quote(conversation_id, safe='')}/members",
            body=json.dumps({"user_id": user_id}).encode("utf-8"),
            extra_headers={"content-type": "application/json"},
        )

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

    async def agents_connect_check(
        self,
        *,
        code: str,
        user_id: str | None = None,
        context: dict[str, str] | None = None,
    ) -> dict:
        """``POST /v1/agents/connect/check`` — non-consuming pairability check.

        Same ``X-Device-Id`` as ``agents_connect`` so the funnel row is keyed
        identically. ``context`` carries the optional onboarding telemetry
        (``clawchat_gateway.onboarding.onboarding_context``); older backends
        ignore unknown keys. Response: ``{pairable, status, expires_at?,
        user_id_status?, bound_agent?}``.
        """
        if not code.strip():
            raise ClawChatApiError("validation", "connect code is required")
        payload: dict[str, str] = {
            "code": code.strip(),
            "platform": AGENTS_CONNECT_PLATFORM,
            "plugin_version": __version__,
        }
        if user_id and user_id.strip():
            payload["user_id"] = user_id.strip()
        for key, value in (context or {}).items():
            if value:
                payload[key] = value
        body = json.dumps(payload).encode("utf-8")
        return await self._call_json(
            "POST",
            "/v1/agents/connect/check",
            body=body,
            extra_headers={"content-type": "application/json"},
        )

    async def agents_connect(
        self,
        *,
        code: str,
        user_id: str | None = None,
        context: dict[str, str] | None = None,
    ) -> dict:
        if not code.strip():
            raise ClawChatApiError("validation", "invite code is required")
        payload = {
            "code": code.strip(),
            "platform": AGENTS_CONNECT_PLATFORM,
            "type": AGENTS_CONNECT_TYPE,
            "plugin_version": __version__,
        }
        if user_id and user_id.strip():
            payload["user_id"] = user_id.strip()
        for key, value in (context or {}).items():
            if value:
                payload[key] = value
        body = json.dumps(payload).encode("utf-8")
        return await self._call_json(
            "POST",
            "/v1/agents/connect",
            body=body,
            extra_headers={"content-type": "application/json"},
        )

    async def report_plugin(
        self,
        *,
        device_id: str,
        platform: str,
        plugin_version: str,
        agent_version: str,
        runtime_name: str,
        runtime_version: str,
        authenticated: bool = False,
        onboarding: dict[str, Any] | None = None,
    ) -> dict:
        payload = build_plugin_report_payload(
            device_id=device_id,
            platform=platform,
            plugin_version=plugin_version,
            agent_version=agent_version,
            runtime_name=runtime_name,
            runtime_version=runtime_version,
            onboarding=onboarding,
        )
        body = json.dumps(payload).encode("utf-8")
        path = "/v1/agents/me/plugin-report" if authenticated else "/v1/agents/plugin-report"
        return await self._call_json(
            "POST",
            path,
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
        - attempt deadline (``REFRESH_ATTEMPT_CEILING_SECONDS``) hit → TRANSIENT
          (kind ``transport``, not ``connect_failed``: the outcome is unknown).
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._auth_refresh_sync,
                    refresh_token,
                    device_id,
                ),
                timeout=REFRESH_ATTEMPT_CEILING_SECONDS,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise ClawChatApiError(
                "transport",
                "refresh request timed out",
                path="/v1/auth/refresh",
                connect_failed=False,
            ) from exc

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
            gate_data = payload.get("data") if isinstance(payload, dict) else None
            raise ClawChatApiError(
                kind,
                msg or f"code={code}",
                status=status,
                path=path,
                code=code,
                data=gate_data if isinstance(gate_data, dict) else None,
            )
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
    never auto-logs-out. Either no rotation was committed (the old refresh token
    is still valid), or the server rotated but the response was lost, in which
    case a retry inside the backend refresh grace window redeems the old token
    again. A ``10003`` after such a timeout is still permanent: it means the
    window has passed or the token was revoked (spec §B escalation).
    """
    return exc.code in (REFRESH_CODE_INVALID_REFRESH, REFRESH_CODE_BAD_REQUEST)


def _refresh_outcome_unknown(exc: ClawChatApiError) -> bool:
    """True when the failed attempt may have rotated the token server-side.

    Only a failure that provably never reached the server (DNS / connection
    refused) or an explicit ``code:1`` (server says no rotation committed) is
    known not to have rotated; everything else — deadline hit, reset, non-200,
    malformed success body, unknown code — is ambiguous.
    """
    if exc.connect_failed:
        return False
    return exc.code != REFRESH_CODE_INTERNAL


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
    monotonic: Callable[[], float] | None = None,
) -> RefreshResult:
    """Call ``auth_refresh`` and retry ONLY transient failures with exp backoff.

    Transient failures (``code:1``, non-200, network, attempt deadline) retry
    effectively unbounded but rate-limited (mirroring the WS supervisor that
    retries forever); ``max_transient_retries`` bounds it only for tests.
    PERMANENT failures (``code:10003`` / ``code:400``) propagate immediately so
    the caller can auto-logout. (Spec §B.)

    After a transient whose outcome is unknown, the next attempt is held until
    ``REFRESH_UNKNOWN_OUTCOME_SPACING_SECONDS`` + 1-5s jitter after the failed
    attempt began, so replays of the same refresh token land inside the backend
    grace window without exceeding its replay cap (spec §B "Retry within the
    grace window").
    """
    sleeper = sleep if sleep is not None else asyncio.sleep
    clock = monotonic if monotonic is not None else time.monotonic
    attempt = 0
    while True:
        attempt_started = clock()
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
            if _refresh_outcome_unknown(exc):
                low, high = REFRESH_UNKNOWN_OUTCOME_JITTER_SECONDS
                spacing = REFRESH_UNKNOWN_OUTCOME_SPACING_SECONDS + random.uniform(low, high)
                delay = max(delay, spacing - (clock() - attempt_started))
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
    context: dict[str, str] | None = None,
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
    connect_kwargs: dict[str, object] = {"code": code}
    if user_id and user_id.strip():
        connect_kwargs["user_id"] = user_id
    connect_kwargs["context"] = context
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
