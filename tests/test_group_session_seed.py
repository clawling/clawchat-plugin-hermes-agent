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


# --- speaker names ------------------------------------------------------------
#
# The hub often sends ``sender.nick_name`` equal to the user id. Live batches
# resolve the name from the agent's cached profile metadata; the seed and
# catch-up lines must do the same instead of printing a bare ``usr_…``.


def _profile(a, target_type, target_id, **metadata):
    from clawchat_gateway.clawchat_memory import write_clawchat_metadata

    write_clawchat_metadata(a._memory_root, target_type, target_id, metadata)


@pytest.fixture
def named_adapter(adapter, tmp_path):
    adapter._memory_root = tmp_path / "memories"
    return adapter


def test_seed_line_uses_the_cached_profile_name_when_the_hub_sends_the_id(named_adapter):
    a = named_adapter
    _profile(a, "user", "usr_ada", nickname="Ada")
    record(a, "k1", "hello from Ada", sender="usr_ada", name="usr_ada")
    record(a, "k2", "no profile cached", sender="usr_zed", name="usr_zed")
    seed = a._group_context_for_turn(batch("k3"))
    assert "Ada (usr_ada): hello from Ada" in seed
    assert "usr_zed: no profile cached" in seed  # nothing better known


def test_seed_line_names_the_agent_owner_and_the_group_owner(named_adapter):
    a = named_adapter
    _profile(a, "owner", "owner", agent_owner_nickname="Olive")
    _profile(a, "group", GROUP, group_owner_id="usr_gina", group_owner_nickname="Gina")
    record(a, "o1", "owner speaking", sender="usr_owner", name="usr_owner")
    record(a, "o2", "group owner speaking", sender="usr_gina", name="usr_gina")
    seed = a._group_context_for_turn(batch("o3"))
    assert "Olive (usr_owner): owner speaking" in seed
    assert "Gina (usr_gina): group owner speaking" in seed


def test_catch_up_line_uses_the_cached_profile_name(named_adapter):
    a = named_adapter
    _profile(a, "user", "usr_cy", nickname="Cy")
    record(a, "s1", "seeded")
    a._group_context_for_turn(batch("b0"))
    record(a, "u1", "held back", sender="usr_cy", name="usr_cy")
    context = a._group_context_for_turn(batch("q1", mentioned=True))
    assert "Cy (usr_cy): held back" in context


def test_a_real_nick_name_from_the_hub_still_wins(named_adapter):
    a = named_adapter
    _profile(a, "user", "usr_ada", nickname="Old Name")
    record(a, "r1", "hi", sender="usr_ada", name="Ada Now")
    seed = a._group_context_for_turn(batch("r2"))
    assert "Ada Now (usr_ada): hi" in seed


# --- slash commands -----------------------------------------------------------
#
# Commands are instructions to the gateway, not conversation: after /new the
# catch-up must not tell the session about /new, /approve or /cancel.


def _command_frame(message_id, mention_id=None, display=None, command="/new"):
    fragments = []
    if mention_id:
        fragments.append({"kind": "mention", "user_id": mention_id, "display": display})
    fragments.append({"kind": "text", "text": command})
    return {
        "chat_id": GROUP,
        "sender": {"id": "usr_ada", "nick_name": "Ada"},
        "payload": {"message_id": message_id, "message": {"body": {"fragments": fragments}}},
    }


def record_raw(a, message_id, text, frame):
    _clock[0] += 1000
    a._store.insert_message(
        platform="hermes", account_id="default", kind="message", direction="inbound",
        event_type="message.send", chat_id=GROUP, message_id=message_id, text=text,
        raw=frame, created_at=_clock[0],
    )


def test_seed_leaves_out_slash_commands(adapter):
    record(adapter, "c1", "real talk")
    record(adapter, "c2", "/new")
    record(adapter, "c3", "/approve session")
    record(adapter, "c4", "  /cancel")
    record(adapter, "c5", "/usr/local/bin is on the path")  # a path, not a command
    seed = adapter._group_context_for_turn(batch("c6"))
    assert "real talk" in seed
    assert "/new" not in seed and "/approve" not in seed and "/cancel" not in seed
    assert "/usr/local/bin is on the path" in seed


def test_catch_up_leaves_out_slash_commands_even_after_a_mention(adapter):
    record(adapter, "s1", "seeded")
    adapter._group_context_for_turn(batch("b0"))
    record(adapter, "u1", "/new")
    record_raw(adapter, "u2", "@Agent /approve", _command_frame("u2", "usr_agent", "Agent", "/approve"))
    record(adapter, "u3", "an ordinary message")
    context = adapter._group_context_for_turn(batch("q1", mentioned=True))
    assert "an ordinary message" in context
    assert "/new" not in context and "/approve" not in context


def test_only_commands_unseen_means_no_catch_up(adapter):
    record(adapter, "s1", "seeded")
    adapter._group_context_for_turn(batch("b0"))
    record(adapter, "u1", "/new")
    assert adapter._group_context_for_turn(batch("q1", mentioned=True)) is None
