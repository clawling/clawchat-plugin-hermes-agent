"""Public conversation history: reference output, ledger isolation and turn binding."""
import json
import os
import re
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from clawchat_gateway import chat_history as history, plugin_tools, storage, memory_scope
from clawchat_gateway import host_tools_guard as guard, turn_grants as turns
from clawchat_gateway.storage import ClawChatStore
from test_tool_grants_admission import harness, message, coalesced
from test_turn_grants import install_host_context

SCOPE = dict(owner_user_id="usr_owner", self_user_id="usr_agent", self_name="Helper", exclude_id="trigger")
MORE = "More matches than shown — narrow it down with from / to / speaker, or more specific words."


def ids(output):
    return re.findall(r"\(id ([^)]+)\)", output)


@pytest.fixture
def ledger(tmp_path):
    store = ClawChatStore(tmp_path / "ledger.sqlite")
    serial = 0

    def add(mid, ts="2026-10-01", body="alpha", extra=None, raw_extra=None):
        nonlocal serial
        values = dict(platform="hermes", account_id="default", chat_id="cnv_A", kind="message",
                      direction="inbound", event_type="message.send", message_id=mid, text=body,
                      created_at=1793491200000 + serial,
                      raw={"emitted_at": ts, "sender": {"id": "usr_ada", "nick_name": "Ada"}, **(raw_extra or {})})
        serial += 1
        values.update(extra or {})
        assert store.insert_message(**values) is not None

    def run(tool="chat_history_read", args=None, account="default", chat="cnv_a", scope=None):
        rows = store.list_chat_history_messages(account, chat)
        assert rows is not None
        return history.run_chat_history_tool(tool, args or {}, rows, SCOPE if scope is None else scope)

    def search(**args):
        return run("chat_history_search", {"query": "alpha", **args})

    def read(**args):
        return run(args=args)

    def many(count, body="alpha"):
        for i in range(1, count + 1):
            add(f"m{i}", 1790812800000 + i * 1000, body)

    return SimpleNamespace(store=store, add=add, run=run, search=search, read=read, many=many)


def test_exact_specs_registration_manifest_docs_and_prompt(monkeypatch):
    import yaml
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    calls = {}
    plugin_tools.register_tools(SimpleNamespace(register_tool=lambda name, group, schema, handler, **kw:
                                calls.update({name: (group, schema, handler)})))
    s, r = history.CHAT_HISTORY_TOOLS
    assert s["name"] == "chat_history_search" and r["name"] == "chat_history_read"
    assert s["description"] == ('Search the full history of this conversation, including messages no longer in front of you. '
        'Give words that would appear in the message itself (for a link, a word from the link or "http"). '
        'Matches any of the words, best and newest first; each hit has its time, speaker and message id. Only this conversation.')
    assert r["description"] == ('Read messages of this conversation verbatim, oldest first: around a message id from '
        'chat_history_search, or a time range. With neither, the latest messages. Only this conversation.')
    times = {
        "from": {"type": "string", "description": "Earliest time: YYYY-MM-DD or ISO 8601."},
        "to": {"type": "string", "description": "Latest time: YYYY-MM-DD (whole day) or ISO 8601."},
    }
    assert s["parameters"] == {"type": "object", "properties": {
        "query": {"type": "string", "description": "Words to look for, case-insensitive."}, **times,
        "speaker": {"type": "string", "description": "owner, you, or a name or usr_ id."},
        "limit": {"type": "integer", "description": "Hits to return; default 10, at most 30."}}, "required": ["query"]}
    assert r["parameters"] == {"type": "object", "properties": {
        "around": {"type": "string", "description": "A message id to read around."}, **times,
        "limit": {"type": "integer", "description": "Messages to return; default 20, at most 60."}}}
    for spec, handler in zip((s, r), (plugin_tools.handle_chat_history_search, plugin_tools.handle_chat_history_read)):
        assert calls[spec["name"]] == ("clawchat", spec, handler)
    manifest = yaml.safe_load(Path("plugin.yaml").read_text())
    assert set(manifest["provides_tools"]) == set(calls)
    docs = Path("docs/reference/tools.md").read_text()
    assert all(re.search(r"`" + name + r"(?:`|\()", docs) for name in calls)
    assert f"There are **{len(calls)}** tools" in docs
    assert "!tests/test_chat_history_tools.py" in Path(".gitignore").read_text().splitlines()
    sentence = ("**Someone refers to something said earlier — an instruction, a link, a number, an account, a file — "
                "and it is not in front of you?** Look it up with `chat_history_search` before you answer. "
                "Do not answer from memory, and do not just say you don't remember.")
    assert sentence in Path("prompts/platform.md").read_text()


def test_chat_account_platform_isolation_and_injected_arguments(ledger):
    h = ledger
    h.add("dm", body="direct only")
    h.add("group", body="group only", extra={"chat_id": "cnv_B"})
    h.add("other-account", body="other account", extra={"account_id": "work"})
    h.add("other-platform", body="other platform", extra={"platform": "another"})
    for args in ({"chat_id": "cnv_B"}, {"chatId": "cnv_B", "conversation": "cnv_B", "accountId": "work"}):
        assert ids(h.read(**args)) == ["dm"]
        assert h.search(query="group", **args) == 'No message in this conversation matches "group". Try other words, or chat_history_read a time range.'
    assert ids(h.run(chat="cnv_b")) == ["group"]
    assert ids(h.run(account="work")) == ["other-account"]
    with pytest.raises(ValueError, match="INVALID: no message with id group in this conversation."):
        h.read(around="group")


def test_recalled_failed_suppressed_and_internal_hidden(ledger):
    h = ledger
    h.add("kept")
    h.add("recalled")
    assert h.store.recall_message(account_id="default", message_id="recalled") == 1
    h.add("recalled")  # Legacy insert bypasses ingestion's tombstone guard.
    for mid in ("failed", "suppressed"):
        h.add(mid, extra={"direction": "outbound", "event_type": "message.error"})
    h.add("thinking", extra={"kind": "thinking"})
    h.add("internal", extra={"event_type": "notification"})
    h.add("out", extra={"direction": "outbound", "event_type": "message.reply"})
    assert ids(h.read()) == ["kept", "out"]
    assert sorted(ids(h.search())) == ["kept", "out"]


def test_trigger_excluded_from_every_query_including_protocol_id(ledger):
    h = ledger
    h.add("prev")
    h.add("trigger", "2026-10-02")
    for output in (h.read(), h.search(), h.read(**{"from": "2026-10-01"})):
        assert ids(output) == ["prev"]
    with pytest.raises(ValueError, match="INVALID: no message with id trigger"):
        h.read(around="trigger")
    rows = h.store.list_chat_history_messages("default", "cnv_a")
    rows.append({**rows[0], "message_id": "client-trigger", "protocol_message_id": "trigger"})
    assert ids(history.run_chat_history_tool("chat_history_read", {}, rows, SCOPE)) == ["prev"]


def test_old_history_and_malformed_raw(ledger):
    h = ledger
    h.add("old", extra={"raw": None})
    h.add("new", body="beta")
    assert ids(h.search()) == ["old"]
    assert sorted(ids(h.read())) == ["new", "old"]
    rows = h.store.list_chat_history_messages("default", "cnv_a")
    for raw in ("{broken", "[]", "null", "5"):
        assert ids(history.run_chat_history_tool("chat_history_read", {}, [{**rows[0], "raw_json": raw}], SCOPE)) == ["old"]


def test_distinct_term_or_substrings_rank_before_recency(ledger):
    h = ledger
    h.add("best-old", "2026-09-01", "Alpha and BETA")
    h.add("middle", "2026-10-01", "alpha alpha alpha")
    h.add("newest", "2026-10-02", "xxbetaxx")
    h.add("irrelevant", "2026-10-03", "neither")
    assert ids(h.search(query="  ALPHA\talpha  beta ")) == ["best-old", "newest", "middle"]


def test_ties_invalid_times_and_emitted_time_order(ledger):
    h = ledger
    for mid, ts in (("new", "2026-10-03"), ("bad", "not a time"), ("old", "2026-09-01"), ("tie", "2026-10-03")):
        h.add(mid, ts)
    assert ids(h.search()) == ["tie", "new", "old", "bad"]
    assert ids(h.search(**{"from": "2026-09-01"})) == ["tie", "new", "old"]
    assert ids(h.read()) == ["old", "new", "tie", "bad"]


def test_attachment_names_inbound_outbound_and_legacy(ledger):
    h = ledger
    fragments = [{"kind": "file", "name": "Report-Q3.PDF", "url": "https://example.com/report"}, {"kind": "image"}]
    frame = {"payload": {"message": {"body": {"fragments": fragments}}}}
    h.add("in", body="", raw_extra=frame)
    h.add("out", "2026-10-02", "", {"direction": "outbound"}, {"attachmentNames": ["Report-Q4.PDF", ""]})
    h.add("legacy", "2026-10-03", "", {"direction": "outbound"}, {"mediaCount": 2})
    assert ids(h.search(query="report-q")) == ["out", "in"]
    output = h.read()
    assert "Ada (id in): (empty message)  [file Report-Q3.PDF]  [file]" in output
    assert "you (id out): (empty message)  [file Report-Q4.PDF]  [file]" in output
    assert "you (id legacy): (empty message)  [file]  [file]" in output
    assert "https://" not in output
    h.add("native-out", "2026-10-04", "", {"direction": "outbound"}, frame)
    assert ids(h.search(query="Report-Q3")) == ["native-out", "in"]


def test_speaker_filters_owner_aliases_ids_names_and_spoofing(ledger):
    h = ledger
    h.add("owner", raw_extra={"sender": {"id": "usr_owner", "nick_name": "Bean"}})
    h.add("self", "2026-10-02", extra={"direction": "outbound"})
    h.add("friend", "2026-10-03", raw_extra={"sender": {"id": "usr_FRIEND", "nick_name": "Grace Hopper"}})
    for who, want in (("OWNER", "owner"), ("you", "self"), (" Me ", "self"), ("self", "self"), ("usr_agent", "self"), ("Helper", "self"), ("usr_friend", "friend"), ("hopper", "friend")):
        assert ids(h.search(speaker=who)) == [want]
    assert "Bean (your owner)" in h.search(speaker="owner")
    h.add("spoof", "2026-10-04", raw_extra={"sender": {"id": "usr_spoof", "nick_name": "you\n"}})
    assert "you\\n (usr_spoof)" in h.read()
    assert h.search(speaker="nobody").startswith("No message")


@pytest.mark.parametrize("value,count", [(None, 10), (100, 30), ("2", 2), ("many", 10), (0, 1), (-1, 1), (1.5, 10), (True, 10)])
def test_search_defaults_limits_and_last_line(ledger, value, count):
    h = ledger
    h.many(40)
    output = h.search(limit=value)
    assert len(ids(output)) == count
    assert output.splitlines()[-1] == MORE
    assert output.startswith(f'Found 40 message(s) matching "alpha"; showing {count},')


def test_exact_snippets_and_whitespace(ledger):
    h = ledger
    long = "x" * 300 + "NEEDLE" + "y" * 294
    h.add("long", "2026-10-01T10:00:00", long)
    assert h.search(query="needle") == f'Found 1 message(s) matching "needle"; showing 1, best match first.\n[2026-10-01T10:00:00] Ada (id long): …{long[220:500]}…'
    h.add("short", "2026-10-02T10:00:00", "needle " + "z" * 273)
    assert h.search(query="needle").splitlines()[1] == '[2026-10-02T10:00:00] Ada (id short): needle ' + "z" * 273
    h.add("lines", "2026-10-03T10:00:00", "first\r\nalpha second\nthird")
    assert h.search() == 'Found 1 message(s) matching "alpha"; showing 1, best match first.\n[2026-10-03T10:00:00] Ada (id lines): first alpha second third'
    assert h.search(query=" unmatched-one  unmatched-two ") == 'No message in this conversation matches "unmatched-one  unmatched-two". Try other words, or chat_history_read a time range.'


def test_search_whole_reply_and_enormous_query_budget(ledger):
    h = ledger
    for i in range(30):
        h.add(f"m{i}", body="alpha " * 60, raw_extra={"sender": {"id": "usr_ada", "nick_name": "N" * 400}})
    output = h.search(limit=30)
    assert len(output) <= 12000 and 0 < len(ids(output)) < 30
    assert output.splitlines()[-1] == MORE
    assert f"showing {len(ids(output))}," in output
    for query in ("x" * 13000 + " alpha", "x" * 13000):
        assert len(h.search(query=query)) <= 12000
    assert ids(h.search(query="x" * 13000 + " alpha", limit=1)) == ["m29"]


def test_latest_defaults_caps_and_order(ledger):
    h = ledger
    h.many(65)
    assert ids(h.read()) == [f"m{i}" for i in range(46, 66)]
    assert ids(h.read(limit=100)) == [f"m{i}" for i in range(6, 66)]
    assert "More messages" not in h.read()
    assert ids(h.read(limit="2")) == ["m64", "m65"]
    assert ids(h.read(limit=0)) == ["m65"]


def test_around_center_edges_and_range_precedence(ledger):
    h = ledger
    h.many(30)
    for cap in (5, 6):
        output = ids(h.read(around="m15", limit=cap, **{"from": "2027-01-01"}))
        assert len(output) == cap and output.index("m15") == (cap - 1) // 2
    assert ids(h.read(around="m1", limit=5)) == [f"m{i}" for i in range(1, 6)]
    assert ids(h.read(around="m30", limit=5)) == [f"m{i}" for i in range(26, 31)]
    assert len(ids(h.read(around="m1", limit=60))) == 30
    with pytest.raises(ValueError, match="INVALID: no message with id ghost"):
        h.read(around="ghost")


def test_ranges_endpoints_and_continuation_timestamp(ledger):
    h = ledger
    h.many(30)
    assert ids(h.read(**{"from": "2026-10-01T00:00:10Z", "to": "2026-10-01T00:00:20Z", "limit": 4})) == ["m10", "m11", "m12", "m13"]
    assert h.read(**{"from": "2026-10-01T00:00:00Z", "limit": 10}).splitlines()[-1] == 'More messages follow — call again with from: "2026-10-01T00:00:11.000Z".'
    assert "More messages" not in h.read(**{"from": "2026-10-01T00:00:00Z", "to": "2026-10-01T00:00:10Z", "limit": 10})
    assert h.read(**{"from": "2027-01-01"}) == "No messages in this conversation for that range."


def test_verbatim_multiline_empty_and_body_cut(ledger):
    h = ledger
    h.add("one", "2026-10-01T10:00:00", "one line")
    h.add("two", "2026-10-02T10:00:00", "two\nlines", {"direction": "outbound"}, {"attachmentNames": ["gone.png"]})
    h.add("empty", "2026-10-03T10:00:00", "  ")
    assert h.read() == '3 message(s), oldest first:\n[2026-10-01T10:00:00] Ada (id one): one line\n[2026-10-02T10:00:00] you (id two):\ntwo\nlines  [file gone.png]\n[2026-10-03T10:00:00] Ada (id empty): (empty message)'
    h.add("long", "2026-10-04T10:00:00", "q" * 5000)
    assert h.read(limit=1) == '1 message(s), oldest first:\n[2026-10-04T10:00:00] Ada (id long): ' + "q" * 4000 + "… (cut)"


def test_read_whole_reply_cut_points_at_first_omitted(ledger):
    h = ledger
    h.many(20, "x" * 3000)
    output = h.read(**{"from": "2026-10-01T00:00:00Z", "limit": 20})
    count = len(ids(output))
    assert len(output) <= 12000 and 0 < count < 20
    assert output.splitlines()[-1] == f'More messages follow — call again with from: "2026-10-01T00:00:{count + 1:02d}.000Z".'


@pytest.mark.parametrize("zone", ["UTC", "America/Los_Angeles", "Asia/Shanghai"])
def test_local_bare_dates_whole_day_and_iso_offsets(ledger, zone):
    previous = os.environ.get("TZ")
    os.environ["TZ"] = zone
    time.tzset()
    try:
        h = ledger
        h.add("before", "2026-10-29T23:59:00")
        h.add("day", "2026-10-30T23:59:59.999")
        h.add("after", "2026-10-31T00:00:00")
        for output in (h.search(**{"from": "2026-10-30", "to": "2026-10-30"}), h.read(**{"from": "2026-10-30", "to": "2026-10-30"})):
            assert ids(output) == ["day"]
        h.add("offset", "2026-10-31T02:00:00+02:00")
        assert "offset" in ids(h.search(**{"from": "2026-10-31T00:00:00Z", "to": "2026-10-31T00:00:00Z"}))
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_timestamp_fallback_and_invalid_arguments(ledger):
    h = ledger
    h.add("timestamp", extra={"raw": {"timestamp": "2026-10-01T00:00:00Z"}})
    h.add("emitted", 1788220800000, raw_extra={"timestamp": "2027-01-01"})
    h.add("fallback", extra={"raw": None, "created_at": 1785542400000})
    assert ids(h.read()) == ["fallback", "emitted", "timestamp"]
    for field in ("from", "to"):
        for tool in ("chat_history_search", "chat_history_read"):
            with pytest.raises(ValueError, match=f"INVALID: {field} must be YYYY-MM-DD or an ISO 8601 time."):
                h.run(tool, {"query": "alpha", field: "garbage"})
    for query in ("", "  \n ", None, 5):
        with pytest.raises(ValueError, match="INVALID: query must contain at least one word."):
            h.search(query=query)
    assert len(ids(h.search(**{"from": "", "to": 7, "speaker": "  "}))) == 3
    with pytest.raises(ValueError, match='INVALID: unknown history tool "chat_history_x".'):
        h.run("chat_history_x")
    assert h.run(chat="cnv_empty") == "No messages in this conversation for that range."


def test_utf16_budget_and_iso_normalization(ledger):
    h = ledger
    h.add("unicode", "2026-10-01", "😀" * 2010)
    assert h.read(limit=1).endswith("😀" * 2000 + "… (cut)")
    h.add("overflow", "2026-09-31T25:00:00Z")
    assert ids(h.search(**{"from": "2026-10-02T01:00:00Z", "to": "2026-10-02T01:00:00Z"})) == ["overflow"]


@pytest.fixture
def session_ledger(ledger, monkeypatch):
    monkeypatch.setattr(storage, "get_clawchat_store", lambda: ledger.store)
    monkeypatch.setattr(memory_scope, "_owner_user_id", lambda: "usr_owner")
    monkeypatch.setattr(plugin_tools, "_record_tool_call", lambda **_: None)
    return ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_platform", "other_platform", "no_chat", "reader_failure"])
async def test_unresolved_session_returns_error_without_message_query(session_ledger, monkeypatch, case):
    h = session_ledger
    bind = install_host_context(monkeypatch)
    query = Mock(side_effect=AssertionError("must not query"))
    monkeypatch.setattr(h.store, "list_chat_history_messages", query)
    platform = "" if case == "no_platform" else "another" if case == "other_platform" else "clawchat"
    if case == "reader_failure":
        module = sys.modules["gateway.session_context"]
        module.get_session_env = Mock(side_effect=RuntimeError("fixture context failure"))
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "clawchat")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "cnv_A")
    with bind(HERMES_SESSION_PLATFORM=platform, HERMES_SESSION_CHAT_ID="" if case == "no_chat" else "cnv_A"):
        for handler in (plugin_tools.handle_chat_history_search, plugin_tools.handle_chat_history_read):
            assert "error" in json.loads(await handler({"query": "alpha"}))
    query.assert_not_called()


@pytest.mark.asyncio
async def test_contextvar_overrides_environment_and_legacy_fallback(session_ledger, monkeypatch):
    h = session_ledger
    h.add("current")
    h.add("other", extra={"chat_id": "cnv_B"})
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "clawchat")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "cnv_B")
    bind = install_host_context(monkeypatch)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_MESSAGE_ID="current"):
        assert ids(await plugin_tools.handle_chat_history_read({"chat_id": "cnv_B"})) == []
    # An older host has no session-context reader at all.
    monkeypatch.setitem(sys.modules, "gateway.session_context", ModuleType("gateway.session_context"))
    assert ids(await plugin_tools.handle_chat_history_read({})) == ["other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
async def test_adapter_binds_single_and_coalesced_trigger_ids(harness, session_ledger, monkeypatch, owner):
    h = session_ledger
    h.add("old")
    h.add("one")
    h.add("two")
    sender = "usr_owner" if owner else "usr_a"
    incoming = await coalesced([message(sender, "one", chat="cnv_A", kind="group", mid="one"),
                                message(sender, "two", chat="cnv_A", kind="group", mid="two")])
    await harness.adapter._handle_inbound(incoming)
    event = harness.events[-1]
    s = turns.get_snapshot(event.source.message_id)
    assert s.history_exclude_ids == {"one", "two"}
    turns.bind_turn(s.nonce, "s", "t")
    bind = install_host_context(monkeypatch)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_MESSAGE_ID=s.nonce):
        assert ids(await plugin_tools.handle_chat_history_read({}, session_id="s", turn_id="t")) == ["old"]
        assert ids(await plugin_tools.handle_chat_history_search({"query": "alpha"}, session_id="s", turn_id="t")) == ["old"]
        assert "error" in json.loads(await plugin_tools.handle_chat_history_read({"around": "one"}, session_id="s", turn_id="t"))
    # Older hosts without a nonce still expose the unique running batch.
    from clawchat_gateway import terminal_send
    monkeypatch.setattr(terminal_send, "get_clawchat_sender", lambda: harness.adapter)
    harness.adapter._last_inbound_message_id_by_chat["cnv_A"] = "two"
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_MESSAGE_ID=""):
        assert ids(await plugin_tools.handle_chat_history_read({})) == ["old"]
    # A different session's event gets its own exclusion set.
    await harness.adapter._handle_inbound(message(sender, "single", chat="cnv_B", mid="single"))
    single = turns.get_snapshot(harness.events[-1].source.message_id)
    assert single.history_exclude_ids == {"single"}
    assert turns.get_snapshot(s.nonce).history_exclude_ids == {"one", "two"}


@pytest.mark.asyncio
async def test_effective_execution_exclusions_do_not_follow_latest_turn(session_ledger, monkeypatch):
    h = session_ledger
    for mid in ("old", "first", "second"):
        h.add(mid)
    first = turns.register_turn(account_id="usr_agent", chat_id="cnv_A", session_key="history-first",
                                speakers=("usr_a",), grants={}, history_exclude_ids=frozenset({"first"}))
    second = turns.register_turn(account_id="usr_agent", chat_id="cnv_A", session_key="history-second",
                                 speakers=("usr_b",), grants={}, history_exclude_ids=frozenset({"second"}))
    bind = install_host_context(monkeypatch)
    try:
        turns.bind_turn(first.nonce, "s1", "t1")
        turns.bind_turn(second.nonce, "s2", "t2")
        for s, session, turn, expected in ((first, "s1", "t1", ["old", "second"]), (second, "s2", "t2", ["old", "first"])):
            with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_MESSAGE_ID=s.nonce):
                assert ids(await plugin_tools.handle_chat_history_read({}, session_id=session, turn_id=turn)) == expected
        with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_B", HERMES_SESSION_MESSAGE_ID=first.nonce):
            assert "error" in json.loads(await plugin_tools.handle_chat_history_read({}))
    finally:
        turns.release_turn(first.nonce)
        turns.release_turn(second.nonce)


@pytest.mark.asyncio
async def test_database_failure_and_error_redaction(session_ledger, monkeypatch, caplog):
    h = session_ledger
    bind = install_host_context(monkeypatch)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A"):
        fake_secret = "obvious-placeholder-secret"
        output = await plugin_tools.handle_chat_history_read({"around": "https://example.com/?token=" + fake_secret})
        assert "error" in json.loads(output)
        assert fake_secret not in output and fake_secret not in caplog.text
        query = Mock(return_value=None)
        monkeypatch.setattr(h.store, "list_chat_history_messages", query)
        output = await plugin_tools.handle_chat_history_read({})
        assert json.loads(output) == {"error": "Local chat history is unavailable."}
        query.assert_called_once_with("default", "cnv_A")
    h.store._disabled = True
    assert ClawChatStore.list_chat_history_messages(h.store, "default", "cnv_A") is None


@pytest.mark.parametrize("chat_type,user", [("dm", "usr_friend"), ("group", "usr_friend"), ("group", "usr_owner")])
def test_history_is_public_and_session_search_remains_blocked(monkeypatch, chat_type, user):
    bind = install_host_context(monkeypatch)
    names = {"chat_history_search", "chat_history_read", "clawchat_memory_search", "session_search"}
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_owner_user_id", lambda: "usr_owner")
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: "cnv_owner")
    monkeypatch.setattr(guard, "_is_known_group", lambda _: chat_type == "group")
    monkeypatch.setattr(guard, "memory_provider_tools", lambda: frozenset())
    monkeypatch.setattr(guard, "_registry_knows", lambda name: name in names)
    monkeypatch.setattr(guard, "_registry_toolset", lambda name: "session_search" if name == "session_search" else "clawchat")
    monkeypatch.setattr(guard, "_platform_toolsets", lambda: {"clawchat", "session_search"})
    monkeypatch.setattr(guard, "_resolve_tools", lambda group: {"clawchat": list(names - {"session_search"}), "session_search": ["session_search"]}.get(group, []))
    monkeypatch.setattr(guard, "_hook_missing", None)
    assert guard.public_tool_names(names) == names - {"session_search"}
    snapshot = turns.register_turn(account_id="usr_agent", chat_id="cnv_A", speakers=(user,),
                                   session_key="history-public", grants={})
    try:
        definitions = [{"function": {"name": name}} for name in names]
        filtered = guard._filter_grant_definitions(definitions, snapshot)
        assert {item["function"]["name"] for item in filtered} == names - {"session_search"}
    finally:
        turns.release_turn(snapshot.nonce)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_CHAT_TYPE=chat_type,
              HERMES_SESSION_USER_ID=user, HERMES_SESSION_MESSAGE_ID=""):
        for name in names - {"session_search"}:
            assert guard.clawchat_pre_tool_call(tool_name=name, args={"query": "alpha"}) is None
        assert guard.clawchat_pre_tool_call(tool_name="session_search", args={"query": "alpha"})["action"] == "block"
        kept = guard.toolsets_for_source(SimpleNamespace(platform="clawchat", chat_id="cnv_A", chat_type=chat_type, user_id=user), owner_user_id="usr_owner")
        assert "clawchat" in kept and "session_search" not in kept



def test_unicode_whitespace_matches_reference(ledger):
    h = ledger
    h.add("bom", body="\ufeff")
    h.add("control", body="abc\u0085def")
    assert ids(h.search(query="\ufeffabc\u0085def\ufeff")) == ["control"]
    assert "(id bom): (empty message)" in h.read()
    with pytest.raises(ValueError, match="query must contain at least one word"):
        h.search(query="\ufeff")


def test_dst_repeated_and_skipped_local_times_are_deterministic():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        for _ in range(3):
            assert history._parse_iso("2026-11-01T01:30:00") == history._parse_iso("2026-11-01T08:30:00Z")
            assert history._parse_iso("2026-03-08T02:30:00") == history._parse_iso("2026-03-08T10:30:00Z")
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.mark.asyncio
async def test_cached_self_name_and_legacy_reply_target(session_ledger, monkeypatch):
    from clawchat_gateway import terminal_send
    h = session_ledger
    h.add("prior", extra={"direction": "outbound"})
    h.add("trigger")
    sender = SimpleNamespace(_current_agent_identity=lambda _: ("usr_agent", "Helper"),
                             _read_memory_metadata=lambda *_: {}, _current_turn_trigger=lambda _: "trigger")
    monkeypatch.setattr(terminal_send, "get_clawchat_sender", lambda: sender)
    bind = install_host_context(monkeypatch)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A", HERMES_SESSION_MESSAGE_ID=""):
        assert ids(await plugin_tools.handle_chat_history_search({"query": "alpha", "speaker": "helper"})) == ["prior"]
        assert ids(await plugin_tools.handle_chat_history_read({})) == ["prior"]



@pytest.mark.asyncio
async def test_ambiguous_stored_chat_identity_fails_closed(session_ledger, monkeypatch):
    h = session_ledger
    h.add("upper")
    h.add("lower", extra={"chat_id": "cnv_a"})
    bind = install_host_context(monkeypatch)
    with bind(HERMES_SESSION_PLATFORM="clawchat", HERMES_SESSION_CHAT_ID="cnv_A"):
        assert json.loads(await plugin_tools.handle_chat_history_read({})) == {
            "error": "The conversation's stored identity is ambiguous."}
