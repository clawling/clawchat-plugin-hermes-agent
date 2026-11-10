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
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import activate, sediment, history_maintenance as hm
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
    a.gateway_runner = SimpleNamespace(
        _session_key_for_source=lambda source: f'{source.chat_type}:{source.chat_id}:{source.user_id}',
        session_store=SimpleNamespace(peek_session_id=lambda key: 's1'))

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


def test_prompt_skips_what_participants_asked_not_to_remember():
    # A sediment turn reads back the whole conversation; "don't save this" /
    # "forget that" said earlier must win over "keep what will still matter".
    text = sediment.build_sediment_prompt(
        reason="compact", targets=[("group", GROUP, "this group")]
    )
    lowered = text.lower()
    assert "asked you not to remember" in lowered
    assert "forget" in lowered
    assert "skip" in lowered


def test_group_targets_cover_group_speakers_and_owner(adapter):
    adapter._store.insert_message(
        platform="hermes", account_id="default", kind="message", direction="inbound",
        event_type="message.send", chat_id=GROUP, message_id="x1", text="hi",
        raw={"sender": {"id": "usr_ada", "nick_name": "Ada"}},
    )
    targets = adapter._sediment_targets(GROUP, "group", sender_id="")
    assert ("group", GROUP) == targets[0][:2]
    assert ("user", "usr_ada") in [t[:2] for t in targets]
    # A fact about the owner said in the group may go to owner.md (model's call).
    assert targets[-1][:2] == ("owner", "owner")
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


@pytest.mark.parametrize('group_chat', [False, True])
async def test_lease_busy_skips_reset_notes_but_forwards_new(adapter, group_chat):
    keys = []
    def canonical(source):
        keys.append((source.chat_id, source.user_id))
        return 'canonical-session-key'
    adapter.gateway_runner = SimpleNamespace(
        _session_key_for_source=canonical,
        session_store=SimpleNamespace(peek_session_id=lambda key: 'old-session'),
        _turn_leases=SimpleNamespace(_leases={'old-session': SimpleNamespace(
            holder=object(), lock=asyncio.Lock(), pending_acquires=0)}))
    message = group('/new') if group_chat else direct('/new')
    await adapter._handle_inbound(message)
    assert [e.text for e in adapter.dispatched] == ['/new']
    assert keys and set(keys) == {(GROUP, '__group_shared__') if group_chat else (DM, FRIEND)}


@pytest.mark.parametrize('hook,evidence,outcome,expected', [
    (True, False, 'SUCCESS', False),
    (True, True, 'SUCCESS', True),
    (True, 'wrong-session', 'SUCCESS', False),
    (True, 'wrong-key', 'SUCCESS', False),
    (True, True, 'FAILURE', False),
    (True, True, 'CANCELLED', False),
    (False, False, 'SUCCESS', True),
    (False, False, 'FAILURE', False),
    (False, False, 'CANCELLED', False),
])
async def test_sediment_execution_requires_request_and_success(
    adapter, monkeypatch, hook, evidence, outcome, expected,
):
    monkeypatch.setattr(hm, 'sediment_request_hook_supported', lambda: hook)
    async def handle(ev):
        pending = adapter._sediment_done[DM]
        assert pending.nonce == ev.source.message_id
        if evidence:
            values = {'HERMES_SESSION_MESSAGE_ID': pending.nonce,
                      'HERMES_SESSION_KEY': 'other-key' if evidence == 'wrong-key' else pending.key}
            monkeypatch.setattr(hm, '_session_env', lambda name: values.get(name, ''))
            hm.post_api_request(platform='clawchat',
                                session_id='other-session' if evidence == 'wrong-session' else pending.session_id,
                                usage=None)
        await adapter.on_processing_complete(ev, SimpleNamespace(name=outcome))
    monkeypatch.setattr(adapter, 'handle_message', handle)
    assert await adapter._run_sediment_turn(
        chat_id=DM, chat_type='direct', sender_id=FRIEND, reason='compact') is expected
    assert not adapter._sediment_done
    assert not any(item[1]() is adapter for item in hm._sediment_observers.values())


def test_sediment_hook_capability_checks_registration_and_context(monkeypatch):
    hooks = {'post_api_request': [hm.post_api_request]}
    valid_hooks = {'post_api_request'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.plugins', SimpleNamespace(
        VALID_HOOKS=valid_hooks, get_plugin_manager=lambda: SimpleNamespace(_hooks=hooks)))
    context = SimpleNamespace(get_session_env=lambda *args: '', _VAR_MAP={
        name: ContextVar(name) for name in ('HERMES_SESSION_MESSAGE_ID', 'HERMES_SESSION_KEY')})
    monkeypatch.setitem(sys.modules, 'gateway.session_context', context)
    assert hm.sediment_request_hook_supported() is True
    valid_hooks.clear()
    assert hm.sediment_request_hook_supported() is False
    valid_hooks.add('post_api_request')
    hooks['post_api_request'] = [lambda **kwargs: None]
    assert hm.sediment_request_hook_supported() is None
    hooks['post_api_request'] = [hm.post_api_request]
    monkeypatch.delattr(context, 'get_session_env')
    assert hm.sediment_request_hook_supported() is None
    monkeypatch.setitem(sys.modules, 'gateway.session_context', None)
    assert hm.sediment_request_hook_supported() is False


async def test_worker_request_proof_precedes_completion_with_delayed_loop_observer(adapter, monkeypatch):
    monkeypatch.setattr(hm, 'sediment_request_hook_supported', lambda: True)
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    notifications, worker_threads, failures, captured = [], [], [], []
    original = adapter._observe_sediment_request
    # The loop notification deliberately does not apply proof. The worker must
    # publish it synchronously, independent of this callback's delivery order.
    def deferred_observer(*identity):
        assert threading.get_ident() == loop_thread
        notifications.append(identity)
    monkeypatch.setattr(adapter, '_observe_sediment_request', deferred_observer)
    async def handle(ev):
        pending = adapter._sediment_done[DM]
        captured.append(pending)
        values = {'HERMES_SESSION_MESSAGE_ID': pending.nonce, 'HERMES_SESSION_KEY': pending.key}
        monkeypatch.setattr(hm, '_session_env', lambda name: values.get(name, ''))
        def model_worker():
            try:
                worker_threads.append(threading.get_ident())
                hm.post_api_request(platform='clawchat', session_id=pending.session_id)
                assert pending.executed
                asyncio.run_coroutine_threadsafe(
                    adapter.on_processing_complete(ev, SimpleNamespace(name='SUCCESS')), loop).result(timeout=2)
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=model_worker)
        worker.start()
        await asyncio.to_thread(worker.join, 3)
        assert not worker.is_alive() and not failures
    monkeypatch.setattr(adapter, 'handle_message', handle)
    assert await adapter._run_sediment_turn(
        chat_id=DM, chat_type='direct', sender_id=FRIEND, reason='compact')
    assert worker_threads and worker_threads[0] != loop_thread
    assert notifications == [(captured[0].nonce, captured[0].key, captured[0].session_id)]
    # Applying the delayed callback after cleanup cannot affect another turn.
    for identity in notifications:
        original(*identity)
    assert not adapter._sediment_done


async def test_worker_hook_after_deadline_cannot_confirm_replacement(adapter, monkeypatch):
    monkeypatch.setattr(hm, 'sediment_request_hook_supported', lambda: True)
    loop = asyncio.get_running_loop()
    expired, replacements, failures, workers = [], [], [], []
    async def handle(ev):
        pending = adapter._sediment_done[DM]
        if not expired:
            expired.append(pending)
            pending.deadline = time.monotonic() - 1
            await adapter.on_processing_complete(ev, SimpleNamespace(name='SUCCESS'))
            return
        replacements.append(pending)
        old = expired[0]
        values = {'HERMES_SESSION_MESSAGE_ID': old.nonce, 'HERMES_SESSION_KEY': old.key}
        monkeypatch.setattr(hm, '_session_env', lambda name: values.get(name, ''))
        def late_model_worker():
            try:
                hm.post_api_request(platform='clawchat', session_id=old.session_id)
                asyncio.run_coroutine_threadsafe(
                    adapter.on_processing_complete(ev, SimpleNamespace(name='SUCCESS')), loop).result(timeout=2)
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=late_model_worker)
        workers.append(worker)
        worker.start()
        await asyncio.to_thread(worker.join, 3)
        assert not worker.is_alive() and not failures
    monkeypatch.setattr(adapter, 'handle_message', handle)
    for _ in range(2):
        assert not await adapter._run_sediment_turn(
            chat_id=DM, chat_type='direct', sender_id=FRIEND, reason='compact')
    assert replacements[0].nonce != expired[0].nonce
    assert not expired[0].executed and not replacements[0].executed
    assert not failures and all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize('change', ['key', 'operation'])
async def test_sediment_route_or_operation_change_during_preparation_is_discarded(adapter, monkeypatch, change):
    monkeypatch.setattr(hm, 'sediment_request_hook_supported', lambda: False)
    preparing, resume = asyncio.Event(), asyncio.Event()
    key = 'original-key'
    operation_active = True
    adapter.gateway_runner._session_key_for_source = lambda source: key
    async def metadata(chat):
        preparing.set()
        await resume.wait()
    monkeypatch.setattr(adapter, '_ensure_group_participants_metadata', metadata)
    task = asyncio.create_task(adapter._run_sediment_turn(
        chat_id=GROUP, chat_type='group', sender_id=FRIEND, reason='compact',
        operation_current=lambda: operation_active))
    try:
        await asyncio.wait_for(preparing.wait(), 2)
        pending = adapter._sediment_done[GROUP]
        assert pending.key == 'original-key' and pending.session_id == 's1'
        if change == 'key':
            key = 'replacement-key'
        else:
            operation_active = False
        resume.set()
        assert not await asyncio.wait_for(task, 2)
        assert pending.failed and pending.done.is_set() and not adapter.dispatched
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_sediment_events_and_requests_do_not_complete_replacement(adapter):
    from clawchat_gateway.adapter import _SedimentTurn
    current = _SedimentTurn('new-operation', asyncio.Event(), nonce='new-nonce',
                            key='canonical-session-key', session_id='old-session')
    adapter._sediment_done[DM] = current
    old = SimpleNamespace(source=SimpleNamespace(chat_id=DM), raw_message={'clawchat_raw': {
        'clawchat_sediment': True, 'sediment_id': 'old-operation'}})
    adapter._after_host_turn(old)
    for nonce, key, sid in [('old-nonce', current.key, current.session_id),
                            (current.nonce, 'other-key', current.session_id),
                            (current.nonce, current.key, 'other-session')]:
        adapter._observe_sediment_request(nonce, key, sid)
    assert not current.done.is_set() and not current.executed
    adapter._observe_sediment_request(current.nonce, current.key, current.session_id)
    assert current.executed


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


# --- "don't remember" reaches the sediment turn --------------------------------
#
# What is enforced in code and what is not: the plugin cannot tell a fact
# someone asked not to keep from one worth keeping — that is the model's
# judgement, steered by the sediment prompt. Code makes sure (1) the prompt
# carries the rule, (2) the request itself is in front of the sediment turn even
# when the shared group session never saw it (a mention-only group holds back
# messages that do not mention the agent until the next mention), and (3) the
# memory tools the turn calls keep their read scope (a group cannot read
# owner.md). Neither the skip nor keeping to the listed notes is filtered in
# code. The stand-in model below keeps every fact unless the prompt carries the
# rule; then it drops a message that asks not to be remembered.

KEEP = "Our hike moved to Saturday 9am at the north gate."
SECRET = "The codeword is PELICAN-42, please don't remember it."


def _store_group_message(adapter, message_id, sender, name, text):
    adapter._store.insert_message(
        platform="hermes", account_id="default", kind="message", direction="inbound",
        event_type="message.send", chat_id=GROUP, message_id=message_id, text=text,
        raw={"sender": {"id": sender, "nick_name": name}},
    )


def _stand_in_model(prompt):
    """The facts a model following *prompt* would append to the group note."""
    obeys_rule = "asked you not to remember" in prompt.lower()
    lines = [line for line in prompt.splitlines() if ": " in line and "(usr_" in line]
    facts = [line.split(": ", 1)[1] for line in lines]
    if not obeys_rule:
        return facts
    return [fact for fact in facts if "don't remember" not in fact.lower()]


@pytest.mark.asyncio
async def test_group_sediment_writes_the_kept_fact_not_the_one_asked_to_forget(adapter, monkeypatch):
    from clawchat_gateway import plugin_tools, tools
    from clawchat_gateway.clawchat_memory import read_clawchat_memory_file

    memories = adapter._clawchat_config.memory_root
    monkeypatch.setattr(tools, "_resolve_memory_root", lambda: (Path(memories), None))
    # Neither message mentioned the agent, so the shared session has not seen them.
    _store_group_message(adapter, "k1", "usr_ada", "Ada", KEEP)
    _store_group_message(adapter, "k2", "usr_ben", "Ben", SECRET)
    writes = []

    async def handle_message(event):
        adapter.dispatched.append(event)
        raw = event.raw_message.get("clawchat_raw") or {}
        if not raw.get("clawchat_sediment"):
            return
        for key, value in {
            "HERMES_SESSION_PLATFORM": "clawchat", "HERMES_SESSION_CHAT_ID": GROUP,
            "HERMES_SESSION_CHAT_TYPE": "group", "HERMES_SESSION_USER_ID": "__group_shared__",
        }.items():
            monkeypatch.setenv(key, value)
        for fact in _stand_in_model(event.text):
            args = {"targetType": "group", "targetId": GROUP, "mode": "append", "content": fact}
            writes.append(args)
            await plugin_tools.handle_clawchat_memory_write(args)
        await adapter.on_processing_complete(event, None)

    monkeypatch.setattr(adapter, "handle_message", handle_message, raising=False)
    await adapter._handle_inbound(group("/new", sender="usr_ada"))

    sediment_event = adapter.dispatched[0]
    assert _is_sediment(sediment_event)
    prompt = sediment_event.text
    # (1) the rule, (2) both messages, with who said them.
    assert "asked you not to remember" in prompt.lower()
    assert f"Ada (usr_ada): {KEEP}" in prompt
    assert f"Ben (usr_ben): {SECRET}" in prompt
    # The stand-in kept to the listed notes, and only the kept fact landed.
    assert {(w["targetType"], w["targetId"]) for w in writes} <= {
        (t, i) for t, i, _ in adapter._sediment_targets(GROUP, "group", sender_id="")
    }
    body = read_clawchat_memory_file(memories, "group", GROUP)["body"]
    assert KEEP in body
    assert "PELICAN-42" not in body
    assert adapter.frames == []


def test_stand_in_model_without_the_rule_would_keep_the_codeword():
    # Guards the test above: it passes because of the prompt's rule.
    prompt = f"Ada (usr_ada): {KEEP}\nBen (usr_ben): {SECRET}"
    assert any("PELICAN-42" in fact for fact in _stand_in_model(prompt))
