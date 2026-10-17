"""Authorization belongs to one nonce and one execution identity."""
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import ModuleType
import os
import sys

import pytest

from clawchat_gateway import turn_grants as turns


def install_host_context(monkeypatch):
    module = ModuleType("gateway.session_context")
    module._VAR_MAP = {name: ContextVar(name) for name in (
        "HERMES_SESSION_MESSAGE_ID", "HERMES_SESSION_CHAT_ID", "HERMES_SESSION_CHAT_TYPE",
        "HERMES_SESSION_PLATFORM", "HERMES_SESSION_USER_ID",
    )}
    def read(name, default=""):
        return module._VAR_MAP[name].get(os.getenv(name, default))
    module.get_session_env = read
    monkeypatch.setitem(sys.modules, "gateway.session_context", module)

    @contextmanager
    def bind(**values):
        tokens = [(module._VAR_MAP[name], module._VAR_MAP[name].set(value))
                  for name, value in values.items()]
        try:
            yield
        finally:
            for var, token in reversed(tokens):
                var.reset(token)
    return bind


def snapshot(*, grants=None, speakers=("usr_a",), chat="cnv_chat", key="session", any_tools=frozenset()):
    return turns.register_turn(account_id="usr_agent", chat_id=chat, speakers=speakers,
                               grants={"mcp__docs__read": "direct"} if grants is None else grants,
                               session_key=key, any_granted_tools=any_tools)


@pytest.mark.parametrize("case", ["unbound", "turn", "session", "chat", "released", "empty_turn"])
def test_lookup_requires_exact_live_bound_turn(case):
    s = snapshot()
    try:
        if case != "unbound":
            turns.bind_turn(s.nonce, "session-a", "turn-a")
        if case == "released":
            turns.release_turn(s.nonce)
            assert turns.get_snapshot(s.nonce) is None
        assert turns.lookup_for_tool_call(s.nonce, "wrong" if case == "chat" else s.chat_id,
                                         "wrong" if case == "session" else "session-a",
                                         "" if case == "empty_turn" else "wrong" if case == "turn" else "turn-a") == {}
    finally:
        turns.release_turn(s.nonce)


def test_first_binding_is_immutable_and_copies_grants():
    grants = {"mcp__docs__read": "direct"}
    s = snapshot(grants=grants)
    try:
        grants.clear()
        with pytest.raises(TypeError):
            s.grants["terminal"] = "direct"
        with pytest.raises(FrozenInstanceError):
            s.chat_id = "other"
        turns.bind_turn(s.nonce, "s1", "t1")
        turns.bind_turn(s.nonce, "s2", "t2")
        assert turns.lookup_for_tool_call(s.nonce, s.chat_id, "s1", "t1") == {"mcp__docs__read": "direct"}
        assert turns.lookup_for_tool_call(s.nonce, s.chat_id, "s2", "t2") == {}
    finally:
        turns.release_turn(s.nonce)


def test_concurrent_registration_release_and_old_completion_are_isolated():
    def worker(i):
        s = snapshot(chat=f"chat-{i}", key=f"key-{i}")
        turns.bind_turn(s.nonce, f"s-{i}", f"t-{i}")
        assert turns.lookup_for_tool_call(s.nonce, s.chat_id, f"s-{i}", f"t-{i}")
        assert turns.lookup_for_tool_call(s.nonce, f"chat-{i + 1}", f"s-{i}", f"t-{i}") == {}
        turns.release_turn(s.nonce)
        assert turns.active_snapshot("usr_agent", f"key-{i}") is None
        return s.nonce
    with ThreadPoolExecutor(max_workers=8) as pool:
        nonces = list(pool.map(worker, range(100)))
    assert len(set(nonces)) == 100
    old, new = snapshot(), snapshot()
    turns.release_turn(old.nonce)
    assert turns.active_snapshot("usr_agent", "session") is new
    turns.release_turn(new.nonce)


def test_context_reader_never_uses_environment(monkeypatch):
    bind = install_host_context(monkeypatch)
    monkeypatch.setenv("HERMES_SESSION_MESSAGE_ID", "environment-nonce")
    assert turns.session_context_value("HERMES_SESSION_MESSAGE_ID") == ""
    with bind(HERMES_SESSION_MESSAGE_ID="bound-nonce"):
        assert turns.session_context_value("HERMES_SESSION_MESSAGE_ID") == "bound-nonce"
    assert turns.session_context_value("HERMES_SESSION_MESSAGE_ID") == ""
