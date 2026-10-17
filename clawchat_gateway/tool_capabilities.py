"""Shared capability vocabulary; only trusted host registrations prove availability."""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import NamedTuple

AUTHORIZATION_TOOLS = (
    "clawchat_tool_grant", "clawchat_tool_revoke", "clawchat_tool_grants_list", "clawchat_tool_request",
)


def _capability(tools, zh, zh_hant, en, sensitive):
    return MappingProxyType(dict(tools=tuple(tools.split()),
                                names=MappingProxyType(dict(zh=zh, zh_Hant=zh_hant, en=en)),
                                sensitive=sensitive))


TOOL_CAPABILITIES = MappingProxyType({
    "terminal": _capability(
        "terminal process_manage process",
        "终端", "終端", "Terminal", True),
    "files": _capability(
        "read_file write_file patch search_files",
        "文件读写", "檔案讀寫", "Files", True),
    "code": _capability(
        "execute_code",
        "代码执行", "程式碼執行", "Code execution", True),
    "subagent": _capability(
        "delegate_task kanban_show kanban_list kanban_complete kanban_block kanban_schedule "
        "kanban_request_review kanban_request_changes kanban_heartbeat kanban_comment "
        "kanban_attach kanban_attach_url kanban_attachments kanban_create kanban_unblock "
        "kanban_link",
        "子 Agent", "子 Agent", "Sub-agents", True),
    "schedule": _capability(
        "cronjob_manage cronjob",
        "定时任务", "定時任務", "Scheduled tasks", True),
    "browser": _capability(
        "browser_navigate browser_snapshot browser_click browser_type browser_scroll browser_back "
        "browser_press browser_get_images browser_vision browser_console browser_cdp "
        "browser_dialog browser_vault browser_exec computer_use",
        "浏览器与桌面控制", "瀏覽器與桌面控制", "Browser and desktop control", True),
    "cross-chat": _capability(
        "clawchat_mention_message clawchat_send_file clawchat_react_message send_message",
        "跨会话发送", "跨會話傳送", "Cross-chat messaging", False),
    "memory": _capability(
        "memory",
        "宿主记忆", "宿主記憶", "Host memory", False),
    "session-search": _capability(
        "session_search",
        "会话检索", "會話檢索", "Session search", False),
    "feishu-docs": _capability(
        "feishu_doc_read feishu_drive_list_comments feishu_drive_list_comment_replies "
        "feishu_drive_reply_comment feishu_drive_add_comment",
        "飞书文档", "飛書文件", "Feishu documents", False),
    "connections": _capability(
        "manage_connections",
        "连接管理", "連線管理", "Connections", False),
    "web": _capability(
        "web_search web_extract x_search",
        "网页搜索与读取", "網頁搜尋與讀取", "Web search and reading", False),
})
CAPABILITY_TOOL_NAMES = frozenset(tool for cap in TOOL_CAPABILITIES.values() for tool in cap["tools"])


class GrantSelector(NamedTuple):
    what_kind: str
    what: str


def parse_grant_selector(value: str) -> GrantSelector:
    if not isinstance(value, str) or not value or re.search(r"[\s*?\x00-\x1f\x7f]", value):
        raise ValueError("invalid-what")
    if value == "all":
        return GrantSelector("all", "all")
    if value.startswith("cap:") or value in TOOL_CAPABILITIES:
        what = value[4:] if value.startswith("cap:") else value
        if what not in TOOL_CAPABILITIES:
            raise ValueError("invalid-what")
        return GrantSelector("capability", what)
    what = value[5:] if value.startswith("tool:") else value
    if (not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:/-]*", what)
            or what.startswith("group:") or what in AUTHORIZATION_TOOLS):
        raise ValueError("invalid-what")
    return GrantSelector("tool", what)


def selector_string(selector: GrantSelector) -> str:
    return "all" if selector.what_kind == "all" else (
        "tool:" if selector.what_kind == "tool" else "cap:") + selector.what


def normalize_grant_selectors(values, *, known_tools=None, available_tools=None, stored=()) -> list[str]:
    """Validate the entire selection before any write; revoke may name removed rows."""
    if not isinstance(values, (list, tuple)) or not values:
        raise ValueError("invalid-what")
    result = []
    stored = set(stored)
    for value in values:
        selector = parse_grant_selector(value)
        normalized = selector_string(selector)
        if normalized not in stored:
            if selector.what_kind == "tool":
                if known_tools is not None and selector.what not in known_tools:
                    raise ValueError("invalid-what")
                if available_tools is not None and selector.what not in available_tools:
                    raise ValueError("not-available")
            elif selector.what_kind == "capability" and available_tools is not None:
                if not set(TOOL_CAPABILITIES[selector.what]["tools"]) & set(available_tools):
                    raise ValueError("not-available")
        if normalized not in result:
            result.append(normalized)
    return result


def selector_display(value: str, lang: str) -> str:
    selector = parse_grant_selector(value)
    if selector.what_kind != "capability":
        return selector.what
    return TOOL_CAPABILITIES[selector.what]["names"][lang if lang in ("zh", "zh_Hant") else "en"]


def is_sensitive_tool(name: str) -> bool:
    return name.startswith("browser_") or any(
        cap["sensitive"] and name in cap["tools"] for cap in TOOL_CAPABILITIES.values())


def selector_is_sensitive(value: str) -> bool:
    selector = parse_grant_selector(value)
    return selector.what_kind == "all" or (
        is_sensitive_tool(selector.what) if selector.what_kind == "tool" else
        any(is_sensitive_tool(tool) for tool in TOOL_CAPABILITIES[selector.what]["tools"]))


def is_cross_chat_call(tool_name: str, args, trusted_context) -> bool:
    if tool_name not in TOOL_CAPABILITIES["cross-chat"]["tools"]:
        return False
    chat_id = trusted_context.get("chat_id")
    key = "chat_id" if tool_name == "clawchat_send_file" else "target" if tool_name == "send_message" else "chatId"
    target = args.get(key) if isinstance(args, dict) else None
    if isinstance(target, str):
        target = target.strip()
        # Mirror tools._tool_chat_id and the host's send target parser: strip
        # one platform prefix, case-insensitively, without interpreting data.
        if target[:9].lower() == "clawchat:":
            target = target[9:].strip()
    return not (chat_id and isinstance(target, str) and target.strip().lower() == chat_id.strip().lower())


# Audited host routes, not a scan of arbitrary tool arguments. These mutations
# start independent workers or wake persisted subscribers whose destination we
# cannot prove from the model arguments. Do not import newer host internals to
# infer authority: origin inheritance must be implemented before opening them.
KANBAN_SESSION_ROUTES = frozenset({
    "kanban_create", "kanban_complete", "kanban_block", "kanban_schedule",
    "kanban_request_review", "kanban_request_changes", "kanban_unblock",
    "kanban_link", "kanban_comment",
})


def host_tool_targets_current_session(tool_name: str, args) -> bool:
    """Whether an audited call avoids starting/routing work in another session.

    Hermes passes coerced wire arguments to pre_tool_call. It does not fold
    snake/camel case or key case; these audited keys already satisfy its schema
    sanitizer. Legacy action aliases are handled by the same action policy.
    Terminal notify/watch/heartbeat destinations come only from bound host
    context. ClawChat mention/react/file and send_message are ordinary outbound
    messages, so remain governed by cross-chat grants and resource checks.
    """
    if tool_name in KANBAN_SESSION_ROUTES or tool_name == "message_agent":
        return False
    if tool_name in {"delegate_task", "cronjob", "cronjob_manage"}:
        if not isinstance(args, dict):
            return False
        action = args.get("action") or ("spawn" if tool_name == "delegate_task" else "")
        if not isinstance(action, str):
            return False
        action = action.strip().lower()
        # Spawn/steer address an isolated child; cron executes in a fresh
        # session even with deliver=origin/local. Existing jobs and subscriber
        # targets cannot be trusted merely because their id is supplied.
        return action in ({"list", "stop"} if tool_name == "delegate_task" else {"list", "pause", "remove"})
    return True


def resolve_cross_chat_gate(context=None, effective_grant=None) -> str:
    """Pending product decision; retain the host's existing cross-chat checks."""
    return "legacy"


def plan_deny_removal(rows) -> str:
    """Pending product decision; revocation returns to the ordinary default."""
    return "immediate"
