"""Before a conversation's context is reset or compressed, its facts go to notes.

Hermes' own pre-compression and pre-reset memory flush (``flush_memories``) was
removed upstream before v0.12.0, and the background memory review is switched
off for ClawChat (memory.nudge_interval: 0). So nothing would save what a
conversation established before ``/new`` clears it or compression summarises
it away. The plugin runs its own *sediment* turn in that same session first: a
no-reply turn that may only write the ClawChat notes of this conversation
(``users/<id>.md`` of its people, ``groups/<id>.md`` of this group, ``owner.md``
only for facts about the owner) — never Hermes' MEMORY.md / USER.md and never
another conversation's notes. Anything the turn tries to send to the chat is
dropped.

Triggers:

* ``/new`` / ``/reset`` reaching Hermes from ClawChat (``sediment-on-reset``,
  factory on): sediment first, then the command — unless the session is busy
  (the command is an escape hatch) or nothing happened since the last reset.
* After a turn whose last request used ``session-cap-tokens`` minus
  ``sediment-margin-tokens`` or more (factory 150000 / 10000) — the host
  compresses at the cap (``compression.threshold_tokens``, filled in by the
  plugin when missing) — once per Hermes session id (``sediment-on-compact``).
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from clawchat_gateway import activate, sediment
from clawchat_gateway.adapter import NO_REPLY_TOKEN, ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import InboundMessage

AGENT = "usr_agent"
OWNER = "usr_owner"
FRIEND = "usr_friend"
DM = "cnv_dm"
GROUP = "cnv_group"


def _cfg(extra):
    return ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra))


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sediment.reset_usage_registry()
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a._active_sessions = {}
    a.dispatched = []
    a.frames = []

    async def handle_message(event):
        a.dispatched.append(event)
        raw = event.raw_message.get("clawchat_raw") or {}
        if raw.get("clawchat_sediment"):
            # The model tries to say something in the chat; it must not leak.
            await a.send(event.source.chat_id, "I saved some notes!")
            await a.on_processing_complete(event, None)

    async def send_frame(frame, **_kw):
        a.frames.append(frame)
        return True

    async def no_refresh(_group_id):
        return None

    monkeypatch.setattr(a, "handle_message", handle_message, raising=False)
    monkeypatch.setattr(a, "_ensure_group_participants_metadata", no_refresh)
    monkeypatch.setattr(a, "build_source", lambda **kw: SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._group_settings_ready.set()
    return a


def direct(text, sender=FRIEND):
    return InboundMessage(
        chat_id=DM, chat_type="direct", sender_id=sender, sender_name="", text=text,
        raw_message={"payload": {"message_id": f"m-{text}"}},
    )


def group(text, sender="usr_ada"):
    return InboundMessage(
        chat_id=GROUP, chat_type="group", sender_id=sender, sender_name="Ada", text=text,
        raw_message={"payload": {"message_id": f"g-{text}"}},
    )


def _is_sediment(event):
    return bool((event.raw_message.get("clawchat_raw") or {}).get("clawchat_sediment"))


# --- prompt ---------------------------------------------------------------


def test_prompt_for_a_friend_dm_allows_only_their_note():
    text = sediment.build_sediment_prompt(
        reason="reset", targets=[("user", FRIEND, "this person")]
    )
    assert f"targetType=user targetId={FRIEND}" in text
    assert "targetType=owner" not in text
    assert "clawchat_memory_read" in text and "clawchat_memory_write" in text
    assert "MEMORY.md" in text
    assert NO_REPLY_TOKEN in text


def test_group_targets_cover_group_speakers_and_owner(adapter):
    adapter._store.insert_message(
        platform="hermes", account_id="default", kind="message", direction="inbound",
        event_type="message.send", chat_id=GROUP, message_id="x1", text="hi",
        raw={"sender": {"id": "usr_ada", "nick_name": "Ada"}},
    )
    targets = adapter._sediment_targets(GROUP, "group", sender_id="")
    assert ("group", GROUP) == targets[0][:2]
    assert ("user", "usr_ada") in [t[:2] for t in targets]
    assert ("owner", "owner") in [t[:2] for t in targets]
    assert ("user", AGENT) not in [t[:2] for t in targets]


def test_owner_dm_targets_owner_md_only(adapter):
    targets = adapter._sediment_targets(DM, "direct", sender_id=OWNER)
    assert [t[:2] for t in targets] == [("owner", "owner")]


# --- reset trigger ----------------------------------------------------------


@pytest.mark.asyncio
async def test_new_runs_a_silent_sediment_turn_first(adapter):
    await adapter._handle_inbound(direct("/new"))
    assert len(adapter.dispatched) == 2
    first, second = adapter.dispatched
    assert _is_sediment(first)
    assert second.text == "/new"
    assert adapter.frames == []  # the sediment turn's text never reached the chat
    assert DM not in adapter._sediment_chats


@pytest.mark.asyncio
async def test_reset_sediment_can_be_switched_off(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, sediment_on_reset=False)
    await adapter._handle_inbound(direct("/reset"))
    assert [e.text for e in adapter.dispatched] == ["/reset"]


@pytest.mark.asyncio
async def test_busy_session_gets_the_command_at_once(adapter):
    adapter._active_sessions = {f"agent:main:clawchat:dm:{DM}": object()}
    await adapter._handle_inbound(direct("/new"))
    assert [e.text for e in adapter.dispatched] == ["/new"]


@pytest.mark.asyncio
async def test_nothing_to_save_right_after_a_reset(adapter):
    await adapter._handle_inbound(direct("/new"))
    adapter.dispatched.clear()
    await adapter._handle_inbound(direct("/new"))
    assert [e.text for e in adapter.dispatched] == ["/new"]
    await adapter._handle_inbound(direct("hello again"))
    adapter.dispatched.clear()
    await adapter._handle_inbound(direct("/new"))
    assert _is_sediment(adapter.dispatched[0])


@pytest.mark.asyncio
async def test_group_reset_sediment_uses_the_shared_session(adapter):
    await adapter._handle_inbound(group("/new"))
    sed = adapter.dispatched[0]
    assert _is_sediment(sed)
    assert sed.source.user_id == "__group_shared__"


@pytest.mark.asyncio
async def test_other_chats_still_send_during_a_sediment_turn(adapter):
    adapter._sediment_chats.add(DM)
    assert (await adapter.send("cnv_elsewhere", "hello")) is not None
    adapter._sediment_chats.discard(DM)


# --- compaction trigger -------------------------------------------------------


def _usage(chat_id, session_id, prompt_tokens, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", chat_id)
    sediment.clawchat_post_api_request(
        platform="clawchat", session_id=session_id, usage={"prompt_tokens": prompt_tokens}
    )


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_turn_near_the_cap_is_followed_by_one_sediment_turn(adapter, monkeypatch):
    _usage(DM, "s1", 145_000, monkeypatch)
    event = SimpleNamespace(source=SimpleNamespace(chat_id=DM, chat_type="dm", user_id=FRIEND), raw_message={})
    await adapter.on_processing_complete(event, None)
    await _settle()
    assert [_is_sediment(e) for e in adapter.dispatched] == [True]
    await adapter.on_processing_complete(event, None)
    await _settle()
    assert len(adapter.dispatched) == 1  # once per Hermes session id


@pytest.mark.asyncio
async def test_turn_below_the_line_does_nothing(adapter, monkeypatch):
    _usage(DM, "s1", 120_000, monkeypatch)
    event = SimpleNamespace(source=SimpleNamespace(chat_id=DM, chat_type="dm", user_id=FRIEND), raw_message={})
    await adapter.on_processing_complete(event, None)
    await _settle()
    assert adapter.dispatched == []


@pytest.mark.asyncio
async def test_compact_sediment_can_be_switched_off(adapter, monkeypatch):
    adapter._clawchat_config = replace(adapter._clawchat_config, sediment_on_compact=False)
    _usage(DM, "s1", 149_000, monkeypatch)
    event = SimpleNamespace(source=SimpleNamespace(chat_id=DM, chat_type="dm", user_id=FRIEND), raw_message={})
    await adapter.on_processing_complete(event, None)
    await _settle()
    assert adapter.dispatched == []


def test_usage_hook_ignores_other_platforms(monkeypatch):
    sediment.reset_usage_registry()
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", DM)
    sediment.clawchat_post_api_request(platform="telegram", session_id="s", usage={"prompt_tokens": 9})
    assert sediment.latest_usage(DM) is None


# --- config + host defaults -------------------------------------------------


def test_session_keys_defaults_and_clamps():
    cfg = _cfg({})
    assert (cfg.session_cap_tokens, cfg.sediment_margin_tokens) == (150000, 10000)
    assert cfg.sediment_on_compact is True and cfg.sediment_on_reset is True
    cfg = _cfg({"session-cap-tokens": 10, "sediment-margin-tokens": 10**9,
                "sediment-on-compact": "off", "sediment-on-reset": False})
    assert (cfg.session_cap_tokens, cfg.sediment_margin_tokens) == (50000, 50000)
    assert cfg.sediment_on_compact is False and cfg.sediment_on_reset is False


@pytest.fixture
def host_config(monkeypatch, tmp_path):
    state = {"config": {}}
    monkeypatch.setattr(activate, "_load_config", lambda: (tmp_path / "c.yaml", state["config"]))
    monkeypatch.setattr(activate, "_write_config", lambda _p, c: state.update(config=c))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return state


def test_load_fills_the_compression_cap(host_config):
    activate.ensure_clawchat_host_defaults_on_load()
    assert host_config["config"]["compression"]["threshold_tokens"] == 150000


def test_load_uses_the_plugin_cap_key(host_config):
    host_config["config"] = {"platforms": {"clawchat": {"extra": {"session-cap-tokens": 200000}}}}
    activate.ensure_clawchat_host_defaults_on_load()
    assert host_config["config"]["compression"]["threshold_tokens"] == 200000


def test_load_keeps_an_operator_compression_cap(host_config):
    host_config["config"] = {"compression": {"threshold_tokens": 90000, "threshold": 0.6}}
    activate.ensure_clawchat_host_defaults_on_load()
    assert host_config["config"]["compression"] == {"threshold_tokens": 90000, "threshold": 0.6}


@pytest.mark.asyncio
async def test_a_reset_forgets_the_old_sessions_size(adapter, monkeypatch):
    _usage(DM, "s1", 149_000, monkeypatch)
    await adapter._handle_inbound(direct("/new"))
    adapter.dispatched.clear()
    command_event = SimpleNamespace(
        text="/new", source=SimpleNamespace(chat_id=DM, chat_type="dm", user_id=FRIEND), raw_message={}
    )
    await adapter.on_processing_complete(command_event, None)
    await _settle()
    assert adapter.dispatched == []
    assert sediment.latest_usage(DM) is None
