"""Explicit message ids from the model must name a message in that chat.

A model with no id for a message makes one up: a label (``B``) or a host
session id (a UUID). The server only checks that a reaction target is
non-empty, so ``clawchat_react_message`` used to report ``reacted: true`` while
nothing was reacted. An explicit ``targetMessageId`` (react) or
``replyToMessageId`` (mention) is now accepted only when the plugin's
``clawchat_messages`` ledger has that message in the same chat (inbound, or one
of the agent's own messages), matched case-insensitively; the stored ids are
what goes on the wire. When the ledger cannot answer, a shape check stands in.

Also pinned here: a reaction the outbound layer refuses reports
``send_blocked`` instead of ``reacted: true``; the ``clawchat:`` prefix the
host teaches for explicit targets is accepted on chat ids; a mention into a
chat given in another case goes out, and is marked, under the stored case; a
reaction naming another chat never takes its target from the current one.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import replace

import pytest

from clawchat_gateway import storage as storage_mod
from clawchat_gateway import tools
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.storage import ClawChatStore
from clawchat_gateway.terminal_send import (
    clear_terminal_clawchat_sends_for_test,
    consume_terminal_clawchat_send,
)

GROUP = "cnv_01HGRPA" + "0" * 19
OTHER = "cnv_01HXTHR" + "0" * 19
INBOUND_ID = "msg-01HINBOUNDAAAAAAAAAAAAAA"
OTHER_CHAT_ID = "msg-01HOTHERCHATAAAAAAAAAAAA"


def _seed_inbound(store: ClawChatStore, chat_id: str, message_id: str) -> None:
    assert store.claim_message_once(
        platform="hermes",
        account_id="default",
        kind="message",
        direction="inbound",
        event_type="message.send",
        chat_id=chat_id,
        message_id=message_id,
        text="hello",
        raw={"payload": {"message_id": message_id}},
    )


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    clear_terminal_clawchat_sends_for_test()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a.frames = []
    a.send_ok = True

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        if not a.send_ok:
            return False
        a.frames.append(frame)
        return True

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._known_chat_types[GROUP] = "group"
    a._known_chat_types[OTHER] = "group"
    _seed_inbound(a._store, GROUP, INBOUND_ID)
    _seed_inbound(a._store, OTHER, OTHER_CHAT_ID)
    a._last_inbound_message_id_by_chat[GROUP] = INBOUND_ID
    yield a
    clear_terminal_clawchat_sends_for_test()


def _reactions(adapter):
    return [f for f in adapter.frames if f.get("event") == "message.reaction"]


def _sends(adapter):
    return [f for f in adapter.frames if f.get("event") == "message.send"]


def _reply_to(frame):
    context = frame["payload"]["message"].get("context") or {}
    return (context.get("reply") or {}).get("reply_to_msg_id")


# --- react: explicit target ------------------------------------------------


@pytest.mark.parametrize(
    "made_up",
    ["B", "msg", "3f2b8c1e-6a4d-4e2f-9b1a-0c7d5e8f9a21", "msg-01HNEVERSEENAAAAAAAAAAAAA"],
)
async def test_react_rejects_a_target_the_chat_never_had(adapter, made_up):
    result = await adapter.send_reaction_message(
        chat_id=GROUP, target_message_id=made_up, emoji="👍"
    )
    assert result["error"] == "validation"
    assert "reacted" not in result
    assert "targetMessageId" in result["message"]
    assert "message metadata" in result["message"]
    assert _reactions(adapter) == []


async def test_react_rejects_a_message_of_another_chat(adapter):
    result = await adapter.send_reaction_message(
        chat_id=GROUP, target_message_id=OTHER_CHAT_ID, emoji="👍"
    )
    assert result["error"] == "validation"
    assert _reactions(adapter) == []


async def test_react_echo_of_a_long_made_up_id_is_truncated(adapter):
    made_up = "x" * 500
    result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=made_up, emoji="👍")
    assert result["error"] == "validation"
    assert "x" * 64 in result["message"]
    assert "x" * 65 not in result["message"]


async def test_react_accepts_a_stored_id_in_any_case_and_sends_the_stored_ids(adapter):
    result = await adapter.send_reaction_message(
        chat_id=GROUP.lower(), target_message_id=INBOUND_ID.lower(), emoji="👍"
    )
    assert result.get("reacted") is True
    assert result["targetMessageId"] == INBOUND_ID
    [frame] = _reactions(adapter)
    assert frame["chat_id"] == GROUP
    assert frame["payload"]["target_message_id"] == INBOUND_ID


async def test_react_accepts_the_agents_own_mention(adapter):
    sent = await adapter.send_mention_message(
        chat_id=GROUP, text="hi", mentions=[{"userId": "usr_peer", "display": "Peer"}]
    )
    assert sent.get("sent") is True
    result = await adapter.send_reaction_message(
        chat_id=GROUP, target_message_id=sent["messageId"], emoji="🎉"
    )
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == sent["messageId"]


async def test_react_rejects_a_failed_send_of_the_agent(adapter):
    adapter.send_ok = False
    failed = await adapter.send_mention_message(
        chat_id=GROUP, text="hi", mentions=[{"userId": "usr_peer", "display": "Peer"}]
    )
    assert failed["error"] == "transport"
    adapter.send_ok = True
    result = await adapter.send_reaction_message(
        chat_id=GROUP, target_message_id=failed["messageId"], emoji="👍"
    )
    assert result["error"] == "validation"


# --- react: defaults and other chats -------------------------------------


async def test_react_default_target_is_not_checked(adapter):
    adapter._last_inbound_message_id_by_chat[GROUP] = "msg-01HDEFAULTNOTSTOREDAAAAAA"
    result = await adapter.send_reaction_message(chat_id=GROUP, emoji="👍")
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == "msg-01HDEFAULTNOTSTOREDAAAAAA"


async def test_react_default_target_follows_the_stored_chat_case(adapter):
    result = await adapter.send_reaction_message(chat_id=GROUP.lower(), emoji="👍")
    assert result.get("reacted") is True
    [frame] = _reactions(adapter)
    assert frame["chat_id"] == GROUP
    assert frame["payload"]["target_message_id"] == INBOUND_ID


async def test_react_in_another_chat_without_target_never_uses_the_current_message(adapter):
    # The current turn is in GROUP; OTHER has had no live inbound this run.
    result = await adapter.send_reaction_message(chat_id=OTHER, emoji="👍")
    assert result["error"] == "validation"
    assert "targetMessageId" in result["message"]
    assert _reactions(adapter) == []


# --- react: outbound refusals ----------------------------------------------


async def test_react_into_a_server_rejected_chat_is_send_blocked(adapter):
    adapter._mark_chat_server_rejected(GROUP)
    result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=INBOUND_ID, emoji="👍")
    assert result["error"] == "send_blocked"
    assert result["reason"] == "chat_rejected_by_server"
    assert "reacted" not in result
    assert _reactions(adapter) == []


async def test_react_with_an_invalid_chat_id_is_send_blocked(adapter):
    result = await adapter.send_reaction_message(chat_id="usr_nobody", target_message_id=INBOUND_ID, emoji="👍")
    assert result["error"] == "send_blocked"
    assert result["reason"] == "invalid_chat_id"
    assert _reactions(adapter) == []


async def test_react_dropped_by_the_connection_is_not_reported_as_reacted(adapter):
    adapter.send_ok = False
    result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=INBOUND_ID, emoji="👍")
    assert "reacted" not in result
    assert result["error"]


# --- store unavailable: shape fallback --------------------------------------


class _BrokenStore:
    def __getattr__(self, name):
        def boom(*_a, **_kw):
            raise sqlite3.OperationalError("disk I/O error")

        return boom


@pytest.mark.parametrize("broken", ["none", "raises"])
async def test_store_unavailable_falls_back_to_the_shape_check(adapter, caplog, broken):
    adapter._store = None if broken == "none" else _BrokenStore()
    ulid = "01HZZZZZZZZZZZZZZZZZZZZZZZ"
    caplog.set_level(logging.WARNING, logger="clawchat_gateway.adapter")
    for good in ("msg-01HSHAPEOKAAAAAAAAAAAAAAA", "SYS-req-1", ulid):
        result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=good, emoji="👍")
        assert result.get("reacted") is True, good
    for bad in ("B", "3f2b8c1e-6a4d-4e2f-9b1a-0c7d5e8f9a21", "msg- x", "hello"):
        result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=bad, emoji="👍")
        assert result["error"] == "validation", bad
    warnings = [r for r in caplog.records if "message-id store check unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert "msg-01HSHAPEOK" not in warnings[0].getMessage()
    assert GROUP not in warnings[0].getMessage()


# --- mention: reply-to and chat case ---------------------------------------


async def test_mention_rejects_a_made_up_reply_to_and_sends_nothing(adapter):
    result = await adapter.send_mention_message(
        chat_id=GROUP,
        text="hi",
        mentions=[{"userId": "usr_peer", "display": "Peer"}],
        reply_to_message_id="B",
    )
    assert result["error"] == "validation"
    assert "replyToMessageId" in result["message"]
    assert _sends(adapter) == []
    assert consume_terminal_clawchat_send(account_id="default", chat_id=GROUP) is None
    assert adapter._store.recent_outbound_messages(account_id="default", chat_id=GROUP, since_ms=0) == []


async def test_mention_reply_to_a_stored_message_sends_the_stored_id(adapter):
    result = await adapter.send_mention_message(
        chat_id=GROUP,
        text="hi",
        mentions=[{"userId": "usr_peer", "display": "Peer"}],
        reply_to_message_id=INBOUND_ID.lower(),
    )
    assert result.get("sent") is True
    [frame] = _sends(adapter)
    assert _reply_to(frame) == INBOUND_ID


async def test_mention_into_a_chat_given_in_another_case_uses_the_stored_case(adapter):
    result = await adapter.send_mention_message(
        chat_id=GROUP.lower(), text="hi", mentions=[{"userId": "usr_peer", "display": "Peer"}]
    )
    assert result.get("sent") is True
    [frame] = _sends(adapter)
    assert frame["chat_id"] == GROUP
    # The turn's own follow-up reply (host chat id, stored case) is suppressed.
    assert consume_terminal_clawchat_send(account_id="default", chat_id=GROUP) is not None
    rows = adapter._store.recent_outbound_messages(account_id="default", chat_id=GROUP, since_ms=0)
    assert [r["message_id"] for r in rows] == [result["messageId"]]


async def test_mention_into_an_unknown_chat_keeps_the_id_as_given(adapter):
    unseen = "cnv_01HNEWCHAT" + "0" * 16
    result = await adapter.send_mention_message(
        chat_id=unseen, text="hi", mentions=[{"userId": "usr_peer", "display": "Peer"}]
    )
    assert result.get("sent") is True
    assert _sends(adapter)[0]["chat_id"] == unseen


# --- tools: the host's `clawchat:` explicit-target prefix -------------------


class _Sender:
    def __init__(self):
        self.calls = []

    async def send_mention_message(self, **kw):
        self.calls.append(("mention", kw))
        return {"sent": True}

    async def send_reaction_message(self, **kw):
        self.calls.append(("react", kw))
        return {"reacted": True}


@pytest.fixture
def sender(monkeypatch):
    s = _Sender()
    monkeypatch.setattr(tools, "send_clawchat_mention_message", s.send_mention_message)
    monkeypatch.setattr(tools, "send_clawchat_reaction_message", s.send_reaction_message)
    return s


@pytest.mark.parametrize("given", [f"clawchat:{GROUP}", f"ClawChat:{GROUP}", f"  clawchat:{GROUP} "])
async def test_tools_accept_the_host_platform_prefix(sender, given):
    assert (await tools.react_message(given, emoji="👍")) == {"reacted": True}
    assert (
        await tools.mention_message(given, mentions=[{"userId": "usr_peer", "display": "Peer"}])
    ) == {"sent": True}
    assert [kw["chat_id"] for _, kw in sender.calls] == [GROUP, GROUP]


@pytest.mark.parametrize("given", [f"telegram:{GROUP}", f"clawchat:clawchat:{GROUP}", "clawchat:", "clawchat:usr_x"])
async def test_tools_still_refuse_other_prefixes(sender, given):
    result = await tools.react_message(given, emoji="👍")
    assert result["error"] == "validation"
    assert sender.calls == []


async def test_send_file_accepts_the_host_platform_prefix(monkeypatch, tmp_path):
    seen = []

    class FileSender:
        async def send(self, chat_id, caption, **kw):
            seen.append(chat_id)

            class R:
                success = True
                message_id = "msg-file"

            return R()

    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: FileSender())
    monkeypatch.setattr(tools, "ensure_allowed_local_path", lambda p: __import__("pathlib").Path(p))
    f = tmp_path / "a.txt"
    f.write_text("x")
    result = await tools.send_file(f"clawchat:{GROUP}", str(f))
    assert result.get("sent") is True
    assert seen == [GROUP]


# --- storage: lookups and their query plans ---------------------------------


@pytest.fixture
def store(tmp_path):
    s = ClawChatStore(tmp_path / "clawchat.sqlite")
    _seed_inbound(s, GROUP, INBOUND_ID)
    return s


def test_find_seen_message_matches_case_insensitively(store):
    assert store.find_seen_message_in_chat(
        account_id="default", chat_id=GROUP.lower(), message_id=INBOUND_ID.lower()
    ) == (GROUP, INBOUND_ID)
    assert store.find_seen_message_in_chat(account_id="default", chat_id=OTHER, message_id=INBOUND_ID) is False
    assert store.find_seen_message_in_chat(account_id="other", chat_id=GROUP, message_id=INBOUND_ID) is False


def test_find_seen_message_reports_an_unavailable_store_as_none(store):
    store._disabled = True
    assert store.find_seen_message_in_chat(account_id="default", chat_id=GROUP, message_id=INBOUND_ID) is None
    assert store.find_stored_chat_id(account_id="default", chat_id=GROUP) is None


def test_find_stored_chat_id_prefers_the_inbound_case(store):
    store.insert_message(
        platform="hermes", account_id="default", kind="message", direction="outbound",
        event_type="message.send", chat_id=GROUP.lower(), message_id="msg-out",
    )
    assert store.find_stored_chat_id(account_id="default", chat_id=GROUP.lower()) == GROUP
    assert store.find_stored_chat_id(account_id="default", chat_id=OTHER) is False


@pytest.mark.parametrize("sql_name", ["FIND_SEEN_MESSAGE_SQL", "FIND_STORED_CHAT_ID_SQL"])
def test_lookups_seek_an_index_instead_of_scanning_the_ledger(store, sql_name):
    sql = getattr(storage_mod, sql_name)
    params = sql.count("?")
    conn = sqlite3.connect(store.db_path)
    try:
        plan = [row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}", ("x",) * params)]
    finally:
        conn.close()
    assert not any(step.startswith("SCAN clawchat_messages") for step in plan), plan
    assert any("USING INDEX" in step and "nocase" in step for step in plan), plan
