"""Only a real message from the owner in the activation DM can confirm."""
import time
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.tool_grants import GRANT_INVALID_CODE_MESSAGE, GRANT_WRITE_FAILED_MESSAGE, PENDING_TTL_MS
from test_tool_grants_admission import harness, message, OWNER_CHAT, TOOL


@pytest.fixture
def confirmation(harness, tmp_path):
    a = harness.adapter
    a._store = ClawChatStore(tmp_path / "confirm.sqlite")
    a._store.initialize()
    a._owner_locale = lambda: "zh-CN"
    a._send_owner_text = AsyncMock(return_value=True)
    return harness


def create_pending(h, *, age=0, code="482193"):
    h.adapter._store.create_pending_grant(
        account_id="usr_agent", code=code, now=int(time.time() * 1000) - age,
        payload=dict(grantees=[dict(id="usr_a", name="张三")], tools=[TOOL], mode="direct"),
    )


def command(**kwargs):
    return message("usr_owner", "/clawchat-allow 482193", chat=OWNER_CHAT, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale,phrase", [("zh-CN", "已生效"), ("zh-TW", "已生效"), ("en", "Now active"), ("fr", "Now active")])
@pytest.mark.parametrize("backticks", [False, True])
async def test_real_owner_dm_confirms_and_receipts_in_owner_language(confirmation, locale, phrase, backticks):
    h = confirmation
    create_pending(h)
    h.adapter._owner_locale = lambda: locale
    incoming = command()
    if backticks:
        incoming.raw_message["payload"]["message"]["fragments"][0]["text"] = "`/clawchat-allow 482193`"
    await h.adapter._on_message(incoming.raw_message)
    rows = h.adapter._store.list_grants(account_id="usr_agent")
    assert len(rows) == 1 and rows[0]["what"] == TOOL
    h.adapter._send_owner_text.assert_awaited_once()
    assert h.adapter._send_owner_text.call_args.args[0] == OWNER_CHAT
    text = h.adapter._send_owner_text.call_args.args[1]
    assert phrase in text and "张三" in text and TOOL in text
    assert h.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["other_sender", "empty_sender", "group", "synthetic", "other_dm", "off"])
async def test_disallowed_confirmation_is_swallowed_without_dispatch_or_reply(confirmation, case):
    h = confirmation
    create_pending(h)
    incoming = command()
    if case == "other_sender":
        incoming = replace(incoming, sender_id="usr_b", raw_message=incoming.raw_message | {"sender": {"id": "usr_b"}})
    elif case == "empty_sender":
        incoming = replace(incoming, sender_id="", raw_message=incoming.raw_message | {"sender": {"id": ""}})
    elif case == "group":
        incoming = replace(incoming, chat_type="group", raw_message=incoming.raw_message | {"chat_type": "group"})
    elif case == "synthetic":
        incoming = replace(incoming, raw_message=incoming.raw_message | {"synthetic": True})
    elif case == "other_dm":
        incoming = replace(incoming, chat_id="cnv_other", raw_message=incoming.raw_message | {"chat_id": "cnv_other"})
    else:
        h.adapter._clawchat_config = replace(h.adapter._clawchat_config, tool_grants="off")
    await h.adapter._on_message(incoming.raw_message)
    await h.adapter._handle_inbound(incoming)  # synthetic/internal bypass is guarded too
    assert h.adapter._store.list_grants(account_id="usr_agent") == []
    h.adapter._send_owner_text.assert_not_awaited()
    assert h.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong", "expired", "replay"])
async def test_wrong_expired_and_replayed_codes_do_not_write(confirmation, case):
    h = confirmation
    create_pending(h, age=PENDING_TTL_MS + 1000 if case == "expired" else 0)
    incoming = command()
    if case == "wrong":
        incoming.raw_message["payload"]["message"]["fragments"][0]["text"] = "/clawchat-allow 000000"
    if case == "replay":
        await h.adapter._on_message(incoming.raw_message)
        h.adapter._send_owner_text.reset_mock()
    before = h.adapter._store.list_grants(account_id="usr_agent")
    await h.adapter._on_message(incoming.raw_message)
    assert h.adapter._store.list_grants(account_id="usr_agent") == before
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_INVALID_CODE_MESSAGE["zh"]
    assert h.events == []


@pytest.mark.asyncio
async def test_failed_transaction_never_receipts_as_active(confirmation, monkeypatch):
    h = confirmation
    create_pending(h)
    monkeypatch.setattr(h.adapter._store, "_upsert_tool_grant", lambda *args: (_ for _ in ()).throw(RuntimeError()))
    await h.adapter._on_message(command().raw_message)
    assert h.adapter._store.list_grants(account_id="usr_agent") == []
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_WRITE_FAILED_MESSAGE["zh"]
    assert "已生效" not in h.adapter._send_owner_text.call_args.args[1]
    assert h.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("locale,lang", [("zh-CN", "zh"), ("zh-TW", "zh_Hant"), ("en", "en")])
@pytest.mark.parametrize("selector,limited", [("cap:subagent", True), ("cap:schedule", True),
    ("cap:terminal", False), ("all", True), ("tool:web_search", False),
    ("tool:delegate_task", True), ("tool:cronjob", True), ("tool:cronjob_manage", True),
    ("tool:kanban_create", True), ("tool:message_agent", True)])
async def test_localized_confirmation_uses_canonical_selectors(confirmation, locale, lang, selector, limited):
    from clawchat_gateway.tool_capabilities import selector_display
    from clawchat_gateway.tool_grants import HERMES_LIMIT_COPY
    h = confirmation
    h.adapter._owner_locale = lambda: locale
    h.adapter._store.create_pending_grant(account_id="usr_agent", code="482193", now=int(time.time() * 1000),
        payload=dict(grantees=[dict(id="usr_a", name="Person")], tools=[selector], mode="direct"))
    await h.adapter._on_message(command().raw_message)
    rows = h.adapter._store.list_grants(account_id="usr_agent")
    assert len(rows) == 1
    text = h.adapter._send_owner_text.call_args.args[1]
    prefix = "Now active: Person can now use " if lang == "en" else (
        "已生效：Person 現在可以使用 " if lang == "zh_Hant" else "已生效：Person 现在可以使用 ")
    expected = prefix + selector_display(selector, lang)
    if limited:
        expected += "\n" + HERMES_LIMIT_COPY[lang]
    assert text == expected
    assert GRANT_WRITE_FAILED_MESSAGE[lang] not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("locale,lang", [("zh-CN", "zh"), ("zh-TW", "zh_Hant"), ("en", "en")])
@pytest.mark.parametrize("failure", ["display", "receipt"])
async def test_committed_grant_receipt_failure_reports_success(confirmation, monkeypatch, caplog, locale, lang, failure):
    from clawchat_gateway import tool_capabilities, tool_grants
    h = confirmation
    create_pending(h)
    h.adapter._owner_locale = lambda: locale
    def broken(*_, **__):
        raise ValueError("rendering failed")
    monkeypatch.setattr(tool_capabilities if failure == "display" else tool_grants,
                        "selector_display" if failure == "display" else "build_grant_receipt", broken)
    await h.adapter._on_message(command().raw_message)
    assert len(h.adapter._store.list_grants(account_id="usr_agent")) == 1
    assert h.adapter._send_owner_text.call_args.args[1] == tool_grants.GRANT_APPLIED_MESSAGE[lang]
    assert "receipt rendering failed after successful write" in caplog.text
    await h.adapter._on_message(command().raw_message)
    assert h.adapter._send_owner_text.call_args.args[1] == GRANT_INVALID_CODE_MESSAGE[lang]
