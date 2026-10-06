"""Who may run a Hermes slash command, and in which order the group gates run.

Two bugs, one routing function (``ClawChatAdapter._on_message``):

* **Direct chats had no owner check.** Group commands were already limited by
  ``groupCommandMode`` (default ``owner``), but a direct message went straight
  to Hermes, and the plugin's runtime defaults set
  ``CLAWCHAT_ALLOW_ALL_USERS=true``. Anyone who befriended an agent could DM it
  ``/restart`` (restart the owner's whole gateway) or ``/update``. Now a
  non-owner may only run commands that touch nothing but their own direct
  session (an allow-list); everything else is dropped before Hermes sees it.

* **Group commands ran after the mention-only gate.** ``agent-protocol.md``
  §3.3 orders the gates mute -> group command -> reply mode. The plugin ran the
  reply-mode drop first, so in a mention-only group a bare ``/new`` vanished;
  and ``@agent /new`` was never recognised as a command because the slash was
  not at the start of the text.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.group_settings import GroupSettings

AGENT = "usr_agent"
OWNER = "usr_owner"
STRANGER = "usr_stranger"
GROUP = "cnv_group_1"
DIRECT = "cnv_direct_1"


class FakeStore:
    def claim_message_once(self, **_kwargs):
        return True


_counter = {"n": 0}


def _frame(*, chat_id: str, chat_type: str, sender: str, fragments: list) -> dict:
    _counter["n"] += 1
    mentions = [
        {"user_id": f.get("user_id")}
        for f in fragments
        if isinstance(f, dict) and f.get("kind") == "mention"
    ]
    return {
        "event": "message.send",
        "chat_id": chat_id,
        "chat_type": chat_type,
        "trace_id": f"tr_{_counter['n']}",
        "sender": {"id": sender, "nick_name": sender},
        "payload": {
            "message_id": f"msg_{_counter['n']}",
            "message": {"fragments": fragments, "context": {"mentions": mentions}},
        },
    }


def text(value: str) -> dict:
    return {"kind": "text", "text": value}


def mention(user_id: str, display: str) -> dict:
    return {"kind": "mention", "user_id": user_id, "display": display}


def dm(sender: str, body: str) -> dict:
    return _frame(chat_id=DIRECT, chat_type="direct", sender=sender, fragments=[text(body)])


def group(sender: str, *fragments: dict) -> dict:
    return _frame(chat_id=GROUP, chat_type="group", sender=sender, fragments=list(fragments))


@pytest.fixture
def adapter(monkeypatch) -> tuple[ClawChatAdapter, list]:
    a = ClawChatAdapter({})
    a._clawchat_config = replace(
        a._clawchat_config, user_id=AGENT, owner_user_id=OWNER
    )
    a._store = FakeStore()
    dispatched: list = []

    async def fake_handle_inbound(inbound):
        dispatched.append(inbound)

    async def no_forwarded_approval(_inbound):
        return False

    def no_profile_sync(coro):
        coro.close()

    def sender_context(inbound):
        if inbound.chat_type != "group":
            return inbound
        return replace(
            inbound,
            sender_relation="owner" if inbound.sender_id == OWNER else "peer_user",
        )

    monkeypatch.setattr(a, "_handle_inbound", fake_handle_inbound)
    monkeypatch.setattr(a, "_handle_owner_forwarded_approval", no_forwarded_approval)
    monkeypatch.setattr(a, "_schedule_profile_sync", no_profile_sync)
    monkeypatch.setattr(a, "_resolve_inbound_sender_context", sender_context)
    a._group_settings_ready.set()
    return a, dispatched


def set_group(a: ClawChatAdapter, *, reply_mode: str, muted: bool = False, seq: int = 1) -> None:
    a._group_settings_cache.apply_fetched(
        [
            GroupSettings(
                conversation_id=GROUP,
                muted=muted,
                reply_mode=reply_mode,
                batch_delay_seconds=3600,
                version=seq,
            )
        ],
        seq,
    )


# --- Fix 1: owner-only admin commands in direct chats -----------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/restart", "/update", "  /restart now", "/RESTART", "/config", "/yolo", "/approve"])
async def test_non_owner_dm_admin_command_is_dropped(adapter, body):
    a, dispatched = adapter

    await a._on_message(dm(STRANGER, body))

    assert dispatched == [], f"{body!r} from a non-owner must not reach Hermes"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/restart", "/update"])
async def test_owner_dm_admin_command_still_dispatches(adapter, body):
    a, dispatched = adapter

    await a._on_message(dm(OWNER, body))

    assert [m.text for m in dispatched] == [body]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", ["/new", "/reset", "/compress", "/compact", "/help", "/whoami", "/new fresh start"]
)
async def test_non_owner_dm_own_session_commands_are_allowed(adapter, body):
    a, dispatched = adapter

    await a._on_message(dm(STRANGER, body))

    assert [m.text for m in dispatched] == [body]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["hello", "/etc/hosts is broken", "a /restart in the middle"])
async def test_non_owner_dm_plain_text_is_untouched(adapter, body):
    a, dispatched = adapter

    await a._on_message(dm(STRANGER, body))

    assert [m.text for m in dispatched] == [body]


@pytest.mark.asyncio
async def test_dm_admin_command_dropped_when_owner_is_unknown(adapter):
    a, dispatched = adapter
    a._clawchat_config = replace(a._clawchat_config, owner_user_id="")

    await a._on_message(dm(STRANGER, "/restart"))

    assert dispatched == []


# --- Fix 5: group commands run before the mention-only gate -----------------


@pytest.mark.asyncio
async def test_bare_new_from_owner_in_mention_only_group_dispatches(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, text("/new")))

    assert [m.text for m in dispatched] == ["/new"]


@pytest.mark.asyncio
async def test_mention_then_new_is_rewritten_to_the_bare_command(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, mention(AGENT, "Helper Bot"), text(" /new")))

    assert [m.text for m in dispatched] == ["/new"]


@pytest.mark.asyncio
async def test_mention_then_compress_in_mention_only_group(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, mention(AGENT, "Helper"), text(" /compress here 3")))

    assert [m.text for m in dispatched] == ["/compress here 3"]


@pytest.mark.asyncio
async def test_bare_compress_in_mention_only_group(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, text("/compress")))

    assert [m.text for m in dispatched] == ["/compress"]


@pytest.mark.asyncio
async def test_mention_all_then_new_counts_as_addressed(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, mention("all", "所有人"), text(" /new")))

    assert [m.text for m in dispatched] == ["/new"]


@pytest.mark.asyncio
async def test_mention_of_another_agent_then_new_is_not_our_command(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, mention("usr_other", "Other"), text(" /new")))

    assert dispatched == [], "a command addressed to another agent must not reset ours"


@pytest.mark.asyncio
async def test_non_owner_group_command_still_blocked_in_mention_only_group(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(STRANGER, mention(AGENT, "Helper"), text(" /new")))
    await a._on_message(group(STRANGER, text("/new")))

    assert dispatched == []


@pytest.mark.asyncio
async def test_muted_group_still_blocks_commands(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention", muted=True)

    await a._on_message(group(OWNER, text("/new")))
    await a._on_message(group(OWNER, mention(AGENT, "Helper"), text(" /new")))

    assert dispatched == []


@pytest.mark.asyncio
async def test_mention_only_group_still_drops_unmentioned_chat(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, text("just chatting")))
    await a._group_message_coalescer.flush_now(GROUP)

    assert dispatched == []


@pytest.mark.asyncio
async def test_mentioned_chat_is_not_mistaken_for_a_command(adapter):
    a, dispatched = adapter
    set_group(a, reply_mode="mention")

    await a._on_message(group(OWNER, mention(AGENT, "Helper"), text(" what does /new do?")))
    await a._group_message_coalescer.flush_now(GROUP)

    assert len(dispatched) == 1
    assert dispatched[0].text != "/new"
