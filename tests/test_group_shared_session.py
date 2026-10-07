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

"Explicitly" means written in ``config.yaml``. The top-level key cannot be read
from the runtime ``extra`` dict: before building any adapter, Hermes runs
``config.extra.setdefault("group_sessions_per_user",
self.config.group_sessions_per_user)``, and that host setting defaults to true,
so the key is present (and true) on every host whether an operator wrote it or
not. The top-level opt-in is therefore read from the raw
``platforms.clawchat.extra`` in config.yaml; per-group overrides (``groups``)
are never touched by the host and are read as passed.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import replace

import pytest

from clawchat_gateway import config as config_mod
from clawchat_gateway.adapter import GROUP_SHARED_SESSION_USER_ID, ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig, effective_group_sessions_per_user
from clawchat_gateway.inbound import InboundMessage


def _platform(extra):
    return type("P", (), {"extra": extra})()


def _cfg(extra, raw_extra=None):
    """Config as the adapter sees it: ``extra`` at runtime, ``raw_extra`` in config.yaml."""
    return ClawChatConfig.from_platform_config(
        _platform(extra), operator_extra=extra if raw_extra is None else raw_extra
    )


def _host_extra(extra):
    """The runtime extra after Hermes' _create_adapter (host default: true)."""
    extra = dict(extra)
    extra.setdefault("group_sessions_per_user", True)
    extra.setdefault("thread_sessions_per_user", False)
    return extra


@pytest.fixture
def raw_config(monkeypatch):
    """Stand in for the host's ``hermes_cli.config.read_raw_config``."""
    holder = {"config": {}}
    monkeypatch.setattr(
        sys.modules["hermes_cli.config"], "read_raw_config", lambda: holder["config"], raising=False
    )

    def set_extra(extra):
        holder["config"] = {"platforms": {"clawchat": {"extra": extra}}}

    return set_extra


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
def adapter(monkeypatch, tmp_path, raw_config):
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


def test_host_injected_default_does_not_opt_a_group_out(monkeypatch, tmp_path, raw_config, caplog):
    # Nothing in config.yaml; the host's setdefault puts group_sessions_per_user=True
    # into the runtime extra anyway. Groups must still share one session, silently.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw_config({"websocket_url": "wss://example.invalid/ws"})
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.config"):
        adapter = ClawChatAdapter(_platform(_host_extra({"websocket_url": "wss://example.invalid/ws"})))
    assert adapter._clawchat_config.group_sessions_per_user is False
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada")) == GROUP_SHARED_SESSION_USER_ID
    assert not [r for r in caplog.records if "group_sessions_per_user" in r.getMessage()]


def test_host_injected_default_without_a_config_file(monkeypatch, tmp_path, raw_config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = ClawChatAdapter(_platform(_host_extra({})))
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada")) == GROUP_SHARED_SESSION_USER_ID


def test_unreadable_host_config_falls_back_to_shared(monkeypatch, tmp_path):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(sys.modules["hermes_cli.config"], "read_raw_config", boom, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = ClawChatAdapter(_platform(_host_extra({})))
    assert adapter._clawchat_config.group_sessions_per_user is False


def test_explicit_true_in_config_yaml_is_honoured_through_the_host(monkeypatch, tmp_path, raw_config, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    raw_config({"group_sessions_per_user": True})
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.config"):
        adapter = ClawChatAdapter(_platform(_host_extra({"group_sessions_per_user": True})))
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada")) == "usr_ada"
    assert [r for r in caplog.records if "group_sessions_per_user" in r.getMessage()]


def test_explicit_per_group_true_is_honoured_through_the_host(monkeypatch, tmp_path, raw_config):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    groups = {"cnv_old": {"group_sessions_per_user": True}}
    raw_config({"groups": groups})
    adapter = ClawChatAdapter(_platform(_host_extra({"groups": groups})))
    assert adapter._session_user_id_for_inbound(_inbound("group", "usr_ada", "cnv_old")) == "usr_ada"
    assert (
        adapter._session_user_id_for_inbound(_inbound("group", "usr_ada", "cnv_new"))
        == GROUP_SHARED_SESSION_USER_ID
    )


def test_top_level_key_only_in_runtime_extra_is_ignored():
    assert _cfg({"group_sessions_per_user": True}, raw_extra={}).group_sessions_per_user is False


def test_without_operator_extra_the_top_level_key_is_ignored():
    cfg = ClawChatConfig.from_platform_config(_platform({"group_sessions_per_user": True}))
    assert cfg.group_sessions_per_user is False


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
