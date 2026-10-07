"""A group is one Hermes session, shared by everyone in it.

Groups used to default to one session per (group, speaker) — Hermes' default,
designed for channel bots where each person asks the bot for something on their
own. In a ClawChat group that split the group's memory: what A said was not in
the session B talked to, and every per-speaker session re-learned (and re-wrote)
the same group rules. The default is now one shared session per group; direct
chats are unchanged (one session per conversation).

The per-speaker mode is no longer offered. An existing config that explicitly
sets ``group_sessions_per_user: true`` (top level or per group) is still
honoured — silently overriding an operator's config would be worse — with a
one-time deprecation warning.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from clawchat_gateway import config as config_mod
from clawchat_gateway.adapter import GROUP_SHARED_SESSION_USER_ID, ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig, effective_group_sessions_per_user
from clawchat_gateway.inbound import InboundMessage


def _cfg(extra):
    return ClawChatConfig.from_platform_config(type("P", (), {"extra": extra})())


def _inbound(chat_type, sender="usr_ada", chat_id="cnv_g"):
    return InboundMessage(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id=sender,
        sender_name="",
        text="hi",
        raw_message={},
    )


@pytest.fixture(autouse=True)
def _reset_warning(monkeypatch):
    monkeypatch.setattr(config_mod, "_GROUP_SESSIONS_PER_USER_WARNED", False)


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return ClawChatAdapter({})


def test_default_is_one_session_per_group():
    cfg = _cfg({})
    assert cfg.group_sessions_per_user is False
    assert effective_group_sessions_per_user(cfg, "cnv_any") is False


def test_every_speaker_in_a_group_maps_to_the_same_session(adapter):
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada")) == GROUP_SHARED_SESSION_USER_ID
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_bo")) == GROUP_SHARED_SESSION_USER_ID


def test_direct_chats_keep_the_sender(adapter):
    assert adapter._session_user_id_for_inbound(_inbound("direct", "usr_ada", "cnv_d")) == "usr_ada"


def test_explicit_true_is_still_honoured(adapter):
    adapter._clawchat_config = replace(_cfg({"group_sessions_per_user": True}))
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada")) == "usr_ada"


def test_explicit_per_group_true_is_honoured_for_that_group_only():
    cfg = _cfg({"groups": {"cnv_old": {"group_sessions_per_user": True}}})
    assert effective_group_sessions_per_user(cfg, "cnv_old") is True
    assert effective_group_sessions_per_user(cfg, "cnv_other") is False


def test_explicit_true_logs_one_deprecation_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.config"):
        config_mod.warn_if_group_sessions_per_user_set(_cfg({"group_sessions_per_user": "true"}))
        config_mod.warn_if_group_sessions_per_user_set(_cfg({"group_sessions_per_user": True}))
    warnings = [r for r in caplog.records if "group_sessions_per_user" in r.getMessage()]
    assert len(warnings) == 1


def test_per_group_true_also_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.config"):
        config_mod.warn_if_group_sessions_per_user_set(
            _cfg({"groups": {"*": {"group_sessions_per_user": True}}})
        )
    assert any("group_sessions_per_user" in r.getMessage() for r in caplog.records)


def test_false_or_absent_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.config"):
        config_mod.warn_if_group_sessions_per_user_set(_cfg({}))
        config_mod.warn_if_group_sessions_per_user_set(_cfg({"group_sessions_per_user": False}))
    assert not [r for r in caplog.records if "group_sessions_per_user" in r.getMessage()]


def test_adapter_start_checks_for_the_deprecated_key(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        "clawchat_gateway.adapter.warn_if_group_sessions_per_user_set",
        lambda cfg: seen.append(cfg),
    )
    ClawChatAdapter({})
    assert len(seen) == 1
