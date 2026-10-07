""""@name" typed in a group reply becomes a real mention of that member.

A reply used to be one text fragment with an empty ``context.mentions``, so an
agent writing "@Bean have a look" in a group never woke Bean — only the mention
tool with an explicit user id did. Before a group reply goes out, the adapter
now turns "@<member name>" into a ``mention`` fragment and lists it in
``context.mentions`` (docs/client-integration.md §7.1, §10.2), with the same
rules as the reference client:

* an ``@`` starts a mention unless the character before it is an e-mail
  local-part character (``[A-Za-z0-9._%+-]``) — CJK right before it is fine;
* the longest member name that follows wins; an ASCII name must not run on
  into further ASCII letters/digits; case-insensitive for ASCII only, and an
  exact-case match beats a folded one of the same length;
* two different members tied at the same length: no mention;
* never the agent itself, never the ``all`` sentinel;
* anything not recognised stays plain text.

Direct chats and process ("thinking") messages are left alone.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.mention_autolink import autolink_mentions, has_mention_candidate, mentions_in

ME = "usr_me"
ROSTER = [("usr_q", "大Q"), ("usr_bean", "Bean"), ("usr_spark", "Spark")]


def _shape(fragments):
    out = []
    for f in fragments:
        if f["kind"] == "mention":
            out.append(f"@{f['display']}→{f['user_id']}")
        else:
            out.append(f["text"])
    return "|".join(out)


@pytest.mark.parametrize(
    ("text", "roster", "expected"),
    [
        ("@大Q 看一下", ROSTER, "@大Q→usr_q| 看一下"),
        ("让@大Q看一下", ROSTER, "让|@大Q→usr_q|看一下"),
        ("@大Q，辛苦", ROSTER, "@大Q→usr_q|，辛苦"),
        ("这个问题问 @Bean", ROSTER, "这个问题问 |@Bean→usr_bean"),
        ("@bean 在吗", ROSTER, "@Bean→usr_bean| 在吗"),
        ("@Lib Fan 你看", [("usr_lb", "Lib Fan")], "@Lib Fan→usr_lb| 你看"),
        ("@大Q 来", [("usr_q1", "Q"), ("usr_dq", "大Q")], "@大Q→usr_dq| 来"),
        ("@大Q 和 @Spark 一起看", ROSTER, "@大Q→usr_q| 和 |@Spark→usr_spark| 一起看"),
        ("@所有人 注意", ROSTER, "@所有人 注意"),
        ("@所有人 注意", [("all", "所有人")], "@所有人 注意"),
        ("@折光 在吗", [(ME, "折光")], "@折光 在吗"),
        ("@Nobody 在吗", ROSTER, "@Nobody 在吗"),
        ("写信到 alice@bean 就行", [("usr_bean", "bean")], "写信到 alice@bean 就行"),
        ("@Beanstalk 是个库", ROSTER, "@Beanstalk 是个库"),
        ("@Claude 看下", [("usr_a", "Claude"), ("usr_b", "Claude")], "@Claude 看下"),
        ("@大Q 看一下", [], "@大Q 看一下"),
        ("@Bean_x 好", ROSTER, "@Bean→usr_bean|_x 好"),
        ("@BEAN 好", [("usr_lower", "bean"), ("usr_upper", "BEAN")], "@BEAN→usr_upper| 好"),
        ("@ 大Q", ROSTER, "@ 大Q"),
        ("@  大Q ", [("usr_q", "  大Q ")], "@  大Q "),
        ("@大Q", [("usr_q", "  大Q ")], "@大Q→usr_q"),
    ],
)
def test_autolink_rules(text, roster, expected):
    assert _shape(autolink_mentions(text, roster, ME)) == expected


def test_empty_text_gives_no_fragments():
    assert autolink_mentions("", ROSTER, ME) == []


def test_mention_fragment_shape_and_context_dedupe():
    fragments = autolink_mentions("@Bean 先看，看完 @Bean 回我", ROSTER, ME)
    assert [f for f in fragments if f["kind"] == "mention"] == [
        {"kind": "mention", "user_id": "usr_bean", "display": "Bean"},
        {"kind": "mention", "user_id": "usr_bean", "display": "Bean"},
    ]
    assert mentions_in(fragments) == [{"kind": "mention", "user_id": "usr_bean", "display": "Bean"}]
    assert [m["user_id"] for m in mentions_in(autolink_mentions("@Spark @Bean", ROSTER, ME))] == [
        "usr_spark",
        "usr_bean",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("今天先到这里", False), ("收到 @", False), ("写信到 alice@Bean", False), ("@大Q 看一下", True), ("让@Bean 看一下", True)],
)
def test_has_mention_candidate(text, expected):
    assert has_mention_candidate(text) is expected


# --- adapter ---------------------------------------------------------------

AGENT = "usr_agent"
OWNER = "usr_owner"
GROUP = "cnv_group"
DM = "cnv_dm"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a.frames = []

    async def send_frame(frame, **_kw):
        a.frames.append(frame)
        return True

    metadata = {
        ("group", GROUP): {
            "participant_ids": f"{OWNER},{AGENT},usr_bean,usr_q,usr_anon",
            "group_owner_id": "usr_q",
            "group_owner_nickname": "大Q",
        },
        ("owner", "owner"): {"agent_owner_nickname": "Ann", "agent_nickname": "Otter"},
        ("user", "usr_bean"): {"nickname": "Bean"},
        ("user", AGENT): {"nickname": "Otter"},
    }
    monkeypatch.setattr(a, "_read_memory_metadata", lambda t, i: dict(metadata.get((t, i), {})))
    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._known_chat_types[GROUP] = "group"
    a._known_chat_types[DM] = "direct"
    return a


def test_group_roster_comes_from_the_cached_participants(adapter):
    assert sorted(adapter._group_mention_roster(GROUP)) == sorted(
        [(OWNER, "Ann"), ("usr_bean", "Bean"), ("usr_q", "大Q")]
    )


async def test_a_group_reply_mentions_the_members_it_names(adapter):
    await adapter.send(GROUP, "@Bean 你看下，@Ann 也看下，@Otter 是我")
    payload = adapter.frames[0]["payload"]
    assert _shape(payload["message"]["body"]["fragments"]) == "@Bean→usr_bean| 你看下，|@Ann→usr_owner| 也看下，@Otter 是我"
    assert payload["message"]["context"]["mentions"] == [
        {"kind": "mention", "user_id": "usr_bean", "display": "Bean"},
        {"kind": "mention", "user_id": OWNER, "display": "Ann"},
    ]


async def test_a_group_reply_without_names_is_unchanged(adapter):
    await adapter.send(GROUP, "write to alice@Bean")
    payload = adapter.frames[0]["payload"]
    assert payload["message"]["body"]["fragments"] == [{"kind": "text", "text": "write to alice@Bean"}]
    assert payload["message"]["context"]["mentions"] == []


async def test_direct_chats_are_left_alone(adapter):
    await adapter.send(DM, "@Bean hi")
    payload = adapter.frames[0]["payload"]
    assert payload["message"]["body"]["fragments"] == [{"kind": "text", "text": "@Bean hi"}]
    assert payload["message"]["context"]["mentions"] == []


async def test_process_messages_never_mention(adapter):
    await adapter.send(GROUP, "⚙️ asking @Bean", _clawchat_message_mode="thinking")
    payload = adapter.frames[0]["payload"]
    assert payload["message_mode"] == "thinking"
    assert payload["message"]["context"]["mentions"] == []


async def test_a_finalized_streamed_reply_mentions_too(adapter):
    first = await adapter.send(GROUP, "@Bean 你 ▉")
    await adapter.edit_message(GROUP, first.message_id, "@Bean 你看下", finalize=True)
    reply = [f for f in adapter.frames if f["event"] == "message.reply"][-1]["payload"]
    assert reply["message"]["context"]["mentions"] == [
        {"kind": "mention", "user_id": "usr_bean", "display": "Bean"}
    ]
