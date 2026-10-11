"""The owner's group command is answered in the owner's direct chat.

When the owner manages the agent from a group (``/new``, ``@agent /new``,
``/model`` …) the receipt used to land in the group, in front of everyone.
Now it goes to the owner's direct chat, headed with the group and the command
(so "new session started" is not read as the direct chat's own reset), and
the group only gets a ✅ on the command message.

The source of the event stays the group — it decides which session the
command acts on — so only the delivery moves. A reply is recognised by turn:
the plugin marks the context it hands the command to Hermes with, Hermes runs
the command in a task created from it (or inline when busy), and the marker is
closed when that event's processing completes.

Unchanged: a non-owner's command (``group_command_mode=all``), a muted group,
confirm/approval replies (``/approve`` …), commands that run an agent turn,
explicit sends, and every failure to reach the owner's direct chat (the reply
then goes to the group, as before).
"""

from __future__ import annotations

import asyncio
import types
from dataclasses import replace

import pytest

import clawchat_gateway.adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.group_settings import GroupSettings
from clawchat_gateway.terminal_send import explicit_tool_send

AGENT = "usr_agent"
OWNER = "usr_owner"
STRANGER = "usr_stranger"
GROUP = "cnv_group_1"
OTHER_GROUP = "cnv_group_2"
OWNER_DM = "cnv_owner_dm"

_counter = {"n": 0}


class FakeStore:
    def claim_message_once(self, **_kwargs):
        return True


def text(value: str) -> dict:
    return {"kind": "text", "text": value}


def mention(user_id: str, display: str) -> dict:
    return {"kind": "mention", "user_id": user_id, "display": display}


def group(sender: str, *fragments: dict) -> dict:
    _counter["n"] += 1
    mentions = [{"user_id": f["user_id"]} for f in fragments if f.get("kind") == "mention"]
    return {
        "event": "message.send",
        "chat_id": GROUP,
        "chat_type": "group",
        "trace_id": f"tr_{_counter['n']}",
        "sender": {"id": sender, "nick_name": sender},
        "payload": {
            "message_id": f"msg_{_counter['n']}",
            "message": {"fragments": list(fragments), "context": {"mentions": mentions}},
        },
    }


class Host:
    """Stands in for Hermes: answers each dispatched command the way the
    gateway does — from a task created by the dispatch (idle session) or
    inline (busy session) — then reports processing complete."""

    def __init__(self, adapter: ClawChatAdapter) -> None:
        self.adapter = adapter
        self.replies: list[str] = ["done"]
        self.inline = False
        self.after_complete: list[str] = []
        self.tasks: list[asyncio.Task] = []
        self.dispatched: list = []

    async def handle_inbound(self, inbound) -> None:
        self.dispatched.append(inbound)
        event = types.SimpleNamespace(
            raw_message={"clawchat_raw": inbound.raw_message},
            source=types.SimpleNamespace(chat_id=inbound.chat_id),
        )

        async def run() -> None:
            for reply in self.replies:
                await self.adapter.send(inbound.chat_id, reply)
            await self.adapter.on_processing_complete(event, None)
            # A follow-up the same host task drains after the command.
            for reply in self.after_complete:
                await self.adapter.send(inbound.chat_id, reply)

        if self.inline:
            await run()
        else:
            self.tasks.append(asyncio.create_task(run()))

    async def settle(self) -> None:
        await asyncio.gather(*self.tasks)


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a._store = FakeStore()
    a.frames = []
    a.failing_chats = set()

    async def send_frame(frame, **_kw):
        if frame.get("payload", {}).get("chat_id") in a.failing_chats or frame.get("chat_id") in a.failing_chats:
            return False
        a.frames.append(frame)
        return True

    async def no_forwarded_approval(_inbound):
        return False

    def no_profile_sync(coro):
        coro.close()

    def sender_context(inbound):
        return replace(inbound, sender_relation="owner" if inbound.sender_id == OWNER else "peer_user")

    metadata = {("group", GROUP): {"group_title": "Build Crew"}}
    monkeypatch.setattr(a, "_read_memory_metadata", lambda t, i: dict(metadata.get((t, i), {})))
    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    monkeypatch.setattr(a, "_handle_owner_forwarded_approval", no_forwarded_approval)
    monkeypatch.setattr(a, "_schedule_profile_sync", no_profile_sync)
    monkeypatch.setattr(a, "_resolve_inbound_sender_context", sender_context)
    monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: OWNER_DM)
    a._known_chat_types[GROUP] = "group"
    a._known_chat_types[OTHER_GROUP] = "group"
    a._known_chat_types[OWNER_DM] = "direct"
    a._group_settings_ready.set()
    a.host = Host(a)
    monkeypatch.setattr(a, "_handle_inbound", a.host.handle_inbound)
    return a


def set_group(a: ClawChatAdapter, *, muted: bool = False, reply_mode: str = "mention") -> None:
    a._group_settings_cache.apply_fetched(
        [
            GroupSettings(
                conversation_id=GROUP,
                muted=muted,
                reply_mode=reply_mode,
                batch_delay_seconds=3600,
                version=1,
            )
        ],
        1,
    )


def _frame_chat(frame: dict) -> str:
    return frame.get("payload", {}).get("chat_id") or frame.get("chat_id") or ""


def replies_in(a: ClawChatAdapter, chat_id: str) -> list[str]:
    out = []
    for frame in a.frames:
        if frame["event"] != "message.reply" or _frame_chat(frame) != chat_id:
            continue
        fragments = frame["payload"]["message"]["body"]["fragments"]
        out.append("".join(f.get("text", "") for f in fragments))
    return out


def reactions(a: ClawChatAdapter) -> list[tuple[str, str, str]]:
    return [
        (_frame_chat(f), f["payload"]["target_message_id"], f["payload"]["emoji"])
        for f in a.frames
        if f["event"] == "message.reaction"
    ]


async def run(a: ClawChatAdapter, frame: dict) -> str:
    await a._on_message(frame)
    await a.host.settle()
    return frame["payload"]["message_id"]


@pytest.mark.asyncio
async def test_owner_new_goes_to_the_owner_with_the_group_and_command(adapter):
    adapter.host.replies = ["✨ New session started."]

    command_id = await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /new\n\n✨ New session started.']
    assert replies_in(adapter, GROUP) == []
    assert reactions(adapter) == [(GROUP, command_id, "✅")]


@pytest.mark.asyncio
async def test_mention_then_new_is_also_a_receipt_for_the_owner(adapter):
    set_group(adapter, reply_mode="mention")
    adapter.host.replies = ["✨ New session started."]

    command_id = await run(adapter, group(OWNER, mention(AGENT, "Otter"), text(" /new")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /new\n\n✨ New session started.']
    assert replies_in(adapter, GROUP) == []
    assert reactions(adapter) == [(GROUP, command_id, "✅")]


@pytest.mark.asyncio
async def test_every_reply_of_a_command_moves_and_the_group_gets_one_check(adapter):
    adapter.host.replies = ["Current model: a", "Use /model <name> to switch."]

    command_id = await run(adapter, group(OWNER, text("/model")))

    assert replies_in(adapter, OWNER_DM) == [
        'From group "Build Crew": /model\n\nCurrent model: a',
        'From group "Build Crew": /model\n\nUse /model <name> to switch.',
    ]
    assert replies_in(adapter, GROUP) == []
    assert reactions(adapter) == [(GROUP, command_id, "✅")]


@pytest.mark.asyncio
async def test_a_busy_session_answering_inline_is_moved_too(adapter):
    adapter.host.inline = True
    adapter.host.replies = ["Compressed."]

    await run(adapter, group(OWNER, text("/compress")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /compress\n\nCompressed.']
    assert replies_in(adapter, GROUP) == []


@pytest.mark.asyncio
async def test_the_plugins_own_commands_move_too(adapter):
    adapter.host.replies = ["Output visibility: final."]

    await run(adapter, group(OWNER, text("/clawchat-output")))

    assert replies_in(adapter, OWNER_DM) == [
        'From group "Build Crew": /clawchat-output\n\nOutput visibility: final.'
    ]
    assert replies_in(adapter, GROUP) == []


@pytest.mark.asyncio
async def test_a_group_without_a_cached_title_is_named_by_id(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_read_memory_metadata", lambda t, i: {})

    await run(adapter, group(OWNER, text("/status")))

    assert replies_in(adapter, OWNER_DM) == [f'From group "{GROUP}": /status\n\ndone']


@pytest.mark.asyncio
async def test_a_new_groups_title_cached_during_dispatch_names_the_group(adapter, monkeypatch):
    # The first message of a new group arrives before its title is cached;
    # dispatch fetches the group's metadata, so the reply can name it.
    metadata: dict = {}
    monkeypatch.setattr(adapter, "_read_memory_metadata", lambda t, i: dict(metadata.get((t, i), {})))
    host_handle = adapter.host.handle_inbound

    async def fetch_then_handle(inbound):
        metadata[("group", GROUP)] = {"group_title": "Fresh Group"}
        await host_handle(inbound)

    adapter._handle_inbound = fetch_then_handle

    await run(adapter, group(OWNER, text("/model")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Fresh Group": /model\n\ndone']


@pytest.mark.asyncio
async def test_a_failed_command_still_gets_the_check(adapter):
    adapter.host.replies = ["Unknown model: zzz"]

    command_id = await run(adapter, group(OWNER, text("/model zzz")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /model zzz\n\nUnknown model: zzz']
    assert reactions(adapter) == [(GROUP, command_id, "✅")]


@pytest.mark.asyncio
async def test_a_non_owner_command_in_an_all_group_is_answered_in_the_group(adapter):
    adapter._clawchat_config = replace(adapter._clawchat_config, group_command_mode="all")

    await run(adapter, group(STRANGER, text("/new")))

    assert len(adapter.host.dispatched) == 1
    assert replies_in(adapter, GROUP) == ["done"]
    assert replies_in(adapter, OWNER_DM) == []
    assert reactions(adapter) == []


@pytest.mark.asyncio
async def test_a_muted_group_does_not_take_the_command_or_react(adapter):
    set_group(adapter, muted=True)

    await run(adapter, group(OWNER, text("/new")))

    assert adapter.host.dispatched == []
    assert adapter.frames == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/approve", "/deny", "/cancel"])
async def test_approval_replies_keep_their_routing(adapter, body):
    await run(adapter, group(OWNER, text(body)))

    assert replies_in(adapter, GROUP) == ["done"]
    assert replies_in(adapter, OWNER_DM) == []
    assert reactions(adapter) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/plan ship it", "/btw what time is it", "/goal finish the deck", "/retry"])
async def test_commands_that_run_an_agent_turn_answer_in_the_group(adapter, monkeypatch, body):
    # Hermes' own command table (absent here) knows these.
    known = adapter_mod._known_hermes_slash_command_name
    monkeypatch.setattr(
        adapter_mod,
        "_known_hermes_slash_command_name",
        lambda name: name in {"plan", "btw", "goal", "retry"} or known(name),
    )

    await run(adapter, group(OWNER, text(body)))

    assert replies_in(adapter, GROUP) == ["done"]
    assert replies_in(adapter, OWNER_DM) == []
    assert reactions(adapter) == []


@pytest.mark.asyncio
async def test_without_an_owner_direct_chat_the_reply_stays_in_the_group(adapter, monkeypatch):
    monkeypatch.setattr(adapter, "_owner_direct_chat_id", lambda: "")

    await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, GROUP) == ["done"]
    assert reactions(adapter) == []


@pytest.mark.asyncio
async def test_a_failed_owner_send_falls_back_to_the_group(adapter):
    adapter.failing_chats.add(OWNER_DM)

    await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, GROUP) == ["done"]
    assert reactions(adapter) == []


@pytest.mark.asyncio
async def test_a_follow_up_drained_after_the_command_is_not_its_receipt(adapter):
    adapter.host.after_complete = ["an ordinary group reply"]

    await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /new\n\ndone']
    assert replies_in(adapter, GROUP) == ["an ordinary group reply"]


@pytest.mark.asyncio
async def test_only_the_commands_own_group_is_rerouted(adapter):
    async def run_and_send_elsewhere(inbound):
        adapter.host.dispatched.append(inbound)
        await adapter.send(OTHER_GROUP, "elsewhere")
        await adapter.send(inbound.chat_id, "done")

    adapter._handle_inbound = run_and_send_elsewhere

    await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, OTHER_GROUP) == ["elsewhere"]
    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /new\n\ndone']


@pytest.mark.asyncio
async def test_an_explicit_send_into_the_group_is_not_rerouted(adapter):
    async def explicit(inbound):
        adapter.host.dispatched.append(inbound)
        with explicit_tool_send():
            await adapter.send(inbound.chat_id, "sent by a tool")

    adapter._handle_inbound = explicit

    await run(adapter, group(OWNER, text("/new")))

    assert replies_in(adapter, GROUP) == ["sent by a tool"]
    assert replies_in(adapter, OWNER_DM) == []


# --- The confirm before /new is asked in the owner's direct chat -------------
#
# Hermes asks before /new, /reset, /undo …; the prompt is part of the
# command's receipt. It is asked in the owner's direct chat and answered there,
# for the group's session.

HOST_PROMPT = (
    "⚠️ **Confirm /new**\n\nThis starts a new session.\n\n"
    "Choose: /approve, /always, /cancel"
)


class FakeSlashConfirm:
    def __init__(self) -> None:
        self.pending: dict = {}
        self.resolved: list = []

    def get_pending(self, session_key):
        entry = self.pending.get(session_key)
        return dict(entry) if entry else None

    async def resolve(self, session_key, confirm_id, choice, timeout=300):
        self.resolved.append((session_key, confirm_id, choice))
        entry = self.pending.pop(session_key, None)
        if not entry or entry["confirm_id"] != confirm_id:
            return None
        return "/new cancelled." if choice == "cancel" else "✨ Session reset!"


@pytest.fixture
def slash_confirm(monkeypatch):
    import sys

    fake = FakeSlashConfirm()
    tools_pkg = types.ModuleType("tools")
    tools_pkg.slash_confirm = fake
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", fake)
    return fake


def owner_dm(body: str) -> dict:
    frame = group(OWNER, text(body))
    frame["chat_id"] = OWNER_DM
    frame["chat_type"] = "direct"
    return frame


def asks_confirm(adapter, slash_confirm, results: list, *, session_key="sk_group", confirm_id="9"):
    async def handle_inbound(inbound):
        adapter.host.dispatched.append(inbound)
        if inbound.chat_type != "group":
            return

        async def run():
            slash_confirm.pending[session_key] = {"confirm_id": confirm_id, "command": "new"}
            results.append(
                await adapter.send_slash_confirm(
                    chat_id=inbound.chat_id,
                    title="/new",
                    message=HOST_PROMPT,
                    session_key=session_key,
                    confirm_id=confirm_id,
                    metadata=None,
                )
            )

        adapter.host.tasks.append(asyncio.create_task(run()))

    adapter._handle_inbound = handle_inbound


@pytest.mark.asyncio
async def test_the_new_confirm_is_asked_and_answered_in_the_owners_chat(adapter, slash_confirm):
    results: list = []
    asks_confirm(adapter, slash_confirm, results)

    command_id = await run(adapter, group(OWNER, text("/new")))

    assert results[0].success is True, "Hermes does not send its own prompt into the group"
    assert replies_in(adapter, OWNER_DM) == [f'From group "Build Crew": /new\n\n{HOST_PROMPT}']
    assert replies_in(adapter, GROUP) == []
    assert reactions(adapter) == [(GROUP, command_id, "✅")]

    await run(adapter, owner_dm("/approve"))

    assert slash_confirm.resolved == [("sk_group", "9", "once")]
    assert replies_in(adapter, OWNER_DM)[-1] == 'From group "Build Crew": /new\n\n✨ Session reset!'
    assert replies_in(adapter, GROUP) == []
    assert [i.chat_id for i in adapter.host.dispatched] == [GROUP], "the answer never reaches the agent"


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "choice"), [("/always", "always"), ("/cancel", "cancel"), ("/yes", "once")])
async def test_the_owner_may_answer_with_any_choice(adapter, slash_confirm, reply, choice):
    asks_confirm(adapter, slash_confirm, [])
    await run(adapter, group(OWNER, text("/new")))

    await run(adapter, owner_dm(reply))

    assert slash_confirm.resolved == [("sk_group", "9", choice)]


@pytest.mark.asyncio
async def test_an_expired_confirm_says_so_in_the_owners_chat(adapter, slash_confirm):
    asks_confirm(adapter, slash_confirm, [])
    await run(adapter, group(OWNER, text("/new")))
    slash_confirm.pending.clear()

    await run(adapter, owner_dm("/approve"))

    assert slash_confirm.resolved == []
    assert replies_in(adapter, OWNER_DM)[-1] == (
        'From group "Build Crew": /new\n\nThat confirmation has expired. Send /new in the group again.'
    )


@pytest.mark.asyncio
async def test_another_command_in_the_owners_chat_drops_the_pending_confirm(adapter, slash_confirm):
    asks_confirm(adapter, slash_confirm, [])
    await run(adapter, group(OWNER, text("/new")))

    await run(adapter, owner_dm("/status"))
    await run(adapter, owner_dm("/approve"))

    assert slash_confirm.resolved == []
    assert [i.text for i in adapter.host.dispatched[1:]] == ["/status", "/approve"]


@pytest.mark.asyncio
async def test_without_an_owner_chat_hermes_asks_in_the_group(adapter, slash_confirm, monkeypatch):
    monkeypatch.setattr(adapter, "_owner_direct_chat_id", lambda: "")
    results: list = []
    asks_confirm(adapter, slash_confirm, results)

    await run(adapter, group(OWNER, text("/new")))

    assert results[0].success is False, "Hermes then sends its own text prompt"
    assert adapter._owner_group_slash_confirms == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["approve", "no", "/approve AB12CD"])
async def test_plain_words_and_code_replies_are_not_taken_for_the_group_confirm(adapter, slash_confirm, reply):
    # Plain words stay conversation with the agent; a code-bearing reply
    # answers a forwarded dangerous-command approval.
    asks_confirm(adapter, slash_confirm, [])
    await run(adapter, group(OWNER, text("/new")))

    await run(adapter, owner_dm(reply))

    assert slash_confirm.resolved == []
    assert [i.text for i in adapter.host.dispatched[1:]] == [reply]


@pytest.mark.asyncio
async def test_a_blocking_approval_in_the_owners_chat_answers_first(adapter, slash_confirm, monkeypatch):
    asks_confirm(adapter, slash_confirm, [])
    await run(adapter, group(OWNER, text("/new")))
    monkeypatch.setattr(adapter, "_direct_chat_approval_blocking", lambda chat_id: chat_id == OWNER_DM)

    await run(adapter, owner_dm("/approve"))

    assert slash_confirm.resolved == []
    assert [i.text for i in adapter.host.dispatched[1:]] == ["/approve"]
    assert OWNER_DM in adapter._owner_group_slash_confirms, "still answerable once that one is settled"


@pytest.mark.asyncio
async def test_a_follow_up_hermes_drains_from_an_inline_command_is_not_its_receipt(adapter):
    # Busy session: Hermes answers a command inline, without processing hooks for
    # it, and starts the queued message's turn from the same call — so that
    # task copies the open marker. Its own processing start drops it.
    async def busy_host(inbound):
        adapter.host.dispatched.append(inbound)
        await adapter.send(inbound.chat_id, "Compressed.")
        queued = types.SimpleNamespace(
            raw_message={"clawchat_raw": {"payload": {"message_id": "msg_queued"}}},
            source=types.SimpleNamespace(chat_id=inbound.chat_id),
        )

        async def drained_turn():
            await adapter.on_processing_start(queued)
            await adapter.send(inbound.chat_id, "answer to Bob")
            await adapter.send(inbound.chat_id, "and a second message")

        adapter.host.tasks.append(asyncio.create_task(drained_turn()))

    adapter._handle_inbound = busy_host

    await run(adapter, group(OWNER, text("/compress")))

    assert replies_in(adapter, OWNER_DM) == ['From group "Build Crew": /compress\n\nCompressed.']
    assert replies_in(adapter, GROUP) == ["answer to Bob", "and a second message"]


@pytest.mark.asyncio
async def test_the_error_notice_of_a_failed_command_is_its_receipt_too(adapter):
    async def failing_host(inbound):
        adapter.host.dispatched.append(inbound)
        event = types.SimpleNamespace(
            raw_message={"clawchat_raw": inbound.raw_message},
            source=types.SimpleNamespace(chat_id=inbound.chat_id),
        )

        async def run_command():
            await adapter.on_processing_start(event)
            await adapter.on_processing_complete(event, types.SimpleNamespace(name="FAILURE"))
            await adapter.send(inbound.chat_id, "Sorry, something went wrong.")

        adapter.host.tasks.append(asyncio.create_task(run_command()))

    adapter._handle_inbound = failing_host

    command_id = await run(adapter, group(OWNER, text("/compress")))

    assert replies_in(adapter, OWNER_DM) == [
        'From group "Build Crew": /compress\n\nSorry, something went wrong.'
    ]
    assert replies_in(adapter, GROUP) == []
    assert reactions(adapter) == [(GROUP, command_id, "✅")]
