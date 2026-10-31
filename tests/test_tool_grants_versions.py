"""Capability probes and actual turn ids define the four supported tiers."""
from types import SimpleNamespace

import pytest

from clawchat_gateway import host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.tool_grants import GRANT_BLOCK_MESSAGE, GRANT_ASK_UNAVAILABLE_MESSAGE, GRANT_TOOL_NAMES
from test_tool_grants_hook import host, context, call, TOOL
from test_turn_grants import snapshot
from test_tool_grants_ask import approval


@pytest.mark.parametrize("tier,has_turn,override,approve", [
    ("0.12.0-0.19.0", False, False, False),
    ("0.19.1-0.20.0", True, False, False),
    ("0.20.1-0.20.x", True, True, False),
    ("0.21.0+", True, True, True),
])
def test_four_version_tiers(host, approval, monkeypatch, tier, has_turn, override, approve):
    monkeypatch.setattr(guard, "host_supports_toolset_override", lambda: override)
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: approve)
    if not has_turn:
        monkeypatch.setattr(guard, "_host_supports_pre_llm_call", lambda: False)
    direct, ask = snapshot(key="direct"), snapshot(grants={TOOL: "ask"}, key="ask")
    owner = turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", speakers=("usr_owner",),
                                grants={}, session_key="owner", owner_verified=True, owner_side=True)
    try:
        # These are independent executions; each needs its own turn identity.
        for s in (direct, ask, owner):
            turns.bind_turn(s.nonce, "s", s.nonce)
        with host(**context(direct)):
            result = call(**({"turn_id": direct.nonce} if has_turn else {}))
            assert result is None if has_turn else result["message"] == GRANT_BLOCK_MESSAGE
        source = SimpleNamespace(chat_id=direct.chat_id, chat_type="dm", user_id="usr_a", message_id=direct.nonce)
        if override:
            assert "docs" in guard.toolsets_for_source(source, owner_user_id="usr_owner")
        with host(**context(ask)):
            result = call(**({"turn_id": ask.nonce} if has_turn else {}))
            if approve:
                assert result["action"] == "approve"
            else:
                assert result["action"] == "block"
                assert result["message"] == (GRANT_ASK_UNAVAILABLE_MESSAGE if has_turn else GRANT_BLOCK_MESSAGE)
        with host(**context(owner)):
            for tool in GRANT_TOOL_NAMES:
                result = call(tool, **({"turn_id": owner.nonce} if has_turn else {}))
                assert result is None if has_turn else result == {"action": "block", "message": guard.MANAGEMENT_UPGRADE_MESSAGE}
    finally:
        for s in (direct, ask, owner):
            turns.release_turn(s.nonce)


def test_unknown_host_and_missing_turn_fail_closed(host, approval, monkeypatch):
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: False)
    s = snapshot(grants={TOOL: "ask"})
    try:
        with host(**context(s)):
            turns.bind_turn(s.nonce, "s", "t")
            assert call(turn_id="t")["message"] == GRANT_ASK_UNAVAILABLE_MESSAGE
            assert call()["action"] == "block"
    finally:
        turns.release_turn(s.nonce)
