"""Ask grants require a delivered owner card and consent for this call only."""
import sys
from concurrent.futures import ThreadPoolExecutor
from inspect import getclosurevars
from threading import Barrier, Event, Lock
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clawchat_gateway import host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.tool_grants import GRANT_ASK_DENIED_MESSAGE, GRANT_ASK_UNAVAILABLE_MESSAGE
from test_tool_grants_hook import host, context, call, TOOL
from test_turn_grants import snapshot
from test_tool_grants_admission import harness, message, OWNER_CHAT, CHAT
from test_command_gating import FakeApprovalQueue, _frame_text

_READ_APPROVAL_CONFIG = guard._approval_config_read


@pytest.fixture
def approval(monkeypatch):
    from clawchat_gateway import tools
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: SimpleNamespace(_owner_direct_chat_id=lambda: OWNER_CHAT))
    module = ModuleType("tools.approval")
    class ExactQueue(FakeApprovalQueue):
        def resolve_gateway_approval(self, session_key, choice, resolve_all=False, reason=None, request_id=None):
            if not request_id:
                return super().resolve_gateway_approval(session_key, choice, resolve_all, reason)
            pending = self.pending.get(session_key, [])
            matching = [item for item in pending if item.get("request_id") == request_id]
            if not matching:
                return 0
            for item in matching:
                pending.remove(item)
            self.resolved.append((session_key, choice, resolve_all, reason))
            return len(matching)

    queue = ExactQueue()
    module.has_blocking_approval = queue.has_blocking_approval
    module.resolve_gateway_approval = queue.resolve_gateway_approval
    module.list_gateway_approvals = lambda session_key: list(queue.pending.get(session_key, []))
    module._yolo_active = lambda: False
    module.approval_context = SimpleNamespace(_get_approval_mode=lambda: "manual")
    module.get_current_session_key = lambda: "sk"
    module.is_approved = lambda session, key: False
    module.persisted = []
    module.approve_session = lambda session, key: module.persisted.append((session, key))
    module.approve_permanent = lambda key: module.persisted.append(key)
    module._persist_choice = lambda session, choice, keys: module.persisted.append((session, choice, keys))
    module.outcome = {"approved": False, "message": "Host denial with private details"}
    module.request_tool_approval = lambda *args, **kw: module.outcome
    module.queue = queue
    package = ModuleType("tools")
    package.approval = module
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.approval", module)
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    monkeypatch.setattr(guard, "_approval_config_read", lambda: {})
    yield module
    with turns._lock:
        turns._approvals.clear()


def ask(host):
    s = snapshot(grants={TOOL: "ask"})
    turns.bind_turn(s.nonce, "s", "t")
    with host(**context(s)):
        directive = call(turn_id="t", args={"path": "/tmp/visible-only-to-owner"})
    turns.release_turn(s.nonce)
    return directive


@pytest.mark.parametrize("supports_approve", [False, True], ids=["pre-0.21", "0.21+"])
@pytest.mark.parametrize("policy", ["off", "approve"])
@pytest.mark.parametrize("mode", [None, "direct", "ask"])
def test_open_tool_ask_precedes_ordinary_access(host, approval, monkeypatch, supports_approve, policy, mode):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"non-owner-host-tools": policy})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: supports_approve)
    monkeypatch.setattr(guard, "_registry_toolset", lambda _: "web")
    s = snapshot(grants={"web_search": mode} if mode else {})
    turns.bind_turn(s.nonce, "s", "t")
    try:
        with host(**context(s)):
            result = call("web_search", turn_id="t", args={"query": "weather"})
        if mode != "ask":
            assert result is None
        elif supports_approve:
            assert result["action"] == "approve"
            assert result["rule_key"].startswith("clawchat-grant-ask:")
            assert "web_search" in result["message"]
        else:
            assert result == {"action": "block", "reasonCode": "ask-unavailable", "message": GRANT_ASK_UNAVAILABLE_MESSAGE, "blockReason": GRANT_ASK_UNAVAILABLE_MESSAGE}
    finally:
        turns.release_turn(s.nonce)


@pytest.mark.parametrize("failure", ["bypass", "directive_error", "gate_unavailable"])
def test_open_tool_unreliable_ask_fails_closed(host, approval, monkeypatch, failure):
    monkeypatch.setattr(guard, "_registry_toolset", lambda _: "web")
    if failure == "bypass":
        approval._yolo_active = lambda: True
    elif failure == "directive_error":
        monkeypatch.setattr(guard, "_ask_directive", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    else:
        del approval.list_gateway_approvals
    s = snapshot(grants={"web_search": "ask"})
    turns.bind_turn(s.nonce, "s", "t")
    try:
        with host(**context(s)):
            assert call("web_search", turn_id="t") == {"action": "block", "reasonCode": "ask-unavailable", "message": GRANT_ASK_UNAVAILABLE_MESSAGE, "blockReason": GRANT_ASK_UNAVAILABLE_MESSAGE}
    finally:
        turns.release_turn(s.nonce)


def test_concurrent_first_gate_install_has_one_layer_and_one_consent(approval, monkeypatch):
    start = Barrier(2)
    second_attempt = Event()

    class ContendedLock:
        def __init__(self):
            self.lock = Lock()
            self.counter_lock = Lock()
            self.attempts = 0

        def __enter__(self):
            with self.counter_lock:
                self.attempts += 1
                if self.attempts == 2:
                    second_attempt.set()
            self.lock.acquire()
            if not second_attempt.wait(5):
                self.lock.release()
                raise AssertionError("Both installers must reach the installation lock")

        def __exit__(self, *_):
            self.lock.release()

    lock = ContendedLock()
    monkeypatch.setattr(guard, "_GRANT_APPROVAL_GATE_LOCK", lock)
    originals = {name: getattr(approval, name) for name in
                 ("request_tool_approval", "_persist_choice", "approve_session", "approve_permanent")}

    def install():
        start.wait(timeout=5)
        return guard._install_grant_approval_gate()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: install(), range(2)))
    assert results == [True, True] and lock.attempts == 2
    for name, captured in (("request_tool_approval", "original"), ("_persist_choice", "persist"),
                           ("approve_session", "approve_session"), ("approve_permanent", "approve_permanent")):
        assert getclosurevars(getattr(approval, name)).nonlocals[captured] is originals[name]
    gate = approval.request_tool_approval
    assert guard._install_grant_approval_gate() and approval.request_tool_approval is gate
    record = turns.register_approval("sk", CHAT, TOOL, "one call", 600)
    assert turns.route_approval("sk", CHAT, record.description) is record
    turns.mark_approval_delivered(record.nonce)
    assert turns.approval_decision("sk", "once") == "once"
    approval.outcome = {"approved": True}
    kwargs = {"rule_key": "clawchat-grant-ask:" + record.nonce}
    assert gate(TOOL, record.description, **kwargs)["approved"]
    assert not gate(TOOL, record.description, **kwargs)["approved"]


@pytest.mark.parametrize("bypass", ["yolo", "off", "session_allowlist", "permanent_allowlist", "unreadable", "config_read_error"])
def test_ask_bypass_is_denied(host, approval, bypass, monkeypatch):
    if bypass == "yolo":
        approval._yolo_active = lambda: True
    elif bypass == "off":
        approval.approval_context._get_approval_mode = lambda: "off"
    elif bypass.endswith("allowlist"):
        approval.is_approved = lambda *_: True
    elif bypass == "config_read_error":
        monkeypatch.setattr(guard, "_approval_config_read", lambda: (_ for _ in ()).throw(OSError()))
    else:
        approval.is_approved = lambda *_: (_ for _ in ()).throw(RuntimeError())
    assert ask(host) == {"action": "block", "reasonCode": "ask-unavailable", "message": GRANT_ASK_UNAVAILABLE_MESSAGE, "blockReason": GRANT_ASK_UNAVAILABLE_MESSAGE}


@pytest.mark.parametrize("marker", ["host_fallback", "class_name", "attribute", "key"])
def test_failed_config_read_fallback_denies_ask(host, approval, monkeypatch, marker):
    class FailedConfigRead(dict):
        def __init__(self, data, *, error):
            super().__init__(data)
            self.read_error = error

    class MarkedConfig(dict):
        pass

    data = {"approvals": {"mode": "manual"}, "command_allowlist": []}
    if marker == "host_fallback":
        config = FailedConfigRead(data, error=OSError("unreadable config"))
    elif marker == "class_name":
        config = type("FailedConfigRead", (dict,), {})(data)
    elif marker == "attribute":
        config = MarkedConfig(data)
        config.read_error = OSError("unreadable config")
    else:
        config = dict(data, read_error="unreadable config")
    module = ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: config
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    monkeypatch.setattr(guard, "_approval_config_read", _READ_APPROVAL_CONFIG)
    with pytest.raises(ValueError, match="Unreadable approval policy"):
        guard._approval_config_read()
    assert ask(host) == {"action": "block", "reasonCode": "ask-unavailable", "message": GRANT_ASK_UNAVAILABLE_MESSAGE, "blockReason": GRANT_ASK_UNAVAILABLE_MESSAGE}


def test_readable_config_without_new_host_class_allows_ask(host, approval, monkeypatch):
    module = ModuleType("hermes_cli.config")
    module.load_config_readonly = lambda: {"approvals": {"mode": "manual"}, "command_allowlist": []}
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    monkeypatch.setitem(sys.modules, "hermes_cli.config_read_errors", ModuleType("hermes_cli.config_read_errors"))
    monkeypatch.setattr(guard, "_approval_config_read", _READ_APPROVAL_CONFIG)
    assert ask(host)["action"] == "approve"


def test_ask_rule_keys_are_unique_and_card_contains_speaker_type_and_raw_args(host, approval):
    s = turns.register_turn(account_id="usr_agent", chat_id=CHAT, speakers=("usr_a",),
                            speaker_names=("Speaker Name",), grants={TOOL: "ask"}, session_key="key")
    turns.bind_turn(s.nonce, "s", "t")
    try:
        with host(**context(s, group=True)):
            first, second = call(turn_id="t", args={"path": "/tmp/key"}), call(turn_id="t", args={"path": "/tmp/key"})
        assert first["action"] == second["action"] == "approve"
        assert first["rule_key"] != second["rule_key"]
        assert all(value in first["message"] for value in ("Speaker Name", "group", TOOL, '"path": "/tmp/key"'))
    finally:
        turns.release_turn(s.nonce)


def test_old_approval_layout_has_no_extracted_context_or_persist_helper(host, approval):
    del approval._yolo_active, approval.approval_context, approval._persist_choice
    approval._YOLO_MODE_FROZEN = False
    approval.is_current_session_yolo_enabled = lambda: False
    approval._get_approval_mode = lambda: "manual"
    assert ask(host)["action"] == "approve"
    approval.is_current_session_yolo_enabled = lambda: True
    assert ask(host)["message"] == GRANT_ASK_UNAVAILABLE_MESSAGE


@pytest.mark.parametrize("outcome", ["deny", "timeout", "error", "silent_bypass"])
def test_host_denial_timeout_and_unconfirmed_autoapproval_use_ask_message(host, approval, outcome):
    directive = ask(host)
    if outcome in {"deny", "timeout"}:
        record = turns.route_approval("sk", CHAT, directive["message"])
        turns.mark_approval_delivered(record.nonce)
    if outcome == "silent_bypass":
        approval.outcome = {"approved": True}
    if outcome == "error":
        del approval.outcome
    result = approval.request_tool_approval(TOOL, directive["message"], rule_key=directive["rule_key"])
    reason = "ask-declined" if outcome in {"deny", "timeout"} else "ask-unavailable"
    expected = GRANT_ASK_DENIED_MESSAGE if reason == "ask-declined" else GRANT_ASK_UNAVAILABLE_MESSAGE
    assert result == {"approved": False, "reasonCode": reason, "message": expected, "blockReason": expected}


def test_single_call_consent_and_no_persistence_even_external_scope_choice(host, approval):
    directive = ask(host)
    record = turns.route_approval("sk", CHAT, directive["message"])
    assert record and turns.approval_decision("sk", "once") == "once"
    turns.mark_approval_delivered(record.nonce)
    approval.outcome = {"approved": True}
    key = "plugin_rule:" + directive["rule_key"]
    approval._persist_choice("sk", "always", [key])
    approval.approve_session("sk", key)
    approval.approve_permanent(key)
    assert approval.persisted == []
    assert approval.request_tool_approval(TOOL, directive["message"], rule_key=directive["rule_key"])["approved"]
    assert not approval.request_tool_approval(TOOL, directive["message"], rule_key=directive["rule_key"])["approved"]
    approval.request_tool_approval("ordinary", "ordinary", rule_key="ordinary")
    approval._persist_choice("sk", "always", ["ordinary"])
    assert approval.persisted == [("sk", "always", ["ordinary"])]


def test_gate_rechecks_bypass_and_expired_or_parallel_records_fail_closed(host, approval):
    directive = ask(host)
    approval._yolo_active = lambda: True
    assert not approval.request_tool_approval(TOOL, directive["message"], rule_key=directive["rule_key"])["approved"]
    first = turns.register_approval("sk", CHAT, TOOL, "first", 600)
    second = turns.register_approval("sk", CHAT, TOOL, "second", 600)
    assert turns.route_approval("sk", CHAT, first.description)
    assert turns.route_approval("sk", CHAT, second.description)
    assert turns.approval_decision("sk", "once") == "deny"
    assert not turns.finish_approval(first.nonce) and not turns.finish_approval(second.nonce)
    expired = turns.register_approval("sk", CHAT, TOOL, "expired", -1)
    assert turns.route_approval("sk", CHAT, expired.description) is None
    assert not turns.finish_approval(expired.nonce)


async def forward(h, approval, *, chat=CHAT, description=None):
    a = h.adapter
    a._connection.send_frame = AsyncMock(return_value=True)
    a._known_chat_types[chat] = "group" if chat != CHAT else "direct"
    a._is_non_owner_direct_chat = lambda *args: True
    record = turns.register_approval("sk", chat, TOOL, "Speaker: Ada\nArguments: /tmp/key", 600)
    approval.queue.pending["sk"] = [{"pattern_key": "plugin_rule:clawchat-grant-ask:" + record.nonce, "request_id": record.nonce}]
    result = await a.send_exec_approval(chat, "<tool>", "sk", description or record.description)
    return record, result


@pytest.mark.asyncio
async def test_friend_grant_approval_routes_owner_and_restores_raw_description(harness, approval):
    record, result = await forward(harness, approval)
    assert result.success and not approval.queue.resolved
    frame = harness.adapter._connection.send_frame.call_args.args[0]
    assert frame["chat_id"] == OWNER_CHAT
    text = _frame_text(frame)
    assert "/tmp/key" in text and "Approve Once" in text
    assert "Approve Session" not in text and "Always Approve" not in text
    assert record.routed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["false", "error"])
async def test_ask_delivery_failure_denies_and_drops_owner_route(harness, approval, failure):
    a = harness.adapter
    a._is_non_owner_direct_chat = lambda *args: True
    a._connection.send_frame = AsyncMock(return_value=False)
    if failure == "error":
        a._connection.send_frame.side_effect = RuntimeError("delivery error")
    record = turns.register_approval("sk", CHAT, TOOL, "raw arguments", 600)
    approval.queue.pending["sk"] = [{"pattern_key": "plugin_rule:clawchat-grant-ask:" + record.nonce, "request_id": record.nonce}]
    if failure == "error":
        with pytest.raises(RuntimeError):
            await a.send_exec_approval(CHAT, "<tool>", "sk", record.description)
    else:
        assert not (await a.send_exec_approval(CHAT, "<tool>", "sk", record.description)).success
    assert record.choice == "deny" and approval.queue.resolved[-1][1] == "deny"
    assert a._owner_approval_route_table() == {}


@pytest.mark.asyncio
async def test_redacted_host_description_restores_raw_ask_args_by_nonce(harness, approval):
    a = harness.adapter
    a._is_non_owner_direct_chat = lambda *args: True
    a._connection.send_frame = AsyncMock(return_value=True)
    approval.queue.pending["sk"] = [{}]
    record = turns.register_approval("sk", CHAT, TOOL, "Arguments: private-raw-value", 600)
    redacted = record.description.replace("private-raw-value", "[REDACTED]")
    assert (await a.send_exec_approval(CHAT, "<tool>", "sk", redacted)).success
    assert "private-raw-value" in _frame_text(a._connection.send_frame.call_args.args[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["text", "interaction"])
async def test_synthetic_owner_approval_cannot_authorize_ask(harness, approval, channel):
    record, _ = await forward(harness, approval)
    a = harness.adapter
    a.send = AsyncMock()
    if channel == "text":
        code = next(iter(a._owner_approval_route_table()[OWNER_CHAT]))
        incoming = message("usr_owner", f"/approve {code}", chat=OWNER_CHAT, synthetic=True)
        assert await ClawChatAdapter._handle_owner_forwarded_approval(a, incoming)
    else:
        assert await a._handle_interaction_submit({"sender": {"id": "usr_owner"}, "chat_id": OWNER_CHAT,
            "synthetic": True, "payload": {"type": "exec_approval", "session_key": "sk", "decision": "once"}})
    assert record.choice == "deny"


@pytest.mark.asyncio
async def test_grant_decision_targets_nonce_without_resolving_ordinary_approval(harness, approval):
    record, _ = await forward(harness, approval)
    approval.queue.pending["sk"].insert(0, {"request_id": "ordinary", "pattern_key": "ordinary"})
    a = harness.adapter
    a.send = AsyncMock()
    code = next(iter(a._owner_approval_route_table()[OWNER_CHAT]))
    assert await ClawChatAdapter._handle_owner_forwarded_approval(
        a, message("usr_owner", f"/approve {code}", chat=OWNER_CHAT))
    assert record.choice == "once"
    assert approval.queue.pending["sk"] == [{"request_id": "ordinary", "pattern_key": "ordinary"}]
    assert a._resolve_gateway_approval("sk", "always", resolve_all=False) == 1
    assert approval.queue.resolved[-1][1] == "always"


@pytest.mark.asyncio
@pytest.mark.parametrize("command,choice", [("approve", "once"), ("deny", "deny")])
async def test_forwarded_approval_code_resolves_while_owner_turn_is_busy(harness, approval, command, choice):
    a = harness.adapter
    await a._handle_inbound(message("usr_owner", chat=OWNER_CHAT))
    owner_nonce = harness.events[0].source.message_id
    record, _ = await forward(harness, approval)
    a.send = AsyncMock()
    harness.monkeypatch.setattr(a, "_handle_owner_forwarded_approval",
                               ClawChatAdapter._handle_owner_forwarded_approval.__get__(a))
    code = next(iter(a._owner_approval_route_table()[OWNER_CHAT]))
    await a._on_message(message("usr_owner", f"/{command} {code}", chat=OWNER_CHAT).raw_message)
    assert record.choice == choice
    assert approval.queue.resolved[-1][1] == choice
    assert not a._grant_admission_queue._pending
    assert len(harness.events) == 1
    assert turns.get_snapshot(owner_nonce).owner_verified


@pytest.mark.asyncio
async def test_unavailable_owner_route_and_failed_delivery_deny(harness, approval):
    harness.adapter._owner_direct_chat_id = lambda: ""
    record, result = await forward(harness, approval)
    assert not result.success and record.choice == "deny"
    assert approval.queue.resolved[-1][1] == "deny"


@pytest.mark.asyncio
async def test_non_grant_friend_approval_still_denies_on_the_spot(harness, approval):
    a = harness.adapter
    a._is_non_owner_direct_chat = lambda *args: True
    a._connection.send_frame = AsyncMock(return_value=True)
    approval.queue.pending["sk"] = [{}]
    await a.send_exec_approval(CHAT, "sensitive command", "sk", "ordinary approval")
    assert approval.queue.resolved[-1][1] == "deny"
    frame = a._connection.send_frame.call_args.args[0]
    assert frame["chat_id"] == CHAT
    assert "sensitive command" not in _frame_text(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["session", "always", "ses", "permanent", "permanently", "s", "a", "all"])
async def test_text_scope_synonyms_are_denied_without_persistence(harness, approval, choice):
    record, _ = await forward(harness, approval)
    a = harness.adapter
    a.send = AsyncMock()
    code = next(iter(a._owner_approval_route_table()[OWNER_CHAT]))
    incoming = message("usr_owner", f"/approve {choice} {code}", chat=OWNER_CHAT)
    assert await ClawChatAdapter._handle_owner_forwarded_approval(a, incoming)
    assert record.choice == "deny" and approval.queue.resolved[-1][1] == "deny"
    assert approval.persisted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["session", "always", "permanent", "ses", "once", "approve", "deny"])
async def test_interaction_scopes_only_allow_once(harness, approval, choice):
    record, _ = await forward(harness, approval)
    a = harness.adapter
    a.send = AsyncMock()
    assert await a._handle_interaction_submit({"sender": {"id": "usr_owner"}, "chat_id": OWNER_CHAT,
        "payload": {"type": "exec_approval", "session_key": "sk", "decision": choice, "grant_nonce": record.nonce}})
    expected = "once" if choice in {"once", "approve"} else "deny"
    assert record.choice == expected and approval.queue.resolved[-1][1] == expected
    assert approval.persisted == []


@pytest.mark.asyncio
async def test_ask_interaction_carries_nonce_and_stale_ordinary_card_cannot_approve(harness, approval):
    record, _ = await forward(harness, approval)
    a = harness.adapter
    fragment = a._exec_approval_fragment("<tool>", record.description, "sk")
    assert [action["payload"]["decision"] for action in fragment["actions"]] == ["once", "deny"]
    assert all(action["payload"]["grant_nonce"] == record.nonce for action in fragment["actions"])
    a.send = AsyncMock()
    assert await a._handle_interaction_submit({"sender": {"id": "usr_owner"}, "chat_id": OWNER_CHAT,
        "payload": {"type": "exec_approval", "session_key": "sk", "decision": "once"}})
    assert record.choice == "deny"


def test_new_ask_denial_prose_does_not_disclose_private_details():
    assert GRANT_ASK_DENIED_MESSAGE == "The owner did not approve this call or has not answered yet, so it did not run. Do not retry this request. They can try later or ask the owner directly."
    for forbidden in ("Ada", "usr_", "tool-grants", "approvals.mode", "/tmp", "0.21", TOOL, "terminal"):
        assert forbidden not in GRANT_ASK_DENIED_MESSAGE
