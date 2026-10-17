"""Host-independent tool grants. All numeric timestamps are epoch milliseconds."""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from clawchat_gateway.tool_capabilities import (
    AUTHORIZATION_TOOLS, TOOL_CAPABILITIES, parse_grant_selector,
    normalize_grant_selectors, selector_display, selector_is_sensitive,
    is_sensitive_tool, is_cross_chat_call, plan_deny_removal,
    canonical_tool_name,
)

GRANT_TOOL_NAMES = (
    "clawchat_tool_grant", "clawchat_tool_revoke", "clawchat_tool_grants_list",
)
TOOL_GRANTS_CONFIG_KEY = "tool-grants"
PENDING_TTL_MS = 600_000
# Apply to trimmed inbound text; use fullmatch to require the whole command.
GRANT_CONFIRM_COMMAND = re.compile(r"^/clawchat-allow\s+(\d{6})$", re.ASCII)

# Host registrations across the supported range: hosts up to 0.21.x name the
# process and cron tools ``process`` / ``cronjob``; later ones ``*_manage``.
# Other ``browser_`` names receive the same sensitive warning, never implicit permission.
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

NON_OWNER_GRANTS_HINT = "Explain only the requested ability in the conversation's language. For missing access, tell them the owner can allow it in their direct chat. For a mixed-turn block, an allowed person can ask again alone. Do not invent reasons or reveal others' access, settings, paths, or versions."
GRANT_BLOCK_MESSAGE = 'This tool is not available to the caller yet. Do not try another way. Explain that the owner can allow this ability in their direct chat.'
GRANT_INTERSECTION_BLOCK_MESSAGE = 'Someone in this turn is not allowed to use this tool, so it did not run. Do not retry. Say that an allowed person can call you again alone.'
GRANT_ASK_DENIED_MESSAGE = 'The owner did not approve this call or has not answered yet, so it did not run. Do not retry this request. They can try later or ask the owner directly.'
GRANT_DENIED_MESSAGE = 'The owner has not opened access to this ability. Do not retry or send another access request.'
GRANT_ASK_UNAVAILABLE_MESSAGE = 'This machine cannot ask the owner for approval on each call, so it did not run. Do not retry or treat this as direct access. Tell them to ask the owner directly.'
GRANT_NOT_AVAILABLE_MESSAGE = 'This ability is not available here. Say you cannot do it. Do not suggest requesting access or trying another route.'
GRANT_CROSS_SESSION_MESSAGE = 'This call would start or route work in another session, so it did not run. Do not retry or use another route. Ask the owner directly.'

GRANT_FAILURE_MESSAGES = {
    "not-granted": GRANT_BLOCK_MESSAGE, "denied": GRANT_DENIED_MESSAGE,
    "batch-intersection": GRANT_INTERSECTION_BLOCK_MESSAGE, "ask-declined": GRANT_ASK_DENIED_MESSAGE,
    "ask-unavailable": GRANT_ASK_UNAVAILABLE_MESSAGE, "not-available": GRANT_NOT_AVAILABLE_MESSAGE,
    "cross-session": GRANT_CROSS_SESSION_MESSAGE,
}

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

GRANT_DENY_RECEIPT = {
    "zh": "已生效：未向 {names} 开放 {tools}",
    "zh_Hant": "已生效：未向 {names} 開放 {tools}",
    "en": "Now active: {tools} is not open to {names}",
}
GRANT_ASK_UNAVAILABLE_RECEIPT = {
    "zh": "这台机器上做不到每次问你，只能选直接用或不给。",
    "zh_Hant": "這台機器上做不到每次問你，只能選直接用或不給。",
    "en": "This machine cannot ask you each time. You can only choose Use directly or Deny.",
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_baseline_tool(name: str) -> bool:
    """Compatibility name for warning copy only; sensitivity never changes a mode."""
    return is_sensitive_tool(name)


_RANK = {"direct": 1, "ask": 2, "deny": 3}


@dataclass(frozen=True)
class _SpeakerPlan:
    # Tuples keep the dispatch snapshot independent of later database changes.
    rows: tuple[tuple[str, str, str, float | None], ...]
    public_tools: frozenset[str]
    unmatched: str

    def resolve(self, tool, args, context, now):
        tool = canonical_tool_name(tool)
        level, result = 0, None
        for kind, what, mode, expiry in self.rows:
            if now is not None and expiry is not None and expiry <= now:
                continue
            matches = (canonical_tool_name(what) == tool if kind == "tool" else True if kind == "all" else
                       tool in TOOL_CAPABILITIES[what]["tools"] and
                       (what != "cross-chat" or
                        (not (context or {}).get("current_chat_catalog") and
                         (args is None or context is None or is_cross_chat_call(tool, args, context)))))
            priority = {"tool": 3, "capability": 2, "all": 1}[kind]
            if matches and (priority > level or priority == level and _RANK[mode] > _RANK.get(result, 0)):
                level, result = priority, mode
        public = tool in {canonical_tool_name(name) for name in self.public_tools}
        default_allowed = context.get("default_allowed", public) if context else public
        return result or ("direct" if default_allowed else
                          "ask" if self.unmatched == "ask" else None)


@dataclass(frozen=True, eq=False)
class CompiledGrants(Mapping):
    plans: tuple[_SpeakerPlan, ...] = ()
    candidates: frozenset[str] = frozenset()
    non_owner_speakers: frozenset[str] = frozenset()
    owner_only: bool = False

    def resolve(self, tool, args=None, context=None, now=None):
        if tool in AUTHORIZATION_TOOLS:
            return None
        if self.owner_only:
            return "direct"
        modes = [plan.resolve(tool, args, context, now) for plan in self.plans]
        if "deny" in modes:
            return "deny"
        if not modes or None in modes:
            return None
        return "ask" if "ask" in modes else "direct"

    def __getitem__(self, tool):
        result = self.resolve(tool)
        if result is None:
            raise KeyError(tool)
        return result

    def __iter__(self):
        return (tool for tool in sorted(self.candidates) if self.resolve(tool) is not None)

    def __len__(self):
        return sum(1 for _ in self)

    def __bool__(self):
        # Broad rules must retain the admission boundary even while the host
        # catalog is empty: a tool can become available during this turn.
        return bool(len(self) or any(plan.unmatched == "ask" or any(
            kind == "all" and mode in ("direct", "ask") for kind, _, mode, _ in plan.rows)
            for plan in self.plans))


def evaluate_tool_grant(grants, tool, args=None, context=None, now=None):
    try:
        if now is not None and not _finite(now):
            return None
        return freeze_turn_grants(grants).resolve(tool, args, context, now)
    except Exception:  # noqa: BLE001 - malformed authority fails closed
        return None


def tool_grant_visible(grants, tool, *, default_allowed, now):
    """A schema is useful when either the current-chat or cross-chat call can run.

    Evaluate complete batch intersections for each case; do not combine one
    speaker's current-chat access with another speaker's cross-chat access.
    Exact-tool and all rules still apply to current-chat actions.
    """
    context = {"default_allowed": default_allowed}
    if evaluate_tool_grant(grants, tool, context=context, now=now) in ("direct", "ask"):
        return True
    return (tool in TOOL_CAPABILITIES["cross-chat"]["tools"] and
            evaluate_tool_grant(grants, tool, context={**context, "current_chat_catalog": True},
                                now=now) in ("direct", "ask"))


def resolve_turn_grants(speakers, owner_user_id, rows, now, *, public_tools=(), unmatched="none",
                        available_tools=()) -> CompiledGrants:
    """Exact > capability > all, then intersect trusted non-owner speakers."""
    def fallback():
        return CompiledGrants((_SpeakerPlan((), frozenset(public_tools), "none"),), frozenset(public_tools))
    try:
        if (not _finite(now) or not isinstance(owner_user_id, str) or not owner_user_id.strip()
                or not isinstance(speakers, (list, tuple)) or any(
                    not isinstance(speaker, str) or not speaker.strip() for speaker in speakers)):
            return fallback()
        ids = frozenset(speaker.strip() for speaker in speakers) - {owner_user_id.strip()}
        if not ids:
            return CompiledGrants(owner_only=True) if speakers else fallback()
        by_speaker = {speaker: [] for speaker in ids}
        candidates = set(public_tools) | set(available_tools)
        for row in rows:
            target = row["grantee_user_id"]
            if target not in by_speaker:
                continue
            # Old callers/payloads use exact names, including reserved capability IDs.
            kind, what = row.get("what_kind", "tool"), row.get("what", row.get("tool"))
            mode, expiry = row["mode"], row.get("expires_at")
            if expiry is not None:
                if not _finite(expiry):
                    raise ValueError("Invalid expiry")
                if expiry <= now:
                    continue
            if mode not in _RANK or not isinstance(what, str) or not what:
                raise ValueError("Invalid grant")
            if kind == "tool" and what in AUTHORIZATION_TOOLS:
                continue
            if (kind not in ("tool", "capability", "all") or
                    kind == "capability" and what not in TOOL_CAPABILITIES or kind == "all" and what != "all"):
                raise ValueError("Invalid selector")
            by_speaker[target].append((kind, what, mode, expiry))
            if kind == "tool":
                candidates.add(what)
            elif kind == "capability":
                candidates.update(TOOL_CAPABILITIES[what]["tools"])
        return CompiledGrants(tuple(_SpeakerPlan(tuple(by_speaker[id_]), frozenset(public_tools), unmatched)
                                    for id_ in sorted(ids)), frozenset(candidates), ids)
    except Exception:  # noqa: BLE001 - a read failure must not activate approve defaults
        return fallback()


def freeze_turn_grants(grants):
    if isinstance(grants, CompiledGrants):
        return grants
    rows = tuple(("tool", tool, mode, None) for tool, mode in grants.items() if tool not in AUTHORIZATION_TOOLS)
    if any(mode not in _RANK for _, _, mode, _ in rows):
        raise ValueError("Invalid grant mode")
    return CompiledGrants((_SpeakerPlan(rows, frozenset(), "none"),), frozenset(grants))


def intersect_turn_grants(snapshots):
    """Owner-only snapshots are neutral, including when their finite index is empty."""
    try:
        snapshots = [freeze_turn_grants(snapshot) for snapshot in snapshots]
        others = [snapshot for snapshot in snapshots if not snapshot.owner_only]
        if not others:
            return CompiledGrants(owner_only=bool(snapshots))
        ids = frozenset(id_ for snapshot in others for id_ in snapshot.non_owner_speakers)
        if any(not snapshot.plans for snapshot in others):
            return CompiledGrants(non_owner_speakers=ids)
        # Overlaps already contain earlier intersections; equivalent plans are
        # idempotent and must not multiply with each additional live snapshot.
        plans = tuple(dict.fromkeys(plan for snapshot in others for plan in snapshot.plans))
        return CompiledGrants(plans, frozenset(tool for snapshot in others for tool in snapshot.candidates), ids)
    except Exception:  # noqa: BLE001
        return CompiledGrants()


def has_grant_rows(grants, *, now=None):
    """Whether frozen speaker rules contain any active explicit authorization row."""
    if isinstance(grants, CompiledGrants):
        return any(expiry is None or now is None or expiry > now
                   for plan in grants.plans for _, _, _, expiry in plan.rows)
    return bool(grants)


def grant_failure_code(grants, tool, *, available=True, args=None, context=None, now=None, speakers=()):
    if not available:
        return "not-available"
    mode = evaluate_tool_grant(grants, tool, args, context, now)
    if mode in ("direct", "ask"):
        return None
    ids = (grants.non_owner_speakers if isinstance(grants, CompiledGrants) else frozenset()) or frozenset(speakers)
    if len(ids) > 1:
        return "batch-intersection"
    return "denied" if mode == "deny" else "not-granted"


def build_non_owner_grants_hint(grants, available_tools):
    blocked = [f"{cap['names']['en']} ({id_})" for id_, cap in TOOL_CAPABILITIES.items()
               if any(tool in available_tools and evaluate_tool_grant(grants, tool) not in ("direct", "ask")
                      for tool in cap["tools"])]
    return ("Not available to the caller yet: " + ", ".join(blocked) + ". " if blocked else "") + NON_OWNER_GRANTS_HINT


def generate_grant_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


_UNSAFE_TEXT = re.compile(r"[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]")


def _clean_text(text: str, *, identifier: bool = False) -> str:
    text = " ".join(_UNSAFE_TEXT.sub(" ", text).split())
    # Untrusted profile/label text must never create Markdown or a clickable
    # slash command. The confirmation command is appended separately in code.
    punctuation = "[]()*~`/\\<>!#|" + ("" if identifier else "_")
    return text.translate(str.maketrans({char: chr(ord(char) + 0xFEE0) for char in punctuation}))


def _card_language(lang: str) -> str:
    return lang if lang in ("zh", "zh_Hant") else "en"


def build_grant_receipt(*, lang: str, names: list[str], tools: list[str], mode: str = "direct") -> str:
    lang = _card_language(lang)
    separator = ", " if lang == "en" else "、"
    values = {"names": separator.join(map(_clean_text, names)),
              "tools": separator.join(_clean_text(tool, identifier=True) for tool in tools)}
    return re.sub(r"\{(names|tools)\}", lambda match: values[match[1]], (GRANT_DENY_RECEIPT if mode == "deny" else GRANT_CONFIRM_RECEIPT)[lang])


def _format_expiry(expires_at: float, time_zone: str | None) -> str:
    zone = ZoneInfo(time_zone) if time_zone else timezone.utc
    text = datetime.fromtimestamp(expires_at / 1000, zone).strftime("%Y-%m-%d %H:%M")
    return text if time_zone else text + " (UTC)"


def valid_grant_tools(value: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(
        isinstance(tool, str) and bool(tool) and not re.search(r"[\s*?]", tool)
        and tool not in AUTHORIZATION_TOOLS for tool in value
    )


def is_pending_tool_grant_payload(value: Any, version: int = 2) -> bool:
    if not isinstance(value, dict):
        return False
    if version not in (1, 2):
        return False
    if version == 2:
        try:
            normalize_grant_selectors(value.get("tools"))
        except ValueError:
            return False
    grantees = value.get("grantees")
    return (
        isinstance(grantees, list) and bool(grantees)
        and all(isinstance(g, dict) and all(
            isinstance(g.get(key), str) and bool(g[key].strip()) for key in ("id", "name")
        ) for g in grantees)
        and valid_grant_tools(value.get("tools"))
        and value.get("mode") in ("direct", "ask", "deny")
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
    tools = normalize_grant_selectors(params.get("tools"))
    if params.get("mode") not in ("direct", "ask", "deny"):
        raise ValueError("mode must be direct, ask or deny")
    if not _finite(now):
        raise ValueError("Invalid tool grant time")
    result.update(tools=tools, mode=params["mode"])
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


CARD_COPY = {'sensitive': {'zh': '{names}能在这台机器上跑任何命令、读写任何文件，包括这个 Agent 的登录凭据、你的模型 key 和这张授权列表。拿到凭据的人可以冒充这个 Agent '
                     '跟别人说话。只给你完全信任的人。',
               'zh_Hant': '{names}能在這台機器上執行任何命令、讀寫任何檔案，包括這個 Agent 的登入憑證、你的模型 key 和這張授權列表。拿到憑證的人可以冒充這個 Agent '
                          '跟別人說話。只給你完全信任的人。',
               'en': '{names} can run any command and read or write any file on this machine, including this '
                     "Agent's login credentials, your model API key, and this grant list. Anyone with the "
                     'credentials can impersonate this Agent when talking to others. Only allow people you '
                     'trust completely.'},
 'expiry': {'zh': '到期：不限', 'zh_Hant': '到期：不限', 'en': 'Expires: No limit'},
 'expiry_date': {'zh': '到期：{expiry}', 'zh_Hant': '到期：{expiry}', 'en': 'Expires: {expiry}'},
 'expiry_hint': {'zh': '想设期限就说「……到周五」。',
                 'zh_Hant': '想設期限就說「……到週五」。',
                 'en': 'To set an expiry, say “…until Friday.”'},
 'all_future': {'zh': '包括以后新装的工具。', 'zh_Hant': '包括以後新裝的工具。', 'en': 'Includes tools installed in the future.'},
 'all_sensitive': {'zh': '包括敏感工具。', 'zh_Hant': '包括敏感工具。', 'en': 'Includes sensitive tools.'},
 'all_exclusions': {'zh': '不包括授权、收回、列出授权和申请授权这四个工具。',
                    'zh_Hant': '不包括授權、收回、列出授權和申請授權這四個工具。',
                    'en': 'Excludes the four tools for granting, revoking, listing, and requesting access.'},
 'ask': {'zh': '这类操作每次都会先问你。',
         'zh_Hant': '這類操作每次都會先問你。',
         'en': 'These operations will ask you first every time.'},
 'unavailable': {'zh': '这台机器上做不到每次问你，只能选直接用或不给。',
                 'zh_Hant': '這台機器上做不到每次問你，只能選直接用或不給。',
                 'en': 'This machine cannot ask you each time. You can only choose Use directly or Deny.'},
 'consequence': {'zh': '这意味着：{names}能让我代为调用这些工具，你用这些工具能看到、能做到的，{names}都能。',
                 'zh_Hant': '這意味著：{names}能讓我代為呼叫這些工具，你用這些工具能看到、能做到的，{names}都能。',
                 'en': 'This means: {names} can have me call these tools for them. Whatever you can see or '
                       'do with these tools, they can too.'},
 'resource': {'zh': '这次按工具授权，不保证只限某份文档或某个资源。',
              'zh_Hant': '這次按工具授權，不保證只限某份文件或某個資源。',
              'en': 'This grants access by tool; it does not guarantee access is limited to one document or '
                    'resource.'},
 'title': {'zh': '授权确认（码 {code}）', 'zh_Hant': '授權確認（碼 {code}）', 'en': 'Grant confirmation (code {code})'},
 'fields': {'zh': '允许：{people}；使用：{abilities}；方式：{mode}',
            'zh_Hant': '允許：{people}；使用：{abilities}；方式：{mode}',
            'en': 'Allow: {people}; Use: {abilities}; Mode: {mode}'},
 'direct_mode': {'zh': '直接用', 'zh_Hant': '直接用', 'en': 'Use directly'},
 'ask_mode': {'zh': '每次问我', 'zh_Hant': '每次問我', 'en': 'Ask me each time'},
 'tail': {'zh': '确认请点：`{command}`（10 分钟内有效；不确认就不会生效）',
          'zh_Hant': '確認請點：`{command}`（10 分鐘內有效；不確認就不會生效）',
          'en': 'Click to confirm: `{command}` (valid for 10 minutes; it will not take effect without '
                'confirmation)'},
 'deny_receipt': {'zh': '已生效：未向 {names} 开放 {tools}',
                  'zh_Hant': '已生效：未向 {names} 開放 {tools}',
                  'en': 'Now active: {tools} is not open to {names}'}}



def grant_card_modes(ask_available=True):
    return ("direct", "ask", "deny") if ask_available else ("direct", "deny")


def build_grant_card(*, grantees, tools, mode, lang, code, now, label=None,
                     expires_at=None, time_zone=None, ask_available=True):
    if any(not isinstance(g.get("name"), str) or not _clean_text(g["name"]) for g in grantees):
        return None
    payload = dict(grantees=grantees, tools=tools, mode=mode, expires_at=expires_at)
    if label is not None:
        payload["label"] = label
    if (not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code) or not _finite(now)
            or not is_pending_tool_grant_payload(payload) or mode == "deny"):
        raise ValueError("Invalid grant card input")
    if mode not in grant_card_modes(ask_available):
        raise ValueError("ask-unavailable")
    lang = _card_language(lang)
    en = lang == "en"
    separator = ", " if en else "、"
    names = separator.join(_clean_text(g["name"]) for g in grantees)
    people = separator.join(
        f'{_clean_text(g["name"])} ({_clean_text(g["id"], identifier=True)})' if en else
        f'{_clean_text(g["name"])}（{_clean_text(g["id"], identifier=True)}）' for g in grantees)
    abilities = separator.join(_clean_text(selector_display(tool, lang), identifier=True) for tool in tools)
    label = _clean_text(label) if label else ""
    if label:
        abilities += (f' (you called it “{label}”)' if en else
                      f'（你說的「{label}」）' if lang == "zh_Hant" else f'（你说的「{label}」）')
    def copy(key, **values):
        return CARD_COPY[key][lang].format(**values)
    fields = [field.format(people=people, abilities=abilities, mode=copy(mode + "_mode"))
              for field in CARD_COPY["fields"][lang].split("; " if en else "；")]
    lines = [copy("title", code=code), *fields,
             copy("expiry") if expires_at is None else copy("expiry_date", expiry=_format_expiry(expires_at, time_zone)),
             copy("expiry_hint"), copy("consequence", names=names), copy("resource")]
    if any(parse_grant_selector(tool).what_kind == "all" for tool in tools):
        lines += [copy(key) for key in ("all_future", "all_sensitive", "all_exclusions")]
    if mode == "direct" and any(selector_is_sensitive(tool) for tool in tools):
        lines.append(copy("sensitive", names=names))
    if mode == "ask":
        lines.append(copy("ask"))
    if not ask_available:
        lines.append(copy("unavailable"))
    lines.append(copy("tail", command="/clawchat-allow " + code))
    return "\n".join(lines)
