from __future__ import annotations

import json
import logging
import time

from clawchat_gateway.storage import get_clawchat_store

logger = logging.getLogger(__name__)


def _tool_result(payload: dict) -> str:
    """Return a Hermes v0.12-compatible tool result string."""
    return json.dumps(payload, ensure_ascii=False)


def _account_id_from_kwargs(kw) -> str | None:
    account_id = kw.get("account_id")
    if isinstance(account_id, str) and account_id:
        return account_id
    return None


def _record_tool_call(
    *,
    tool_name: str,
    args: dict,
    account_id: str | None,
    result,
    error: str | None,
    started_at: int,
    ended_at: int,
) -> None:
    try:
        get_clawchat_store().record_tool_call(
            platform="hermes",
            account_id=account_id or "default",
            tool_name=tool_name,
            args=args,
            result=result,
            error=error,
            started_at=started_at,
            ended_at=ended_at,
        )
    except Exception:  # noqa: BLE001
        logger.warning("clawchat tool database persistence failed tool_name=%s", tool_name)


async def _recorded_tool_call(tool_name: str, args: dict, account_id: str | None, fn):
    started = int(time.time() * 1000)
    safe_args = dict(args or {})
    try:
        result = await fn()
    except Exception as exc:
        ended = int(time.time() * 1000)
        _record_tool_call(
            tool_name=tool_name,
            args=safe_args,
            account_id=account_id,
            result=None,
            error=str(exc),
            started_at=started,
            ended_at=ended,
        )
        raise
    ended = int(time.time() * 1000)
    _record_tool_call(
        tool_name=tool_name,
        args=safe_args,
        account_id=account_id,
        result=result,
        error=None,
        started_at=started,
        ended_at=ended,
    )
    return result


async def handle_clawchat_get_account_profile(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_get_account_profile start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_get_account_profile",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.get_account_profile(),
    )
    logger.info("clawchat_get_account_profile done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_get_user_profile(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_get_user_profile start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_get_user_profile",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.get_user_profile(str(args.get("userId") or "")),
    )
    logger.info("clawchat_get_user_profile done task_id=%s", task_id)
    return _tool_result(result)


def _optional_int_arg(value):
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


async def handle_clawchat_list_account_friends(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_list_account_friends start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_list_account_friends",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.list_account_friends(
            page=_optional_int_arg(args.get("page")),
            page_size=_optional_int_arg(args.get("pageSize")),
        ),
    )
    logger.info("clawchat_list_account_friends done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_search_users(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_search_users start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_search_users",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.search_users(
            q=args.get("q") if isinstance(args.get("q"), str) else "",
            limit=_optional_int_arg(args.get("limit")),
        ),
    )
    logger.info("clawchat_search_users done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_list_moments(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_list_moments start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_list_moments",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.list_moments(
            before=_optional_int_arg(args.get("before")),
            limit=_optional_int_arg(args.get("limit")),
        ),
    )
    logger.info("clawchat_list_moments done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_list_conversations(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_list_conversations start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_list_conversations",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.list_conversations(
            before=args.get("before"),
            limit=_optional_int_arg(args.get("limit")),
        ),
    )
    logger.info("clawchat_list_conversations done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_get_conversation(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_get_conversation start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_get_conversation",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.get_conversation(args.get("conversationId")),
    )
    logger.info("clawchat_get_conversation done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_mention_message(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_mention_message start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_mention_message",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.mention_message(
            str(args.get("chatId") or ""),
            chat_type=args.get("chatType") or "group",
            text=args.get("text") if isinstance(args.get("text"), str) else None,
            mentions=args.get("mentions"),
            reply_to_message_id=args.get("replyToMessageId"),
        ),
    )
    logger.info("clawchat_mention_message done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_create_moment(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_create_moment start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_create_moment",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.create_moment(
            text=args.get("text") if isinstance(args.get("text"), str) else None,
            images=args.get("images") if isinstance(args.get("images"), list) else None,
        ),
    )
    logger.info("clawchat_create_moment done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_delete_moment(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_delete_moment start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_delete_moment",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.delete_moment(_optional_int_arg(args.get("momentId"))),
    )
    logger.info("clawchat_delete_moment done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_toggle_moment_reaction(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_toggle_moment_reaction start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_toggle_moment_reaction",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.toggle_moment_reaction(
            _optional_int_arg(args.get("momentId")),
            str(args.get("emoji") or ""),
        ),
    )
    logger.info("clawchat_toggle_moment_reaction done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_create_moment_comment(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_create_moment_comment start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_create_moment_comment",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.create_moment_comment(
            _optional_int_arg(args.get("momentId")),
            str(args.get("text") or ""),
        ),
    )
    logger.info("clawchat_create_moment_comment done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_reply_moment_comment(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_reply_moment_comment start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_reply_moment_comment",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.reply_moment_comment(
            _optional_int_arg(args.get("momentId")),
            _optional_int_arg(args.get("replyToCommentId")),
            str(args.get("text") or ""),
        ),
    )
    logger.info("clawchat_reply_moment_comment done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_delete_moment_comment(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_delete_moment_comment start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_delete_moment_comment",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.delete_moment_comment(
            _optional_int_arg(args.get("momentId")),
            _optional_int_arg(args.get("commentId")),
        ),
    )
    logger.info("clawchat_delete_moment_comment done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_update_account_profile(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_update_account_profile start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_update_account_profile",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.update_account_profile(
            nickname=args.get("nickname") if isinstance(args.get("nickname"), str) else None,
            avatar_url=args.get("avatar_url") if isinstance(args.get("avatar_url"), str) else None,
            bio=args.get("bio") if isinstance(args.get("bio"), str) else None,
        ),
    )
    logger.info("clawchat_update_account_profile done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_memory_read(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_memory_read start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_memory_read",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.memory_read(
            str(args.get("targetType") or ""),
            str(args.get("targetId") or ""),
            offset=_optional_int_arg(args.get("offset")),
            limit=_optional_int_arg(args.get("limit")),
        ),
    )
    logger.info("clawchat_memory_read done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_memory_search(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_memory_search start task_id=%s", task_id)
    from clawchat_gateway import tools

    max_results = _optional_int_arg(args.get("maxResults"))
    result = await _recorded_tool_call(
        "clawchat_memory_search",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.memory_search(
            str(args.get("query") or ""),
            target_types=args.get("targetTypes") if isinstance(args.get("targetTypes"), list) else None,
            max_results=10 if max_results is None else max_results,
        ),
    )
    logger.info("clawchat_memory_search done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_memory_write(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_memory_write start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_memory_write",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.memory_write(
            str(args.get("targetType") or ""),
            str(args.get("targetId") or ""),
            mode=str(args.get("mode") or ""),
            content=args.get("content"),
        ),
    )
    logger.info("clawchat_memory_write done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_memory_edit(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_memory_edit start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_memory_edit",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.memory_edit(
            str(args.get("targetType") or ""),
            str(args.get("targetId") or ""),
            old_text=args.get("oldText"),
            new_text=args.get("newText"),
        ),
    )
    logger.info("clawchat_memory_edit done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_metadata_sync(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_metadata_sync start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_metadata_sync",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.metadata_sync(
            str(args.get("targetType") or ""),
            str(args.get("targetId") or ""),
            direction=str(args.get("direction") or ""),
            fields=args.get("fields") if isinstance(args.get("fields"), list) else None,
        ),
    )
    logger.info("clawchat_metadata_sync done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_metadata_update(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_metadata_update start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_metadata_update",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.metadata_update(
            str(args.get("targetType") or ""),
            str(args.get("targetId") or ""),
            patch=args.get("patch") if isinstance(args.get("patch"), dict) else {},
        ),
    )
    logger.info("clawchat_metadata_update done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_upload_avatar_image(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_upload_avatar_image start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_upload_avatar_image",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.upload_avatar_image(str(args.get("filePath") or "")),
    )
    logger.info("clawchat_upload_avatar_image done task_id=%s", task_id)
    return _tool_result(result)


async def handle_clawchat_upload_media_file(args, **kw):
    task_id = kw.get("task_id") or "default"
    logger.info("clawchat_upload_media_file start task_id=%s", task_id)
    from clawchat_gateway import tools

    result = await _recorded_tool_call(
        "clawchat_upload_media_file",
        args,
        _account_id_from_kwargs(kw),
        lambda: tools.upload_media_file(str(args.get("filePath") or "")),
    )
    logger.info("clawchat_upload_media_file done task_id=%s", task_id)
    return _tool_result(result)


_DIRECT_TOOL_USE_INSTRUCTION = (
    "Use this registered ClawChat plugin tool directly. Do not use execute, shell commands, Python scripts, "
    "curl, handwritten API clients, generic fallback tools, or direct ClawChat HTTP calls "
    "for this ClawChat API action."
)


def _direct_tool_description(description: str) -> str:
    return description + " " + _DIRECT_TOOL_USE_INSTRUCTION


def register_tools(ctx) -> None:
    target_properties = {
        "targetType": {
            "type": "string",
            "enum": ["owner", "user", "group"],
            "description": "Target namespace: owner, user, or group.",
        },
        "targetId": {
            "type": "string",
            "description": "Required explicit target id. Use owner for owner targets; do not pass file paths.",
        },
    }
    metadata_patch_schema = {
        "type": "object",
        "minProperties": 1,
        "additionalProperties": False,
        "properties": {
            "nickname": {"type": "string"},
            "avatar_url": {"type": "string"},
            "bio": {"type": "string"},
            "agent_behavior": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
        },
    }

    ctx.register_tool(
        "clawchat_memory_search",
        "clawchat",
        {
            "name": "clawchat_memory_search",
            "description": _direct_tool_description(
                "Search local ClawChat memory Markdown files by keyword across owner.md, users/*.md, and groups/*.md. "
                "Use this when the user asks who/what a remembered person, alias, relationship, prior note, group rule, group context, or local ClawChat memory item is and no explicit targetId is known. "
                "This searches local memory metadata and agent-authored Markdown body. It does not contact the ClawChat server. "
                "Use this before answering unknown when the user provides a name, alias, phrase, or uncertain reference that may exist in local memory. "
                "If exactly one relevant result is found, use clawchat_memory_read with the returned targetType and targetId when full context is needed. If multiple relevant results are found, summarize the candidates or ask the user to clarify."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Keyword to search in local ClawChat memory metadata and Markdown body.",
                    },
                    "targetTypes": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["owner", "user", "group"]},
                        "minItems": 1,
                        "description": "Optional target namespaces to search. Defaults to owner, user, and group.",
                    },
                    "maxResults": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum local memory matches to return. Default 10, max 50.",
                    },
                },
                "required": ["query"],
            },
        },
        handle_clawchat_memory_search,
        is_async=True,
        description="Search ClawChat Memory",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_memory_read",
        "clawchat",
        {
            "name": "clawchat_memory_read",
            "description": _direct_tool_description(
                "Read one local ClawChat memory Markdown file by explicit targetType and targetId. Use this only when the memory target is already known, such as owner, a concrete userId, a concrete groupId, the current sender_id, or a target returned by clawchat_memory_search. Do not guess targetId from names, nicknames, aliases, or plain text. If the user gives a name, alias, phrase, relationship, or uncertain reference, use clawchat_memory_search first. This reads metadata and agent-authored body; it does not contact the ClawChat server."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **target_properties,
                    "offset": {"type": "integer", "minimum": 0, "description": "Content offset for bounded reads."},
                    "limit": {"type": "integer", "minimum": 0, "description": "Maximum content characters to return."},
                },
                "required": ["targetType", "targetId"],
            },
        },
        handle_clawchat_memory_read,
        is_async=True,
        description="Read ClawChat Memory",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_memory_write",
        "clawchat",
        {
            "name": "clawchat_memory_write",
            "description": _direct_tool_description(
                "Append to or replace only the agent-authored body of a ClawChat memory Markdown file by explicit targetType and targetId. This never modifies the metadata block. Do not use this to write or refresh ClawChat profile/metadata fields such as agent_nickname, agent_avatar_url, agent_bio, agent_behavior, owner_nickname, owner_avatar_url, owner_bio, nickname, avatar_url, bio, profile_type, title, or description. When the user asks to refresh, sync, or update local ClawChat current-agent/owner/user/group profile information, use clawchat_metadata_sync with direction=pull instead. Do not use this to search memory. Use clawchat_memory_search to locate uncertain names, aliases, relationships, or prior notes before writing. Use append for new long-term memory notes and replace only when intentionally rewriting the whole body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **target_properties,
                    "mode": {"type": "string", "enum": ["append", "replace"]},
                    "content": {"type": "string"},
                },
                "required": ["targetType", "targetId", "mode", "content"],
            },
        },
        handle_clawchat_memory_write,
        is_async=True,
        description="Write ClawChat Memory",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_memory_edit",
        "clawchat",
        {
            "name": "clawchat_memory_edit",
            "description": _direct_tool_description(
                "Replace exactly one existing text span in the agent-authored body of a ClawChat memory Markdown file. This never modifies the metadata block. Do not use this to edit ClawChat profile/metadata fields such as agent_nickname, agent_avatar_url, agent_bio, agent_behavior, owner_nickname, owner_avatar_url, owner_bio, nickname, avatar_url, bio, profile_type, title, or description. Use clawchat_metadata_sync or clawchat_metadata_update for metadata. Do not use this to search memory. Use clawchat_memory_search to locate uncertain names, aliases, relationships, or prior notes before editing. The oldText must match exactly once; use read first when unsure."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **target_properties,
                    "oldText": {"type": "string", "minLength": 1},
                    "newText": {"type": "string"},
                },
                "required": ["targetType", "targetId", "oldText", "newText"],
            },
        },
        handle_clawchat_memory_edit,
        is_async=True,
        description="Edit ClawChat Memory",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_metadata_sync",
        "clawchat",
        {
            "name": "clawchat_metadata_sync",
            "description": _direct_tool_description(
                "Synchronize the ClawChat metadata block for an explicit owner, user, or group target. Use direction=pull when the user asks to refresh, sync, or update local ClawChat current agent profile/behavior, owner profile information, user information, or group information from the server; this fetches the authoritative server record and rewrites only the metadata block. Use direction=push only to push selected existing local metadata fields to the server, then refresh the metadata block from the server response. For direction=push, pass non-empty fields containing only pushable metadata field names: owner supports agent_behavior only; user supports nickname/avatar_url/bio for the connected account only; group supports title/description. This does not modify the agent-authored body. This synchronizes only the metadata block. It does not search or read agent-authored long-term memory body. For remembered aliases or local notes, use clawchat_memory_search or clawchat_memory_read. Do not combine clawchat_get_user_profile with clawchat_memory_write to update local profile metadata."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **target_properties,
                    "direction": {"type": "string", "enum": ["pull", "push"]},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": "Required for direction=push. Push only these local metadata fields.",
                    },
                },
                "required": ["targetType", "targetId", "direction"],
            },
        },
        handle_clawchat_metadata_sync,
        is_async=True,
        description="Sync ClawChat Metadata",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_metadata_update",
        "clawchat",
        {
            "name": "clawchat_metadata_update",
            "description": _direct_tool_description(
                "Update ClawChat server metadata for an explicit owner, connected user account, or group target, then refresh the local metadata block from the server response. Use this only when the user wants to change server-side metadata fields: current agent behavior via owner target agent_behavior, connected-user nickname/avatar_url/bio, or group title/description. To refresh local metadata from the server without changing the server, use clawchat_metadata_sync with direction=pull. This always pushes to the server first and does not modify the agent-authored body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    **target_properties,
                    "patch": metadata_patch_schema,
                },
                "required": ["targetType", "targetId", "patch"],
            },
        },
        handle_clawchat_metadata_update,
        is_async=True,
        description="Update ClawChat Metadata",
        emoji="M",
    )

    ctx.register_tool(
        "clawchat_get_account_profile",
        "clawchat",
        {
            "name": "clawchat_get_account_profile",
            "description": _direct_tool_description(
                "Fetch the agent's connected ClawChat account profile (the configured ClawChat account: user id, nickname/display name, avatar, bio). "
                "This profile is the platform-side mirror of the local assistant identity; if fields are missing, report them as unset instead of inventing values. "
                "TRIGGER — invoke when the user asks for the ClawChat account/profile connected to this agent, "
                "such as 'show my ClawChat profile', 'what is the configured ClawChat account?', "
                "'当前 ClawChat 账号资料', or 'ClawChat 昵称头像简介'. "
                "Do not frame this as a human user's personal account."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handle_clawchat_get_account_profile,
        is_async=True,
        description="Get ClawChat Account Profile",
        emoji="👤",
    )

    ctx.register_tool(
        "clawchat_get_user_profile",
        "clawchat",
        {
            "name": "clawchat_get_user_profile",
            "description": _direct_tool_description(
                "Fetch a ClawChat user's server-side public profile by explicit userId. "
                "TRIGGER — invoke when the user asks to view or inspect a specific ClawChat user's public profile and provides a concrete userId, or after clawchat_search_users returns a userId. "
                "Do not guess or infer userId from nickname, display name, alias, or local memory text. "
                "This is a read-only lookup and server lookup. It does not read local ClawChat memory files and does not update local metadata. "
                "When the user asks to refresh, sync, or update that user's local profile information, call clawchat_metadata_sync with targetType=user, that targetId, and direction=pull. "
                "Use `clawchat_get_account_profile` for the agent's own connected ClawChat account unless an explicit userId is provided."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "userId": {
                        "type": "string",
                        "description": "Explicit target ClawChat user id (required). Do not infer this from a nickname; use clawchat_get_account_profile for the agent's own connected ClawChat account unless an explicit userId is provided.",
                    },
                },
                "required": ["userId"],
            },
        },
        handle_clawchat_get_user_profile,
        is_async=True,
        description="Get ClawChat User Profile",
        emoji="🧑",
    )

    ctx.register_tool(
        "clawchat_list_account_friends",
        "clawchat",
        {
            "name": "clawchat_list_account_friends",
            "description": _direct_tool_description(
                "List friends/contacts of the agent's connected ClawChat account (the configured ClawChat account). "
                "These are the agent's ClawChat-platform contacts. "
                "TRIGGER — invoke when the user asks for this ClawChat account's friends, contacts, or friend list."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
        handle_clawchat_list_account_friends,
        is_async=True,
        description="List ClawChat Account Friends",
        emoji="👥",
    )

    ctx.register_tool(
        "clawchat_search_users",
        "clawchat",
        {
            "name": "clawchat_search_users",
            "description": _direct_tool_description(
                "Search ClawChat users by username or nickname. Search server-side ClawChat users in the ClawChat user directory. "
                "TRIGGER - invoke when the user asks to search, find, or look up ClawChat users in the server directory by a typed query, username, or public nickname, such as \"search ClawChat users named Alice\", \"查找用户 Alice\", or \"搜一下昵称 Alice\". "
                "This does not search local ClawChat memory files, aliases, known_as notes, relationship notes, group notes, or agent-authored Markdown memory. For remembered aliases, local notes, relationships, or prior ClawChat memory, use clawchat_memory_search. "
                "Empty q returns no users. Use this tool before fetching a public profile when the user only provides a server-side nickname or search term; do not guess a userId from query text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "q": {"type": "string", "description": "Search query for ClawChat username or nickname"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max results (default 20)"},
                },
            },
        },
        handle_clawchat_search_users,
        is_async=True,
        description="Search ClawChat Users",
        emoji="🔎",
    )

    ctx.register_tool(
        "clawchat_list_moments",
        "clawchat",
        {
            "name": "clawchat_list_moments",
            "description": _direct_tool_description(
                "List the configured ClawChat account's visible moments feed, including moments from the account and its friends. "
                "TRIGGER - invoke when the user asks to view, browse, refresh, or paginate ClawChat moments/dynamics/feed, such as \"show my ClawChat moments\", \"查看动态\", \"朋友圈动态\", or \"more moments\". "
                "Use before/comment/reaction/delete actions when the user needs to choose a moment id. This is a friends-only feed endpoint, not a global public timeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "before": {"type": "integer", "minimum": 1, "description": "Cursor; return moments with id < before"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max items (default 30)"},
                },
            },
        },
        handle_clawchat_list_moments,
        is_async=True,
        description="List ClawChat Moments",
        emoji="📰",
    )

    ctx.register_tool(
        "clawchat_list_conversations",
        "clawchat",
        {
            "name": "clawchat_list_conversations",
            "description": _direct_tool_description(
                "List conversations visible to the agent's connected ClawChat account. "
                "TRIGGER - invoke when the user asks to view, browse, or choose from ClawChat conversations. "
                "This is read-only; do not use it to create, leave, dissolve, update, or change conversation membership."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "before": {"type": "string", "description": "Cursor timestamp; return conversations before this value"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Max conversations (default 20)"},
                },
            },
        },
        handle_clawchat_list_conversations,
        is_async=True,
        description="List ClawChat Conversations",
        emoji="💬",
    )

    ctx.register_tool(
        "clawchat_get_conversation",
        "clawchat",
        {
            "name": "clawchat_get_conversation",
            "description": _direct_tool_description(
                "Fetch a ClawChat conversation by conversationId. "
                "TRIGGER - invoke when the user asks to inspect a specific ClawChat conversation and provides a concrete conversationId. "
                "This is read-only; do not use it to create, leave, dissolve, update, or change conversation membership."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "conversationId": {"type": "string", "description": "Explicit ClawChat conversation id to fetch"},
                },
                "required": ["conversationId"],
            },
        },
        handle_clawchat_get_conversation,
        is_async=True,
        description="Get ClawChat Conversation",
        emoji="🧾",
    )

    ctx.register_tool(
        "clawchat_mention_message",
        "clawchat",
        {
            "name": "clawchat_mention_message",
            "description": _direct_tool_description(
                "Send a real ClawChat mention message to a direct or group conversation. "
                "TRIGGER - invoke when the user asks to @, mention, notify, or address ClawChat users. "
                "Prefer current group context sender_id for the mentioned participant. "
                "Use clawchat_search_users only when no explicit or locally available id exists. "
                "Never guess userId from names, nicknames, or plain @name text. Plain @name is not a real mention. "
                'After this tool succeeds, the mention message has already been sent; return exactly "" and do not send a normal follow-up reply.'
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "chatId": {"type": "string", "description": "Explicit ClawChat conversation id to send to."},
                    "chatType": {
                        "type": "string",
                        "enum": ["direct", "group"],
                        "description": "Conversation type. Defaults to group when omitted.",
                    },
                    "text": {"type": "string", "description": "Optional text after the mention fragments."},
                    "mentions": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "userId": {
                                    "type": "string",
                                    "description": "Explicit ClawChat user id to mention. Do not infer from nickname.",
                                },
                                "display": {
                                    "type": "string",
                                    "description": "Optional visible mention label. @ is added if omitted.",
                                },
                            },
                            "required": ["userId"],
                        },
                        "description": "Mention targets. At least one explicit userId is required.",
                    },
                    "replyToMessageId": {
                        "type": "string",
                        "description": "Optional ClawChat message id to attach as reply context.",
                    },
                },
                "required": ["chatId", "mentions"],
            },
        },
        handle_clawchat_mention_message,
        is_async=True,
        description="Send ClawChat Mention Message",
        emoji="@",
    )

    ctx.register_tool(
        "clawchat_create_moment",
        "clawchat",
        {
            "name": "clawchat_create_moment",
            "description": _direct_tool_description(
                "Create a new ClawChat moment/dynamic for the configured ClawChat account. "
                "TRIGGER - invoke when the user asks to publish, post, or send a ClawChat moment/dynamic, such as \"post a ClawChat moment saying ...\", \"发布动态 ...\", or \"发朋友圈 ...\". "
                "At least one of text or images must be present. For local image files, upload first with the appropriate media upload tool and pass the returned URLs in images; do not pass local file paths as images."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Moment text. At least one of text or images is required."},
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Image URLs for the moment. Upload local files first; do not pass local paths.",
                    },
                },
            },
        },
        handle_clawchat_create_moment,
        is_async=True,
        description="Create ClawChat Moment",
        emoji="📝",
    )

    ctx.register_tool(
        "clawchat_delete_moment",
        "clawchat",
        {
            "name": "clawchat_delete_moment",
            "description": _direct_tool_description(
                "Delete a ClawChat moment by moment id. "
                "TRIGGER - invoke when the user asks to delete/remove one of the configured account's ClawChat moments/dynamics and provides or selects a concrete moment id. "
                "Only the moment author can delete it. Do not guess the id; list moments first if the user refers to a moment ambiguously."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "momentId": {"type": "integer", "minimum": 1, "description": "Concrete ClawChat moment id to delete"},
                },
                "required": ["momentId"],
            },
        },
        handle_clawchat_delete_moment,
        is_async=True,
        description="Delete ClawChat Moment",
        emoji="🗑️",
    )

    ctx.register_tool(
        "clawchat_toggle_moment_reaction",
        "clawchat",
        {
            "name": "clawchat_toggle_moment_reaction",
            "description": _direct_tool_description(
                "Toggle an emoji reaction on a ClawChat moment. "
                "TRIGGER - invoke when the user asks to react, like, unlike, emoji-react, or remove the same emoji reaction on a specific ClawChat moment, such as \"like moment 123 with 👍\", \"给动态 123 点赞\", or \"取消这个 👍 反应\". "
                "The API adds the reaction if missing and removes it if already present. Require a concrete moment id and emoji."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "momentId": {"type": "integer", "minimum": 1, "description": "Concrete ClawChat moment id to react to"},
                    "emoji": {"type": "string", "description": "Emoji reaction to toggle"},
                },
                "required": ["momentId", "emoji"],
            },
        },
        handle_clawchat_toggle_moment_reaction,
        is_async=True,
        description="Toggle ClawChat Moment Reaction",
        emoji="👍",
    )

    ctx.register_tool(
        "clawchat_create_moment_comment",
        "clawchat",
        {
            "name": "clawchat_create_moment_comment",
            "description": _direct_tool_description(
                "Create a top-level comment on a ClawChat moment. "
                "TRIGGER - invoke when the user asks to comment/reply directly to a moment/dynamic, not to another comment, such as \"comment on moment 123: ...\", \"评论动态 123 ...\", or \"在这条动态下留言 ...\". "
                "Require a concrete moment id and non-empty text. Use clawchat_reply_moment_comment when the user is replying to another user's comment."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "momentId": {"type": "integer", "minimum": 1, "description": "Concrete ClawChat moment id to comment on"},
                    "text": {"type": "string", "description": "Top-level comment text"},
                },
                "required": ["momentId", "text"],
            },
        },
        handle_clawchat_create_moment_comment,
        is_async=True,
        description="Create ClawChat Moment Comment",
        emoji="💬",
    )

    ctx.register_tool(
        "clawchat_reply_moment_comment",
        "clawchat",
        {
            "name": "clawchat_reply_moment_comment",
            "description": _direct_tool_description(
                "Reply to an existing ClawChat moment comment with a single-level reply. "
                "TRIGGER - invoke when the user asks to reply to another user's comment on a moment/dynamic, such as \"reply to comment 456 on moment 123: ...\", \"回复评论 456 ...\", or \"回复他那条评论 ...\". "
                "Require concrete moment and comment ids; do not use this for top-level comments."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "momentId": {"type": "integer", "minimum": 1, "description": "Concrete ClawChat moment id containing the comment"},
                    "replyToCommentId": {"type": "integer", "minimum": 1, "description": "Concrete comment id being replied to"},
                    "text": {"type": "string", "description": "Reply text"},
                },
                "required": ["momentId", "replyToCommentId", "text"],
            },
        },
        handle_clawchat_reply_moment_comment,
        is_async=True,
        description="Reply To ClawChat Moment Comment",
        emoji="↩️",
    )

    ctx.register_tool(
        "clawchat_delete_moment_comment",
        "clawchat",
        {
            "name": "clawchat_delete_moment_comment",
            "description": _direct_tool_description(
                "Delete a comment on a ClawChat moment. "
                "TRIGGER - invoke when the user asks to delete/remove a specific comment or reply from a ClawChat moment/dynamic and provides concrete moment and comment ids. "
                "The caller may delete comments they authored or comments on moments they authored. Do not guess ids; list moments first if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "momentId": {"type": "integer", "minimum": 1, "description": "Concrete ClawChat moment id containing the comment"},
                    "commentId": {"type": "integer", "minimum": 1, "description": "Concrete comment id to delete"},
                },
                "required": ["momentId", "commentId"],
            },
        },
        handle_clawchat_delete_moment_comment,
        is_async=True,
        description="Delete ClawChat Moment Comment",
        emoji="🧹",
    )

    ctx.register_tool(
        "clawchat_update_account_profile",
        "clawchat",
        {
            "name": "clawchat_update_account_profile",
            "description": _direct_tool_description(
                "Update nickname/avatar_url/bio on the agent's connected ClawChat account (the configured ClawChat account), which mirrors the local assistant identity. "
                "TRIGGER — invoke this tool whenever the user's message asks to change the ClawChat account profile or local assistant name/profile while ClawChat is connected: "
                "(1) ClawChat account nickname/name change: 'change the ClawChat account nickname to X', "
                "'set this assistant name to X', 'ClawChat 昵称改为 X', '账号昵称改成 X', '账号名字叫 X' "
                "→ call with `nickname = X`; "
                "(2) ClawChat account avatar/profile-picture change: 'change the ClawChat account avatar', "
                "'use this image as the assistant profile picture', 'ClawChat 头像改为 …', '账号头像换成 …' "
                "→ first obtain the avatar URL (upload via `clawchat_upload_avatar_image`, OR use a provided URL directly), "
                "then call this tool with `avatar_url = <url>`; "
                "(3) ClawChat account bio/self-introduction change: 'update the ClawChat bio', "
                "'set the assistant self-introduction to X', 'ClawChat 简介改成 X', '账号简介改为 X', '个人简介改为 X' "
                "→ call with `bio = X`. "
                "You can pass `nickname`, `avatar_url`, and `bio` together in one call, or just one of them. "
                "At least one of the three must be present. Do not frame this as updating a human user's personal account."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nickname": {"type": "string", "description": "New nickname/display name for the agent's connected ClawChat account, mirroring the local assistant identity"},
                    "avatar_url": {"type": "string", "description": "Avatar URL for the agent's connected ClawChat account profile (use clawchat_upload_avatar_image first to obtain one from a local image)"},
                    "bio": {"type": "string", "description": "New self-introduction / bio text for the agent's connected ClawChat account, mirroring the local assistant identity"},
                },
            },
        },
        handle_clawchat_update_account_profile,
        is_async=True,
        description="Update ClawChat Account Profile",
        emoji="✏️",
    )

    ctx.register_tool(
        "clawchat_upload_avatar_image",
        "clawchat",
        {
            "name": "clawchat_upload_avatar_image",
            "description": _direct_tool_description(
                "Upload an absolute local image path for use as the agent's connected ClawChat account avatar (max 20MB), returning a hosted avatar URL. "
                "TRIGGER — invoke when the user provides an absolute local image path and asks to upload it for the ClawChat account avatar/profile picture. "
                "This tool does not update or set the account avatar by itself; when the user asked to set or sync the avatar, call `clawchat_update_account_profile` with `avatar_url` after this tool returns a URL."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute local path of the avatar image to upload for the agent's connected ClawChat account (max 20MB)"},
                },
                "required": ["filePath"],
            },
        },
        handle_clawchat_upload_avatar_image,
        is_async=True,
        description="Upload ClawChat Avatar Image",
        emoji="🖼️",
    )

    ctx.register_tool(
        "clawchat_upload_media_file",
        "clawchat",
        {
            "name": "clawchat_upload_media_file",
            "description": _direct_tool_description(
                "Upload an absolute local file/media path to ClawChat media storage (max 20MB) and return a ClawChat-accessible public/shareable URL. "
                "TRIGGER — invoke when the user provides an absolute local file path and asks to upload, share, or create a ClawChat-accessible link for that file. "
                "Do not use this tool to send an attachment in the current chat; use the current runtime's native media-send mechanism instead (for example, MEDIA:/absolute/local/path where supported). "
                "Do not use this for account avatar changes; use `clawchat_upload_avatar_image` for avatar images. Do not use this just to mirror local assistant identity."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string", "description": "Absolute local path of the non-avatar media/file to upload to ClawChat for a ClawChat-accessible URL (max 20MB)"},
                },
                "required": ["filePath"],
            },
        },
        handle_clawchat_upload_media_file,
        is_async=True,
        description="Upload ClawChat Media File",
        emoji="📎",
    )
