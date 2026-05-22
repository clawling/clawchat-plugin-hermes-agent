from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "clawchat"
DEFAULT_ACCOUNT_ID = "bot"


def _first_present(*pairs: tuple[dict[str, Any], str]) -> Any:
    for source, key in pairs:
        if key in source:
            return source[key]
    raise KeyError


def relation_for_sender(
    sender_id: str,
    *,
    agent_user_id: str,
    owner_user_id: str,
    profile_type: str | None = None,
) -> str:
    if sender_id and sender_id == agent_user_id:
        return "self_agent"
    if sender_id and owner_user_id and sender_id == owner_user_id:
        return "owner"
    if profile_type == "agent":
        return "peer_agent"
    return "peer_user"


async def ensure_user_profile_for_sender(
    store: Any,
    client: Any,
    sender_id: str,
    agent_user_id: str,
    owner_user_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str | None = None,
) -> bool:
    if not sender_id:
        return False
    resolved_account_id = account_id or agent_user_id or DEFAULT_ACCOUNT_ID
    created = bool(
        store.upsert_minimal_profile(
            platform=platform,
            account_id=resolved_account_id,
            profile_kind="user",
            profile_id=sender_id,
            relation=relation_for_sender(
                sender_id,
                agent_user_id=agent_user_id,
                owner_user_id=owner_user_id,
            ),
            now_ms=now_ms,
        )
    )
    if not created:
        return False
    try:
        result = await client.get_user_info(sender_id)
    except Exception:  # noqa: BLE001
        logger.warning("clawchat user profile refresh failed user_id=%s", sender_id, exc_info=True)
        return False
    return upsert_user_profile_from_result(
        store,
        result,
        sender_id=sender_id,
        agent_user_id=agent_user_id,
        owner_user_id=owner_user_id,
        now_ms=now_ms,
        platform=platform,
        account_id=resolved_account_id,
    )


async def ensure_group_profile_for_chat(
    store: Any,
    client: Any,
    chat_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> bool:
    if not chat_id:
        return False
    created = bool(
        store.upsert_minimal_profile(
            platform=platform,
            account_id=account_id,
            profile_kind="group",
            profile_id=chat_id,
            now_ms=now_ms,
        )
    )
    if not created:
        return False
    return await refresh_group_profile(
        store,
        client,
        chat_id,
        now_ms,
        platform=platform,
        account_id=account_id,
    )


async def refresh_agent_behavior_profile(
    store: Any,
    client: Any,
    agent_user_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str | None = None,
) -> bool:
    if not agent_user_id:
        return False
    resolved_account_id = account_id or agent_user_id or DEFAULT_ACCOUNT_ID
    try:
        result = await client.get_agent_detail(agent_user_id)
    except Exception:  # noqa: BLE001
        logger.warning(
            "clawchat agent behavior refresh failed agent_user_id=%s",
            agent_user_id,
            exc_info=True,
        )
        return False
    detail = result.get("agent") if isinstance(result.get("agent"), dict) else result
    if not isinstance(detail, dict):
        return False
    profile_id = str(detail.get("user_id") or detail.get("userId") or detail.get("id") or agent_user_id)
    kwargs = {
        "platform": platform,
        "account_id": resolved_account_id,
        "profile_id": profile_id,
        "raw": detail,
        "now_ms": now_ms,
    }
    if "behavior" in detail:
        kwargs["behavior"] = detail["behavior"]
    store.upsert_agent_profile(**kwargs)
    return True


async def refresh_group_profile(
    store: Any,
    client: Any,
    chat_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> bool:
    if not chat_id:
        return False
    try:
        result = await client.get_conversation(chat_id)
    except Exception:  # noqa: BLE001
        logger.warning("clawchat group profile refresh failed chat_id=%s", chat_id, exc_info=True)
        return False
    return upsert_group_profile_from_result(
        store,
        result,
        chat_id=chat_id,
        now_ms=now_ms,
        platform=platform,
        account_id=account_id,
    )


def upsert_group_profile_from_result(
    store: Any,
    result: dict[str, Any],
    *,
    chat_id: str,
    now_ms: int,
    platform: str = DEFAULT_PLATFORM,
    account_id: str = DEFAULT_ACCOUNT_ID,
) -> bool:
    detail = result.get("conversation") if isinstance(result.get("conversation"), dict) else result
    if not isinstance(detail, dict):
        return False
    profile_id = str(detail.get("conversation_id") or detail.get("conversationId") or detail.get("id") or chat_id)
    group = detail.get("group") if isinstance(detail.get("group"), dict) else {}
    metadata_version = detail.get("metadata_version") or detail.get("metadataVersion")
    if not isinstance(metadata_version, int):
        metadata_version = group.get("metadata_version") or group.get("metadataVersion")
    kwargs = {
        "platform": platform,
        "account_id": account_id,
        "profile_id": profile_id,
        "raw": group or detail,
        "now_ms": now_ms,
    }
    try:
        kwargs["title"] = _first_present((detail, "title"), (group, "title"))
    except KeyError:
        pass
    try:
        kwargs["description"] = _first_present(
            (detail, "description"),
            (group, "description"),
        )
    except KeyError:
        pass
    if isinstance(metadata_version, int):
        kwargs["metadata_version"] = metadata_version
    store.upsert_group_profile(**kwargs)
    return True


def upsert_user_profile_from_result(
    store: Any,
    result: dict[str, Any],
    *,
    sender_id: str,
    agent_user_id: str,
    owner_user_id: str,
    now_ms: int,
    platform: str = DEFAULT_PLATFORM,
    account_id: str | None = None,
) -> bool:
    detail = result.get("user") if isinstance(result.get("user"), dict) else result
    if not isinstance(detail, dict):
        return False
    profile_id = str(detail.get("user_id") or detail.get("userId") or detail.get("id") or sender_id)
    profile_type = detail.get("type") or detail.get("profile_type")
    resolved_account_id = account_id or agent_user_id or DEFAULT_ACCOUNT_ID
    kwargs = {
        "platform": platform,
        "account_id": resolved_account_id,
        "profile_id": profile_id,
        "relation": relation_for_sender(
            profile_id,
            agent_user_id=agent_user_id,
            owner_user_id=owner_user_id,
            profile_type=profile_type if isinstance(profile_type, str) else None,
        ),
        "raw": detail,
        "now_ms": now_ms,
    }
    if isinstance(profile_type, str):
        kwargs["profile_type"] = profile_type
    for key in ("nickname", "bio"):
        if key in detail:
            kwargs[key] = detail[key]
    if "avatar_url" in detail:
        kwargs["avatar_url"] = detail["avatar_url"]
    elif "avatarUrl" in detail:
        kwargs["avatar_url"] = detail["avatarUrl"]
    store.upsert_user_profile(**kwargs)
    return True
