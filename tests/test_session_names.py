"""Hermes' session list names ClawChat sessions by who / which group.

Every ClawChat session was built with ``chat_name=<cnv_ id>`` and no
``user_name``, so Hermes' own session list (and its channel directory, which
resolves "send to <name>" through ``chat_name``) showed only conversation ids.

* A direct chat is named after the peer: ``chat_name`` and ``user_name`` are
  the peer's nickname (the owner's, in the owner's chat).
* A group is named after its title (``group_title`` from the group metadata).
  A group is one session shared by every speaker, so ``user_name`` stays empty:
  pinning the current speaker would label the whole shared session with one
  member. Only an operator-configured per-speaker group session gets the
  speaker's name.
* With nothing cached, the conversation id remains the fallback.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage

OWNER = "usr_owner"


def _inbound(chat_id, chat_type, sender_id, sender_name=""):
    return InboundMessage(
        chat_id=chat_id, chat_type=chat_type, sender_id=sender_id,
        sender_name=sender_name, text="hi", raw_message={},
    )


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, owner_user_id=OWNER, user_id="usr_agent")
    a._test_meta = {}

    def read(kind, key):
        return dict(a._test_meta.get((kind, key), {}))

    monkeypatch.setattr(a, "_read_memory_metadata", read)
    return a


def test_direct_chat_is_named_after_the_peer(adapter):
    adapter._test_meta[("user", "usr_ada")] = {"nickname": "Ada"}
    assert adapter._session_names(_inbound("cnv_dm", "direct", "usr_ada")) == ("Ada", "Ada")


def test_owner_chat_is_named_after_the_owner(adapter):
    adapter._test_meta[("owner", "owner")] = {"agent_owner_nickname": "Mia"}
    names = adapter._session_names(_inbound("cnv_owner", "direct", OWNER))
    assert names == ("Mia", "Mia")


def test_direct_chat_without_a_nickname_falls_back_to_the_id(adapter):
    assert adapter._session_names(_inbound("cnv_dm", "direct", "usr_x")) == ("cnv_dm", None)


def notification(chat="cnv_owner"):
    return InboundMessage.internal_synthetic(chat_id=chat, chat_type="direct",
        sender_id="clawchat-awareness", sender_name="ClawChat", text="ClawChat: contacts updated.", raw_message={})


def test_owner_notification_before_first_real_turn_uses_owner_metadata(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_owner_direct_chat_id", lambda: "cnv_owner")
    adapter._test_meta[("owner", "owner")] = {"agent_owner_nickname": "Owner"}
    assert adapter._session_names(notification()) == adapter._session_names(
        _inbound("cnv_owner", "direct", OWNER)) == ("Owner", "Owner")


def test_notification_reuses_peer_display_name_and_keeps_sender_attribution(adapter):
    real = _inbound("cnv_dm", "direct", "usr_peer", "Peer")
    expected = adapter._session_names(real)
    note = notification("cnv_dm")
    assert adapter._session_names(note) == expected == ("Peer", "Peer")
    assert note.sender_name == "ClawChat" and note.text.startswith("ClawChat:")


def test_notification_uses_cached_peer_metadata_without_previous_session_names(adapter):
    adapter._direct_chat_peers["cnv_dm"] = "usr_peer"
    adapter._test_meta[("user", "usr_peer")] = {"nickname": "Peer"}
    assert adapter._session_names(notification("cnv_dm")) == ("Peer", "Peer")


def test_notification_without_known_peer_uses_conversation_id(adapter):
    assert adapter._session_names(notification("cnv_unknown")) == ("cnv_unknown", None)


def test_shared_group_is_named_after_its_title_with_no_user_name(adapter):
    adapter._test_meta[("group", "cnv_g")] = {"group_title": "Spring Trip"}
    adapter._test_meta[("user", "usr_ada")] = {"nickname": "Ada"}
    names = adapter._session_names(_inbound("cnv_g", "group", "usr_ada", "Ada"))
    assert names == ("Spring Trip", None)


def test_per_speaker_group_session_keeps_the_speaker(adapter, monkeypatch):
    monkeypatch.setattr(adapter_mod, "effective_group_sessions_per_user", lambda *_a: True)
    adapter._test_meta[("group", "cnv_g")] = {"group_title": "Spring Trip"}
    names = adapter._session_names(_inbound("cnv_g", "group", "usr_ada", "Ada"))
    assert names == ("Spring Trip", "Ada")


def test_group_without_a_cached_title_falls_back_to_the_id(adapter):
    assert adapter._session_names(_inbound("cnv_g", "group", "usr_ada", "Ada")) == ("cnv_g", None)


class _Stop(Exception):
    pass


async def test_handle_inbound_builds_the_source_with_the_names(adapter, monkeypatch):
    adapter._test_meta[("user", "usr_ada")] = {"nickname": "Ada"}
    seen = {}

    def build_source(**kw):
        seen.update(kw)
        raise _Stop

    async def no_consent(_inbound):
        return False

    monkeypatch.setattr(adapter, "build_source", build_source, raising=False)
    monkeypatch.setattr(adapter, "_maybe_consume_skill_update_consent", no_consent)
    with pytest.raises(_Stop):
        await adapter._handle_inbound(_inbound("cnv_dm", "direct", "usr_ada"))
    assert seen["chat_name"] == "Ada"
    assert seen["user_name"] == "Ada"
    assert seen["chat_id"] == "cnv_dm"
