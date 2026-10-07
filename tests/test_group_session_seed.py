"""The first turn of a group's shared session is seeded with recent group messages.

When a group moves to one shared session (an upgrade from per-speaker sessions,
or the very first turn in a group) that session starts empty, while the
plugin's own message ledger already holds the group's recent history. The first
turn therefore carries the last ``rebuild-recent-messages`` messages (factory
20), within ``rebuild-recent-chars`` characters (factory 4000), oldest first,
each with its speaker. A persisted marker makes it happen once per group, not
once per process.

After that the session has the history itself, so an @-mention only adds the
group messages the session has *not* seen (for example ones a mention-only
group held back), never ones already delivered, and never the agent's own
replies. A legacy per-speaker group keeps the old mention prior-context.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import InboundMessage

GROUP = "cnv_group"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    assert a._store is not None
    return a


_clock = [1_000_000]


def record(a, message_id, text, *, sender="usr_ada", name="Ada", direction="inbound", chat=GROUP):
    _clock[0] += 1000
    frame = {"chat_id": chat, "sender": {"id": sender, "nick_name": name}, "payload": {"message_id": message_id}}
    a._store.insert_message(
        platform="hermes",
        account_id="default",
        kind="message",
        direction=direction,
        event_type="message.reply" if direction == "outbound" else "message.send",
        chat_id=chat,
        message_id=message_id,
        text=text,
        raw=frame if direction == "inbound" else {"chat_id": chat},
        created_at=_clock[0],
    )


def batch(*message_ids, mentioned=False):
    frames = [
        {"chat_id": GROUP, "sender": {"id": "usr_bo", "nick_name": "Bo"}, "payload": {"message_id": mid}}
        for mid in message_ids
    ]
    return InboundMessage(
        chat_id=GROUP,
        chat_type="group",
        sender_id="usr_bo",
        sender_name="Bo",
        text="ClawChat group messages: ...",
        raw_message={"clawchat_group_batch": True, "messages": frames},
        was_mentioned=mentioned,
    )


def test_first_shared_turn_is_seeded_once(adapter):
    record(adapter, "m1", "the meetup is on Friday", sender="usr_ada", name="Ada")
    record(adapter, "m2", "noted", direction="outbound")
    record(adapter, "x1", "other group chatter", chat="cnv_other")
    record(adapter, "m3", "what day is the meetup?", sender="usr_bo", name="Bo")
    seed = adapter._group_context_for_turn(batch("m3"))
    assert seed is not None
    assert "Ada (usr_ada): the meetup is on Friday" in seed
    assert "you: noted" in seed
    assert "other group chatter" not in seed
    assert "what day is the meetup?" not in seed  # already in this turn's batch
    assert seed.index("the meetup is on Friday") < seed.index("you: noted")
    # Once only — and the marker is persisted, so a new adapter does not reseed.
    record(adapter, "m4", "hello again", sender="usr_ada", name="Ada")
    assert adapter._group_context_for_turn(batch("m4")) is None
    assert adapter._store.has_group_shared_session("default", GROUP) is True


def test_seed_respects_message_and_char_limits(adapter):
    for i in range(30):
        record(adapter, f"n{i}", f"message number {i:02d}")
    adapter._clawchat_config = replace(adapter._clawchat_config, rebuild_recent_messages=5)
    seed = adapter._group_context_for_turn(batch("n29"))
    assert "message number 28" in seed and "message number 24" in seed
    assert "message number 23" not in seed


def test_seed_char_budget(adapter):
    for i in range(10):
        record(adapter, f"c{i}", f"{i}" * 300)
    adapter._clawchat_config = replace(adapter._clawchat_config, rebuild_recent_chars=1000)
    seed = adapter._group_context_for_turn(batch("c9"))
    body = seed.split("\n", 1)[1]
    assert len(body) <= 1000
    assert "8" * 300 in seed  # newest kept first
    assert "0" * 300 not in seed


def test_mention_after_seed_adds_only_unseen_messages(adapter):
    record(adapter, "s1", "seeded line")
    adapter._group_context_for_turn(batch("b0"))  # seeds
    record(adapter, "d1", "delivered in a batch")
    adapter._group_context_for_turn(batch("d1"))  # a normal batch delivers d1
    record(adapter, "g1", "held back by mention-only mode", sender="usr_cy", name="Cy")
    record(adapter, "r1", "agent reply", direction="outbound")
    record(adapter, "q1", "@agent what did Cy say?")
    context = adapter._group_context_for_turn(batch("q1", mentioned=True))
    assert "Cy (usr_cy): held back by mention-only mode" in context
    assert "delivered in a batch" not in context
    assert "seeded line" not in context
    assert "agent reply" not in context
    # Nothing new since -> no context.
    record(adapter, "q2", "@agent and now?")
    assert adapter._group_context_for_turn(batch("q2", mentioned=True)) is None


def test_no_mention_no_extra_context_after_seed(adapter):
    record(adapter, "s1", "seeded")
    adapter._group_context_for_turn(batch("b0"))
    record(adapter, "u1", "unseen but not a mention turn")
    assert adapter._group_context_for_turn(batch("b1")) is None


def test_legacy_per_speaker_group_keeps_old_mention_context(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, group_sessions_per_user=True)
    record(adapter, "p1", "earlier text")
    assert adapter._group_context_for_turn(batch("p2")) is None
    context = adapter._group_context_for_turn(batch("p2", mentioned=True))
    assert context.startswith("[ClawChat group prior context")
    assert adapter._store.has_group_shared_session("default", GROUP) is False


def test_rebuild_keys_read_from_plugin_config():
    cfg = ClawChatConfig.from_platform_config(
        type("P", (), {"extra": {"rebuild-recent-messages": 500, "rebuild-recent-chars": "2000"}})()
    )
    assert cfg.rebuild_recent_messages == 100
    assert cfg.rebuild_recent_chars == 2000
    default = ClawChatConfig.from_platform_config(type("E", (), {"extra": {}})())
    assert (default.rebuild_recent_messages, default.rebuild_recent_chars) == (20, 4000)
