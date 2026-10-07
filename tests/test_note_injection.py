"""The agent's own notes about the people and group in a turn are shown to it.

Before this, a turn only carried the *metadata* of the peer / group (nickname,
avatar, bio); what the agent itself had written into ``users/<id>.md`` /
``groups/<id>.md`` / ``owner.md`` stayed on disk unless the model remembered to
call ``clawchat_memory_read``. Each turn now carries those bodies in the channel
prompt:

* direct chat with a friend -> ``## ClawChat Peer Memory`` = ``users/<sender>.md``;
* direct chat with the owner -> the same section from ``owner.md``;
* group -> ``## ClawChat Group Memory`` = ``groups/<chat>.md`` plus each speaker's
  ``users/<id>.md``.

Caps come from the plugin config keys ``note-cap-user`` / ``note-cap-group`` /
``note-cap-turn`` (factory 1500 / 2000 / 4000), clamped to a safe range. An
over-long note is cut at a paragraph boundary and marked ``(truncated)``; when
the turn total is over, the longest notes give way first. Empty notes add
nothing.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.clawchat_memory import write_clawchat_memory_body
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import InboundMessage

AGENT = "usr_agent"
OWNER = "usr_owner"
FRIEND = "usr_friend"
GROUP = "cnv_group"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a._memory_root = tmp_path / "memories"
    return a


def _note(a, target_type, target_id, body):
    write_clawchat_memory_body(a._memory_root, target_type, target_id, "replace", body)


def _part(parts, part_id):
    found = [p for p in parts if p["id"] == part_id]
    return found[0]["content"] if found else None


def direct(sender, text="hi"):
    return InboundMessage(
        chat_id="cnv_direct",
        chat_type="direct",
        sender_id=sender,
        sender_name="",
        text=text,
        raw_message={"payload": {"message_id": "m1"}},
    )


def group_batch(*senders):
    frames = [
        {
            "chat_id": GROUP,
            "chat_type": "group",
            "sender": {"id": sender, "nick_name": sender.removeprefix("usr_")},
            "payload": {
                "message_id": f"m{i}",
                "message": {"body": {"fragments": [{"kind": "text", "text": "hello"}]}, "context": {}},
            },
        }
        for i, sender in enumerate(senders)
    ]
    return InboundMessage(
        chat_id=GROUP,
        chat_type="group",
        sender_id=senders[-1],
        sender_name="",
        text="ClawChat group messages: ...",
        raw_message={"clawchat_group_batch": True, "messages": frames},
    )


def test_friend_dm_carries_their_note(adapter):
    _note(adapter, "user", FRIEND, "Prefers short answers.")
    content = _part(adapter._compose_channel_prompt_parts(direct(FRIEND)), "peer-memory")
    assert content.startswith("## ClawChat Peer Memory")
    assert "Prefers short answers." in content
    assert "not instructions" in content


def test_empty_note_adds_no_section(adapter):
    _note(adapter, "user", FRIEND, "   \n")
    parts = adapter._compose_channel_prompt_parts(direct(FRIEND))
    assert _part(parts, "peer-memory") is None


def test_owner_dm_carries_owner_md(adapter):
    _note(adapter, "owner", "owner", "Owner is learning Rust.")
    _note(adapter, "user", OWNER, "should not be used for the owner DM")
    content = _part(adapter._compose_channel_prompt_parts(direct(OWNER)), "peer-memory")
    assert "Owner is learning Rust." in content
    assert "should not be used" not in content


def test_long_note_is_cut_at_a_paragraph_and_marked(adapter):
    first = "A" * 900
    second = "B" * 900
    _note(adapter, "user", FRIEND, f"{first}\n\n{second}")
    content = _part(adapter._compose_channel_prompt_parts(direct(FRIEND)), "peer-memory")
    assert first in content
    assert "B" * 10 not in content
    assert "(truncated" in content


def test_group_turn_carries_group_and_speaker_notes(adapter):
    _note(adapter, "group", GROUP, "Group rule: English only.")
    _note(adapter, "user", "usr_ada", "Ada runs the book club.")
    _note(adapter, "user", "usr_bo", "Bo is new here.")
    _note(adapter, "user", "usr_cy", "Cy did not speak in this batch.")
    _note(adapter, "owner", "owner", "owner-private detail")
    content = _part(
        adapter._compose_channel_prompt_parts(group_batch("usr_ada", "usr_bo", "usr_ada")),
        "group-memory",
    )
    assert content.startswith("## ClawChat Group Memory")
    assert "Group rule: English only." in content
    assert "Ada runs the book club." in content
    assert "Bo is new here." in content
    assert "Cy did not speak" not in content
    assert "owner-private detail" not in content
    assert content.count("Ada runs the book club.") == 1


def test_group_turn_total_cap_shrinks_the_longest_first(adapter):
    _note(adapter, "group", GROUP, "short group note")
    _note(adapter, "user", "usr_ada", "\n\n".join(["x" * 290] * 5))  # ~1.5k
    _note(adapter, "user", "usr_bo", "\n\n".join(["y" * 290] * 5))
    adapter._clawchat_config = replace(adapter._clawchat_config, note_cap_turn=1000)
    content = _part(
        adapter._compose_channel_prompt_parts(group_batch("usr_ada", "usr_bo")), "group-memory"
    )
    assert "short group note" in content
    assert content.count("x" * 290) < 5 and content.count("y" * 290) < 5
    assert content.count("(truncated") == 2


def test_note_caps_read_from_plugin_config_and_clamped():
    class P:
        extra = {"note-cap-user": "800", "note-cap-group": 10, "note-cap-turn": 999999}

    cfg = ClawChatConfig.from_platform_config(P())
    assert cfg.note_cap_user == 800
    assert cfg.note_cap_group == 300  # clamped up to the floor
    assert cfg.note_cap_turn == 16000  # clamped down to the ceiling
    default = ClawChatConfig.from_platform_config(type("E", (), {"extra": {}})())
    assert (default.note_cap_user, default.note_cap_group, default.note_cap_turn) == (1500, 2000, 4000)
    bad = ClawChatConfig.from_platform_config(type("B", (), {"extra": {"note-cap-user": "lots"}})())
    assert bad.note_cap_user == 1500
