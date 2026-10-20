"""Group exec approvals forwarded to the owner's direct chat.

Two bugs are pinned here:

* **Host call shape.** Current Hermes hosts call ``send_exec_approval`` with
  ``allow_permanent`` / ``allow_session`` / ``smart_denied`` keyword flags
  (``gateway/run_turn_runner.py``). An adapter signature without them raised
  ``TypeError``; the host swallowed it and fell back to a plain-text send that
  recorded no route, so the owner's ``/approve`` never reached the group session.
* **Concurrent routes.** Every group's card lands in the same owner DM; a single
  ``owner_dm -> session_key`` slot let the second card overwrite the first.
  Each forwarded card now carries a short code the owner can quote.

The host approval queue (``tools.approval``) is replaced by a small fake that
records which session each decision was resolved against.
"""

from __future__ import annotations

import json
import re
import sys
import types
from typing import Any

import pytest

import clawchat_gateway.adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage

OWNER = "usr_owner_test"
OWNER_DM = "cnv_owner_dm_test"
GROUP_A = "cnv_group_a_test"
GROUP_B = "cnv_group_b_test"

CODE_RE = re.compile(r"/approve ([A-Z][2-9])\b")


class FakeApprovalHost:
    def __init__(self) -> None:
        self.pending: dict[str, int] = {}
        self.calls: list[tuple[str, str, bool]] = []

    def block(self, session_key: str, count: int = 1) -> None:
        self.pending[session_key] = self.pending.get(session_key, 0) + count

    def has_blocking_approval(self, session_key: str) -> bool:
        return self.pending.get(session_key, 0) > 0

    def resolve_gateway_approval(
        self,
        session_key: str,
        choice: str,
        resolve_all: bool = False,
        reason: str | None = None,
        request_id: str | None = None,
    ) -> int:
        self.calls.append((session_key, choice, resolve_all))
        n = self.pending.get(session_key, 0)
        if not n:
            return 0
        resolved = n if resolve_all else 1
        self.pending[session_key] = n - resolved
        return resolved


class FakeConnection:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.accept = True

    async def send_frame(self, frame: dict[str, Any], wait_for_ack: bool = False) -> bool:
        self.frames.append(frame)
        return self.accept


@pytest.fixture
def host(monkeypatch) -> FakeApprovalHost:
    fake = FakeApprovalHost()
    tools_pkg = types.ModuleType("tools")
    approval = types.ModuleType("tools.approval")
    approval.has_blocking_approval = fake.has_blocking_approval
    approval.resolve_gateway_approval = fake.resolve_gateway_approval
    tools_pkg.approval = approval
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    return fake


@pytest.fixture
def adapter(host) -> ClawChatAdapter:
    a = ClawChatAdapter({})
    a._connection = FakeConnection()
    a._owner_direct_chat_id = lambda: OWNER_DM  # type: ignore[method-assign]
    a._owner_user_id = lambda: OWNER  # type: ignore[method-assign]
    a._known_chat_types[GROUP_A] = "group"
    a._known_chat_types[GROUP_B] = "group"
    a.sent_replies = []  # type: ignore[attr-defined]

    async def fake_send(chat_id, content="", reply_to=None, metadata=None, **kwargs):
        a.sent_replies.append((chat_id, content))  # type: ignore[attr-defined]
        return None

    a.send = fake_send  # type: ignore[method-assign]
    return a


def frame_text(frame: dict[str, Any]) -> str:
    return json.dumps(frame, ensure_ascii=False)


def last_code(adapter) -> str:
    match = CODE_RE.search(frame_text(adapter._connection.frames[-1]))
    assert match, "forwarded card carries no approval code"
    return match.group(1)


async def host_style_request(adapter, chat_id: str, session_key: str, **flags):
    """Exactly the call shape of Hermes ``TurnRunner._approval_notify_sync``.

    The real host enqueues the approval before calling the notify callback, so
    the fake host is told the session is blocked first.
    """
    adapter._test_host.block(session_key)
    defaults = {"allow_permanent": True, "allow_session": True, "smart_denied": False}
    defaults.update(flags)
    return await adapter.send_exec_approval(
        chat_id=chat_id,
        command="echo approval-test",
        session_key=session_key,
        description="approval test",
        metadata=None,
        **defaults,
    )


@pytest.fixture(autouse=True)
def _bind_host(adapter, host):
    adapter._test_host = host


def owner_dm(text: str, sender: str = OWNER) -> InboundMessage:
    return InboundMessage(
        chat_id=OWNER_DM,
        chat_type="direct",
        sender_id=sender,
        sender_name="owner",
        text=text,
        raw_message={},
    )


def last_reply(adapter) -> str:
    assert adapter.sent_replies, "no reply sent to the owner"
    return adapter.sent_replies[-1][1]


# --- host call shape -----------------------------------------------------------


async def test_host_call_shape_routes_card_to_owner_dm(adapter):
    result = await host_style_request(adapter, GROUP_A, "sk-a")
    assert result.success
    frames = adapter._connection.frames
    assert len(frames) == 1
    assert OWNER_DM in frame_text(frames[0])
    assert adapter._owner_approval_route_chat_id("sk-a") == OWNER_DM


async def test_legacy_positional_call_shape_still_works(adapter, host):
    host.block("sk-a")
    result = await adapter.send_exec_approval(GROUP_A, "echo approval-test", "sk-a")
    assert result.success
    assert adapter._owner_approval_route_chat_id("sk-a") == OWNER_DM


async def test_unknown_future_kwargs_are_accepted(adapter):
    result = await host_style_request(adapter, GROUP_A, "sk-a", some_future_flag=1)
    assert result.success


async def test_card_offers_only_allowed_scopes(adapter):
    await host_style_request(adapter, GROUP_A, "sk-a", allow_permanent=False)
    text = frame_text(adapter._connection.frames[-1])
    assert "approve session" in text
    assert "approve always" not in text

    await host_style_request(adapter, GROUP_B, "sk-b", smart_denied=True, allow_session=False, allow_permanent=False)
    text = frame_text(adapter._connection.frames[-1])
    assert "approve session" not in text
    assert "approve always" not in text
    assert "/deny" in text


# --- single approval UX --------------------------------------------------------


async def test_single_forwarded_approval_resolves_with_bare_approve(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    handled = await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert handled
    assert host.calls == [("sk-a", "once", False)]
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
    assert last_reply(adapter).startswith("Approved once")


async def test_single_forwarded_approval_keeps_scope_variants(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    assert await adapter._handle_owner_forwarded_approval(owner_dm("/approve session"))
    assert host.calls == [("sk-a", "session", False)]


async def test_card_shows_code(adapter):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code = last_code(adapter)
    text = frame_text(adapter._connection.frames[-1])
    assert f"/deny {code}" in text


async def test_non_owner_and_non_approval_texts_are_not_intercepted(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    assert not await adapter._handle_owner_forwarded_approval(owner_dm("/approve", sender="usr_other"))
    assert not await adapter._handle_owner_forwarded_approval(owner_dm("hello"))
    assert host.calls == []


async def test_no_pending_route_falls_through_to_host(adapter, host):
    assert not await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert host.calls == []


# --- concurrent group approvals ------------------------------------------------


async def test_two_groups_each_decision_reaches_its_own_session(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code_a = last_code(adapter)
    await host_style_request(adapter, GROUP_B, "sk-b")
    code_b = last_code(adapter)
    assert code_a != code_b

    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve {code_a}"))
    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/deny {code_b.lower()}"))

    assert host.calls == [("sk-a", "once", False), ("sk-b", "deny", False)]
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
    assert adapter._owner_approval_route_chat_id("sk-b") == ""


async def test_code_and_scope_in_either_order(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code_a = last_code(adapter)
    await host_style_request(adapter, GROUP_B, "sk-b")
    code_b = last_code(adapter)
    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve always {code_b}"))
    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve {code_a} session"))
    assert host.calls == [("sk-b", "always", False), ("sk-a", "session", False)]


async def test_bare_approve_with_two_pending_resolves_nothing(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code_a = last_code(adapter)
    await host_style_request(adapter, GROUP_B, "sk-b")
    code_b = last_code(adapter)

    assert await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert host.calls == []
    reply = last_reply(adapter)
    assert code_a in reply and code_b in reply
    assert GROUP_A in reply and GROUP_B in reply

    assert await adapter._handle_owner_forwarded_approval(owner_dm("/deny all"))
    assert host.calls == []


async def test_unknown_code_is_an_error_and_resolves_nothing(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code_a = last_code(adapter)
    wrong = "Z9" if code_a != "Z9" else "Y8"
    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve {wrong}"))
    assert host.calls == []
    reply = last_reply(adapter)
    assert wrong in reply and code_a in reply
    # The real one is still resolvable afterwards.
    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve {code_a}"))
    assert host.calls == [("sk-a", "once", False)]


async def test_stale_code_with_nothing_pending_is_an_error_not_a_host_approval(adapter, host):
    assert await adapter._handle_owner_forwarded_approval(owner_dm("/approve K4"))
    assert host.calls == []
    assert "K4" in last_reply(adapter)


async def test_codes_are_not_reused_while_pending(adapter, host):
    codes = set()
    for i in range(30):
        await host_style_request(adapter, GROUP_A, f"sk-{i}")
        codes.add(last_code(adapter))
    assert len(codes) == 30


# --- cleanup ---------------------------------------------------------------------


async def test_host_timeout_prunes_route(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    code_a = last_code(adapter)
    await host_style_request(adapter, GROUP_B, "sk-b")
    host.pending["sk-a"] = 0  # host gave up waiting (approvals.timeout)

    # Only one live approval remains, so bare /approve is unambiguous again.
    assert await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert host.calls == [("sk-b", "once", False)]
    assert adapter._owner_approval_route_chat_id("sk-a") == ""

    assert await adapter._handle_owner_forwarded_approval(owner_dm(f"/approve {code_a}"))
    assert host.calls == [("sk-b", "once", False)]
    assert code_a in last_reply(adapter)


async def test_ttl_expiry_prunes_route(adapter, host, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: now[0])
    await host_style_request(adapter, GROUP_A, "sk-a")
    now[0] += adapter_mod.OWNER_APPROVAL_ROUTE_TTL_SECONDS + 1
    assert not await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert host.calls == []
    assert adapter._owner_approval_route_chat_id("sk-a") == ""


async def test_failed_send_records_no_route_and_frees_code(adapter, host):
    adapter._connection.accept = False
    result = await host_style_request(adapter, GROUP_A, "sk-a")
    assert not result.success
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
    assert not await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))


async def test_group_dissolution_drops_its_routes(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    await host_style_request(adapter, GROUP_B, "sk-b")
    adapter._evict_chat_state(GROUP_A)
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
    assert await adapter._handle_owner_forwarded_approval(owner_dm("/approve"))
    assert host.calls == [("sk-b", "once", False)]


async def test_owner_dm_dissolution_drops_all_routes(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    await host_style_request(adapter, GROUP_B, "sk-b")
    adapter._evict_chat_state(OWNER_DM)
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
    assert adapter._owner_approval_route_chat_id("sk-b") == ""


async def test_interaction_submit_clears_code_route(adapter, host):
    await host_style_request(adapter, GROUP_A, "sk-a")
    frame = {
        "chat_id": OWNER_DM,
        "sender": {"id": OWNER},
        "payload": {"type": "exec_approval", "session_key": "sk-a", "decision": "deny"},
    }
    assert await adapter._handle_interaction_submit(frame)
    assert host.calls == [("sk-a", "deny", False)]
    assert adapter._owner_approval_route_chat_id("sk-a") == ""
