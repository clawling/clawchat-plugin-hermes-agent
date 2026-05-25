from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from clawchat_gateway.clawchat_memory import (
    ensure_clawchat_memory_target_safe,
    read_clawchat_memory_file,
    write_clawchat_metadata,
)

logger = logging.getLogger(__name__)

__all__ = [
    "owner_metadata_from_agent",
    "user_metadata_from_profile",
    "group_metadata_from_conversation",
    "pull_owner_metadata",
    "pull_user_metadata",
    "pull_group_metadata",
    "push_metadata",
    "update_metadata",
]

_OWNER_MUTABLE_FIELDS = ("agent_behavior",)
_USER_MUTABLE_FIELDS = ("nickname", "avatar_url", "bio")
_GROUP_MUTABLE_FIELDS = ("title", "description")
_MUTABLE_FIELDS_BY_TARGET = {
    "owner": _OWNER_MUTABLE_FIELDS,
    "user": _USER_MUTABLE_FIELDS,
    "group": _GROUP_MUTABLE_FIELDS,
}


def _detail(result: dict[str, Any], key: str) -> dict[str, Any] | None:
    nested = result.get(key)
    if isinstance(nested, dict):
        return nested
    return result if isinstance(result, dict) else None


def _first_string(source: dict[str, Any], *keys: str, fallback: str = "") -> str:
    for key in keys:
        if key in source:
            value = source[key]
            if isinstance(value, str):
                return value
            if value is not None:
                return str(value)
    return fallback


def _copy_present(
    metadata: dict[str, str],
    target: str,
    source: dict[str, Any],
    *keys: str,
) -> None:
    for key in keys:
        if key in source:
            value = source[key]
            metadata[target] = value if isinstance(value, str) else str(value)
            return


def owner_metadata_from_agent(
    result: dict[str, Any],
    *,
    connected_user_id: str = "",
    owner_user_id: str = "",
) -> dict[str, str]:
    detail = _detail(result, "agent")
    if not isinstance(detail, dict):
        return {}
    metadata: dict[str, str] = {}
    _copy_present(metadata, "updated_at", detail, "updated_at", "updatedAt")
    resolved_agent_id = _first_string(
        detail,
        "user_id",
        "userId",
        fallback=connected_user_id,
    )
    if resolved_agent_id:
        metadata["agent_id"] = resolved_agent_id
    resolved_owner_id = _first_string(
        detail,
        "owner_id",
        "ownerId",
        "owner_user_id",
        "ownerUserId",
        fallback=owner_user_id,
    )
    if resolved_owner_id:
        metadata["owner_id"] = resolved_owner_id
    _copy_present(metadata, "agent_nickname", detail, "nickname")
    _copy_present(metadata, "agent_avatar_url", detail, "avatar_url", "avatarUrl")
    _copy_present(metadata, "agent_bio", detail, "bio")
    _copy_present(metadata, "agent_behavior", detail, "behavior")
    return metadata


def add_owner_profile_metadata(
    metadata: dict[str, str],
    result: dict[str, Any],
) -> None:
    detail = _detail(result, "user")
    if not isinstance(detail, dict):
        return
    _copy_present(metadata, "owner_nickname", detail, "nickname")
    _copy_present(metadata, "owner_avatar_url", detail, "avatar_url", "avatarUrl")
    _copy_present(metadata, "owner_bio", detail, "bio")


def user_metadata_from_profile(result: dict[str, Any], *, user_id: str) -> dict[str, str]:
    detail = _detail(result, "user")
    if not isinstance(detail, dict):
        return {}
    metadata: dict[str, str] = {}
    _copy_present(metadata, "updated_at", detail, "updated_at", "updatedAt")
    metadata["id"] = user_id
    _copy_present(metadata, "nickname", detail, "nickname")
    _copy_present(metadata, "avatar_url", detail, "avatar_url", "avatarUrl")
    _copy_present(metadata, "bio", detail, "bio")
    _copy_present(metadata, "profile_type", detail, "profile_type", "type")
    return metadata


def group_metadata_from_conversation(
    result: dict[str, Any],
    *,
    group_id: str,
) -> dict[str, str]:
    detail = _detail(result, "conversation")
    if not isinstance(detail, dict):
        return {}
    group = detail.get("group") if isinstance(detail.get("group"), dict) else {}
    metadata: dict[str, str] = {}
    _copy_present(metadata, "updated_at", detail, "updated_at", "updatedAt")
    metadata["id"] = group_id
    _copy_present(metadata, "type", detail, "type", "conversation_type", "conversationType")
    for field in ("title", "description"):
        if field in detail:
            _copy_present(metadata, field, detail, field)
        elif field in group:
            _copy_present(metadata, field, group, field)
    _copy_present(metadata, "creator_id", detail, "creator_id", "creatorId")
    _copy_present(metadata, "created_at", detail, "created_at", "createdAt")
    return metadata


def _participant_user_id(participant: dict[str, Any]) -> str:
    return _first_string(participant, "id", "user_id", "userId")


def _participants(result: dict[str, Any]) -> list[dict[str, Any]]:
    detail = _detail(result, "conversation")
    if not isinstance(detail, dict):
        return []
    raw = detail.get("participants")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


async def pull_owner_metadata(
    root: str | Path,
    client: Any,
    agent_id: str,
    *,
    connected_user_id: str = "",
    owner_user_id: str = "",
) -> dict[str, Any]:
    if not agent_id:
        raise ValueError("agent_id is required")
    ensure_clawchat_memory_target_safe(root, "owner", "owner")
    result = await client.get_agent_detail(agent_id)
    metadata = owner_metadata_from_agent(
        result,
        connected_user_id=connected_user_id,
        owner_user_id=owner_user_id,
    )
    owner_id = metadata.get("owner_id", "")
    if owner_id:
        add_owner_profile_metadata(metadata, await client.get_user_info(owner_id))
    write_clawchat_metadata(root, "owner", "owner", metadata)
    return {"ok": True, "target_type": "owner", "target_id": "owner", "metadata": metadata}


async def pull_user_metadata(root: str | Path, client: Any, user_id: str) -> dict[str, Any]:
    if not user_id:
        raise ValueError("user_id is required")
    ensure_clawchat_memory_target_safe(root, "user", user_id)
    result = await client.get_user_info(user_id)
    metadata = user_metadata_from_profile(result, user_id=user_id)
    write_clawchat_metadata(root, "user", user_id, metadata)
    return {"ok": True, "target_type": "user", "target_id": user_id, "metadata": metadata}


async def pull_group_metadata(root: str | Path, client: Any, group_id: str) -> dict[str, Any]:
    if not group_id:
        raise ValueError("group_id is required")
    ensure_clawchat_memory_target_safe(root, "group", group_id)
    result = await client.get_conversation(group_id)
    group_metadata = group_metadata_from_conversation(result, group_id=group_id)
    write_clawchat_metadata(root, "group", group_id, group_metadata)

    failures: list[dict[str, str]] = []
    for participant in _participants(result):
        user_id = _participant_user_id(participant)
        try:
            existing = read_clawchat_memory_file(root, "user", user_id)
            if existing.get("exists"):
                continue
            profile = await client.get_user_info(user_id)
            metadata = user_metadata_from_profile(profile, user_id=user_id)
            write_clawchat_metadata(root, "user", user_id, metadata)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "clawchat participant metadata write failed user_id=%s",
                user_id,
                exc_info=True,
            )
            failures.append(
                {
                    "target_type": "user",
                    "target_id": user_id,
                    "error": str(exc),
                }
            )

    return {
        "ok": not failures,
        "target_type": "group",
        "target_id": group_id,
        "metadata": group_metadata,
        "partial_failures": failures,
    }


async def push_metadata(
    root: str | Path,
    client: Any,
    target_type: str,
    target_id: str,
    *,
    fields: list[str] | tuple[str, ...],
    agent_id: str = "",
    connected_user_id: str = "",
) -> dict[str, Any]:
    memory = read_clawchat_memory_file(root, target_type, target_id)
    patch = _push_patch_for_target(
        target_type,
        target_id,
        memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {},
        fields=fields,
        agent_id=agent_id,
        connected_user_id=connected_user_id,
    )
    return await update_metadata(
        root,
        client,
        target_type,
        target_id,
        patch,
        agent_id=agent_id,
        connected_user_id=connected_user_id,
    )


def _mutable_fields_for_target(
    target_type: str,
    target_id: str,
    *,
    agent_id: str,
    connected_user_id: str,
) -> tuple[str, ...]:
    if target_type == "owner":
        if target_id != "owner":
            raise ValueError("owner target requires target_id='owner'")
        if not agent_id:
            raise ValueError("agent_id is required")
    elif target_type == "user":
        if target_id != connected_user_id:
            raise ValueError("user metadata update is allowed only for the connected user")
    elif target_type != "group":
        raise ValueError(f"unsupported ClawChat metadata target_type: {target_type}")
    return _MUTABLE_FIELDS_BY_TARGET[target_type]


def _push_patch_for_target(
    target_type: str,
    target_id: str,
    metadata: dict[str, str],
    *,
    fields: list[str] | tuple[str, ...],
    agent_id: str,
    connected_user_id: str,
) -> dict[str, str]:
    allowed = _mutable_fields_for_target(
        target_type,
        target_id,
        agent_id=agent_id,
        connected_user_id=connected_user_id,
    )
    if not fields:
        raise ValueError("fields are required for metadata push")

    patch: dict[str, str] = {}
    for field in fields:
        if not isinstance(field, str) or not field:
            raise ValueError("fields must contain non-empty strings")
        if field not in allowed:
            raise ValueError(f"fields contain non-pushable metadata field: {field}")
        if field not in metadata:
            raise ValueError(f"missing_metadata_field: {field}")
        patch[field] = metadata[field]
    return patch


def _patch_for_target(
    target_type: str,
    target_id: str,
    metadata: dict[str, str],
    *,
    agent_id: str,
    connected_user_id: str,
) -> dict[str, str]:
    allowed = _mutable_fields_for_target(
        target_type,
        target_id,
        agent_id=agent_id,
        connected_user_id=connected_user_id,
    )
    return {field: metadata[field] for field in allowed if field in metadata}


async def update_metadata(
    root: str | Path,
    client: Any,
    target_type: str,
    target_id: str,
    patch: dict[str, Any],
    *,
    agent_id: str = "",
    connected_user_id: str = "",
) -> dict[str, Any]:
    ensure_clawchat_memory_target_safe(root, target_type, target_id)
    allowed_patch = _patch_for_target(
        target_type,
        target_id,
        {key: str(value) for key, value in patch.items()},
        agent_id=agent_id,
        connected_user_id=connected_user_id,
    )
    if not allowed_patch:
        raise ValueError("metadata patch is empty")

    if target_type == "owner":
        result = await client.patch_agent(agent_id, behavior=allowed_patch["agent_behavior"])
        metadata = owner_metadata_from_agent(
            result,
            connected_user_id=connected_user_id,
        )
        owner_id = metadata.get("owner_id", "")
        if owner_id:
            add_owner_profile_metadata(metadata, await client.get_user_info(owner_id))
        write_clawchat_metadata(root, "owner", "owner", metadata)
    elif target_type == "user":
        result = await client.update_my_profile(**allowed_patch)
        metadata = user_metadata_from_profile(result, user_id=target_id)
        write_clawchat_metadata(root, "user", target_id, metadata)
    elif target_type == "group":
        result = await client.patch_conversation(target_id, **allowed_patch)
        metadata = group_metadata_from_conversation(result, group_id=target_id)
        write_clawchat_metadata(root, "group", target_id, metadata)
    else:
        raise ValueError(f"unsupported ClawChat metadata target_type: {target_type}")

    return {
        "ok": True,
        "target_type": target_type,
        "target_id": target_id,
        "metadata": metadata,
    }
