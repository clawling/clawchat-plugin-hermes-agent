"""Host-independent tool grants. All numeric timestamps are epoch milliseconds."""

from __future__ import annotations

import math
import re
import secrets
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

GRANT_TOOL_NAMES = (
    "clawchat_tool_grant", "clawchat_tool_revoke", "clawchat_tool_grants_list",
)
TOOL_GRANTS_CONFIG_KEY = "tool-grants"
PENDING_TTL_MS = 600_000
# Apply to trimmed inbound text; use fullmatch to require the whole command.
GRANT_CONFIRM_COMMAND = re.compile(r"^/clawchat-allow\s+(\d{6})$", re.ASCII)

# Host registrations across the supported range: hosts up to 0.21.x name the
# process and cron tools ``process`` / ``cronjob``; later ones ``*_manage``.
# Any other ``browser_`` tool is browser too (see is_baseline_tool).
BASELINE_CATEGORY = {
    "terminal": "command", "process": "command", "process_manage": "command",
    "read_file": "files", "write_file": "files", "patch": "files", "search_files": "files",
    "execute_code": "code",
    "delegate_task": "subagent",
    "cronjob": "schedule", "cronjob_manage": "schedule",
    "browser_back": "browser", "browser_cdp": "browser", "browser_click": "browser",
    "browser_console": "browser", "browser_dialog": "browser", "browser_exec": "browser",
    "browser_get_images": "browser", "browser_navigate": "browser", "browser_press": "browser",
    "browser_scroll": "browser", "browser_snapshot": "browser", "browser_type": "browser",
    "browser_vault": "browser", "browser_vision": "browser", "computer_use": "browser",
}

NON_OWNER_GRANTS_HINT = 'Some abilities here are the owner\'s only: running commands, reading or writing files, running code, sub-agents, scheduled tasks, the browser, and other tools or apps the owner connected. If someone asks for one, do not try it. Tell them, in this conversation\'s language, that you can\'t use it now, and that the owner can allow it by telling you in their direct chat: "Allow <their name> to use <ability>" — for commands, files, code, sub-agents, scheduled tasks or the browser add ", ask me each time". If this turn has several people and one of them is not allowed, say an allowed person can call you again alone. If the ability does not exist here at all, just say you can\'t. Never mention who else has access, settings, file paths, versions or other tools.'
GRANT_BLOCK_MESSAGE = 'This tool is not available to the person asking. Do not try it another way. Tell them you can\'t use it now and that the owner can allow it by telling you in their direct chat: "Allow <their name> to use <ability>". Do not mention other tools, settings or paths.'
GRANT_INTERSECTION_BLOCK_MESSAGE = "Someone in this turn is not allowed to use this tool, so it did not run. Do not retry. Say that an allowed person can call you again alone."
GRANT_ASK_DENIED_MESSAGE = "Your owner did not approve this call (or has not answered yet), so it did not run. Do not call it again for this request; say the owner did not approve it this time and they can try later or ask the owner directly."

GRANT_CONFIRM_RECEIPT = {
    "zh": "已生效：{names} 现在可以使用 {tools}",
    "zh_Hant": "已生效：{names} 現在可以使用 {tools}",
    "en": "Now active: {names} can now use {tools}",
}
GRANT_WRITE_FAILED_MESSAGE = {
    "zh": "没写进去，请稍后再说一次。", "zh_Hant": "沒寫進去，請稍後再說一次。",
    "en": "It was not saved. Please ask again later.",
}
GRANT_INVALID_CODE_MESSAGE = {
    "zh": "这个确认码无效或已过期。", "zh_Hant": "這個確認碼無效或已過期。",
    "en": "This confirmation code is invalid or has expired.",
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_baseline_tool(name: str) -> bool:
    return name in BASELINE_CATEGORY or (isinstance(name, str) and name.startswith("browser_"))


def resolve_turn_grants(
    speakers: list[str], owner_user_id: str, rows: list[dict[str, Any]], now: float,
) -> dict[str, str]:
    """Intersect non-owner speakers' grants; any identity/read error fails closed."""
    try:
        if not _finite(now) or not isinstance(owner_user_id, str) or not owner_user_id.strip():
            return {}
        if not isinstance(speakers, (list, tuple)) or any(
            not isinstance(speaker, str) or not speaker.strip() for speaker in speakers
        ):
            return {}
        ids = {speaker.strip() for speaker in speakers} - {owner_user_id.strip()}
        if not ids:
            return {}
        by_speaker: dict[str, dict[str, str]] = {speaker: {} for speaker in ids}
        for row in rows:
            grants = by_speaker.get(row["grantee_user_id"])
            tool = row["tool"]
            if grants is None or not tool or tool in GRANT_TOOL_NAMES:
                continue
            if not isinstance(tool, str):
                return {}
            if row["mode"] not in ("direct", "ask"):
                return {}
            expires_at = row.get("expires_at")
            if expires_at is not None and (not _finite(expires_at) or expires_at <= now):
                continue
            grants[tool] = "ask" if (
                is_baseline_tool(tool) or row["mode"] == "ask" or grants.get(tool) == "ask"
            ) else "direct"
        return intersect_turn_grants(list(by_speaker.values()))
    except Exception:  # noqa: BLE001 - authorization errors must fail closed
        return {}


def intersect_turn_grants(snapshots: list[dict[str, str]]) -> dict[str, str]:
    """Shared with overlapping turn snapshots; the strictest mode wins."""
    try:
        if not snapshots:
            return {}
        result = {}
        for tool, mode in snapshots[0].items():
            if tool in GRANT_TOOL_NAMES:
                continue
            if mode not in ("direct", "ask"):
                return {}
            for snapshot in snapshots[1:]:
                if tool not in snapshot:
                    break
                next_mode = snapshot[tool]
                if next_mode not in ("direct", "ask"):
                    return {}
                if next_mode == "ask":
                    mode = "ask"
            else:
                result[tool] = "ask" if is_baseline_tool(tool) else mode
        return result
    except Exception:  # noqa: BLE001
        return {}


def generate_grant_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


_UNSAFE_TEXT = re.compile(r"[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def _clean_text(text: str) -> str:
    return " ".join(_UNSAFE_TEXT.sub(" ", text).split())


def _card_language(lang: str) -> str:
    return lang if lang in ("zh", "zh_Hant") else "en"


def build_grant_receipt(*, lang: str, names: list[str], tools: list[str]) -> str:
    lang = _card_language(lang)
    separator = ", " if lang == "en" else "、"
    values = {"names": separator.join(map(_clean_text, names)), "tools": separator.join(map(_clean_text, tools))}
    return re.sub(r"\{(names|tools)\}", lambda match: values[match[1]], GRANT_CONFIRM_RECEIPT[lang])


def _format_expiry(expires_at: float, time_zone: str | None) -> str:
    zone = ZoneInfo(time_zone) if time_zone else timezone.utc
    text = datetime.fromtimestamp(expires_at / 1000, zone).strftime("%Y-%m-%d %H:%M")
    return text if time_zone else text + " (UTC)"


def valid_grant_tools(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(tool, str) and bool(tool) and not re.search(r"[\s*?]", tool)
        and tool not in GRANT_TOOL_NAMES for tool in value
    )


def is_pending_tool_grant_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    grantees = value.get("grantees")
    return (
        isinstance(grantees, list) and bool(grantees)
        and all(isinstance(g, dict) and all(
            isinstance(g.get(key), str) and bool(g[key].strip()) for key in ("id", "name")
        ) for g in grantees)
        and valid_grant_tools(value.get("tools"))
        and value.get("mode") in ("direct", "ask")
        and ("label" not in value or isinstance(value["label"], str))
        and (value.get("expires_at") is None or _finite(value["expires_at"]))
    )


def validate_grant_params(params: Any, now: float) -> dict[str, Any]:
    """Validate public camelCase arguments; return normalized snake_case values.

    Names and current group membership must be resolved by the caller. Calendar
    dates use UTC, as in the reference. Datetimes require an explicit offset;
    the specification does not define a timezone for a bare local datetime.
    """
    if not isinstance(params, dict) or ("userIds" in params) == ("groupId" in params):
        raise ValueError("Supply exactly one of userIds or groupId")
    if params.keys() - {"userIds", "groupId", "tools", "mode", "expiresAt", "label"}:
        raise ValueError("Unknown grant parameter")
    result = {}
    if "userIds" in params:
        ids = params["userIds"]
        if not isinstance(ids, list) or not ids or not all(
            isinstance(id_, str) and re.fullmatch(r"usr_[^\s]+", id_) for id_ in ids
        ):
            raise ValueError("userIds must be a nonempty array of explicit usr_ ids")
        result["user_ids"] = list(dict.fromkeys(ids))
    else:
        group_id = params["groupId"]
        if not isinstance(group_id, str) or not re.fullmatch(r"cnv_[0-9A-HJKMNP-TV-Za-hjkmnp-tv-z]{26}", group_id):
            raise ValueError("groupId must be a ClawChat conversation id")
        result["group_id"] = group_id
    if not valid_grant_tools(params.get("tools")):
        raise ValueError("tools must be nonempty exact names, without whitespace, wildcards or grant tools")
    if params.get("mode") not in ("direct", "ask"):
        raise ValueError("mode must be direct or ask")
    if not _finite(now):
        raise ValueError("Invalid tool grant time")
    result.update(tools=list(dict.fromkeys(params["tools"])), mode=params["mode"])
    if "expiresAt" in params:
        value = params["expiresAt"]
        try:
            if not isinstance(value, str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))?", value, re.ASCII
            ):
                raise ValueError
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            expires_at = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp() * 1000
            if expires_at <= now:
                raise ValueError
        except (ValueError, OverflowError):
            raise ValueError("expiresAt must be an ISO 8601 future date") from None
        result["expires_at"] = expires_at
    if "label" in params:
        if not isinstance(params["label"], str) or len(params["label"]) > 40:
            raise ValueError("label must be at most 40 characters")
        result["label"] = params["label"]
    return result


def build_grant_card(*, grantees: list[dict[str, str]], tools: list[str], mode: str,
                     lang: str, code: str, now: float, label: str | None = None,
                     expires_at: float | None = None, time_zone: str | None = None) -> str | None:
    # A failed profile lookup must never produce a confirmation card.
    if any(not isinstance(g.get("name"), str) or not _clean_text(g["name"]) for g in grantees):
        return None
    payload = dict(grantees=grantees, tools=tools, mode=mode, expires_at=expires_at)
    if label is not None:
        payload["label"] = label
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not _finite(now) or not is_pending_tool_grant_payload(payload):
        raise ValueError("Invalid grant card input")
    lang = _card_language(lang)
    en = lang == "en"
    separator = ", " if en else "、"
    names = separator.join(_clean_text(g["name"]) for g in grantees)
    people = separator.join(
        f'{_clean_text(g["name"])} ({_clean_text(g["id"])})' if en
        else f'{_clean_text(g["name"])}（{_clean_text(g["id"])}）' for g in grantees
    )
    tool_list = separator.join(map(_clean_text, tools))
    baseline_tools = [tool for tool in tools if is_baseline_tool(tool)]
    direct_tools = [tool for tool in tools if not is_baseline_tool(tool)]
    ask = mode == "ask" or bool(baseline_tools)
    mixed = mode == "direct" and bool(baseline_tools) and bool(direct_tools)
    direct_list = separator.join(map(_clean_text, direct_tools))
    ask_list = separator.join(map(_clean_text, baseline_tools))
    expiry = _format_expiry(expires_at, time_zone) if expires_at is not None else None
    command = f"`/clawchat-allow {code}`"
    label = _clean_text(label) if label else ""
    if lang == "zh":
        return "\n".join([
            f"授权确认（码 {code}）", f"允许：{people}",
            f"使用：{tool_list}" + (f"（你说的「{label}」）" if label else ""),
            *([f"直接用：{direct_list}", f"每次先问你：{ask_list}"] if mixed else [f"方式：{'每次先问你' if ask else '直接用'}"]),
            f"期限：到 {expiry}" if expiry else "期限：不限",
            f"这意味着：{names}能让我代为调用这些工具，你用这些工具能看到、能做到的，{names}都能。",
            *(["这类操作每次都会先问你。"] if ask else []),
            f"确认请点：{command}（10 分钟内有效；不确认就不会生效）",
        ])
    if lang == "zh_Hant":
        return "\n".join([
            f"授權確認（碼 {code}）", f"允許：{people}",
            f"使用：{tool_list}" + (f"（你說的「{label}」）" if label else ""),
            *([f"直接用：{direct_list}", f"每次先問你：{ask_list}"] if mixed else [f"方式：{'每次先問你' if ask else '直接用'}"]),
            f"期限：到 {expiry}" if expiry else "期限：不限",
            f"這意味著：{names}能讓我代為呼叫這些工具，你用這些工具能看到、能做到的，{names}都能。",
            *(["這類操作每次都會先問你。"] if ask else []),
            f"確認請點：{command}（10 分鐘內有效；不確認就不會生效）",
        ])
    return "\n".join([
        f"Grant confirmation (code {code})", f"Allow: {people}",
        f"Use: {tool_list}" + (f" (you called it “{label}”)" if label else ""),
        *([f"Use directly: {direct_list}", f"Ask you each time: {ask_list}"] if mixed else [f"Mode: {'Ask you each time' if ask else 'Use directly'}"]),
        f"Expires: At {expiry}" if expiry else "Expires: No limit",
        f"This means: {names} can have me call these tools for them. Whatever you can see or do with these tools, they can too.",
        *(["These operations will ask you first every time."] if ask else []),
        f"Click to confirm: {command} (valid for 10 minutes; it will not take effect without confirmation)",
    ])
