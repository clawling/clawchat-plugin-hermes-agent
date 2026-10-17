"""Selector, migration, and execution regressions for capability grants."""
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
import json
import sqlite3
import sys
import time

import pytest

from clawchat_gateway import host_tools_guard as guard, tool_grants as grants, turn_grants as turns
from clawchat_gateway import storage, tools
from clawchat_gateway.tool_capabilities import (
    AUTHORIZATION_TOOLS, TOOL_CAPABILITIES, CAPABILITY_TOOL_NAMES,
    parse_grant_selector, normalize_grant_selectors, selector_display,
    is_sensitive_tool, selector_is_sensitive, resolve_cross_chat_gate, plan_deny_removal,
)
from test_tool_grants_hook import host, context, call
from test_tool_grants_ask import approval
from test_tool_grants_tools import management, PARAMS
from test_tool_grants_admission import harness, message

NOW = 1_800_000_000_000


def row(what="all", mode="direct", user="usr_alice", kind="all", **kw):
    return dict(grantee_user_id=user, grantee_name="Person", what_kind=kind, what=what, mode=mode, **kw)


def resolve(rows=(), speakers=("usr_alice",), **kw):
    return grants.resolve_turn_grants(list(speakers), "usr_owner", list(rows), NOW, **kw)


@pytest.mark.parametrize("value,expected", [
    ("tool:terminal", ("tool", "terminal")), ("terminal", ("capability", "terminal")),
    ("cap:terminal", ("capability", "terminal")), ("read_file", ("tool", "read_file")),
    ("tool:Provider:Read", ("tool", "Provider:Read")), ("tool:all", ("tool", "all")),
    ("all", ("all", "all")), ("Terminal", ("tool", "Terminal")),
])
def test_selector_reserved_words_and_case(value, expected):
    assert parse_grant_selector(value) == expected


@pytest.mark.parametrize("value", [None, "", " terminal", "terminal ", "cap:missing", "cap:all", "tool:",
    "group:terminal", "tool:group:terminal", "a*", "a?", "x\n", "x\0", "tool:clawchat_tool_request",
    *AUTHORIZATION_TOOLS])
def test_invalid_selector_is_atomic(value):
    with pytest.raises(ValueError, match="^invalid-what$"):
        normalize_grant_selectors(["files", value, "all"])


def test_normalize_deduplicates_and_checks_unknown_vs_unavailable():
    known = {"terminal", "read_file", "process", "all", "Provider:Read"}
    available = {"terminal", "read_file"}
    assert normalize_grant_selectors(["files", "cap:files", "read_file", "tool:read_file", "all", "all"],
                                    known_tools=known, available_tools=available) == ["cap:files", "tool:read_file", "all"]
    for value, reason in [("missing", "invalid-what"), ("tool:all", "not-available"),
                          ("tool:process", "not-available"), ("browser", "not-available")]:
        with pytest.raises(ValueError, match="^" + reason + "$"):
            normalize_grant_selectors(["all", value], known_tools=known, available_tools=available)
    assert normalize_grant_selectors(["tool:removed"], known_tools=known, available_tools=available,
                                     stored=["tool:removed"]) == ["tool:removed"]


@pytest.mark.parametrize("id_,names,sensitive", [
    ("terminal", ("终端", "終端", "Terminal"), True),
    ("files", ("文件读写", "檔案讀寫", "Files"), True),
    ("code", ("代码执行", "程式碼執行", "Code execution"), True),
    ("subagent", ("子 Agent", "子 Agent", "Sub-agents"), True),
    ("schedule", ("定时任务", "定時任務", "Scheduled tasks"), True),
    ("browser", ("浏览器与桌面控制", "瀏覽器與桌面控制", "Browser and desktop control"), True),
    ("cross-chat", ("跨会话发送", "跨會話傳送", "Cross-chat messaging"), False),
    ("memory", ("宿主记忆", "宿主記憶", "Host memory"), False),
    ("session-search", ("会话检索", "會話檢索", "Session search"), False),
    ("feishu-docs", ("飞书文档", "飛書文件", "Feishu documents"), False),
    ("connections", ("连接管理", "連線管理", "Connections"), False),
    ("web", ("网页搜索与读取", "網頁搜尋與讀取", "Web search and reading"), False),
])
def test_shared_names_and_sensitivity(id_, names, sensitive):
    assert tuple(selector_display("cap:" + id_, lang) for lang in ("zh", "zh_Hant", "en")) == names
    assert selector_display(id_, "fr") == names[2]
    assert TOOL_CAPABILITIES[id_]["sensitive"] is sensitive
    assert selector_is_sensitive(id_) is sensitive


def test_priority_expiry_and_exact_override_of_capability_deny():
    rules = [row(), row("terminal", "deny", kind="capability")]
    result = resolve(rules)
    assert result.get("terminal") == result.get("process_manage") == "deny"
    result = resolve([*rules, row("process_manage", kind="tool")])
    assert result.get("terminal") == result.get("process") == "deny"
    assert result.get("process_manage") == "direct"
    assert result.get("read_file") == result.get("unclassified_future") == "direct"
    assert resolve([*rules, row("process_manage", kind="tool", expires_at=NOW)]).get("process_manage") == "deny"
    assert resolve([*rules, row("process_manage", kind="tool", expires_at=NOW+1)]).resolve("process_manage", now=NOW+1) == "deny"


@pytest.mark.parametrize("modes,expected", [(("direct", "ask"), "ask"), (("ask", "deny"), "deny"),
                                         (("deny", "direct"), "deny")])
def test_same_priority_uses_strictest_mode(modes, expected):
    for order in (modes, modes[::-1]):
        assert resolve([row("files", mode, kind="capability") for mode in order]).get("read_file") == expected


@pytest.mark.parametrize("second,expected,reason", [("deny", "deny", "batch-intersection"),
    (None, None, "batch-intersection"), ("ask", "ask", None), ("direct", "direct", None)])
def test_multi_person_intersection_and_reason(second, expected, reason):
    rows = [row()] + ([row(mode=second, user="usr_bob")] if second else [])
    result = resolve(rows, ("usr_owner", "usr_alice", "usr_bob"))
    assert result.get("terminal") == expected
    assert grants.grant_failure_code(result, "terminal") == reason
    assert result.non_owner_speakers == {"usr_alice", "usr_bob"}
    assert grants.grant_failure_code(result, "terminal", available=False) == "not-available"


@pytest.mark.parametrize("mode,reason", [("deny", "denied"), (None, "not-granted")])
def test_owner_plus_one_guest_is_single_person_reason(mode, reason):
    result = resolve([row(mode=mode)] if mode else [], ("usr_owner", "usr_alice"))
    assert grants.grant_failure_code(result, "terminal") == reason


def test_approve_default_is_per_tool_and_never_replaces_deny_or_read_failure():
    result = resolve([row("feishu-docs", kind="capability"), row("terminal", "deny", kind="capability")],
                     unmatched="ask", public_tools={"web_search"})
    assert result.get("feishu_doc_read") == "direct"
    assert result.get("read_file") == "ask"
    assert result.get("terminal") == "deny"
    assert result.get("web_search") == "direct"
    failed = grants.resolve_turn_grants(["usr_alice"], "usr_owner", None, NOW,
                                        unmatched="ask", public_tools={"web_search"})
    assert failed.get("terminal") is None and failed.get("web_search") == "direct"
    assert resolve([], (), unmatched="ask").get("terminal") is None


def test_all_excludes_authorization_and_prefixes_never_authorize():
    result = resolve([row()])
    assert all(result.get(tool) is None for tool in AUTHORIZATION_TOOLS)
    assert is_sensitive_tool("browser_third_party")
    assert resolve([row("browser", kind="capability")]).get("browser_third_party") is None
    assert resolve([row("subagent", kind="capability")]).get("kanban_third_party") is None
    assert "clawchat_memory_read" not in TOOL_CAPABILITIES["memory"]["tools"]
    assert resolve_cross_chat_gate({}, "direct") == "legacy"
    assert plan_deny_removal([row(mode="deny")]) == "immediate"


@contextmanager
def bound_rules(rows, *, speakers=("usr_alice",), public=(), unmatched="none", available=()):
    result = resolve(rows, speakers, public_tools=public, unmatched=unmatched, available_tools=available)
    snapshot = turns.register_turn(account_id="usr_agent", chat_id="cnv_rules", speakers=speakers,
                                   grants=result, session_key="rules")
    turns.bind_turn(snapshot.nonce, "s", "t")
    try:
        yield snapshot
    finally:
        turns.release_turn(snapshot.nonce)


@pytest.mark.parametrize("tool", ["terminal", "process_manage", "read_file", "write_file", "patch", "search_files", "execute_code"])
def test_sensitive_direct_executes_with_local_arguments(host, tool):
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call(tool, turn_id="t", args={"path": "/tmp/fixture", "command": "cat /tmp/fixture"}) is None


@pytest.mark.parametrize("policy", ["off", "approve"])
def test_deny_precedes_default_public_access_and_group_approve(host, policy, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": policy})
    rows = [row("web_search", "deny", kind="tool"), row("terminal", "deny", kind="capability")]
    with bound_rules(rows, public={"web_search"}, unmatched="ask") as snapshot, host(**context(snapshot, group=True)):
        for tool in ("web_search", "terminal"):
            assert call(tool, turn_id="t") == {"action": "block", "reasonCode": "denied", "message": grants.GRANT_DENIED_MESSAGE}


def test_group_approve_asks_for_another_unmatched_tool(host, approval):
    with bound_rules([row("feishu-docs", kind="capability")], unmatched="ask") as snapshot, host(**context(snapshot, group=True)):
        assert call("terminal", turn_id="t")["action"] == "approve"
        assert call("feishu_doc_read", turn_id="t") is None


def test_unavailable_host_tool_precedes_all_permission(host, monkeypatch):
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: ({"terminal"}, frozenset()))
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call("terminal", turn_id="t") == {"action": "block", "reasonCode": "not-available", "message": grants.GRANT_NOT_AVAILABLE_MESSAGE}


def test_all_dispatch_uses_unnarrowed_surface_and_inner_tool_checks(host, monkeypatch):
    groups = {"terminal": {"terminal", "process_manage"}, "web": {"web_search"},
              "file": {"read_file"}, "clawchat-grants": set(AUTHORIZATION_TOOLS)}
    available = frozenset().union(*groups.values())
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: set(groups))
    monkeypatch.setattr(guard, "_resolve_tools", lambda group: list(groups.get(group, ())))
    monkeypatch.setattr(guard, "_registry_toolset", lambda tool: next((group for group, members in groups.items() if tool in members), ""))
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (available, available))
    monkeypatch.setattr(guard, "_narrowed_toolsets", lambda: ["web", guard.NO_MCP])
    rows = [row(), row("terminal", "deny", kind="capability"), row("process_manage", kind="tool")]
    with bound_rules(rows, available=available) as snapshot:
        source = SimpleNamespace(chat_id=snapshot.chat_id, chat_type="dm", user_id="usr_alice", message_id=snapshot.nonce)
        offered = guard.toolsets_for_source(source, owner_user_id="usr_owner")
        assert {"terminal", "file", "web"} <= set(offered)
        assert "clawchat-grants" not in offered
        with host(**context(snapshot)):
            assert call("process_manage", turn_id="t") is None
            assert call("terminal", turn_id="t")["reasonCode"] == "denied"
            assert call("read_file", turn_id="t", args={"path": "/tmp/fixture"}) is None
            for tool in AUTHORIZATION_TOOLS:
                assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.parametrize("allowed", [False, True])
def test_kanban_is_subagent_capability_at_dispatch_and_execution(host, monkeypatch, allowed):
    members = frozenset(tool for tool in TOOL_CAPABILITIES["subagent"]["tools"] if tool.startswith("kanban_"))
    assert len(members) == 15
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"web", "kanban", "delegation"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda group: list(members) if group == "kanban" else
                        ["delegate_task"] if group == "delegation" else ["web_search"])
    monkeypatch.setattr(guard, "_registry_toolset", lambda tool: "kanban" if tool in members else
                        "delegation" if tool == "delegate_task" else "web")
    with bound_rules([row("subagent", kind="capability")] if allowed else []) as snapshot:
        source = SimpleNamespace(chat_id=snapshot.chat_id, chat_type="dm", user_id="usr_alice", message_id=snapshot.nonce)
        assert ("kanban" in guard.toolsets_for_source(source, owner_user_id="usr_owner")) is allowed
        with host(**context(snapshot)):
            for tool in members:
                result = call(tool, turn_id="t")
                assert result is None if allowed else result["reasonCode"] == "not-granted"


@pytest.mark.parametrize("order", ["owner-first", "guest-first"])
def test_owner_empty_snapshot_is_neutral_and_release_cannot_widen_overlap(order):
    def owner():
        return turns.register_turn(account_id="usr_agent", chat_id="cnv_overlap", speakers=("usr_owner",),
                                   grants={}, session_key="overlap", owner_side=True)
    def guest():
        return turns.register_turn(account_id="usr_agent", chat_id="cnv_overlap", speakers=("usr_alice",),
                                   grants=resolve([row()]), session_key="overlap")
    first = owner() if order == "owner-first" else guest()
    second = guest() if order == "owner-first" else owner()
    third = None
    try:
        for snapshot in (first, second):
            turns.bind_turn(snapshot.nonce, "s", snapshot.nonce)
            assert turns.lookup_for_tool_call(snapshot.nonce, snapshot.chat_id, "s", snapshot.nonce).get("terminal") == "direct"
            assert turns.get_snapshot(snapshot.nonce).non_owner_speakers == {"usr_alice"}
        third = turns.register_turn(account_id="usr_agent", chat_id="cnv_overlap", speakers=("usr_bob",),
                                    grants=resolve([], ("usr_bob",)), session_key="overlap")
        turns.release_turn(third.nonce)
        for snapshot in (first, second):
            effective = turns.lookup_for_tool_call(snapshot.nonce, snapshot.chat_id, "s", snapshot.nonce)
            assert effective.get("terminal") is None
            assert grants.grant_failure_code(effective, "terminal") == "batch-intersection"
    finally:
        for snapshot in (first, second, third):
            if snapshot:
                turns.release_turn(snapshot.nonce)


def old_database(path, *, prohibited=False):
    with sqlite3.connect(path) as conn:
        for version, name, sql in storage.MIGRATIONS[:13]:
            conn.executescript(sql)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, NOW))
            conn.commit()
        for tool, mode in [("terminal", "ask"), ("read_file", "direct")]:
            conn.execute("INSERT INTO tool_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("usr_agent", "usr_alice", "Person", tool, mode, "label", NOW+100, NOW-1))
        if prohibited:
            conn.execute("INSERT INTO tool_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("usr_agent", "usr_alice", "Person", "clawchat_tool_request", "ask", None, None, NOW))
        conn.execute("INSERT INTO tool_grant_pending VALUES (?, ?, ?, ?, ?, NULL)",
                     ("usr_agent", "000123", json.dumps(dict(grantees=[dict(id="usr_alice", name="Person")],
                      tools=["terminal"], mode="ask")), NOW, NOW+100))


def test_migration_preserves_every_field_old_ask_and_pending_version(tmp_path):
    path = tmp_path / "old.sqlite"
    old_database(path)
    store = storage.ClawChatStore(path)
    store.initialize()
    assert not store._disabled
    rows = store.list_grants(account_id="usr_agent", now=NOW)
    terminal = next(row for row in rows if row["what"] == "terminal")
    assert terminal == dict(account_id="usr_agent", grantee_user_id="usr_alice", grantee_name="Person",
                           what_kind="tool", what="terminal", mode="ask", label="label", expires_at=NOW+100, created_at=NOW-1)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT purpose, payload_version FROM tool_grant_pending").fetchone() == ("grant", 1)
        assert conn.execute("SELECT count(*) FROM tool_grants").fetchone()[0] == 2
    confirmed = store.confirm_pending_grant(account_id="usr_agent", code="000123", now=NOW+1)
    assert confirmed[0]["what_kind"] == "tool" and confirmed[0]["what"] == "terminal" and confirmed[0]["mode"] == "ask"
    assert resolve(confirmed).get("process_manage") is None


@pytest.mark.parametrize("failure", ["copy-count", "constraint", "pending-column"])
def test_migration_copy_validation_and_failure_roll_back(tmp_path, monkeypatch, failure):
    path = tmp_path / "rollback.sqlite"
    old_database(path, prohibited=failure == "constraint")
    sql = storage.TOOL_GRANTS_V2_SCHEMA
    if failure == "copy-count":
        sql = sql.replace("FROM tool_grants;", "FROM tool_grants WHERE 0;")
    elif failure == "pending-column":
        sql += "SELECT missing_column FROM tool_grant_pending;"
    monkeypatch.setattr(storage, "MIGRATIONS", [*storage.MIGRATIONS[:13], (14, "tool_grants_v2", sql)])
    store = storage.ClawChatStore(path)
    store.initialize()
    assert store._disabled
    with sqlite3.connect(path) as conn:
        assert "tool" in [row[1] for row in conn.execute("PRAGMA table_info(tool_grants)")]
        assert conn.execute("SELECT tool, mode FROM tool_grants WHERE tool = 'terminal'").fetchone() == ("terminal", "ask")
        assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version = 14").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'tool_grants_v2'").fetchone()[0] == 0
        assert "payload_version" not in [row[1] for row in conn.execute("PRAGMA table_info(tool_grant_pending)")]
    with pytest.raises(RuntimeError):
        store.apply_grant(account_id="usr_agent", payload=dict(grantees=[dict(id="usr_alice", name="Person")], tools=["all"], mode="deny"))


def test_pending_v2_and_once_only_ask_confirmation_does_not_downgrade(tmp_path):
    store = storage.ClawChatStore(tmp_path / "pending.sqlite")
    payload = dict(grantees=[dict(id="usr_alice", name="Person")], tools=["terminal"], mode="ask")
    store.create_pending_grant(account_id="usr_agent", code="000234", payload=payload, now=NOW)
    with pytest.raises(ValueError, match="ask-unavailable"):
        store.confirm_pending_grant(account_id="usr_agent", code="000234", now=NOW+1, ask_available=False)
    assert store.list_grants(account_id="usr_agent", now=NOW+1) == []
    confirmed = store.confirm_pending_grant(account_id="usr_agent", code="000234", now=NOW+2)
    assert confirmed[0]["what_kind"] == "capability" and confirmed[0]["mode"] == "ask"
    assert store.confirm_pending_grant(account_id="usr_agent", code="000234", now=NOW+3) is None


@pytest.mark.asyncio
async def test_deny_management_writes_immediately_lists_camel_case_and_revokes(management):
    h = management
    response = await tools.manage_tool_grants("grant", {**PARAMS, "tools": ["all", "terminal", "cap:terminal"], "mode": "deny"}, session_id="s")
    assert response == {"applied": True, "mode": "deny", "count": 2}
    h.adapter._send_owner_text.assert_not_awaited()
    listed = await tools.manage_tool_grants("list", {}, session_id="s")
    assert len(listed["grants"]) == 2
    assert all(set(item) == {"granteeUserId", "granteeName", "whatKind", "what", "mode", "expiresAt"}
               and item["mode"] == "deny" for item in listed["grants"])
    assert await tools.manage_tool_grants("revoke", dict(userId="usr_a", tools=["terminal"]), session_id="s") == {"revoked": 1}
    assert await tools.manage_tool_grants("revoke", dict(userId="usr_a"), session_id="s") == {"revoked": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("known,available,selector,reason", [
    ({"terminal"}, {"terminal"}, "missing", "invalid-what"),
    ({"terminal"}, set(), "tool:terminal", "not-available"),
    ({"terminal"}, set(), "terminal", "not-available"),
])
async def test_management_invalid_or_disabled_never_writes_pending_or_sends(management, monkeypatch, known, available, selector, reason):
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (known, available))
    result = await tools.manage_tool_grants("grant", {**PARAMS, "tools": ["all", selector]}, session_id="s")
    assert result["error"] == "validation" and result["message"] == reason
    management.adapter._send_owner_text.assert_not_awaited()
    with sqlite3.connect(management.adapter._store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM tool_grant_pending").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_deny_write_failure_is_atomic_and_not_reported_as_success(management, monkeypatch):
    store = management.adapter._store
    original = store._upsert_tool_grant
    def fail(conn, item):
        original(conn, item)
        raise sqlite3.OperationalError("fixture write failure")
    monkeypatch.setattr(store, "_upsert_tool_grant", fail)
    result = await tools.manage_tool_grants("grant", {**PARAMS, "mode": "deny"}, session_id="s")
    assert result["error"] == "grant" and "applied" not in result
    assert store.list_grants(account_id="usr_agent") == []


@pytest.mark.asyncio
async def test_revoke_can_match_removed_and_expired_exact_objects(management, monkeypatch):
    store = management.adapter._store
    now = int(time.time() * 1000)
    store.create_pending_grant(account_id="usr_agent", code="000345", now=now-100,
        payload=dict(grantees=[dict(id="usr_a", name="Person")], tools=["tool:removed"], mode="direct", expires_at=now-1))
    store.confirm_pending_grant(account_id="usr_agent", code="000345", now=now-99)
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (set(), set()))
    assert await tools.manage_tool_grants("revoke", dict(userId="usr_a", tools=["removed"]), session_id="s") == {"revoked": 1}


@pytest.mark.asyncio
async def test_owner_only_group_is_full_but_management_is_still_private(harness, host):
    h = harness
    h.rows.append(row("terminal", "deny", kind="capability"))
    inbound = message("usr_owner", kind="group")
    await h.adapter._handle_inbound(inbound)
    snapshot = turns.get_snapshot(h.events[-1].source.message_id)
    assert snapshot.owner_side and not snapshot.owner_verified
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        assert call("terminal", turn_id="t") is None
        assert call("clawchat_mention_message", turn_id="t", args={"chatId": "cnv_elsewhere"}) is None
        for tool in AUTHORIZATION_TOOLS:
            assert call(tool, turn_id="t")["action"] == "block"


def test_hint_lists_only_available_blocked_capabilities():
    result = resolve([row("files", kind="capability")])
    hint = grants.build_non_owner_grants_hint(result, {"read_file", "terminal", "unknown_mcp"})
    assert hint == "Not available to the caller yet: Terminal (terminal). " + grants.NON_OWNER_GRANTS_HINT
    assert grants.build_non_owner_grants_hint(result, {"read_file"}) == grants.NON_OWNER_GRANTS_HINT


def test_host_catalog_uses_platform_policy_not_last_narrowed_catalog(monkeypatch):
    registry = SimpleNamespace(get_all_tool_names=lambda: ["terminal", "read_file", "web_search"])
    registry_module = ModuleType("tools.registry")
    registry_module.registry = registry
    model_tools = ModuleType("model_tools")
    calls = []
    def definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False, skip_tool_search_assembly=False):
        calls.append((enabled_toolsets, disabled_toolsets, quiet_mode, skip_tool_search_assembly))
        return [{"function": {"name": name}} for name in ("terminal", "web_search")]
    model_tools.get_tool_definitions = definitions
    model_tools._last_resolved_tool_names = ["web_search"]
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)
    monkeypatch.setitem(sys.modules, "model_tools", model_tools)
    monkeypatch.setattr(guard, "_host_config", lambda: {"agent": {"disabled_toolsets": ["file"]}})
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"terminal", "file", "web"})
    monkeypatch.setattr(guard, "_narrowed_toolsets", lambda: (_ for _ in ()).throw(AssertionError("narrowed catalog")))
    known, available = guard.host_tool_catalog()
    assert known == {"terminal", "read_file", "web_search"} and available == {"terminal", "web_search"}
    assert calls == [(["file", "terminal", "web"], ["file"], True, True)]


ALL_CARDS = {('zh', 'direct'): '授权确认（码 000123）\n'
                   '允许：Person（usr_alice）\n'
                   '使用：all\n'
                   '方式：直接用\n'
                   '到期：不限\n'
                   '想设期限就说「……到周五」。\n'
                   '这意味着：Person能让我代为调用这些工具，你用这些工具能看到、能做到的，Person都能。\n'
                   '这次按工具授权，不保证只限某份文档或某个资源。\n'
                   '包括以后新装的工具。\n'
                   '包括敏感工具。\n'
                   '不包括授权、收回、列出授权和申请授权这四个工具。\n'
                   'Person能在这台机器上跑任何命令、读写任何文件，包括这个 Agent 的登录凭据、你的模型 key 和这张授权列表。拿到凭据的人可以冒充这个 Agent '
                   '跟别人说话。只给你完全信任的人。\n'
                   '确认请点：`/clawchat-allow 000123`（10 分钟内有效；不确认就不会生效）',
 ('zh', 'ask'): '授权确认（码 000123）\n'
                '允许：Person（usr_alice）\n'
                '使用：all\n'
                '方式：每次问我\n'
                '到期：不限\n'
                '想设期限就说「……到周五」。\n'
                '这意味着：Person能让我代为调用这些工具，你用这些工具能看到、能做到的，Person都能。\n'
                '这次按工具授权，不保证只限某份文档或某个资源。\n'
                '包括以后新装的工具。\n'
                '包括敏感工具。\n'
                '不包括授权、收回、列出授权和申请授权这四个工具。\n'
                '这类操作每次都会先问你。\n'
                '确认请点：`/clawchat-allow 000123`（10 分钟内有效；不确认就不会生效）',
 ('zh_Hant', 'direct'): '授權確認（碼 000123）\n'
                        '允許：Person（usr_alice）\n'
                        '使用：all\n'
                        '方式：直接用\n'
                        '到期：不限\n'
                        '想設期限就說「……到週五」。\n'
                        '這意味著：Person能讓我代為呼叫這些工具，你用這些工具能看到、能做到的，Person都能。\n'
                        '這次按工具授權，不保證只限某份文件或某個資源。\n'
                        '包括以後新裝的工具。\n'
                        '包括敏感工具。\n'
                        '不包括授權、收回、列出授權和申請授權這四個工具。\n'
                        'Person能在這台機器上執行任何命令、讀寫任何檔案，包括這個 Agent 的登入憑證、你的模型 key 和這張授權列表。拿到憑證的人可以冒充這個 '
                        'Agent 跟別人說話。只給你完全信任的人。\n'
                        '確認請點：`/clawchat-allow 000123`（10 分鐘內有效；不確認就不會生效）',
 ('zh_Hant', 'ask'): '授權確認（碼 000123）\n'
                     '允許：Person（usr_alice）\n'
                     '使用：all\n'
                     '方式：每次問我\n'
                     '到期：不限\n'
                     '想設期限就說「……到週五」。\n'
                     '這意味著：Person能讓我代為呼叫這些工具，你用這些工具能看到、能做到的，Person都能。\n'
                     '這次按工具授權，不保證只限某份文件或某個資源。\n'
                     '包括以後新裝的工具。\n'
                     '包括敏感工具。\n'
                     '不包括授權、收回、列出授權和申請授權這四個工具。\n'
                     '這類操作每次都會先問你。\n'
                     '確認請點：`/clawchat-allow 000123`（10 分鐘內有效；不確認就不會生效）',
 ('en', 'direct'): 'Grant confirmation (code 000123)\n'
                   'Allow: Person (usr_alice)\n'
                   'Use: all\n'
                   'Mode: Use directly\n'
                   'Expires: No limit\n'
                   'To set an expiry, say “…until Friday.”\n'
                   'This means: Person can have me call these tools for them. Whatever you can see or '
                   'do with these tools, they can too.\n'
                   'This grants access by tool; it does not guarantee access is limited to one document '
                   'or resource.\n'
                   'Includes tools installed in the future.\n'
                   'Includes sensitive tools.\n'
                   'Excludes the four tools for granting, revoking, listing, and requesting access.\n'
                   'Person can run any command and read or write any file on this machine, including '
                   "this Agent's login credentials, your model API key, and this grant list. Anyone "
                   'with the credentials can impersonate this Agent when talking to others. Only allow '
                   'people you trust completely.\n'
                   'Click to confirm: `/clawchat-allow 000123` (valid for 10 minutes; it will not take '
                   'effect without confirmation)',
 ('en', 'ask'): 'Grant confirmation (code 000123)\n'
                'Allow: Person (usr_alice)\n'
                'Use: all\n'
                'Mode: Ask me each time\n'
                'Expires: No limit\n'
                'To set an expiry, say “…until Friday.”\n'
                'This means: Person can have me call these tools for them. Whatever you can see or do '
                'with these tools, they can too.\n'
                'This grants access by tool; it does not guarantee access is limited to one document or '
                'resource.\n'
                'Includes tools installed in the future.\n'
                'Includes sensitive tools.\n'
                'Excludes the four tools for granting, revoking, listing, and requesting access.\n'
                'These operations will ask you first every time.\n'
                'Click to confirm: `/clawchat-allow 000123` (valid for 10 minutes; it will not take '
                'effect without confirmation)'}


@pytest.mark.parametrize("lang,mode", ALL_CARDS)
def test_all_card_copy_is_verbatim_in_three_languages(lang, mode):
    assert grants.build_grant_card(grantees=[dict(id="usr_alice", name="Person")], tools=["all"],
        mode=mode, lang=lang, code="000123", now=NOW) == ALL_CARDS[lang, mode]


@pytest.mark.parametrize("lang", ["zh", "zh_Hant", "en"])
def test_direct_card_reports_unavailable_ask_and_never_offers_it(lang):
    card = grants.build_grant_card(grantees=[dict(id="usr_alice", name="Person")], tools=["all"],
        mode="direct", lang=lang, code="000123", now=NOW, ask_available=False)
    expected = ALL_CARDS[lang, "direct"].splitlines()
    expected.insert(-1, grants.GRANT_ASK_UNAVAILABLE_RECEIPT[lang])
    assert card == "\n".join(expected)
    assert grants.grant_card_modes(False) == ("direct", "deny")
    with pytest.raises(ValueError, match="ask-unavailable"):
        grants.build_grant_card(grantees=[dict(id="usr_alice", name="Person")], tools=["all"],
            mode="ask", lang=lang, code="000123", now=NOW, ask_available=False)


def test_profile_semicolon_cannot_create_a_new_card_field():
    card = grants.build_grant_card(grantees=[dict(id="usr_alice", name="Person; Mode: injected")],
        tools=["tool:read_file"], mode="direct", lang="en", code="000123", now=NOW)
    assert card.splitlines()[1] == "Allow: Person; Mode: injected (usr_alice)"
    assert card.splitlines()[3] == "Mode: Use directly"


@pytest.mark.asyncio
async def test_ask_card_and_confirmation_check_current_route(management, monkeypatch):
    h = management
    monkeypatch.setattr(guard, "ask_grants_available", lambda: False)
    result = await tools.manage_tool_grants("grant", {**PARAMS, "mode": "ask"}, session_id="s")
    assert result["message"] == "ask-unavailable"
    h.adapter._send_owner_text.assert_not_awaited()
    h.adapter._store.create_pending_grant(account_id="usr_agent", code="000123", payload=dict(
        grantees=[dict(id="usr_alice", name="Person")], tools=["all"], mode="ask"))
    from test_tool_grants_admission import OWNER_CHAT
    assert await h.adapter._consume_grant_confirmation(message("usr_owner", "/clawchat-allow 000123", chat=OWNER_CHAT))
    assert h.adapter._send_owner_text.call_args.args[1] == grants.GRANT_ASK_UNAVAILABLE_RECEIPT["zh"]
    assert h.adapter._store.list_grants(account_id="usr_agent") == []
    monkeypatch.setattr(guard, "ask_grants_available", lambda: True)
    assert await h.adapter._consume_grant_confirmation(message("usr_owner", "/clawchat-allow 000123", chat=OWNER_CHAT))
    assert h.adapter._store.list_grants(account_id="usr_agent")[0]["mode"] == "ask"


def test_cached_host_catalog_is_filtered_per_snapshot_and_unfiltered_for_all(host, monkeypatch):
    model_tools = ModuleType("model_tools")
    raw = [{"function": {"name": tool}} for tool in
           ("terminal", "process_manage", "read_file", "web_search", *AUTHORIZATION_TOOLS)]
    calls = []
    def definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False, skip_tool_search_assembly=False):
        calls.append(skip_tool_search_assembly)
        # Simulate a host cache shared by turns with the same toolset selection.
        return raw
    model_tools.get_tool_definitions = definitions
    model_tools._last_resolved_tool_names = ["fixture_previous"]
    registry_module = ModuleType("tools.registry")
    registry_module.registry = SimpleNamespace(get_all_tool_names=lambda: [item["function"]["name"] for item in raw])
    monkeypatch.setitem(sys.modules, "model_tools", model_tools)
    monkeypatch.setitem(sys.modules, "tools.registry", registry_module)
    monkeypatch.setattr(guard, "_host_config", lambda: {})
    assert guard._install_grant_catalog_filter()
    wrapped = model_tools.get_tool_definitions
    assert guard._install_grant_catalog_filter() and model_tools.get_tool_definitions is wrapped
    def names():
        return {item["function"]["name"] for item in wrapped(quiet_mode=True, skip_tool_search_assembly=True)}
    with bound_rules([row("process_manage", kind="tool"), row("web_search", "deny", kind="tool")]) as snapshot, host(**context(snapshot)):
        assert names() == {"process_manage"}
        previous = list(model_tools._last_resolved_tool_names)
        known, available = guard.host_tool_catalog()
        assert "terminal" in known and "terminal" in available
        assert model_tools._last_resolved_tool_names == previous
        assert names() == {"process_manage"}
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert names() == {"terminal", "process_manage", "read_file", "web_search"}
    with host(HERMES_SESSION_PLATFORM="telegram"):
        assert set(AUTHORIZATION_TOOLS) <= names()
    assert all(calls)


def test_empty_compiled_authority_cannot_disappear_from_intersection():
    result = grants.intersect_turn_grants([resolve([row()]), grants.CompiledGrants()])
    assert result.get("future_tool") is None
    assert result.get("terminal") is None
    assert resolve([row()])  # A broad rule with no finite candidate index is still authority.
    assert resolve([], unmatched="ask")


def test_repeated_overlap_intersections_keep_equivalent_plans_once():
    snapshots = []
    try:
        for index in range(12):
            snapshots.append(turns.register_turn(account_id="usr_agent", chat_id="cnv_overlap_size",
                speakers=("usr_alice",), grants=resolve([row()]), session_key="overlap-size"))
        assert len(turns.get_snapshot(snapshots[-1].nonce).grants.plans) == 1
        assert turns.get_snapshot(snapshots[0].nonce).grants.get("future_tool") == "direct"
    finally:
        for snapshot in snapshots:
            turns.release_turn(snapshot.nonce)


def test_search_assembly_only_receives_authorized_schemas(host, monkeypatch):
    model_tools = ModuleType("model_tools")
    raw = [{"function": {"name": name}} for name in ("terminal", "process_manage", "web_search")]
    def definitions(enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False, skip_tool_search_assembly=False):
        return raw
    model_tools.get_tool_definitions = definitions
    model_tools._resolve_active_context_length = lambda: 10000
    search = ModuleType("tools.tool_search")
    search.load_config = lambda: SimpleNamespace(enabled="on")
    assembled = []
    def assemble(definitions, **kw):
        assembled.append(definitions)
        return SimpleNamespace(tool_defs=[{"function": {"name": "tool_search"}}])
    search.assemble_tool_defs = assemble
    monkeypatch.setitem(sys.modules, "model_tools", model_tools)
    monkeypatch.setitem(sys.modules, "tools.tool_search", search)
    assert guard._install_grant_catalog_filter()
    with bound_rules([row("process_manage", kind="tool"), row("web_search", "deny", kind="tool")]) as snapshot, host(**context(snapshot)):
        assert model_tools.get_tool_definitions(quiet_mode=True) == [{"function": {"name": "tool_search"}}]
        assert assembled == [[{"function": {"name": "process_manage"}}]]
        assert call("terminal", turn_id="t")["reasonCode"] == "not-granted"
        assert call("process_manage", turn_id="t") is None
        monkeypatch.setattr(guard, "_filter_grant_definitions", lambda *_: (_ for _ in ()).throw(RuntimeError("fixture")))
        assert model_tools.get_tool_definitions(quiet_mode=True) == []


@pytest.mark.parametrize("ids", [{}, {"session_id": "s"}, {"turn_id": "t"}])
@pytest.mark.parametrize("policy", ["off", "approve"])
def test_legacy_host_without_execution_ids_keeps_ordinary_approval(host, monkeypatch, ids, policy):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": policy})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    with bound_rules([row(mode="deny")]) as snapshot, host(**context(snapshot, group=True)):
        result = guard.clawchat_pre_tool_call(tool_name="terminal", args={}, **ids)
        if policy == "approve":
            assert result == {"action": "approve", "message": guard.APPROVE_MESSAGE,
                              "rule_key": "clawchat-non-owner:terminal"}
        else:
            assert result == {"action": "block", "message": grants.GRANT_BLOCK_MESSAGE}
        # Management never falls back to ordinary approval on an old host.
        assert guard.clawchat_pre_tool_call(tool_name="clawchat_tool_grant", args={}, **ids) == {
            "action": "block", "message": grants.GRANT_BLOCK_MESSAGE}


@pytest.mark.parametrize("failure", ["missing", "mismatch", "unbound", "empty_ids", "read_error"])
def test_capable_host_cannot_approve_a_missing_or_unreadable_snapshot(host, monkeypatch, failure):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    with bound_rules([row(mode="deny")]) as snapshot:
        values = context(snapshot, group=True)
        if failure == "missing":
            values["HERMES_SESSION_MESSAGE_ID"] = "missing"
        elif failure == "mismatch":
            values["HERMES_SESSION_CHAT_ID"] = "cnv_other"
        elif failure == "read_error":
            monkeypatch.setattr(turns, "lookup_snapshot_for_call", lambda *a: (_ for _ in ()).throw(RuntimeError("fixture")))
        with host(**values):
            result = guard.clawchat_pre_tool_call(tool_name="terminal", args={},
                session_id="" if failure == "empty_ids" else "s",
                turn_id="" if failure == "empty_ids" else "other" if failure == "unbound" else "t")
        expected = guard.BLOCK_MESSAGE if failure == "read_error" else grants.GRANT_BLOCK_MESSAGE
        assert result["action"] == "block" and result["message"] == expected
        if failure != "read_error":
            assert result["reasonCode"] == "not-granted"


@pytest.mark.parametrize("rows", [[], [row("feishu-docs", kind="capability")]])
def test_bound_group_approve_is_per_unmatched_tool(host, approval, rows):
    with bound_rules(rows, unmatched="ask") as snapshot, host(**context(snapshot, group=True)):
        result = call("terminal", turn_id="t")
        assert result["action"] == "approve"
        assert result["rule_key"].startswith("clawchat-grant-ask:")
        assert "Tool: terminal" in result["message"]


@pytest.mark.parametrize("tool,args", [
    ("skill_manage", {"action": "create"}), ("skill_view", {"name": "private"}),
    ("text_to_speech", {"text": "fixture", "output_path": "/tmp/private.wav"}),
    ("vision_analyze", {"image_url": "/tmp/private.png", "question": "fixture"}),
])
@pytest.mark.parametrize("has_other_rule", [False, True])
def test_dedicated_limited_message_survives_without_explicit_grant_rows(host, monkeypatch, tool, args, has_other_rule):
    known, available = guard.host_tool_catalog()
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (known | {tool}, available | {tool}))
    monkeypatch.setattr(guard, "_skills_inline_shell", lambda: True)
    monkeypatch.setattr(guard, "_plugin_skill_ids", lambda: frozenset())
    monkeypatch.setattr(guard, "_media_roots", lambda: [])
    rows = [row("terminal", kind="capability")] if has_other_rule else []
    with bound_rules(rows) as snapshot, host(**context(snapshot)):
        expected = grants.GRANT_BLOCK_MESSAGE if has_other_rule else guard.LIMITED_BLOCK_MESSAGE
        assert call(tool, turn_id="t", args=args) == {
            "action": "block", "reasonCode": "not-granted", "message": expected}


def test_legacy_catalog_preserves_approve_but_capable_missing_nonce_stays_narrowed(host, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    definitions = [{"function": {"name": tool}} for tool in ("terminal", "web_search", *AUTHORIZATION_TOOLS)]
    with host(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_rules"):
        assert guard._filter_grant_definitions(definitions, None) == definitions[:2]
        monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: True)
        assert guard._filter_grant_definitions(definitions, None) == definitions[1:2]
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"terminal", "web"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: ["terminal"] if name == "terminal" else ["web_search"])
    legacy = SimpleNamespace(chat_id="cnv_rules", chat_type="group", user_id="usr_alice")
    assert guard.toolsets_for_source(legacy, owner_user_id="usr_owner") is None
    modern = SimpleNamespace(**vars(legacy), message_id="missing")
    assert "terminal" not in guard.toolsets_for_source(modern, owner_user_id="usr_owner")


@pytest.mark.parametrize("rows", [[], [row(mode="deny")]])
def test_old_host_nonce_cannot_replace_legacy_catalog_approval(host, monkeypatch, rows):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"terminal", "web"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda name: ["terminal"] if name == "terminal" else ["web_search"])
    definitions = [{"function": {"name": tool}} for tool in ("terminal", "web_search", *AUTHORIZATION_TOOLS)]
    with bound_rules(rows) as snapshot, host(**context(snapshot, group=True)):
        source = SimpleNamespace(chat_id=snapshot.chat_id, chat_type="group", user_id="usr_alice", message_id=snapshot.nonce)
        assert guard.toolsets_for_source(source, owner_user_id="usr_owner") is None
        assert guard._filter_grant_definitions(definitions, snapshot) == definitions[:2]
