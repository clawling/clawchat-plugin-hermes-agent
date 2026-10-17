import ast
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import config
from clawchat_gateway import tool_grants as grants

NOW = int(datetime.fromisoformat("2026-10-13T12:00:00+00:00").timestamp() * 1000)


def row(user="u", tool="feishu_doc_read", mode="direct", **kwargs):
    return dict(grantee_user_id=user, tool=tool, mode=mode, **kwargs)


def resolve(speakers, rows, owner="owner", now=NOW):
    return grants.resolve_turn_grants(speakers, owner, rows, now)


def test_owner_neutral_and_speakers_deduplicated():
    assert resolve(["owner", "u", " u "], [row()]) == {"feishu_doc_read": "direct"}
    for speakers in ([], ["owner"], ["owner", " "], ["owner", " owner "]):
        assert resolve(speakers, [row("owner")]) == {}


@pytest.mark.parametrize("identity", ["", " ", None, 42, False])
def test_missing_identity_fails_closed(identity):
    assert resolve(["u"], [row()], owner=identity) == {}
    assert resolve([identity, "u"], [row()]) == {}
    assert resolve(["u", identity], [row()]) == {}


def test_intersection_and_strictest_mode():
    rows = [row(), row("v", mode="ask"), row(tool="other")]
    assert resolve(["u", "v"], rows) == {"feishu_doc_read": "ask"}
    assert resolve(["u", "missing"], rows) == {}
    assert resolve(["u"], rows) == {"feishu_doc_read": "direct", "other": "direct"}
    for modes in (("ask", "direct"), ("direct", "ask")):
        assert resolve(["u"], [row(mode=m) for m in modes]) == {"feishu_doc_read": "ask"}


def test_expiration_boundary_and_unlimited():
    rows = [row(tool=tool, expires_at=expiry) for tool, expiry in (
        ("past", NOW - 1), ("edge", NOW), ("future", NOW + 1), ("forever", None),
    )] + [row()]
    assert resolve(["u"], rows) == {"future": "direct", "forever": "direct", "feishu_doc_read": "direct"}
    assert resolve(["u"], [row(expires_at=float("nan"))]) == {}


def test_exact_baseline_names_and_reserved_tools():
    expected = {
        "command": {"terminal", "process", "process_manage"},
        "files": {"read_file", "write_file", "patch", "search_files"},
        "code": {"execute_code"}, "subagent": {"delegate_task"}, "schedule": {"cronjob", "cronjob_manage"},
        "browser": {"browser_back", "browser_cdp", "browser_click", "browser_console", "browser_dialog",
                    "browser_exec", "browser_get_images", "browser_navigate", "browser_press", "browser_scroll",
                    "browser_snapshot", "browser_type", "browser_vault", "browser_vision", "computer_use"},
    }
    assert grants.BASELINE_CATEGORY == {tool: category for category, tools in expected.items() for tool in tools}
    names = list(grants.BASELINE_CATEGORY)
    assert resolve(["u"], [row(tool=t) for t in names + list(grants.GRANT_TOOL_NAMES)]) == dict.fromkeys(names, "direct")
    for tool in names:
        assert grants.is_baseline_tool(tool)
    for tool in ("browser_unknown", "browser_vault_fill"):
        assert grants.is_baseline_tool(tool)
    for tool in ("TERMINAL", "memory", "session_search", "manage_connections", "mcp_exec",
                 "web_search", "web_extract"):
        assert not grants.is_baseline_tool(tool)


def test_errors_fail_closed():
    class BrokenRows:
        def __iter__(self):
            yield row()
            raise RuntimeError("read failure")

    assert resolve(["u"], BrokenRows()) == {}
    for rows in (None, [None], [row(mode="invalid")], [row(tool=[])], [row(), row(tool=42)], [{}]):
        assert resolve(["u"], rows) == {}
    for now in (float("nan"), float("inf"), None, True):
        assert resolve(["u"], [row()], now=now) == {}
    assert resolve(None, [row()]) == {}


def test_snapshot_intersection():
    assert grants.intersect_turn_grants([]) == {}
    assert grants.intersect_turn_grants([{"x": "direct", "terminal": "direct"}, {"x": "ask", "terminal": "direct"}]) == {"x": "ask", "terminal": "direct"}
    assert grants.intersect_turn_grants([{"x": "direct"}, {}]) == {}
    assert grants.intersect_turn_grants([{"x": "invalid"}]) == {}
    assert grants.intersect_turn_grants([{"x": "direct"}, {"x": "invalid"}]) == {}
    assert grants.intersect_turn_grants([{"y": "direct", "x": "direct"}, {"y": "direct", "x": None}]) == {}
    assert grants.intersect_turn_grants([None]) == {}
    assert grants.intersect_turn_grants([dict.fromkeys(grants.GRANT_TOOL_NAMES, "direct")]) == {}


CARD = dict(code="482193", grantees=[dict(id="usr_zhang", name="张三")],
            tools=["feishu_doc_read", "feishu_bitable_list"], mode="ask", label="飞书文档", now=NOW)

ZH = "\n".join([
    "授权确认（码 482193）", "允许：张三（usr_zhang）",
    "使用：feishu_doc_read、feishu_bitable_list（你说的「飞书文档」）", "方式：每次问我", "到期：不限", "想设期限就说「……到周五」。",
    "这意味着：张三能让我代为调用这些工具，你用这些工具能看到、能做到的，张三都能。",
    "这次按工具授权，不保证只限某份文档或某个资源。",
    "这类操作每次都会先问你。", "确认请点：`/clawchat-allow 482193`（10 分钟内有效；不确认就不会生效）",
])
ZH_HANT = "\n".join([
    "授權確認（碼 482193）", "允許：张三（usr_zhang）",
    "使用：feishu_doc_read、feishu_bitable_list（你說的「飞书文档」）", "方式：每次問我", "到期：不限", "想設期限就說「……到週五」。",
    "這意味著：张三能讓我代為呼叫這些工具，你用這些工具能看到、能做到的，张三都能。",
    "這次按工具授權，不保證只限某份文件或某個資源。",
    "這類操作每次都會先問你。", "確認請點：`/clawchat-allow 482193`（10 分鐘內有效；不確認就不會生效）",
])
EN = "\n".join([
    "Grant confirmation (code 482193)", "Allow: 张三 (usr_zhang)",
    "Use: feishu_doc_read, feishu_bitable_list (you called it “飞书文档”)", "Mode: Ask me each time", "Expires: No limit", 'To set an expiry, say “…until Friday.”',
    "This means: 张三 can have me call these tools for them. Whatever you can see or do with these tools, they can too.",
    "This grants access by tool; it does not guarantee access is limited to one document or resource.",
    "These operations will ask you first every time.",
    "Click to confirm: `/clawchat-allow 482193` (valid for 10 minutes; it will not take effect without confirmation)",
])


@pytest.mark.parametrize("lang,expected", [("zh", ZH), ("zh_Hant", ZH_HANT), ("en", EN), ("ja", EN), ("es", EN)])
def test_card_copy(lang, expected):
    assert grants.build_grant_card(**CARD, lang=lang) == expected


@pytest.mark.parametrize("lang,use,direct,ask,separator", [
    ("zh", "使用：", "直接用：", "每次先问你：", "、"),
    ("zh_Hant", "使用：", "直接用：", "每次先問你：", "、"),
    ("en", "Use: ", "Use directly: ", "Ask you each time: ", ", "),
])
def test_mixed_modes_list_actual_tools(lang, use, direct, ask, separator):
    tools = ["feishu_doc_read", "read_file", "feishu_bitable_list", "tool:terminal"]
    card = grants.build_grant_card(**(CARD | dict(tools=tools, mode="direct", label=None)), lang=lang)
    assert use + separator.join([*tools[:3], "terminal"]) + "\n" in card
    assert ("Mode: Use directly" if lang == "en" else "方式：直接用") in card
    assert ("Only allow people you trust completely." if lang == "en" else
            "只給你完全信任的人。" if lang == "zh_Hant" else "只给你完全信任的人。") in card
    assert "每次都会先问你" not in card and "Ask me each time" not in card
    all_ask = grants.build_grant_card(**(CARD | dict(tools=tools)), lang=lang)
    assert "Only allow people you trust completely." not in all_ask


def test_direct_baseline_people_and_expiry():
    direct = grants.build_grant_card(**(CARD | dict(mode="direct", label=None)), lang="zh")
    assert "方式：直接用" in direct and "每次都会先问你" not in direct and "你说的" not in direct
    expiry = int(datetime.fromisoformat("2026-10-21T06:59:00+00:00").timestamp() * 1000)
    baseline = grants.build_grant_card(**(CARD | dict(tools=["terminal"], mode="direct", expires_at=expiry,
        grantees=CARD["grantees"] + [dict(id="usr_agent", name="另一个 Agent")])), lang="zh")
    assert "允许：张三（usr_zhang）、另一个 Agent（usr_agent）" in baseline
    assert "方式：直接用" in baseline and "只给你完全信任的人。" in baseline
    assert "到期：2026-10-21 06:59 (UTC)" in baseline
    zoned = grants.build_grant_card(**CARD, lang="zh", expires_at=expiry, time_zone="America/Los_Angeles")
    assert "到期：2026-10-20 23:59\n" in zoned


@pytest.mark.parametrize("name", [None, "", " ", "\u200b\u202e", "\n\t"])
def test_missing_any_name_returns_no_card(name):
    people = CARD["grantees"] + [dict(id="usr_missing", name=name)]
    assert grants.build_grant_card(**(CARD | dict(grantees=people)), lang="zh") is None
    assert grants.build_grant_card(**(CARD | dict(grantees=[dict(id="usr_missing")])), lang="en") is None


@pytest.mark.parametrize("changes", [dict(code="4821"), dict(code="１２３４５６"), dict(tools=[grants.GRANT_TOOL_NAMES[0]]),
                                   dict(now=float("nan")), dict(expires_at=float("nan")), dict(mode="invalid")])
def test_invalid_card(changes):
    with pytest.raises(ValueError):
        grants.build_grant_card(**(CARD | changes), lang="zh")


def test_clean_text_and_receipt_substitution():
    dirty = grants.build_grant_card(**(CARD | dict(grantees=[dict(id="usr_\u202ex", name="张\n三\u200b")],
        label="飞书\0\t文档", tools=["feishu_doc_read"])), lang="zh")
    assert "张 三（usr_ x）" in dirty and "feishu_doc_read（你说的「飞书 文档」）" in dirty
    assert not any(c in dirty for c in "\0\u200b\u202e")
    assert grants.build_grant_receipt(lang="zh", names=["张三"], tools=["x", "y"]) == "已生效：张三 现在可以使用 x、y"
    assert grants.build_grant_receipt(lang="zh_Hant", names=["{tools}"], tools=["x"]) == "已生效：{tools} 現在可以使用 x"
    assert grants.build_grant_receipt(lang="ja", names=["A\nB"], tools=["x", "y"]) == "Now active: A B can now use x, y"
    assert grants.GRANT_WRITE_FAILED_MESSAGE == {"zh": "没写进去，请稍后再说一次。", "zh_Hant": "沒寫進去，請稍後再說一次。", "en": "It was not saved. Please ask again later."}
    assert grants.GRANT_INVALID_CODE_MESSAGE == {"zh": "这个确认码无效或已过期。", "zh_Hant": "這個確認碼無效或已過期。", "en": "This confirmation code is invalid or has expired."}


def test_approved_hints_verbatim_and_no_disclosure():
    messages = [grants.NON_OWNER_GRANTS_HINT, grants.GRANT_BLOCK_MESSAGE,
                grants.GRANT_INTERSECTION_BLOCK_MESSAGE, grants.GRANT_ASK_DENIED_MESSAGE]
    assert messages == ["Explain only the requested ability in the conversation's language. For missing access, tell them the owner can allow it in their direct chat. For a mixed-turn block, an allowed person can ask again alone. Do not invent reasons or reveal others' access, settings, paths, or versions.", 'This tool is not available to the caller yet. Do not try another way. Explain that the owner can allow this ability in their direct chat.', 'Someone in this turn is not allowed to use this tool, so it did not run. Do not retry. Say that an allowed person can call you again alone.', 'The owner did not approve this call or has not answered yet, so it did not run. Do not retry this request. They can try later or ask the owner directly.']
    for message in messages + list(grants.GRANT_WRITE_FAILED_MESSAGE.values()) + list(grants.GRANT_INVALID_CODE_MESSAGE.values()):
        for forbidden in ("张三", "李四", "Alice", "Bob", "tool-grants", "non-owner-host-tools", "approvals.plugin",
                          "/home/", "/tmp/", "config.yaml", "clawchat.sqlite", "0.21.0", "2026.9.8",
                          "feishu_doc_read", *grants.BASELINE_CATEGORY, *grants.GRANT_TOOL_NAMES):
            assert forbidden not in message


def test_code_randomness_and_command(monkeypatch):
    calls = []
    values = iter([0, 999999])

    def randbelow(limit):
        calls.append(limit)
        return next(values)

    monkeypatch.setattr(grants.secrets, "randbelow", randbelow)
    assert grants.generate_grant_code() == "000000"
    assert grants.generate_grant_code() == "999999"
    assert calls == [1000000, 1000000]
    assert grants.GRANT_CONFIRM_COMMAND.fullmatch("  /clawchat-allow 000123  ".strip())[1] == "000123"
    for text in ("/clawchat-allow 12345", "/clawchat-allow 1234567", "/clawchat-allow abcdef", "x /clawchat-allow 123456",
                 "/clawchat-allow 123456 x", "/clawchat-allow 123456\n", "/clawchat-allow １２３４５６"):
        assert not grants.GRANT_CONFIRM_COMMAND.fullmatch(text)
    assert grants.PENDING_TTL_MS == 600000
    assert grants.TOOL_GRANTS_CONFIG_KEY == "tool-grants"
    assert grants.GRANT_TOOL_NAMES == ("clawchat_tool_grant", "clawchat_tool_revoke", "clawchat_tool_grants_list")


PARAMS = dict(userIds=["usr_u"], tools=["feishu_doc_read"], mode="direct")


def test_grant_params_normalize_without_profile_lookup():
    assert grants.validate_grant_params(PARAMS | dict(userIds=["usr_u", "usr_u"], tools=["x", "x"]), NOW) == dict(user_ids=["usr_u"], tools=["tool:x"], mode="direct")
    for body in ("0" * 26, "a" * 26, "A" * 26):
        assert grants.validate_grant_params(dict(groupId="cnv_" + body, tools=["x"], mode="ask"), NOW)["group_id"] == "cnv_" + body
    result = grants.validate_grant_params(PARAMS | dict(expiresAt="2026-10-20T23:59:00+08:00", label="文" * 40), NOW)
    assert result["expires_at"] == datetime.fromisoformat("2026-10-20T15:59:00+00:00").timestamp() * 1000
    assert result["label"] == "文" * 40
    assert grants.validate_grant_params(PARAMS | dict(expiresAt="2026-10-20"), NOW)["expires_at"] > NOW


@pytest.mark.parametrize("params", [None, {}, PARAMS | dict(groupId="cnv_" + "0" * 26),
    dict(tools=["x"], mode="direct"), PARAMS | dict(userIds=[]), PARAMS | dict(userIds=["u"]),
    PARAMS | dict(userIds=[None]), PARAMS | dict(userIds=["usr_ x"]), PARAMS | dict(userIds="usr_u"),
    dict(groupId="cnv_x", tools=["x"], mode="ask"), dict(groupId="cnv_" + "i" * 26, tools=["x"], mode="ask"),
    PARAMS | dict(tools=[]), PARAMS | dict(tools=[None]), PARAMS | dict(tools=["x y"]),
    PARAMS | dict(tools=["x*"]), PARAMS | dict(tools=["x?"]), PARAMS | dict(mode="allow"),
    PARAMS | dict(expiresAt="2026-10-13T12:00:00Z"), PARAMS | dict(expiresAt="2026-02-30"),
    PARAMS | dict(expiresAt="2026-10-20T23:59:00"),
    PARAMS | dict(expiresAt="October 20, 2026"), PARAMS | dict(expiresAt=NOW + 1), PARAMS | dict(expiresAt=None),
    PARAMS | dict(label="文" * 41), PARAMS | dict(label=None), PARAMS | dict(extra="ignored"),
    *[PARAMS | dict(tools=[tool]) for tool in grants.GRANT_TOOL_NAMES],
])
def test_grant_params_reject_invalid(params):
    with pytest.raises(ValueError):
        grants.validate_grant_params(params, NOW)


@pytest.mark.parametrize("value,expected", [(None, "on"), ("on", "on"), ("off", "off"), (" OFF ", "off"),
                                          ("unknown", "on"), ([], "on"), (False, "off"), (True, "on")])
def test_config_default_off_and_unknown(monkeypatch, value, expected):
    from clawchat_gateway import memory_scope

    monkeypatch.setattr(config, "_get_env", lambda *args: "")
    extra = {} if value is None else {"tool-grants": value}
    monkeypatch.setattr(memory_scope, "_config_extra", lambda: extra)
    # Isolate credential reads performed by the platform config factory.
    from clawchat_gateway import storage
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: SimpleNamespace(get_revoked_refresh_fingerprint=lambda **kw: None))
    assert config.tool_grants() == expected
    assert config.ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra)).tool_grants == expected
    assert config.ClawChatConfig(websocket_url="").tool_grants == "on"


def test_config_read_error_uses_default(monkeypatch):
    from clawchat_gateway import memory_scope

    def broken():
        raise OSError("config unavailable")

    monkeypatch.setattr(memory_scope, "_config_extra", broken)
    assert config.tool_grants() == "on"


@pytest.mark.parametrize("value,expected", [("on", "on"), ("off", "off"), ('"off"', "off"), ("typo", "on")])
def test_config_reads_profile_yaml_extra(monkeypatch, tmp_path, value, expected):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Config writes belong to Hermes; optional string switches live in extra.
    path = tmp_path / "config.yaml"
    path.write_text(f"platforms:\n  clawchat:\n    extra:\n      tool-grants: {value}\n", encoding="utf-8")
    assert config.tool_grants() == expected


def test_logic_module_has_only_standard_library_imports():
    tree = ast.parse(Path(grants.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in {"math", "re", "secrets"} for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0 and node.module in {"__future__", "datetime", "typing", "zoneinfo", "collections.abc", "dataclasses", "clawchat_gateway.tool_capabilities"}
