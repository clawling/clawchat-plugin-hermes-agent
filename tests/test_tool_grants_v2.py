"""Selector, migration, and execution regressions for capability grants."""
from contextlib import contextmanager
from dataclasses import replace
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
    KANBAN_SESSION_ROUTES,
    HOST_TOOL_ALIASES,
)
from test_tool_grants_hook import host, context, call
from test_tool_grants_ask import approval
from test_tool_grants_tools import management, PARAMS
from test_tool_grants_admission import harness, message, coalesced

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
    assert result.get("terminal") == "deny"
    assert result.get("process") == "direct"
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
@pytest.mark.parametrize("group", [False, True])
def test_sensitive_direct_executes_with_local_arguments(host, tool, group):
    with bound_rules([row()]) as snapshot, host(**context(snapshot, group=group)):
        assert call(tool, turn_id="t", args={"path": "/tmp/fixture", "command": "cat /tmp/fixture"}) is None


@pytest.mark.parametrize("policy", ["off", "approve"])
def test_deny_precedes_default_public_access_and_group_approve(host, policy, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": policy})
    rows = [row("web_search", "deny", kind="tool"), row("terminal", "deny", kind="capability")]
    with bound_rules(rows, public={"web_search"}, unmatched="ask") as snapshot, host(**context(snapshot, group=True)):
        for tool in ("web_search", "terminal"):
            assert call(tool, turn_id="t") == {"action": "block", "reasonCode": "denied", "message": grants.GRANT_DENIED_MESSAGE, "blockReason": grants.GRANT_DENIED_MESSAGE}


def test_group_approve_asks_for_another_unmatched_tool(host, approval):
    with bound_rules([row("feishu-docs", kind="capability")], unmatched="ask") as snapshot, host(**context(snapshot, group=True)):
        assert call("terminal", turn_id="t")["action"] == "approve"
        assert call("feishu_doc_read", turn_id="t") is None


def test_unavailable_host_tool_precedes_all_permission(host, monkeypatch):
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: ({"terminal"}, frozenset()))
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call("terminal", turn_id="t") == {"action": "block", "reasonCode": "not-available", "message": grants.GRANT_NOT_AVAILABLE_MESSAGE, "blockReason": grants.GRANT_NOT_AVAILABLE_MESSAGE}


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
            for tool in grants.GRANT_TOOL_NAMES:
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
                if allowed:
                    from clawchat_gateway.tool_capabilities import KANBAN_SESSION_ROUTES
                    assert result["reasonCode"] == "cross-session" if tool in KANBAN_SESSION_ROUTES else result is None
                else:
                    assert result["reasonCode"] == "not-granted"


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
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


def test_hint_lists_only_available_blocked_capabilities():
    result = resolve([row("files", kind="capability")])
    hint = grants.build_non_owner_grants_hint(result, {"read_file", "terminal", "unknown_mcp"})
    assert hint == "Not available to the caller yet: Terminal (terminal). " + grants.NON_OWNER_GRANTS_HINT
    assert grants.build_non_owner_grants_hint(result, {"read_file"}) == grants.NON_OWNER_GRANTS_HINT


@pytest.mark.parametrize("capability_deny", [False, True])
def test_hint_exact_terminal_access_keeps_schema_and_execution_per_tool(host, capability_deny):
    rows = [row("terminal", kind="tool")]
    if capability_deny:
        rows.append(row("terminal", "deny", kind="capability"))
    available = {"terminal", "process", "process_manage"}
    with bound_rules(rows) as snapshot, host(**context(snapshot)):
        definitions = [{"function": {"name": name}} for name in sorted(available)]
        assert guard._filter_grant_definitions(definitions, snapshot) == [{"function": {"name": "terminal"}}]
        assert call("terminal", turn_id="t") is None
        assert call("process_manage", turn_id="t")["reasonCode"] == ("denied" if capability_deny else "not-granted")
        assert "Terminal (terminal)" not in grants.build_non_owner_grants_hint(snapshot.grants, available)
    assert "Terminal (terminal)" in grants.build_non_owner_grants_hint(resolve(), available)


@pytest.fixture
def search_catalog(host, monkeypatch):
    model_tools = ModuleType("model_tools")
    raw = [{"function": {"name": name}} for name in
           ("terminal", "process", "process_manage", "read_file", "web_search", "manage_connections", *AUTHORIZATION_TOOLS)]
    def definitions(*, skip_tool_search_assembly=False):
        assert skip_tool_search_assembly
        return raw
    model_tools.get_tool_definitions = definitions
    search = ModuleType("tools.tool_search")
    search.BRIDGE_TOOL_NAMES = frozenset({"tool_search", "tool_describe", "tool_call"})
    monkeypatch.setitem(sys.modules, "model_tools", model_tools)
    monkeypatch.setitem(sys.modules, "tools.tool_search", search)
    return model_tools


@pytest.mark.parametrize("bridge", ["tool_search", "tool_describe"])
@pytest.mark.parametrize("policy", ["exact", "all-deny", "intersection"])
def test_search_bridges_read_only_authorized_bound_catalog(host, search_catalog, bridge, policy):
    rows = [row("terminal", kind="tool"), row("read_file", "deny", kind="tool"),
            row("process", kind="tool", expires_at=NOW - 1)]
    speakers = ("usr_alice",)
    expected = {"terminal", "web_search", "clawchat_tool_request"}
    if policy == "all-deny":
        rows.append(row(mode="deny"))
        expected.remove("web_search")
    elif policy == "intersection":
        speakers += ("usr_bob",)
        expected.remove("terminal")
    assert guard._install_grant_catalog_filter()
    with bound_rules(rows, speakers=speakers) as snapshot, host(**context(snapshot)):
        catalog = search_catalog.get_tool_definitions(skip_tool_search_assembly=True)
        assert {item["function"]["name"] for item in catalog} == expected
        assert call("tool_call", turn_id="t")["reasonCode"] == "not-available"
        assert call("process_manage", turn_id="t")["reasonCode"] == ("batch-intersection" if policy == "intersection" else "denied" if policy == "all-deny" else "not-granted")
        assert call(bridge, turn_id="t") is None


@pytest.mark.parametrize("bridge", ["tool_search", "tool_describe"])
@pytest.mark.parametrize("policy", ["all-deny-exact", "all-direct", "all-direct-connections-denied"])
def test_search_bridges_block_remote_connector_catalog(host, search_catalog, bridge, policy):
    rows = ([row(mode="deny"), row("manage_connections", kind="tool")]
            if policy == "all-deny-exact" else [row()])
    if policy == "all-direct-connections-denied":
        rows.append(row("manage_connections", "deny", kind="tool"))
    assert guard._install_grant_catalog_filter()
    with bound_rules(rows) as snapshot, host(**context(snapshot)):
        catalog = search_catalog.get_tool_definitions(skip_tool_search_assembly=True)
        connections_visible = policy != "all-direct-connections-denied"
        assert ("manage_connections" in {item["function"]["name"] for item in catalog}) is connections_visible
        result = call(bridge, turn_id="t")
        if connections_visible:
            assert result is not None and result["reasonCode"] == "not-available"
        else:
            assert result is None


@pytest.mark.parametrize("bridge", ["tool_search", "tool_describe"])
def test_search_bridges_allow_all_grant_when_host_has_no_connections(host, search_catalog, monkeypatch, bridge):
    assert guard._install_grant_catalog_filter()
    known, available = guard.host_tool_catalog()
    monkeypatch.setattr(guard, "host_tool_catalog",
                        lambda: (known - {"manage_connections"}, available - {"manage_connections"}))
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call(bridge, turn_id="t") is None


@pytest.mark.parametrize("bridge", ["tool_search", "tool_describe"])
@pytest.mark.parametrize("failure", ["no-filter", "no-search-module", "chat", "grants", "broad-rules", "speakers", "overlap", "visibility-error"])
def test_search_bridges_fail_closed_without_matching_filtered_catalog(host, search_catalog, monkeypatch, bridge, failure):
    if failure != "no-filter":
        assert guard._install_grant_catalog_filter()
    if failure == "no-search-module":
        monkeypatch.setitem(sys.modules, "tools.tool_search", None)
    if failure == "visibility-error":
        def broken_filter(*args):
            raise RuntimeError("catalog visibility unavailable")
        monkeypatch.setattr(guard, "_filter_grant_definitions", broken_filter)
    with bound_rules([row("terminal", kind="tool")]) as snapshot, host(**context(snapshot)):
        if failure == "broad-rules":
            # The finite Mapping view is identical, but future-tool access differs.
            broader = resolve([row("terminal", kind="tool"), row()])
            assert dict(broader) == dict(snapshot.grants)
            monkeypatch.setattr(turns, "get_snapshot", lambda _: replace(snapshot, grants=broader))
        elif failure in {"chat", "grants", "speakers"}:
            changes = {"chat_id": "cnv_other"} if failure == "chat" else (
                {"grants": resolve([row()])} if failure == "grants" else {"non_owner_speakers": frozenset({"usr_bob"})})
            monkeypatch.setattr(turns, "get_snapshot", lambda _: replace(snapshot, **changes))
        if failure == "overlap":
            overlap = turns.register_turn(account_id="usr_agent", chat_id=snapshot.chat_id,
                speakers=("usr_bob",), grants=resolve([], ("usr_bob",)), session_key="rules")
            # The binding was obtained just before the overlapping input narrowed the catalog.
            monkeypatch.setattr(turns, "lookup_snapshot_for_call", lambda *args: snapshot)
            try:
                assert call(bridge, turn_id="t")["reasonCode"] == "not-available"
            finally:
                turns.release_turn(overlap.nonce)
        else:
            assert call(bridge, turn_id="t")["reasonCode"] == "not-available"


@pytest.mark.parametrize("bridge", ["tool_search", "tool_describe"])
def test_search_bridges_accept_equivalent_catalog_snapshot(host, search_catalog, monkeypatch, bridge):
    assert guard._install_grant_catalog_filter()
    with bound_rules([row("terminal", kind="tool")]) as snapshot, host(**context(snapshot)):
        equivalent = replace(snapshot, grants=resolve([row("terminal", kind="tool")]))
        assert equivalent is not snapshot and equivalent.grants is not snapshot.grants
        monkeypatch.setattr(turns, "get_snapshot", lambda _: equivalent)
        assert call(bridge, turn_id="t") is None


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
                   '在这台机器上，别人发起的回合不能派子 Agent、建或运行定时任务、创建看板任务，只能查看和停止已有的。\n'
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
                   '在这台机器上，别人发起的回合不能派子 Agent、建或运行定时任务、创建看板任务，只能查看和停止已有的。\n'
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
                   '在這台機器上，別人發起的回合不能派子 Agent、建立或執行定時任務、建立看板任務，只能查看和停止現有的。\n'
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
                   '在這台機器上，別人發起的回合不能派子 Agent、建立或執行定時任務、建立看板任務，只能查看和停止現有的。\n'
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
                   'On this machine, turns started by others cannot dispatch sub-agents, create or run scheduled tasks, or create Kanban tasks; they can only view and stop existing ones.\n'
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
                   'On this machine, turns started by others cannot dispatch sub-agents, create or run scheduled tasks, or create Kanban tasks; they can only view and stop existing ones.\n'
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
@pytest.mark.parametrize("locale,lang", [("zh-CN", "zh"), ("zh-TW", "zh_Hant"), (None, "en"), ("ja", "en"), ("unreadable", "en")])
async def test_ask_card_and_confirmation_check_current_route(management, monkeypatch, locale, lang):
    h = management
    def owner_locale():
        if locale == "unreadable":
            raise RuntimeError("locale unavailable")
        return locale
    h.adapter._owner_locale = owner_locale
    monkeypatch.setattr(guard, "ask_grants_available", lambda: False)
    result = await tools.manage_tool_grants("grant", {**PARAMS, "mode": "ask"}, session_id="s")
    assert result == {"error": "validation", "message": "ask-unavailable", "note": grants.GRANT_ASK_UNAVAILABLE_RECEIPT[lang]}
    h.adapter._send_owner_text.assert_not_awaited()
    with sqlite3.connect(h.adapter._store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM tool_grant_pending").fetchone()[0] == 0
    assert h.adapter._store.list_grants(account_id="usr_agent") == []
    h.adapter._owner_locale = lambda: "zh-CN"
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
        assert names() == {"process_manage", "clawchat_tool_request"}
        previous = list(model_tools._last_resolved_tool_names)
        known, available = guard.host_tool_catalog()
        assert "terminal" in known and "terminal" in available
        assert model_tools._last_resolved_tool_names == previous
        assert names() == {"process_manage", "clawchat_tool_request"}
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert names() == {"terminal", "process_manage", "read_file", "web_search", "clawchat_tool_request"}
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
        result = call(tool, turn_id="t", args=args)
        expected_result = {"action": "block", "reasonCode": "not-granted", "message": expected}
        if has_other_rule:
            expected_result["blockReason"] = expected
        assert result == expected_result


def test_legacy_catalog_preserves_approve_but_capable_missing_nonce_stays_narrowed(host, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    definitions = [{"function": {"name": tool}} for tool in ("terminal", "web_search", *AUTHORIZATION_TOOLS)]
    with host(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_rules"):
        assert guard._filter_grant_definitions(definitions, None) == definitions[:2] + definitions[-1:]
        monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: True)
        assert guard._filter_grant_definitions(definitions, None) == definitions[1:2] + definitions[-1:]
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
        assert guard._filter_grant_definitions(definitions, snapshot) == definitions[:2] + definitions[-1:]


# Each routed host entry is covered, including unadvertised action/parameter
# aliases. Hermes does not fold parameter case or snake/camel spellings.
ROUTED_CALLS = [
    (name, {"task_id": "task-other", "session_id": "session-other", "assignee": "other"})
    for name in sorted(KANBAN_SESSION_ROUTES)
] + [
    ("kanban_create", {"sessionId": "session-other", "assignee": "other"}),
    ("kanban_create", {"session_id": "s", "assignee": "other"}),
    ("delegate_task", {"tasks": [{"goal": "work"}]}),
    ("delegate_task", {"goal": "work", "background": False}),
    ("delegate_task", {"action": " STEER ", "subagent_id": "child-other", "message": "work"}),
    ("message_agent", {"target": "other", "message": "work"}),
    ("message_agent", {"Target": "other", "message": "work"}),
] + [
    (name, args) for name in ("cronjob", "cronjob_manage") for args in (
        {"action": "create", "deliver": "clawchat:cnv_other", "attach_to_session": True},
        {"action": "create", "deliver": ["origin", "clawchat:cnv_other"]},
        {"action": "create", "deliver": "local", "failure_deliver": "bot-chat:other"},
        {"action": "create", "failureDeliver": "bot-chat:other", "attachToSession": True},
        {"action": "create", "origin": {"chat_id": "cnv_other"}},
        {"action": "create", "deliver": "origin"},
        {"action": " UPDATE ", "job_id": "other", "context_from": "other"},
        {"action": "resume", "job_id": "other"},
        *({"action": action, "job_id": "other"} for action in ("run", "run_now", "trigger")),
    )
]


@pytest.mark.parametrize("tool,args", ROUTED_CALLS)
@pytest.mark.parametrize("permission", ["all-direct", "all-ask", "cap-direct", "tool-direct", "approve"])
def test_audited_routes_cannot_use_authorization_to_start_another_session(host, approval, monkeypatch, tool, args, permission):
    known, available = guard.host_tool_catalog()
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (known | {tool}, available | {tool}))
    if permission == "cap-direct":
        capability = "schedule" if tool.startswith("cronjob") else "subagent"
        rows = [row(capability, kind="capability")]
        if tool == "message_agent":  # injected by the host, outside capability mappings
            rows = [row(tool, kind="tool")]
    elif permission == "tool-direct":
        rows = [row(tool, kind="tool")]
    else:
        rows = [] if permission == "approve" else [row(mode="ask" if permission == "all-ask" else "direct")]
    with bound_rules(rows, unmatched="ask" if permission == "approve" else "none") as snapshot, host(**context(snapshot, group=True)):
        monkeypatch.setattr(guard, "_ask_directive", lambda *_: pytest.fail("route must be checked before asking"))
        result = call(tool, turn_id="t", args=args)
        assert result == guard._grant_block("cross-session")
        assert all(value not in result["blockReason"] for value in ("cnv_other", "session-other", "task-other", "settings", "version"))


@pytest.mark.parametrize("tool,args", ROUTED_CALLS)
def test_legacy_approve_cannot_bypass_audited_routes(host, monkeypatch, tool, args):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    with host(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_rules", HERMES_SESSION_CHAT_TYPE="group"):
        assert guard.clawchat_pre_tool_call(tool_name=tool, args=args) == guard._grant_block("cross-session")


@pytest.mark.parametrize("tool,args", [
    ("delegate_task", {"action": "list"}), ("delegate_task", {"action": "stop", "subagent_id": "child"}),
    ("cronjob_manage", {"action": "list", "deliver": "bot-chat:other"}),
    ("cronjob_manage", {"action": "pause", "job_id": "other"}),
    ("cronjob", {"action": "remove", "job_id": "other"}),
    ("terminal", {"command": "true", "background": True, "notify": True}),
    ("terminal", {"command": "true", "background": True, "notify_on_complete": True}),
    ("terminal", {"command": "true", "background": True, "watch_patterns": ["done"], "heartbeat": 60}),
    ("kanban_show", {"task_id": "task"}), ("kanban_list", {}),
    ("kanban_heartbeat", {"task_id": "task"}), ("kanban_attachments", {"task_id": "task"}),
    ("kanban_attach_url", {"task_id": "task", "url": "https://example.org/file"}),
    ("feishu_bitable_update_record", {"fields": {"chat_id": "other", "target": "other", "to": "other", "session": "other"}}),
])
def test_non_routing_operations_and_business_fields_keep_normal_grants(host, monkeypatch, tool, args):
    known, available = guard.host_tool_catalog()
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (known | {tool}, available | {tool}))
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call(tool, turn_id="t", args=args) is None


@pytest.mark.parametrize("kind", ["direct", "group"])
@pytest.mark.parametrize("mode", ["direct", "deny"])
def test_cross_chat_outbound_and_mention_visibility_follow_the_same_caller(host, kind, mode):
    tools_ = ["clawchat_mention_message", "clawchat_react_message", "clawchat_send_file"]
    with bound_rules([row("cross-chat", mode, kind="capability")]) as snapshot, host(**context(snapshot, group=kind == "group")):
        definitions = [{"function": {"name": name}} for name in tools_]
        assert guard._filter_grant_definitions(definitions, snapshot) == definitions
        for tool in tools_:
            key = "chat_id" if tool == "clawchat_send_file" else "chatId"
            assert call(tool, turn_id="t", args={key: snapshot.chat_id}) is None
            args = {"chat_id" if tool == "clawchat_send_file" else "chatId": "cnv_other"}
            result = call(tool, turn_id="t", args=args)
            assert result is None if mode == "direct" else result == guard._grant_block("denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("speakers,reason", [
    (("usr_owner", "usr_alice"), "not-granted"),
    (("usr_alice", "usr_bob"), "batch-intersection"),
    (("usr_owner", "usr_alice", "usr_bob"), "batch-intersection"),
])
async def test_actual_group_batches_use_only_trusted_nonowner_intersection(harness, host, speakers, reason):
    inbound = await coalesced([message(sender, kind="group") for sender in speakers])
    harness.rows.append(row(user="usr_alice") if "usr_bob" in speakers else row(user="usr_else"))
    await harness.adapter._handle_inbound(inbound)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.non_owner_speakers == set(speakers) - {"usr_owner"}
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        result = call("terminal", turn_id="t")
        assert result["reasonCode"] == reason
        assert result["message"] == grants.GRANT_FAILURE_MESSAGES[reason]
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [{"synthetic": True}, {"messages": []}, {"messages": [{"sender": {}}]},
                                      {"messages": [message("usr_owner").raw_message, {"synthetic": True}]}])
async def test_unattributed_batches_use_defaults_without_group_approve(harness, host, approval, monkeypatch, raw):
    from dataclasses import replace
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "public_tool_names", lambda _: frozenset({"web_search"}))
    inbound = await coalesced([message("usr_owner", kind="group"),
                               replace(message("", kind="group"), raw_message=raw)])
    await harness.adapter._handle_inbound(inbound)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert not snapshot.owner_side
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        assert call("terminal", turn_id="t")["action"] == "block"
        assert call("web_search", turn_id="t") is None


@pytest.mark.parametrize("order", ["owner-first", "guest-first"])
def test_owner_direct_overlap_never_borrows_session_owner_identity(host, order):
    def owner():
        return turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", speakers=("usr_owner",),
                                   grants={}, session_key="overlap", owner_side=True, owner_verified=True)
    def guest():
        return turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", speakers=("usr_alice",),
                                   grants=resolve([row("terminal", "deny", kind="capability")]), session_key="overlap")
    snapshots = [owner(), guest()] if order == "owner-first" else [guest(), owner()]
    definitions = [{"function": {"name": name}} for name in ("terminal", *AUTHORIZATION_TOOLS)]
    try:
        for snapshot in snapshots:
            turns.bind_turn(snapshot.nonce, "s", snapshot.nonce)
            current = turns.get_snapshot(snapshot.nonce)
            assert not current.owner_side
            assert guard._filter_grant_definitions(definitions, current) == definitions[-1:]
            with host(**dict(context(snapshot), HERMES_SESSION_USER_ID="usr_owner")):
                assert call("terminal", turn_id=snapshot.nonce) == guard._grant_block("denied")
                for tool in grants.GRANT_TOOL_NAMES:
                    assert call(tool, turn_id=snapshot.nonce)["action"] == "block"
    finally:
        for snapshot in snapshots:
            turns.release_turn(snapshot.nonce)


@pytest.mark.asyncio
async def test_owner_surface_is_unchanged_after_friend_grants(harness, host):
    a = harness.adapter
    from test_tool_grants_admission import OWNER_CHAT
    definitions = [{"function": {"name": name}} for name in ("terminal", "delegate_task", "web_search", *AUTHORIZATION_TOOLS[:-1])]
    for rows in ([], [row(mode="deny")], [row()]):
        harness.rows[:] = rows
        a._active_sessions.clear()
        await a._handle_inbound(message("usr_owner", chat=OWNER_CHAT))
        snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
        assert guard._filter_grant_definitions(definitions, snapshot) == definitions
        turns.bind_turn(snapshot.nonce, "s", "t")
        with host(**context(snapshot)):
            assert call("terminal", turn_id="t") is None
            assert call("delegate_task", turn_id="t", args={"goal": "work"}) is None
        source = harness.events[-1].source
        assert guard.toolsets_for_source(source, owner_user_id="usr_owner") is None
        turns.release_turn(snapshot.nonce)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["yolo", "route", "unsupported", "off"])
async def test_ask_cards_recheck_after_profile_lookup(management, monkeypatch, change):
    h = management
    async def profile(_):
        if change == "yolo":
            h.approval._yolo_active = lambda: True
        elif change == "route":
            monkeypatch.setattr(h.adapter, "_owner_direct_chat_id", lambda: "")
        elif change == "unsupported":
            monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
        else:
            h.approval.approval_context._get_approval_mode = lambda: "off"
        return {"user": {"nickname": "Person"}}
    h.client.get_user_info.side_effect = profile
    result = await tools.manage_tool_grants("grant", {**PARAMS, "mode": "ask"}, session_id="s")
    assert result == {"error": "validation", "message": "ask-unavailable", "note": grants.GRANT_ASK_UNAVAILABLE_RECEIPT["zh"]}
    h.adapter._send_owner_text.assert_not_awaited()
    with sqlite3.connect(h.adapter._store.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM tool_grant_pending").fetchone()[0] == 0
    assert h.adapter._store.list_grants(account_id="usr_agent") == []


@pytest.mark.asyncio
async def test_direct_card_uses_current_ask_availability_after_profile_lookup(management):
    h = management
    async def profile(_):
        h.approval._yolo_active = lambda: True
        return {"user": {"nickname": "Person"}}
    h.client.get_user_info.side_effect = profile
    assert (await tools.manage_tool_grants("grant", PARAMS, session_id="s"))["pending"]
    card = h.adapter._send_owner_text.call_args.args[1]
    assert grants.GRANT_ASK_UNAVAILABLE_RECEIPT["zh"] in card
    assert grants.CARD_COPY["ask"]["zh"] not in card


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["on", "off"])
@pytest.mark.parametrize("sender", ["clawchat-maintenance", "clawchat-awareness", "usr_owner"])
@pytest.mark.parametrize("binding", [True, False])
async def test_unattributed_synthetic_owner_chat_uses_defaults_on_all_hosts(harness, host, approval, monkeypatch, setting, sender, binding):
    from dataclasses import replace
    from clawchat_gateway.adapter import ClawChatAdapter
    from test_tool_grants_admission import OWNER_CHAT
    a = harness.adapter
    monkeypatch.setattr(a, "_is_owner_turn", ClawChatAdapter._is_owner_turn.__get__(a))
    monkeypatch.setattr(guard, "public_tool_names", lambda _: frozenset({"web_search"}))
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve", "tool-grants": setting})
    a._clawchat_config = replace(a._clawchat_config, tool_grants=setting)
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: binding)
    inbound = message(sender, chat=OWNER_CHAT, synthetic=True)
    assert not a._is_owner_turn(inbound)
    assert not a._verified_owner_inbound(inbound)
    await a._handle_inbound(inbound)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert not snapshot.owner_side and not snapshot.speakers and not snapshot.legacy_owner_tools
    definitions = [{"function": {"name": tool}} for tool in ("terminal", "web_search", *AUTHORIZATION_TOOLS)]
    assert guard._filter_grant_definitions(definitions, snapshot) == (definitions[1:2] if binding else definitions[:2]) + (definitions[-1:] if setting == "on" else [])
    if binding:
        turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**dict(context(snapshot), HERMES_SESSION_USER_ID=sender)):
        assert call("terminal", turn_id="t")["action"] == ("block" if setting == "on" else "approve")
        assert call("web_search", turn_id="t") is None
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.asyncio
async def test_failed_grant_read_uses_default_and_never_approve(harness, host, approval, monkeypatch):
    def unreadable(**_):
        raise OSError("private storage detail")
    monkeypatch.setattr(harness.adapter._store, "load_grants_for_users", unreadable)
    monkeypatch.setattr(guard, "public_tool_names", lambda _: frozenset({"web_search"}))
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    await harness.adapter._handle_inbound(message(kind="group"))
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        result = call("terminal", turn_id="t")
        assert result["action"] == "block" and "private storage detail" not in str(result)
        assert call("web_search", turn_id="t") is None


def test_only_nonempty_owner_overlaps_are_full_and_unverified_input_revokes_full_power(host):
    snapshots = [turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", speakers=("usr_owner",),
                    grants={}, session_key="all-owner", owner_side=True, owner_verified=True) for _ in range(2)]
    try:
        for snapshot in snapshots:
            assert turns.get_snapshot(snapshot.nonce).owner_side
            turns.bind_turn(snapshot.nonce, "s", snapshot.nonce)
            with host(**context(snapshot, group=True)):
                assert call("terminal", turn_id=snapshot.nonce) is None
        turns.mark_unverified_input("usr_agent", "all-owner")
        for snapshot in snapshots:
            assert not turns.get_snapshot(snapshot.nonce).owner_side
            with host(**dict(context(snapshot), HERMES_SESSION_USER_ID="usr_owner")):
                assert call("terminal", turn_id=snapshot.nonce)["action"] == "block"
    finally:
        turns.release_session("usr_agent", "all-owner")
        turns.clear_unverified_input("usr_agent", "all-owner")
    empty = turns.register_turn(account_id="usr_agent", chat_id="cnv_empty", speakers=(), grants={},
                                session_key="empty-owner", owner_side=True)
    try:
        assert not empty.owner_side
    finally:
        turns.release_turn(empty.nonce)


@pytest.mark.parametrize("tool,key", [("clawchat_send_file", "chat_id"),
                                      ("clawchat_mention_message", "chatId"), ("clawchat_react_message", "chatId"),
                                      ("send_message", "target")])
def test_cross_chat_uses_each_hosts_actual_wire_target_key(tool, key):
    from clawchat_gateway.tool_capabilities import is_cross_chat_call
    target = "clawchat:cnv_here" if tool == "send_message" else "cnv_here"
    assert not is_cross_chat_call(tool, {key: target}, {"chat_id": "cnv_here"})
    assert not is_cross_chat_call(tool, {key: " ClawChat: cnv_here "}, {"chat_id": "cnv_here"})
    assert is_cross_chat_call(tool, {key: "cnv_other"}, {"chat_id": "cnv_here"})


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["direct", "group"])
async def test_owner_solo_group_can_send_and_mention_another_chat(harness, host, monkeypatch, target_kind):
    from unittest.mock import AsyncMock
    from clawchat_gateway import plugin_tools
    destination = "cnv_01ARZ3NDEKTSV4RRFFQ69G5FAX"
    send = AsyncMock(return_value={"sent": True, "messageId": "message"})
    monkeypatch.setattr(tools, "send_clawchat_mention_message", send)
    monkeypatch.setattr(plugin_tools, "_record_tool_call", lambda **_: None)
    await harness.adapter._handle_inbound(message("usr_owner", kind="group"))
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert harness.adapter._non_owner_turn_section(message("usr_owner", kind="group")) is None
    turns.bind_turn(snapshot.nonce, "s", "t")
    args = {"chatId": destination, "chatType": target_kind,
            "mentions": [{"userId": "usr_recipient", "display": "Person"}], "text": "hello"}
    with host(**context(snapshot, group=True)):
        assert call("clawchat_mention_message", turn_id="t", args=args) is None
        assert json.loads(await plugin_tools.handle_clawchat_mention_message(args))["sent"]
    assert send.call_args.kwargs["chat_id"] == destination
    assert send.call_args.kwargs["chat_type"] == target_kind


@pytest.mark.asyncio
async def test_dynamic_group_hint_describes_common_access_without_identifying_people(harness, monkeypatch):
    from dataclasses import replace
    a = harness.adapter
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: ({"terminal", "read_file"}, {"terminal", "read_file"}))
    harness.rows.extend([row(user="usr_alice"), row("files", user="usr_bob", kind="capability")])
    inbound = await coalesced([message("usr_alice", kind="group"), message("usr_bob", kind="group")])
    await a._handle_inbound(inbound)
    hint = a._non_owner_turn_section(inbound)
    assert "Not available to the caller yet: Terminal (terminal). " + grants.NON_OWNER_GRANTS_HINT in hint
    assert "Files (files)" not in hint and "Scheduled tasks (schedule)" not in hint
    assert "usr_alice" not in hint and "usr_bob" not in hint


@pytest.mark.parametrize("old,new", HOST_TOOL_ALIASES.items())
@pytest.mark.parametrize("origin", ["migration", "v1-pending"])
def test_legacy_exact_alias_keeps_ask_over_all_direct_without_rewriting_storage(tmp_path, old, new, origin):
    path = tmp_path / "legacy.sqlite"
    old_database(path)
    with sqlite3.connect(path) as conn:
        if origin == "migration":
            conn.execute("INSERT INTO tool_grants VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         ("usr_agent", "usr_alice", "Person", old, "ask", None, None, NOW))
        else:
            conn.execute("INSERT INTO tool_grant_pending VALUES (?, ?, ?, ?, ?, NULL)",
                         ("usr_agent", "000234", json.dumps(dict(grantees=[dict(id="usr_alice", name="Person")],
                          tools=[old], mode="ask")), NOW, NOW + 100))
    store = storage.ClawChatStore(path)
    store.initialize()
    if origin == "v1-pending":
        store.confirm_pending_grant(account_id="usr_agent", code="000234", now=NOW + 1)
    store.create_pending_grant(account_id="usr_agent", code="000345", payload=dict(
        grantees=[dict(id="usr_alice", name="Person")], tools=["all"], mode="direct"), now=NOW)
    store.confirm_pending_grant(account_id="usr_agent", code="000345", now=NOW + 1)
    rows = store.list_grants(account_id="usr_agent", now=NOW + 1)
    rules = resolve(rows)
    assert rules.get(old) == rules.get(new) == "ask"
    assert grants.freeze_turn_grants({old: "ask", new: "direct"}).get(new) == "ask"
    assert any(item["what_kind"] == "tool" and item["what"] == old and item["mode"] == "ask"
               for item in store.list_grants(account_id="usr_agent", now=NOW + 1))


@pytest.mark.parametrize("old,new", HOST_TOOL_ALIASES.items())
@pytest.mark.parametrize("modes,expected", [(("direct", "ask"), "ask"), (("ask", "deny"), "deny"),
                                          (("deny", "direct"), "deny")])
def test_alias_conflicts_use_strictest_mode_in_either_order(old, new, modes, expected):
    rows = [row(), row(old, modes[0], kind="tool"), row(new, modes[1], kind="tool")]
    for ordered in (rows, rows[::-1]):
        rules = resolve(ordered)
        assert rules.get(old) == rules.get(new) == expected
    assert resolve([row(old.upper(), "deny", kind="tool"), row()]).get(new) == "direct"


@pytest.mark.parametrize("old,new", HOST_TOOL_ALIASES.items())
def test_dispatch_keeps_legacy_exact_ask_for_canonical_name(host, monkeypatch, old, new):
    known, available = guard.host_tool_catalog()
    monkeypatch.setattr(guard, "host_tool_catalog", lambda: (known | {new}, available | {new}))
    seen = []
    def ask(snapshot, name, args):
        seen.append(name)
        return {"action": "approve", "message": "owner approval"}
    monkeypatch.setattr(guard, "_ask_directive", ask)
    with bound_rules([row(old, "ask", kind="tool"), row()]) as snapshot, host(**context(snapshot)):
        assert call(new, turn_id="t", args={"action": "list"})["action"] == "approve"
        assert seen == [new]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,chat,sender,synthetic", [
    ("group", "cnv_group", "usr_owner", True),
    ("direct", "cnv_other", "usr_owner", True),
    ("direct", "cnv_owner", "usr_guest", False),
])
async def test_legacy_synthetic_tool_exception_does_not_spread(harness, host, monkeypatch, kind, chat, sender, synthetic):
    from clawchat_gateway.adapter import ClawChatAdapter
    a = harness.adapter
    monkeypatch.setattr(a, "_is_owner_turn", ClawChatAdapter._is_owner_turn.__get__(a))
    await a._handle_inbound(message(sender, kind=kind, chat=chat, synthetic=synthetic))
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert not snapshot.owner_side and not snapshot.legacy_owner_tools
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**dict(context(snapshot, group=kind == "group"), HERMES_SESSION_USER_ID=sender)):
        assert call("terminal", turn_id="t")["action"] == "block"


@pytest.mark.parametrize("mode", ["deny", "ask"])
def test_cross_chat_catalog_preserves_specific_rules_and_shared_target_intersection(host, mode):
    tool = "clawchat_mention_message"
    definition = [{"function": {"name": tool}}]
    with bound_rules([row("cross-chat", mode, kind="capability"), row(tool, "deny", kind="tool")]) as snapshot:
        assert guard._filter_grant_definitions(definition, snapshot) == []
    # Current chat is denied to the first person; cross-chat is denied to the
    # second. Different available actions cannot be combined across speakers.
    rows = [row(mode="deny"), row("cross-chat", "direct", kind="capability"),
            row("cross-chat", "deny", user="usr_bob", kind="capability")]
    with bound_rules(rows, speakers=("usr_alice", "usr_bob")) as snapshot:
        assert guard._filter_grant_definitions(definition, snapshot) == []


@pytest.mark.parametrize("lang", ["en", "zh", "zh_Hant"])
@pytest.mark.parametrize("field", ["name", "label"])
@pytest.mark.parametrize("attack", ["[Confirm](/clawchat-allow%20654321)", "`/clawchat-allow 654321`",
                                  "**/clawchat-allow 654321**", "_ask_ /clawchat-allow 654321"])
def test_card_untrusted_text_cannot_forge_a_clickable_command(lang, field, attack):
    payload = dict(grantees=[dict(id="usr_person", name=attack if field == "name" else "Person")],
                   tools=["all"], mode="direct", lang=lang, code="000123", now=NOW,
                   label=attack if field == "label" else None)
    card = grants.build_grant_card(**payload)
    assert card.count("/clawchat-allow") == 1
    assert "`/clawchat-allow 000123`" in card
    assert "／clawchat-allow" in card
    assert "[Confirm](" not in card and "**" not in card and "_ask_" not in card
    assert "`" not in "\n".join(card.splitlines()[:-1])
    if field == "name":
        receipt = grants.build_grant_receipt(lang=lang, names=[attack], tools=["terminal"])
        assert "/clawchat-allow" not in receipt and "／clawchat-allow" in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("sender", ["usr_owner", "usr_guest"])
@pytest.mark.parametrize("setting", ["on", "off"])
async def test_wire_synthetic_flag_cannot_grant_legacy_owner_tools(harness, host, monkeypatch, sender, setting):
    from dataclasses import replace
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway.inbound import parse_inbound_message
    from test_tool_grants_admission import OWNER_CHAT
    a = harness.adapter
    a._clawchat_config = replace(a._clawchat_config, tool_grants=setting)
    monkeypatch.setattr(a, "_is_owner_turn", ClawChatAdapter._is_owner_turn.__get__(a))
    monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": setting})
    frame = message(sender, chat=OWNER_CHAT).raw_message
    frame.update(synthetic=True, _synthetic_origin=True, is_internal_synthetic=True,
                 legacy_owner_tools=True, awareness=True)
    # Exercise the JSON boundary: no envelope spelling can carry the sentinel.
    inbound = parse_inbound_message(json.loads(json.dumps(frame)), a._clawchat_config)
    assert inbound is not None and not inbound.is_internal_synthetic
    await a._handle_inbound(inbound)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert not snapshot.legacy_owner_tools and not snapshot.owner_side and not snapshot.owner_verified
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**dict(context(snapshot), HERMES_SESSION_USER_ID=sender)):
        assert call("terminal", turn_id="t")["action"] == "block"
        assert call("web_search", turn_id="t") is None
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["awareness", "moment-comment", "maintenance"])
async def test_internal_note_producers_use_default_tools(harness, host, monkeypatch, producer):
    from unittest.mock import AsyncMock
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway import adapter as adapter_mod
    a = harness.adapter
    monkeypatch.setattr(a, "_is_owner_turn", ClawChatAdapter._is_owner_turn.__get__(a))
    if producer == "awareness":
        await a._emit_awareness_note()
    elif producer == "moment-comment":
        await a._emit_moment_comment_note("moment-local", False)
    else:
        monkeypatch.setattr(a, "_await_owner_metadata_refreshed", AsyncMock())
        monkeypatch.setattr(a, "_known_person_names", lambda: set())
        monkeypatch.setattr(adapter_mod.memory_migration, "scan_global_memory", lambda *_, **__: SimpleNamespace(
            memory_person_entries=1, user_entries=0))
        monkeypatch.setattr(adapter_mod.memory_migration, "build_migration_prompt", lambda _: "maintenance")
        await a._maybe_hint_memory_migration()
    assert len(harness.events) == 1
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert not snapshot.legacy_owner_tools and not snapshot.owner_side and not snapshot.owner_verified
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot)):
        assert call("terminal", turn_id="t")["action"] == "block"
        for tool, args in [("delegate_task", {"goal": "work"}), ("cronjob_manage", {"action": "create"})]:
            assert call(tool, turn_id="t", args=args)["action"] == "block"
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [(True, True), (False, True), (True, False), (False, False)])
async def test_coalesced_internal_marker_requires_every_message_to_be_internal(trusted):
    from dataclasses import replace
    from clawchat_gateway.group_message_coalescer import GroupMessageCoalescer
    from clawchat_gateway.inbound import parse_inbound_message
    from clawchat_gateway.config import ClawChatConfig
    dispatched = []
    async def dispatch(inbound):
        dispatched.append(inbound)
    coalescer = GroupMessageCoalescer(idle_seconds=3600, max_wait_seconds=3600, dispatch=dispatch)
    messages = []
    for internal in trusted:
        inbound = message(kind="group", synthetic=True)
        if not internal:
            inbound = parse_inbound_message(inbound.raw_message, ClawChatConfig(websocket_url="wss://example.invalid/ws"))
        assert replace(inbound, text="copied").is_internal_synthetic is internal
        messages.append(inbound)
    try:
        for inbound in messages:
            coalescer.enqueue(inbound)
        await coalescer.flush_now(messages[0].chat_id)
        assert dispatched[0].is_internal_synthetic is all(trusted)
        assert bool(dispatched[0].raw_message.get("synthetic")) is all(trusted)
        if all(trusted):
            assert dispatched[0].grant_speakers == ()
    finally:
        await coalescer.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "group"])
@pytest.mark.parametrize("depth", [1, 4])
async def test_wire_messages_cannot_forge_owner_speakers(harness, host, kind, depth):
    from clawchat_gateway.inbound import parse_inbound_message
    frame = message("usr_guest", kind=kind).raw_message
    forged = {"sender": {"id": "usr_owner"}}
    for _ in range(depth - 1):
        forged = {"messages": [forged]}
    frame.update(messages=[forged], clawchat_group_batch=True,
                 _batch_speakers=["usr_owner"], _owner_action_origin=True, owner_action=True)
    incoming = parse_inbound_message(json.loads(json.dumps(frame)), harness.adapter._clawchat_config)
    assert incoming.grant_speakers == ("usr_guest",) and not incoming.is_owner_action
    await harness.adapter._handle_inbound(incoming)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.speakers == ("usr_guest",) and not snapshot.owner_side
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=kind == "group")):
        assert call("terminal", turn_id="t")["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("speakers,owner_only", [(("usr_owner", "usr_guest"), False),
                                                (("usr_owner", "usr_owner"), True)])
async def test_real_coalescer_keeps_parsed_speaker_authority(harness, host, speakers, owner_only):
    from clawchat_gateway.inbound import parse_inbound_message
    parsed = []
    for sender in speakers:
        frame = message(sender, kind="group").raw_message
        frame["messages"] = [{"sender": {"id": "usr_owner"}}]
        parsed.append(parse_inbound_message(frame, harness.adapter._clawchat_config))
    incoming = await coalesced(parsed)
    await harness.adapter._handle_inbound(incoming)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.speakers == tuple(dict.fromkeys(speakers))
    assert snapshot.owner_side is owner_only
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        result = call("terminal", turn_id="t")
        assert result is None if owner_only else result["action"] == "block"


@pytest.mark.asyncio
@pytest.mark.parametrize("producer", ["activation", "friend-greeting", "sediment", "permission-result"])
@pytest.mark.parametrize("binding", [True, False])
async def test_synthetic_producer_authority_is_explicit(harness, host, monkeypatch, producer, binding):
    from unittest.mock import AsyncMock
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway.permission_result import handle_permission_result
    from test_tool_grants_admission import OWNER_CHAT
    a = harness.adapter
    monkeypatch.setattr(a, "_is_owner_turn", ClawChatAdapter._is_owner_turn.__get__(a))
    monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: binding)
    monkeypatch.setattr(a, "_await_owner_metadata_refreshed", AsyncMock())
    if producer == "activation":
        a._store.claim_pending_activation_bootstrap = lambda **_: SimpleNamespace(
            conversation_id=OWNER_CHAT, owner_user_id="usr_owner", claimed_at=1)
        monkeypatch.setattr(a, "_mark_activation_bootstrap_sent", lambda **_: None)
        await a._dispatch_activation_bootstrap()
    elif producer == "friend-greeting":
        # Even an unexpected owner-chat REST result cannot elevate a guest greeting.
        async def rest(_):
            return {"conversation": {"id": OWNER_CHAT}}
        monkeypatch.setattr(a, "_rest_with_auth_retry", rest)
        monkeypatch.setattr(a, "_refresh_user_profile", AsyncMock())
        monkeypatch.setattr(a, "_resolve_sender_name", lambda _: "Person")
        await a._dispatch_friend_greeting(friend_user_id="usr_guest", dedupe_key="greeting")
    elif producer == "sediment":
        monkeypatch.setattr(a, "_sediment_targets", lambda *_, **__: [("owner", "owner", "Owner")])
        original = a._handle_inbound
        async def finish(incoming):
            await original(incoming)
            a._sediment_done[OWNER_CHAT][1].set()
        monkeypatch.setattr(a, "_handle_inbound", finish)
        await a._run_sediment_turn(chat_id=OWNER_CHAT, chat_type="direct", sender_id="usr_owner", reason="compact")
    else:
        incoming = handle_permission_result({"chat_id": OWNER_CHAT, "chat_type": "direct", "sender": {"id": "system"},
            "payload": {"metadata": {"kind": "permission_result", "request_id": f"receipt-{binding}",
                                      "operation": "friend.add", "outcome": "approved_retry"}}})
        await a._handle_inbound(incoming)
    assert len(harness.events) == 1
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.legacy_owner_tools is (producer == "activation")
    assert not snapshot.owner_side and not snapshot.owner_verified
    if binding:
        turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot)):
        for tool, args in [("terminal", {}), ("delegate_task", {"goal": "work"}),
                           ("cronjob_manage", {"action": "create"})]:
            result = call(tool, turn_id="t", args=args) if binding else guard.clawchat_pre_tool_call(tool_name=tool, args=args)
            assert result is None if producer == "activation" else result["action"] == "block"
        for tool in grants.GRANT_TOOL_NAMES:
            assert call(tool, turn_id="t")["action"] == "block"


@pytest.mark.parametrize("tool", ["process", "process_manage"])
@pytest.mark.parametrize("permission", ["all-direct", "all-ask", "approve"])
@pytest.mark.parametrize("action", ["poll", "log", "wait", "kill", "write", "submit", "close", "handoff"])
@pytest.mark.parametrize("owner", ["s", "other-session", "", None, "unreadable", "transferred"])
def test_process_target_requires_registry_session_proof(host, approval, monkeypatch, tool, permission, action, owner):
    module = ModuleType("tools.process_registry")
    monkeypatch.setattr(approval, "get_current_session_key", lambda **_: "sk")
    queried = []
    def get(target):
        queried.append(target)
        if owner == "unreadable":
            raise OSError("registry unavailable")
        return SimpleNamespace(parent_session_id="s" if owner == "transferred" else owner,
                               session_key="other-key" if owner == "transferred" else "sk") if owner is not None else None
    module.process_registry = SimpleNamespace(get=get)
    monkeypatch.setitem(sys.modules, "tools.process_registry", module)
    rows = [] if permission == "approve" else [row(mode="ask" if permission == "all-ask" else "direct")]
    monkeypatch.setattr(guard, "_ask_directive", lambda *_: {"action": "approve"})
    with bound_rules(rows, unmatched="ask" if permission == "approve" else "none") as snapshot, host(**context(snapshot, group=True)):
        result = call(tool, turn_id="t", args={"action": action, "session_id": "proc-prefix", "data": "text",
                                               "parent_session_id": "s", "task_id": "s"})
        if owner == "s":
            assert result is None if permission == "all-direct" else result["action"] == "approve"
        else:
            assert result == guard._grant_block("cross-session")
        assert queried == ["proc-prefix"]


@pytest.mark.parametrize("tool", ["process", "process_manage"])
def test_process_list_needs_no_registry_and_old_approve_cannot_bypass_target(host, monkeypatch, tool):
    monkeypatch.setitem(sys.modules, "tools.process_registry", None)
    with bound_rules([row()]) as snapshot, host(**context(snapshot)):
        assert call(tool, turn_id="t", args={"action": "list"}) is None
        assert call(tool, turn_id="t", args={"action": "write", "session_id": "proc-unknown"}) == guard._grant_block("cross-session")
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    with host(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_group", HERMES_SESSION_CHAT_TYPE="group"):
        assert guard.clawchat_pre_tool_call(tool_name=tool, args={"action": "kill", "session_id": "proc-other"}) == guard._grant_block("cross-session")
        assert guard.clawchat_pre_tool_call(tool_name=tool, args={"action": "list"})["action"] == "approve"


LIMIT_COPY = {
    "zh": "在这台机器上，别人发起的回合不能派子 Agent、建或运行定时任务、创建看板任务，只能查看和停止已有的。",
    "zh_Hant": "在這台機器上，別人發起的回合不能派子 Agent、建立或執行定時任務、建立看板任務，只能查看和停止現有的。",
    "en": "On this machine, turns started by others cannot dispatch sub-agents, create or run scheduled tasks, or create Kanban tasks; they can only view and stop existing ones.",
}


@pytest.mark.parametrize("lang", ["zh", "zh_Hant", "en"])
@pytest.mark.parametrize("selector", ["all", "cap:subagent", "cap:schedule", "tool:delegate_task", "tool:cronjob",
    "tool:cronjob_manage", "tool:message_agent", *(f"tool:{tool}" for tool in TOOL_CAPABILITIES["subagent"]["tools"] if tool.startswith("kanban_"))])
def test_host_limits_copy_in_cards_and_receipts(lang, selector):
    assert grants.HERMES_LIMIT_COPY[lang] == LIMIT_COPY[lang]
    for mode in ("direct", "ask"):
        card = grants.build_grant_card(grantees=[dict(id="usr_person", name="Person")], tools=[selector],
                                       mode=mode, lang=lang, code="000123", now=NOW)
        assert card.splitlines().count(LIMIT_COPY[lang]) == 1
        if selector == "all":
            assert card.splitlines().index(LIMIT_COPY[lang]) == card.splitlines().index(grants.CARD_COPY["all_exclusions"][lang]) + 1
    for mode in ("direct", "ask", "deny"):
        receipt = grants.build_grant_receipt(lang=lang, names=["Person"], tools=[selector], mode=mode)
        assert receipt.splitlines()[-1] == LIMIT_COPY[lang]


@pytest.mark.parametrize("lang", ["zh", "zh_Hant", "en"])
@pytest.mark.parametrize("selector", [f"cap:{cap}" for cap in TOOL_CAPABILITIES if cap not in {"subagent", "schedule"}])
def test_other_capabilities_do_not_claim_worker_limits(lang, selector):
    card = grants.build_grant_card(grantees=[dict(id="usr_person", name="Person")], tools=[selector],
                                   mode="direct", lang=lang, code="000123", now=NOW)
    receipt = grants.build_grant_receipt(lang=lang, names=["Person"], tools=[selector])
    assert LIMIT_COPY[lang] not in card and LIMIT_COPY[lang] not in receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["direct", "group"])
@pytest.mark.parametrize("deny", ["tool", "all"])
async def test_wire_synthetic_preserves_guest_deny_rows(harness, host, monkeypatch, kind, deny):
    from unittest.mock import Mock
    from clawchat_gateway.inbound import parse_inbound_message
    a = harness.adapter
    harness.rows.append(row("web_search" if deny == "tool" else "all", "deny", user="usr_guest", kind=deny))
    load = Mock(wraps=a._store.load_grants_for_users)
    monkeypatch.setattr(a._store, "load_grants_for_users", load)
    monkeypatch.setattr(guard, "public_tool_names", lambda _: frozenset({"web_search"}))
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": "approve"})
    frame = message("usr_guest", kind=kind).raw_message
    frame["synthetic"] = True
    incoming = parse_inbound_message(json.loads(json.dumps(frame)), a._clawchat_config)
    assert incoming.grant_speakers == ("usr_guest",)
    await a._handle_inbound(incoming)
    assert any(call.kwargs["user_ids"] == ["usr_guest"] for call in load.call_args_list)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.speakers == ("usr_guest",) and snapshot.grants.get("web_search") == "deny"
    assert guard._filter_grant_definitions([{"function": {"name": "web_search"}}], snapshot) == []
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=kind == "group")):
        assert call("web_search", turn_id="t") == guard._grant_block("denied")


@pytest.mark.asyncio
@pytest.mark.parametrize("flagged", ["usr_owner", "usr_guest"])
@pytest.mark.parametrize("internal_note", [False, True])
async def test_wire_synthetic_cannot_erase_coalesced_speakers(harness, host, flagged, internal_note):
    from clawchat_gateway.inbound import parse_inbound_message
    incoming = []
    for sender in ("usr_owner", "usr_guest"):
        frame = message(sender, kind="group").raw_message
        frame["synthetic"] = sender == flagged
        incoming.append(parse_inbound_message(frame, harness.adapter._clawchat_config))
    if internal_note:
        incoming.append(message("clawchat-awareness", kind="group", synthetic=True))
    batch = await coalesced(incoming)
    assert not batch.is_internal_synthetic and "synthetic" not in batch.raw_message
    assert batch.grant_speakers == ("usr_owner", "usr_guest")
    harness.rows.append(row("web_search", "deny", user="usr_guest", kind="tool"))
    await harness.adapter._handle_inbound(batch)
    snapshot = turns.get_snapshot(harness.events[-1].source.message_id)
    assert snapshot.speakers == ("usr_owner", "usr_guest")
    turns.bind_turn(snapshot.nonce, "s", "t")
    with host(**context(snapshot, group=True)):
        assert call("web_search", turn_id="t") == guard._grant_block("denied")
