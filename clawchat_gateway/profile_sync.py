from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clawchat_gateway.clawchat_metadata import (
    pull_group_metadata,
    pull_owner_metadata,
    pull_user_metadata,
)

logger = logging.getLogger(__name__)

DEFAULT_PLATFORM = "clawchat"
DEFAULT_ACCOUNT_ID = "bot"


def _missing_memory_root(memory_root: str | Path | None) -> bool:
    return memory_root is None or (
        isinstance(memory_root, str) and not memory_root.strip()
    )


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
    memory_root: str | Path | None = None,
) -> bool:
    if not sender_id:
        return False
    if owner_user_id and sender_id == owner_user_id:
        return False
    if _missing_memory_root(memory_root):
        logger.warning("clawchat user metadata refresh skipped reason=missing memory root user_id=%s", sender_id)
        return False
    try:
        await pull_user_metadata(memory_root, client, sender_id)
    except Exception:  # noqa: BLE001
        logger.warning("clawchat user profile refresh failed user_id=%s", sender_id, exc_info=True)
        return False
    return True


async def ensure_group_profile_for_chat(
    store: Any,
    client: Any,
    chat_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str = DEFAULT_ACCOUNT_ID,
    memory_root: str | Path | None = None,
) -> bool:
    if not chat_id:
        return False
    if _missing_memory_root(memory_root):
        logger.warning("clawchat group metadata refresh skipped reason=missing memory root chat_id=%s", chat_id)
        return False
    return await refresh_group_profile(
        store,
        client,
        chat_id,
        now_ms,
        platform=platform,
        account_id=account_id,
        memory_root=memory_root,
    )


async def refresh_agent_behavior_profile(
    store: Any,
    client: Any,
    *,
    agent_id: str,
    agent_user_id: str,
    now_ms: int,
    platform: str = DEFAULT_PLATFORM,
    account_id: str | None = None,
    memory_root: str | Path | None = None,
) -> bool:
    if not agent_id:
        return False
    if _missing_memory_root(memory_root):
        logger.warning("clawchat owner metadata refresh skipped reason=missing memory root agent_id=%s", agent_id)
        return False
    try:
        await pull_owner_metadata(
            memory_root,
            client,
            agent_id,
            connected_user_id=agent_user_id,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "clawchat agent behavior refresh failed agent_id=%s",
            agent_id,
            exc_info=True,
        )
        return False
    return True


async def refresh_group_profile(
    store: Any,
    client: Any,
    chat_id: str,
    now_ms: int,
    *,
    platform: str = DEFAULT_PLATFORM,
    account_id: str = DEFAULT_ACCOUNT_ID,
    memory_root: str | Path | None = None,
) -> bool:
    if not chat_id:
        return False
    if _missing_memory_root(memory_root):
        logger.warning("clawchat group metadata refresh skipped reason=missing memory root chat_id=%s", chat_id)
        return False
    try:
        await pull_group_metadata(memory_root, client, chat_id)
    except Exception:  # noqa: BLE001
        logger.warning("clawchat group metadata refresh failed chat_id=%s", chat_id, exc_info=True)
        return False
    return True
