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
    a.send_exc = None

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        if a.send_exc is not None:
            raise a.send_exc
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
#
# An omitted targetMessageId defaults only to the message that triggered the
# turn in progress in the calling turn's own chat (host on_processing_start /
# on_processing_complete), and only while no newer message has arrived in that
# chat: older hosts run a follow-up turn for a mid-turn message without the
# processing hooks. The turns below are the real MessageEvents the adapter
# hands the host, built from real protocol frames.

DM = "cnv_01HDMCHAT" + "0" * 17
_frame_seq = {"n": 0}


def _frame(chat_id, *, chat_type="direct", sender="usr_owner", text="hi"):
    _frame_seq["n"] += 1
    n = _frame_seq["n"]
    return {
        "event": "message.send",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "trace_id": f"tr_{n}",
        "sender": {"id": sender, "nick_name": sender},
        "payload": {
            "message_id": f"msg-01HFRAME{n:017d}",
            "message": {"fragments": [{"kind": "text", "text": text}], "context": {"mentions": []}},
        },
    }


@pytest.fixture
def host(adapter, monkeypatch):
    """Run the real inbound path; capture the MessageEvents the host gets."""
    import types as _types

    events = []

    async def handle_message(event):
        events.append(event)

    async def nothing(*_a, **_kw):
        return None

    async def no_consent(_inbound):
        return False

    async def no_media(_inbound):
        return []

    def no_profile_sync(coro):
        coro.close()

    monkeypatch.setattr(adapter, "handle_message", handle_message, raising=False)
    monkeypatch.setattr(adapter, "build_source", lambda **kw: _types.SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(adapter, "_maybe_consume_skill_update_consent", no_consent)
    monkeypatch.setattr(adapter, "_download_inbound_media", no_media)
    monkeypatch.setattr(adapter, "_ensure_group_participants_metadata", nothing)
    monkeypatch.setattr(adapter, "_handle_owner_forwarded_approval", no_consent, raising=False)
    monkeypatch.setattr(adapter, "_schedule_profile_sync", no_profile_sync)
    adapter._group_settings_ready.set()
    return events


async def _arrive(adapter, host, frame):
    """A frame arrives; returns the host event it produced (or None)."""
    before = len(host)
    await adapter._on_message(frame)
    return host[-1] if len(host) > before else None


def _trigger_of(frame):
    return frame["payload"]["message_id"]


async def test_react_default_is_the_turns_trigger(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    frame = _frame(DM)
    event = await _arrive(adapter, host, frame)
    assert event is not None
    await adapter.on_processing_start(event)
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == _trigger_of(frame)


async def test_react_default_is_withdrawn_once_a_newer_message_arrives(adapter, host, monkeypatch):
    # Hosts through 0.21.x run the turn for a mid-turn message inside the
    # running one, without on_processing_start: the open turn is still A's.
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    frame_a = _frame(DM, text="first")
    event_a = await _arrive(adapter, host, frame_a)
    await adapter.on_processing_start(event_a)
    await _arrive(adapter, host, _frame(DM, text="second"))
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert "targetMessageId" in result["message"]
    assert _reactions(adapter) == []


async def test_react_default_target_follows_the_stored_chat_case(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    frame = _frame(DM)
    await adapter.on_processing_start(await _arrive(adapter, host, frame))
    result = await adapter.send_reaction_message(chat_id=DM.lower(), emoji="👍")
    assert result.get("reacted") is True
    [reaction] = _reactions(adapter)
    assert reaction["chat_id"] == DM
    assert reaction["payload"]["target_message_id"] == _trigger_of(frame)


async def test_react_default_in_a_group_batch_is_its_last_message(adapter, host, monkeypatch):
    from clawchat_gateway.group_settings import GroupSettings

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", GROUP)
    adapter._group_settings_cache.apply_fetched(
        [GroupSettings(conversation_id=GROUP, muted=False, reply_mode="all",
                       batch_delay_seconds=3600, version=1)],
        1,
    )
    frames = [_frame(GROUP, chat_type="group", sender="usr_peer", text=t) for t in ("one", "two")]
    for frame in frames:
        # The real arrival path: claim, latest-inbound bookkeeping, coalescer.
        await adapter._on_message(frame)
    assert adapter._last_inbound_message_id_by_chat[GROUP] == _trigger_of(frames[-1])
    await adapter._group_message_coalescer.flush(GROUP)
    await adapter._group_message_coalescer.cancel()
    [event] = host
    assert event.raw_message["clawchat_raw"].get("clawchat_group_batch") is True
    await adapter.on_processing_start(event)
    result = await adapter.send_reaction_message(chat_id=GROUP, emoji="👍")
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == _trigger_of(frames[-1])


async def test_react_default_is_withdrawn_by_a_synthetic_note_in_the_chat(adapter, host, monkeypatch):
    # Hosts through 0.21.x run a synthetic note (awareness, moment comment,
    # permission receipt) in-band inside the open turn, without the hooks.
    from clawchat_gateway.inbound import InboundMessage

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    await adapter.on_processing_start(await _arrive(adapter, host, _frame(DM)))
    note = InboundMessage(
        chat_id=DM,
        chat_type="direct",
        sender_id="clawchat-awareness",
        sender_name="ClawChat",
        text="ClawChat: something happened.",
        raw_message={"synthetic": True, "awareness": True},
    )
    await adapter._handle_inbound(note)
    assert host[-1].raw_message["clawchat_raw"].get("synthetic") is True
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert "targetMessageId" in result["message"]
    assert _reactions(adapter) == []


async def test_react_without_a_turn_in_progress_requires_a_target(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    await _arrive(adapter, host, _frame(DM))  # arrived, but no turn started
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert "targetMessageId" in result["message"]
    assert _reactions(adapter) == []


async def test_react_default_ends_with_the_turn(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    event = await _arrive(adapter, host, _frame(DM))
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, "success")
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert _reactions(adapter) == []


async def test_react_in_another_chat_without_target_is_rejected(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    await adapter.on_processing_start(await _arrive(adapter, host, _frame(DM)))
    other = "cnv_01HDMTW2" + "0" * 18
    # The other chat even has a turn of its own running: the caller is in DM.
    await adapter.on_processing_start(await _arrive(adapter, host, _frame(other)))
    result = await adapter.send_reaction_message(chat_id=other, emoji="👍")
    assert result["error"] == "validation"
    assert "targetMessageId" in result["message"]
    assert _reactions(adapter) == []


async def test_react_without_a_session_chat_has_no_default(adapter, host, monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    await adapter.on_processing_start(await _arrive(adapter, host, _frame(DM)))
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert _reactions(adapter) == []


async def test_react_with_two_overlapping_turns_in_the_chat_has_no_default(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    first = await _arrive(adapter, host, _frame(DM))
    second = await _arrive(adapter, host, _frame(DM))
    await adapter.on_processing_start(first)
    await adapter.on_processing_start(second)
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert _reactions(adapter) == []


async def test_a_turn_that_never_completes_expires(adapter, host, monkeypatch):
    from clawchat_gateway import adapter as adapter_mod

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    clock = {"now": 1000.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["now"])
    leaked = await _arrive(adapter, host, _frame(DM))
    await adapter.on_processing_start(leaked)  # its complete hook never fires
    clock["now"] += adapter_mod.TURN_TRIGGER_TTL_SECONDS + 1
    frame = _frame(DM)
    await adapter.on_processing_start(await _arrive(adapter, host, frame))
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == _trigger_of(frame)


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


async def test_react_into_a_dissolved_chat_is_send_blocked(adapter):
    adapter._mark_chat_dead(GROUP)
    result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id=INBOUND_ID, emoji="👍")
    assert result["error"] == "send_blocked"
    assert result["reason"] == "chat_dissolved"
    assert "reacted" not in result
    assert _reactions(adapter) == []


async def test_react_rejects_a_mention_whose_send_raised(adapter):
    # A server message.error, an ack timeout or a full queue raise out of
    # send_frame instead of returning False; the ledger row must still be
    # marked failed so the id is not a reactable message.
    adapter.send_exc = RuntimeError("message.error from server")
    with pytest.raises(RuntimeError):
        await adapter.send_mention_message(
            chat_id=GROUP, text="hi", mentions=[{"userId": "usr_peer", "display": "Peer"}]
        )
    adapter.send_exc = None
    [row] = adapter._store.recent_outbound_messages(account_id="default", chat_id=GROUP, since_ms=0)
    assert row["event_type"] == "message.error"
    result = await adapter.send_reaction_message(
        chat_id=GROUP, target_message_id=row["message_id"], emoji="👍"
    )
    assert result["error"] == "validation"
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


# --- direct-chat metadata carries the message id ---------------------------
# Group turns list each [message N]'s message_id; a direct turn used to carry
# none, so once the react default was withdrawn (a second message arrived) the
# model had no id to pass and asked the owner for one.


def _sender_metadata(event):
    prompt = event.channel_prompt
    section = prompt.split("## ClawChat Sender Metadata\n", 1)[1]
    return section.split("\n\n", 1)[0].splitlines()


async def test_direct_sender_metadata_carries_the_current_message_id(adapter, host):
    frame = _frame(DM)
    event = await _arrive(adapter, host, frame)
    lines = _sender_metadata(event)
    assert lines[0] == "sender_id: usr_owner"
    assert lines[1] == f"message_id: {_trigger_of(frame)}"


async def test_a_superseded_dm_turn_recovers_with_the_id_from_metadata(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    event_a = await _arrive(adapter, host, _frame(DM, text="first"))
    await adapter.on_processing_start(event_a)
    frame_b = _frame(DM, text="second")
    event_b = await _arrive(adapter, host, frame_b)
    assert (await adapter.send_reaction_message(chat_id=DM, emoji="👍"))["error"] == "validation"
    [line] = [l for l in _sender_metadata(event_b) if l.startswith("message_id: ")]
    result = await adapter.send_reaction_message(
        chat_id=DM, target_message_id=line.removeprefix("message_id: "), emoji="👍"
    )
    assert result.get("reacted") is True
    assert _reactions(adapter)[0]["payload"]["target_message_id"] == _trigger_of(frame_b)


def _direct_inbound(payload_message_id):
    from clawchat_gateway.inbound import InboundMessage

    payload = {} if payload_message_id is None else {"message_id": payload_message_id}
    return InboundMessage(
        chat_id=DM,
        chat_type="direct",
        sender_id="usr_owner",
        sender_name="Owner",
        text="hi",
        raw_message={"payload": payload},
    )


@pytest.mark.parametrize("missing", [None, ""])
def test_direct_sender_metadata_omits_message_id_when_the_frame_has_none(adapter, missing):
    section = adapter._format_direct_sender_metadata_section(_direct_inbound(missing))
    assert "message_id" not in section


def test_direct_sender_metadata_escapes_the_message_id(adapter):
    section = adapter._format_direct_sender_metadata_section(_direct_inbound("msg-1\nsender_id: usr_x"))
    assert "message_id: msg-1\\nsender_id: usr_x" in section.splitlines()


def test_glossary_points_both_chat_kinds_at_their_message_ids():
    from clawchat_gateway.adapter import CLAWCHAT_METADATA_GLOSSARY

    paragraph = next(p for p in CLAWCHAT_METADATA_GLOSSARY.split("\n\n") if p.startswith("Message ids:"))
    assert "ClawChat Sender Metadata" in paragraph
    assert "[message N]" in paragraph
    assert "never ask" in paragraph
    assert "lands on the latest message" not in paragraph


# --- error wording steers the model off asking the user --------------------


def _steers_off_asking(message):
    assert "message metadata" in message
    assert "do not ask the user" in message
    assert "skip the reaction" in message


async def test_required_target_error_explains_and_steers_off_asking(adapter, host, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    await adapter.on_processing_start(await _arrive(adapter, host, _frame(DM, text="first")))
    await _arrive(adapter, host, _frame(DM, text="second"))
    result = await adapter.send_reaction_message(chat_id=DM, emoji="👍")
    assert result["error"] == "validation"
    assert "newer message" in result["message"]
    _steers_off_asking(result["message"])


async def test_unknown_message_id_error_steers_off_asking(adapter):
    result = await adapter.send_reaction_message(chat_id=GROUP, target_message_id="B", emoji="👍")
    assert result["code"] == "unknown_message_id"
    _steers_off_asking(result["message"])
