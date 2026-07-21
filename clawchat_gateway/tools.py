"""Hermes tool handlers for ClawChat.

This module is the single source of truth for the new profile/media tool
surface used by both Hermes tool registration and the profile CLI.
"""

from __future__ import annotations

import asyncio
import mimetypes
import shutil
from types import SimpleNamespace
from pathlib import Path
from typing import Any

from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError
from clawchat_gateway.gate_outcome import map_gate_outcome
from clawchat_gateway.liveware_cli import resolve_liveware_path
from clawchat_gateway.clawchat_memory import (
    delete_clawchat_memory_file,
    edit_clawchat_memory_body,
    read_clawchat_memory_file,
    search_clawchat_memory,
    write_clawchat_memory_body,
)
from clawchat_gateway.clawchat_metadata import (
    pull_group_metadata,
    pull_owner_metadata,
    pull_user_metadata,
    push_metadata,
    update_metadata as update_clawchat_metadata,
)
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.storage import get_clawchat_store, make_owner_profile_persister
from clawchat_gateway.mention_message import normalize_mention_targets
from clawchat_gateway.profile import ProfileConfigError, load_profile_config
from clawchat_gateway.terminal_send import send_clawchat_mention_message

MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# Owner-approval gate business codes returned by the ClawChat backend for sensitive
# agent operations. Both are terminal for this call: the agent must NOT retry.
# - PENDING_APPROVAL: the op needs owner approval; the result arrives later as a
#   normal chat message, not as the return value of this call.
# - POLICY_FORBIDDEN: the op is blocked by owner policy.
CODE_PENDING_APPROVAL = 21001
CODE_POLICY_FORBIDDEN = 21003


def _config_error(message: str) -> dict[str, Any]:
    return {"error": "config", "message": message}


def _validation_error(message: str, *, code: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"error": "validation", "message": message}
    if code:
        result["code"] = code
    return result


def _validation_error_from_exception(exc: ValueError) -> dict[str, Any]:
    message = str(exc)
    if message.startswith("missing_metadata_field:"):
        return _validation_error(message, code="missing_metadata_field")
    return _validation_error(message)


def _api_error(err: ClawChatApiError) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    if err.status is not None:
        meta["status"] = err.status
    if err.path is not None:
        meta["path"] = err.path
    if err.code is not None:
        meta["code"] = err.code

    if err.code in (CODE_PENDING_APPROVAL, CODE_POLICY_FORBIDDEN):
        return _permission_gate_result(err, meta)

    out: dict[str, Any] = {"error": err.kind, "message": err.message}
    if meta:
        out["meta"] = meta
    return out


def _permission_gate_result(err: ClawChatApiError, meta: dict[str, Any]) -> dict[str, Any]:
    """Map an owner-approval gate code into a clear, non-retryable tool result.

    The operation did NOT fail in a transient/transport sense; retrying is wrong.
    the ClawChat backend returns the real outcome later as a normal chat message, so the
    agent should stop and wait rather than re-issue the call.
    """
    request_id = _extract_request_id(err)
    if err.code == CODE_PENDING_APPROVAL:
        message = (
            "This operation requires the owner's approval and has been submitted for review"
            f"{f' (request_id={request_id})' if request_id else ''}. "
            "It has NOT failed. Do not retry — the result will arrive later as a normal "
            "chat message; wait for it instead of calling this tool again."
        )
        status = "pending"
    else:  # CODE_POLICY_FORBIDDEN
        message = (
            "This operation is blocked by the owner's policy (policy_forbidden) and was not "
            "performed. Do not retry — the owner must change the policy before it can succeed."
        )
        status = "forbidden"

    result: dict[str, Any] = {
        "error": "permission",
        "message": message,
        "retryable": False,
        "status": status,
    }
    if request_id:
        result["request_id"] = request_id
    if meta:
        result["meta"] = meta
    return result


def _extract_request_id(err: ClawChatApiError) -> str | None:
    """Best-effort pull of request_id from the error payload, if one is carried.

    ``ClawChatApiError`` now exposes a ``data`` field carrying the envelope data
    object; this helper also checks a ``payload`` attribute as a fallback for
    older error shapes.
    """
    for attr in ("data", "payload"):
        data = getattr(err, attr, None)
        if isinstance(data, dict):
            value = data.get("request_id")
            if isinstance(value, str) and value:
                return value
    return None


def _unknown_error(exc: BaseException) -> dict[str, Any]:
    return {"error": "unknown", "message": str(exc)}


def _build_client() -> tuple[ClawChatApiClient | None, dict[str, Any] | None]:
    try:
        config = load_profile_config()
    except ProfileConfigError as exc:
        return None, _config_error(str(exc))
    return (
        ClawChatApiClient(
            base_url=config.base_url,
            token=config.token,
            user_id=config.user_id,
        ),
        None,
    )


def _resolve_clawchat_config() -> dict[str, str]:
    cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={}))
    return {
        "agent_id": cfg.agent_id,
        "user_id": cfg.user_id,
        "owner_user_id": cfg.owner_user_id,
    }


def _resolve_memory_root() -> tuple[Path | None, dict[str, Any] | None]:
    root = ClawChatConfig.from_platform_config(SimpleNamespace(extra={})).memory_root
    if not root:
        return None, _config_error("ClawChat memory root is not configured")
    return Path(root), None


def _validate_upload_path(file_path: str) -> tuple[Path | None, dict[str, Any] | None]:
    if not isinstance(file_path, str) or not file_path:
        return None, _validation_error("filePath is required")

    path = Path(file_path)
    if not path.is_absolute():
        return None, _validation_error(f"filePath must be an absolute local path (got {file_path!r})")
    if not path.exists():
        return None, _validation_error(f"file does not exist: {path}")
    if not path.is_file():
        return None, _validation_error(f"not a regular file: {path}")

    size = path.stat().st_size
    if size <= 0:
        return None, _validation_error(f"file is empty: {path}")
    if size > MAX_UPLOAD_BYTES:
        return None, _validation_error(f"file too large ({size} bytes; max {MAX_UPLOAD_BYTES})")
    return path, None


def _infer_mime(path: Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _delete_group_memory(conversation_id: str) -> None:
    root, err = _resolve_memory_root()
    if err is not None or root is None:
        return
    delete_clawchat_memory_file(root, "group", conversation_id)


def _is_conversation_not_found(exc: ClawChatApiError) -> bool:
    if exc.status in (404, 410) or exc.code in (404, 410, 40401):
        return True
    return "conversation not found" in str(exc).lower()


def _target_error(target_type: str, target_id: str) -> dict[str, Any] | None:
    if target_type not in {"owner", "user", "group"}:
        return _validation_error("targetType must be owner, user, or group")
    if not isinstance(target_id, str) or not target_id:
        return _validation_error("targetId is required")
    if target_type == "owner" and target_id != "owner":
        return _validation_error("owner target requires targetId='owner'")
    return None


def _pagination(value: Any, default: int, field: str) -> tuple[int | None, dict[str, Any] | None]:
    if value is None:
        return default, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, _validation_error(f"{field} must be an integer")
    if parsed < 0:
        return None, _validation_error(f"{field} must be >= 0")
    return parsed, None


def _metadata_patch_error(target_type: str, patch: Any) -> dict[str, Any] | None:
    if not isinstance(patch, dict) or not patch:
        return _validation_error("patch is required and must be non-empty")
    allowed = {
        "owner": {"agent_behavior"},
        "user": {"nickname", "avatar_url", "bio"},
        "group": {"group_title", "group_description"},
    }.get(target_type, set())
    forbidden = [key for key in patch if key not in allowed]
    if forbidden:
        return _validation_error(f"unsupported metadata patch fields: {', '.join(sorted(forbidden))}")
    non_string = [key for key, value in patch.items() if not isinstance(value, str)]
    if non_string:
        return _validation_error(f"metadata patch values must be strings: {', '.join(sorted(non_string))}")
    return None


async def memory_read(
    target_type: str,
    target_id: str,
    *,
    offset: int | None = 0,
    limit: int | None = 12000,
) -> dict[str, Any]:
    err = _target_error(target_type, target_id)
    if err is not None:
        return err
    offset_value, err = _pagination(offset, 0, "offset")
    if err is not None:
        return err
    limit_value, err = _pagination(limit, 12000, "limit")
    if err is not None:
        return err
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    try:
        memory = read_clawchat_memory_file(root, target_type, target_id)
        content = memory["content"]
        end = offset_value + limit_value
        visible = content[offset_value:end]
        return {
            "targetType": target_type,
            "targetId": target_id,
            "exists": memory["exists"],
            "content": visible,
            "metadata": memory["metadata"],
            "offset": offset_value,
            "limit": limit_value,
            "total": len(content),
            "truncated": end < len(content),
        }
    except ValueError as exc:
        return _validation_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def memory_search(
    query: str,
    *,
    target_types: list[str] | None = None,
    max_results: int | None = 10,
) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        return _validation_error("query is required")
    if target_types is not None:
        if not isinstance(target_types, list) or not target_types:
            return _validation_error("targetTypes must be a non-empty array")
        if any(target_type not in {"owner", "user", "group"} for target_type in target_types):
            return _validation_error("targetTypes must contain only owner, user, or group")
    if max_results is None:
        max_results = 10
    if not isinstance(max_results, int) or max_results < 1 or max_results > 50:
        return _validation_error("maxResults must be between 1 and 50")
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    try:
        return search_clawchat_memory(
            root,
            query,
            target_types=target_types,
            max_results=max_results,
        )
    except ValueError as exc:
        return _validation_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def memory_write(
    target_type: str,
    target_id: str,
    *,
    mode: str,
    content: str,
) -> dict[str, Any]:
    err = _target_error(target_type, target_id)
    if err is not None:
        return err
    if mode not in {"append", "replace"}:
        return _validation_error("mode must be append or replace")
    if not isinstance(content, str):
        return _validation_error("content must be a string")
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    try:
        write_clawchat_memory_body(root, target_type, target_id, mode, content)
        return {"ok": True, "targetType": target_type, "targetId": target_id}
    except ValueError as exc:
        return _validation_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def memory_edit(
    target_type: str,
    target_id: str,
    *,
    old_text: str,
    new_text: str,
) -> dict[str, Any]:
    err = _target_error(target_type, target_id)
    if err is not None:
        return err
    if not isinstance(old_text, str) or not old_text:
        return _validation_error("oldText is required")
    if not isinstance(new_text, str):
        return _validation_error("newText must be a string")
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    try:
        edit_clawchat_memory_body(root, target_type, target_id, old_text, new_text)
        return {"ok": True, "targetType": target_type, "targetId": target_id}
    except ValueError as exc:
        return _validation_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def metadata_sync(
    target_type: str,
    target_id: str,
    *,
    direction: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    err = _target_error(target_type, target_id)
    if err is not None:
        return err
    if direction not in {"pull", "push"}:
        return _validation_error("direction must be pull or push")
    if direction == "push":
        if not isinstance(fields, list) or not fields:
            return _validation_error("fields are required for metadata push")
        if any(not isinstance(field, str) or not field for field in fields):
            return _validation_error("fields must contain non-empty strings")
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    client, err = _build_client()
    if err is not None:
        return err
    cfg = _resolve_clawchat_config()
    try:
        if direction == "push":
            return await push_metadata(
                root,
                client,
                target_type,
                target_id,
                fields=fields,
                agent_id=cfg["agent_id"],
                connected_user_id=cfg["user_id"],
            )
        if target_type == "owner":
            if not cfg["agent_id"]:
                return _config_error("agent_id is required for owner metadata")
            return await pull_owner_metadata(
                root,
                client,
                cfg["agent_id"],
                connected_user_id=cfg.get("user_id", ""),
                owner_user_id=cfg.get("owner_user_id", ""),
                persist_owner_profile=make_owner_profile_persister(get_clawchat_store()),
            )
        if target_type == "user":
            return await pull_user_metadata(root, client, target_id)
        return await pull_group_metadata(
            root,
            client,
            target_id,
            skip_user_ids={cfg.get("user_id", ""), cfg.get("owner_user_id", "")},
        )
    except ClawChatApiError as exc:
        return _api_error(exc)
    except ValueError as exc:
        return _validation_error_from_exception(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def metadata_update(
    target_type: str,
    target_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    err = _target_error(target_type, target_id)
    if err is not None:
        return err
    err = _metadata_patch_error(target_type, patch)
    if err is not None:
        return err
    root, err = _resolve_memory_root()
    if err is not None:
        return err
    client, err = _build_client()
    if err is not None:
        return err
    cfg = _resolve_clawchat_config()
    try:
        return await update_clawchat_metadata(
            root,
            client,
            target_type,
            target_id,
            patch,
            agent_id=cfg["agent_id"],
            connected_user_id=cfg["user_id"],
            persist_owner_profile=make_owner_profile_persister(get_clawchat_store()),
        )
    except ClawChatApiError as exc:
        return _api_error(exc)
    except ValueError as exc:
        return _validation_error_from_exception(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def get_account_profile() -> dict[str, Any]:
    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.get_my_profile()
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def get_user_profile(user_id: str) -> dict[str, Any]:
    if not isinstance(user_id, str) or not user_id.strip():
        return _validation_error("userId is required")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.get_user_info(user_id.strip())
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def list_account_friends(
    page: int | None = None,
    page_size: int | None = None,
) -> dict[str, Any]:
    page_value = 1 if page is None else page
    size_value = 20 if page_size is None else page_size
    if not isinstance(page_value, int) or page_value < 1:
        return _validation_error(f"page must be an integer >= 1 (got {page!r})")
    if not isinstance(size_value, int) or not (1 <= size_value <= 100):
        return _validation_error(f"pageSize must be an integer in 1..100 (got {page_size!r})")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.list_friends(page=page_value, page_size=size_value)
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def send_friend_request(user_id: str, greeting: str | None = None) -> dict[str, Any]:
    if not isinstance(user_id, str) or not user_id.strip():
        return _validation_error("userId is required")
    if greeting is not None and not isinstance(greeting, str):
        return _validation_error("greeting must be a string when provided")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.send_friend_request(user_id=user_id.strip(), greeting=greeting)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def list_friend_requests(direction: str | None = None) -> dict[str, Any]:
    direction_value = direction or "incoming"
    if direction_value not in {"incoming", "outgoing"}:
        return _validation_error("direction must be incoming or outgoing")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.list_friend_requests(direction=direction_value)
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


def _positive_int(value: Any, field: str) -> tuple[int | None, dict[str, Any] | None]:
    if not isinstance(value, int) or value < 1:
        return None, _validation_error(f"{field} must be an integer >= 1")
    return value, None


async def accept_friend_request(request_id: int) -> dict[str, Any]:
    request_id_value, err = _positive_int(request_id, "requestId")
    if err is not None or request_id_value is None:
        return err or _validation_error("requestId is required")

    client, client_err = _build_client()
    if client_err is not None:
        return client_err
    try:
        return await client.accept_friend_request(request_id_value)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def reject_friend_request(request_id: int) -> dict[str, Any]:
    request_id_value, err = _positive_int(request_id, "requestId")
    if err is not None or request_id_value is None:
        return err or _validation_error("requestId is required")

    client, client_err = _build_client()
    if client_err is not None:
        return client_err
    try:
        return await client.reject_friend_request(request_id_value)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def remove_friend(friend_user_id: str) -> dict[str, Any]:
    if not isinstance(friend_user_id, str) or not friend_user_id.strip():
        return _validation_error("friendUserId is required")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.remove_friend(friend_user_id.strip())
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def search_users(q: str | None = None, limit: int | None = None) -> dict[str, Any]:
    if limit is not None and (not isinstance(limit, int) or not (1 <= limit <= 100)):
        return _validation_error("limit must be an integer in 1..100")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.search_users(q=q or "", limit=limit)
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def list_moments(before: int | None = None, limit: int | None = None) -> dict[str, Any]:
    if before is not None and (not isinstance(before, int) or before < 1):
        return _validation_error("before must be an integer >= 1")
    if limit is not None and (not isinstance(limit, int) or not (1 <= limit <= 100)):
        return _validation_error("limit must be an integer in 1..100")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.list_moments(before=before, limit=limit)
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def get_conversation(conversation_id: str) -> dict[str, Any]:
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return _validation_error("conversationId is required")

    conversation_id_value = conversation_id.strip()
    client, err = _build_client()
    if err is not None:
        return err
    try:
        result = await client.get_conversation(conversation_id_value)
        return result
    except ClawChatApiError as exc:
        if _is_conversation_not_found(exc):
            _delete_group_memory(conversation_id_value)
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def leave_group(conversation_id: str) -> dict[str, Any]:
    if not isinstance(conversation_id, str) or not conversation_id.strip():
        return _validation_error("conversationId is required")

    conversation_id_value = conversation_id.strip()
    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.leave_conversation(conversation_id_value)
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def mention_message(
    chat_id: Any,
    *,
    chat_type: Any = "group",
    text: Any = None,
    mentions: Any,
    reply_to_message_id: Any = None,
) -> dict[str, Any]:
    if not isinstance(chat_id, str) or not chat_id.strip():
        return _validation_error("chatId is required")
    if chat_type not in {"direct", "group"}:
        return _validation_error("chatType must be direct or group")
    if text is not None and not isinstance(text, str):
        return _validation_error("text must be a string when provided")
    if reply_to_message_id is not None and not isinstance(reply_to_message_id, str):
        return _validation_error("replyToMessageId must be a string when provided")
    try:
        normalized_mentions = normalize_mention_targets(mentions)
    except ValueError as exc:
        return _validation_error(str(exc))
    try:
        return await send_clawchat_mention_message(
            chat_id=chat_id.strip(),
            chat_type=chat_type,
            text=text,
            mentions=normalized_mentions,
            reply_to_message_id=(
                reply_to_message_id.strip()
                if isinstance(reply_to_message_id, str) and reply_to_message_id.strip()
                else None
            ),
        )
    except RuntimeError as exc:
        return _config_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def create_moment(
    text: str | None = None,
    images: list[str] | None = None,
) -> dict[str, Any]:
    if text is not None and not isinstance(text, str):
        return _validation_error("text must be a string")
    if images is not None and (
        not isinstance(images, list) or any(not isinstance(item, str) for item in images)
    ):
        return _validation_error("images must be a list of image URLs")
    if not text and not images:
        return _validation_error("at least one of text or images is required")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.create_moment(text=text, images=images)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def delete_moment(moment_id: int) -> dict[str, Any]:
    moment_id_value, err = _positive_int(moment_id, "momentId")
    if err is not None:
        return err

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        return await client.delete_moment(moment_id_value)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def toggle_moment_reaction(moment_id: int, emoji: str) -> dict[str, Any]:
    moment_id_value, err = _positive_int(moment_id, "momentId")
    if err is not None:
        return err
    if not isinstance(emoji, str) or not emoji.strip():
        return _validation_error("emoji is required")

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        return await client.toggle_moment_reaction(moment_id=moment_id_value, emoji=emoji)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def create_moment_comment(moment_id: int, text: str) -> dict[str, Any]:
    moment_id_value, err = _positive_int(moment_id, "momentId")
    if err is not None:
        return err
    if not isinstance(text, str) or not text.strip():
        return _validation_error("text is required")

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        return await client.create_moment_comment(moment_id=moment_id_value, text=text)
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def reply_moment_comment(
    moment_id: int,
    reply_to_comment_id: int,
    text: str,
) -> dict[str, Any]:
    moment_id_value, err = _positive_int(moment_id, "momentId")
    if err is not None:
        return err
    reply_to_comment_id_value, rerr = _positive_int(reply_to_comment_id, "replyToCommentId")
    if rerr is not None:
        return rerr
    if not isinstance(text, str) or not text.strip():
        return _validation_error("text is required")

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        return await client.reply_moment_comment(
            moment_id=moment_id_value,
            reply_to_comment_id=reply_to_comment_id_value,
            text=text,
        )
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def delete_moment_comment(moment_id: int, comment_id: int) -> dict[str, Any]:
    moment_id_value, err = _positive_int(moment_id, "momentId")
    if err is not None:
        return err
    comment_id_value, cerr = _positive_int(comment_id, "commentId")
    if cerr is not None:
        return cerr

    client, berr = _build_client()
    if berr is not None:
        return berr
    try:
        return await client.delete_moment_comment(
            moment_id=moment_id_value,
            comment_id=comment_id_value,
        )
    except ClawChatApiError as exc:
        outcome = map_gate_outcome(exc.code, exc.data or {})
        if outcome is not None:
            return outcome
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def update_account_profile(
    nickname: str | None = None,
    avatar_url: str | None = None,
    bio: str | None = None,
) -> dict[str, Any]:
    patch: dict[str, str] = {}
    if isinstance(nickname, str):
        patch["nickname"] = nickname
    if isinstance(avatar_url, str):
        patch["avatar_url"] = avatar_url
    if isinstance(bio, str):
        patch["bio"] = bio
    if not patch:
        return _validation_error("at least one of nickname / avatar_url / bio is required")

    client, err = _build_client()
    if err is not None:
        return err
    try:
        result = await client.update_my_profile(**patch)
        owner_sync = await _sync_owner_metadata_after_account_profile_update(client)
        if owner_sync is None:
            return result
        return {**result, "owner_metadata_sync": owner_sync}
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def _sync_owner_metadata_after_account_profile_update(
    client: ClawChatApiClient,
) -> dict[str, Any] | None:
    root, err = _resolve_memory_root()
    if err is not None:
        return {**err, "target_type": "owner", "target_id": "owner"}
    cfg = _resolve_clawchat_config()
    if not cfg["agent_id"]:
        return _config_error("agent_id is required for owner metadata")
    try:
        return await pull_owner_metadata(
            root,
            client,
            cfg["agent_id"],
            connected_user_id=cfg.get("user_id", ""),
            owner_user_id=cfg.get("owner_user_id", ""),
            persist_owner_profile=make_owner_profile_persister(get_clawchat_store()),
        )
    except ClawChatApiError as exc:
        return _api_error(exc)
    except ValueError as exc:
        return _validation_error_from_exception(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def upload_avatar_image(file_path: str) -> dict[str, Any]:
    path, err = _validate_upload_path(file_path)
    if err is not None:
        return err

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        result = await client.upload_avatar(
            buffer=path.read_bytes(),
            filename=path.name,
            mime=_infer_mime(path),
        )
        return {"url": result.url, "size": result.size, "mime": result.mime}
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def register_app(name: str, app_id: str, url: str) -> dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        return _validation_error("name is required")
    if not isinstance(app_id, str) or not app_id.strip():
        return _validation_error("app_id is required")
    if not isinstance(url, str) or not url.strip():
        return _validation_error("url is required")
    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.register_app(name=name.strip(), app_id=app_id.strip(), url=url.strip())
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def list_apps() -> dict[str, Any]:
    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.list_apps()
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def unregister_app(app_id: str) -> dict[str, Any]:
    if not isinstance(app_id, str) or not app_id.strip():
        return _validation_error("app_id is required")
    client, err = _build_client()
    if err is not None:
        return err
    try:
        return await client.unregister_app(app_id.strip())
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


async def upload_media_file(file_path: str) -> dict[str, Any]:
    path, err = _validate_upload_path(file_path)
    if err is not None:
        return err

    client, cerr = _build_client()
    if cerr is not None:
        return cerr
    try:
        result = await client.upload_media(
            buffer=path.read_bytes(),
            filename=path.name,
            mime=_infer_mime(path),
        )
        return {
            "kind": result.kind,
            "url": result.url,
            "name": result.name,
            "mime": result.mime,
            "size": result.size,
        }
    except ClawChatApiError as exc:
        return _api_error(exc)
    except Exception as exc:  # noqa: BLE001
        return _unknown_error(exc)


_LIVEWARE_LOGIN_TIMEOUT = 30  # seconds


async def liveware_login() -> dict[str, Any]:
    """Log in to liveware using the agent's ClawChat account token.

    The plugin resolves the token from the profile config and passes it to
    the liveware CLI as --access-token. Call this before liveware app/tunnel
    commands that require an authenticated session.

    Security note: liveware's documented interface is --access-token, so the
    token is in child argv briefly; env/stdin preferred if liveware supported
    it. The token is NEVER logged, printed, or returned to callers — on error,
    stderr/stdout are scrubbed with token replacement before returning.
    """
    try:
        cfg = load_profile_config()
    except ProfileConfigError as exc:
        return _config_error(str(exc))

    token = cfg.token

    liveware_path = resolve_liveware_path()
    if liveware_path is None:
        return _validation_error("liveware CLI not found in PATH")

    proc = await asyncio.create_subprocess_exec(
        liveware_path,
        "login",
        "--access-token",
        token,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_LIVEWARE_LOGIN_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        return _validation_error("liveware login timed out")

    if proc.returncode == 0:
        return {"ok": True}

    # Non-zero exit: scrub the token from all output before returning.
    stderr_text = err.decode(errors="replace").replace(token, "***")
    stdout_text = out.decode(errors="replace").replace(token, "***")
    detail = (stderr_text or stdout_text).strip()
    return {
        "error": "subprocess",
        "message": f"liveware login failed (exit {proc.returncode}): {detail}",
    }
