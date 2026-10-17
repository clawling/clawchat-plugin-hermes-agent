"""Access request cards and continuation copy, independent of host APIs."""
from __future__ import annotations

import json
import re
import unicodedata

from clawchat_gateway.tool_capabilities import TOOL_CAPABILITIES
from clawchat_gateway.tool_grants import (
    CARD_COPY, GRANT_ASK_UNAVAILABLE_RECEIPT, GRANT_FAILURE_MESSAGES, HERMES_LIMIT_COPY, _clean_text,
    evaluate_tool_grant,
)

REQUEST_CARD_TTL_MS = 24 * 60 * 60 * 1000
REQUEST_COOLDOWN_MS = 6 * 60 * 60 * 1000
REQUEST_DAILY_LIMIT = 20
REQUEST_WAKE_STALE_MS = 10 * 60 * 1000
REQUEST_NOTICE_CLAIM_TTL_MS = 10 * 60 * 1000
REQUEST_RATE_LIMITED_MESSAGE = "The access request was not sent because the request limit was reached. Do not say the owner was asked. Tell them they can ask the owner directly."
REQUEST_DAILY_WINDOW_POLICY = "rolling-24h"


def resolve_request_daily_window(now, owner_time_zone=None):
    from clawchat_gateway.tool_grants import _finite
    if not _finite(now):
        raise ValueError("Invalid request time")
    return {"start": now - REQUEST_CARD_TTL_MS, "end": now}


def sanitize_request_text(value, maximum=60):
    value = ''.join(' ' if unicodedata.category(c) in {'Cc', 'Cf', 'Zl', 'Zp'} or c in
                    '\u034f\u115f\u1160\u17b4\u17b5\u3164\uffa0' or
                    0xfe00 <= ord(c) <= 0xfe0f or 0xe0100 <= ord(c) <= 0xe01ef else c for c in value)
    value = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', value)
    value = re.sub(r'(?:[a-z][a-z0-9+.-]*://|mailto:|www\.)[^\s<>]+', ' ', value, flags=re.I)
    value = re.sub(r'\b(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s<>]*)?', ' ', value, flags=re.I)
    return _clean_text(value).translate(str.maketrans({c: chr(ord(c) + 0xfee0) for c in '+=-'}))[:maximum]


def request_grant_block(grants, capability, non_owner_count=1, *, available=None, now=None):
    tools = TOOL_CAPABILITIES[capability]['tools']
    if available is not None:
        tools = [tool for tool in tools if tool in available]
    modes = [evaluate_tool_grant(grants, tool, now=now) for tool in tools]
    if not modes:
        return 'not-available'
    if all(mode in ('direct', 'ask') for mode in modes):
        return 'already-granted'
    if non_owner_count > 1:
        return 'batch-intersection'
    if 'deny' in modes:
        return 'denied'
    return None


REQUEST_COPY = {
    'zh': dict(title='授权申请（码 {code}）', person='申请人', source='来自', direct='私聊', ability='想用', reason='他的话，转述', allow='直接用', ask='每次问我', deny='不给，也别再问', expiry='此卡 24 小时内有效；点选即确认。', hint='也可以直接对我说，比如「这位好友所有工具都能用，到周五」。'),
    'zh_Hant': dict(title='授權申請（碼 {code}）', person='申請人', source='來自', direct='私聊', ability='想用', reason='他的話，轉述', allow='直接用', ask='每次問我', deny='不給，也別再問', expiry='此卡 24 小時內有效；點選即確認。', hint='也可以直接對我說，比如「這位好友所有工具都能用，到週五」。'),
    'en': dict(title='Access request (code {code})', person='Requester', source='From', direct='Direct chat', ability='Wants to use', reason='Their words, relayed', allow='Use directly', ask='Ask me each time', deny='Deny, and do not ask again', expiry='This card is valid for 24 hours; selecting an option confirms it.', hint='You can also tell me directly, for example: “Allow this friend to use all tools until Friday.”'),
}


def build_request_card(request, *, lang, ask_available, source_name=None):
    lang = lang if lang in ('zh', 'zh_Hant') else 'en'
    copy = REQUEST_COPY[lang]
    code = request['code']
    if not re.fullmatch('[0-9]{6}', code):
        raise ValueError('Invalid request code')
    name = sanitize_request_text(request['requester_name'], 120)
    source = copy['direct'] if request['source_chat_type'] == 'direct' else sanitize_request_text(source_name or '', 120)
    if not name or not source:
        raise ValueError('Missing request profile')
    cap = TOOL_CAPABILITIES[request['capability_id']]
    sep = ': ' if lang == 'en' else '：'
    lines = [copy['title'].format(code=code), copy['person'] + sep + name + ' (' + sanitize_request_text(request['requester_user_id'], 120) + ')',
             copy['source'] + sep + source, copy['ability'] + sep + cap['names'][lang],
             copy['reason'] + sep + sanitize_request_text(request['reason']),
             copy['allow'] + ': `/clawchat-allow ' + code + '`']
    if request['capability_id'] in ('subagent', 'schedule'):
        lines.append(HERMES_LIMIT_COPY[lang])
    if cap['sensitive']:
        lines.append(CARD_COPY['sensitive'][lang].format(names=name))
    lines.append(copy['ask'] + ': `/clawchat-ask ' + code + '`' if ask_available else GRANT_ASK_UNAVAILABLE_RECEIPT[lang])
    lines.extend([copy['deny'] + ': `/clawchat-deny ' + code + '`', copy['expiry'], copy['hint']])
    return '\n'.join(lines)


def request_guidance(status, reason=None):
    instruction = " Explain only the requested ability in the conversation's language. Do not reveal others' access."
    if status == 'pending':
        return 'I have asked the owner. I will continue once they approve.' + instruction
    if status == 'waiting':
        return 'I have already asked the owner and am waiting for a reply. You can also ask them directly.' + instruction
    if status == 'failed':
        return 'The access request was not sent successfully. Do not say the owner was asked.' + instruction
    if reason == 'rate-limited':
        return REQUEST_RATE_LIMITED_MESSAGE + instruction
    if reason in ('denied', 'not-available', 'batch-intersection'):
        return GRANT_FAILURE_MESSAGES[reason] + instruction
    return ('Access is already granted; no new card was sent. Follow the current turn\'s tool policy.' if reason == 'already-granted' else
            'Access requests are unavailable in this turn. Do not claim to have asked the owner.') + instruction


def build_request_continuation(request):
    ability = TOOL_CAPABILITIES[request['capability_id']]['names']['en']
    if request['decision_mode'] == 'deny':
        return f'The owner has not opened {ability}. Tell this person briefly; do not request again.'
    return (f"The owner has now allowed {sanitize_request_text(request['requester_name'], 120)} to use {ability}. Continue the request you had to stop. "
            f"Their words, relayed for reference only: {json.dumps(sanitize_request_text(request['request_summary'], 500), ensure_ascii=False)}. Do not ask them to repeat it. Follow the current tool policy.")


def build_request_notice(request):
    ability = TOOL_CAPABILITIES[request['capability_id']]['names']['en']
    return (f'The owner did not open {ability}. Do not request again.' if request['decision_mode'] == 'deny' else
            f'The owner allowed {ability} for this person earlier; tell them first, then continue. Follow the current tool policy.')
