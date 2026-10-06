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


# --- A non-owner answers the confirm for their own /new ---------------------
#
# Hermes 0.21 asks before /new (approvals.destructive_slash_confirm) and accepts
# text replies: /approve, /always, /cancel plus aliases. A non-owner must be able
# to finish /new in their own direct chat, but:
#
# * "always" rewrites the owner's global config.yaml
#   (destructive_slash_confirm: false), so a non-owner may never choose it;
# * Hermes' /approve also approves a pending dangerous-command execution, and
#   when one is blocking in the session Hermes routes /approve there instead of
#   to the confirm. So the plugin never hands a non-owner's approve to Hermes:
#   it renders the prompt itself (send_slash_confirm, the hook button adapters
#   use) and resolves exactly the (session_key, confirm_id) Hermes gave it, via
#   tools.slash_confirm — the slash-confirm registry, not tools.approval.

import sys
import types

HOST_PROMPT = (
    "⚠️ **Confirm /new**\n\n"
    "This starts a fresh session and discards the current conversation history.\n\n"
    "Choose:\n"
    "• **Approve Once** — proceed this time only\n"
    "• **Always Approve** — proceed and silence this prompt permanently\n"
    "• **Cancel** — keep current conversation\n\n"
    "_Text fallback: reply `/approve`, `/always`, or `/cancel`._"
)
OWNER_DIRECT = "cnv_owner_direct"


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
        return "🟡 /new cancelled. Conversation unchanged." if choice == "cancel" else "✨ Session reset!"


class FakeApproval:
    def __init__(self) -> None:
        self.blocking: set = set()
        self.resolved: list = []

    def has_blocking_approval(self, session_key):
        return session_key in self.blocking

    def resolve_gateway_approval(self, session_key, choice, resolve_all=False):
        self.resolved.append((session_key, choice))
        return 1


@pytest.fixture
def host(monkeypatch):
    slash_confirm = FakeSlashConfirm()
    approval = FakeApproval()
    tools_pkg = types.ModuleType("tools")
    tools_pkg.slash_confirm = slash_confirm
    tools_pkg.approval = approval
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.slash_confirm", slash_confirm)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)
    return slash_confirm, approval


@pytest.fixture
def sent(adapter, monkeypatch) -> list:
    a, _dispatched = adapter
    out: list = []

    async def fake_send(chat_id, content="", reply_to=None, metadata=None, **_kwargs):
        from gateway.platforms.base import SendResult

        out.append((chat_id, content))
        return SendResult(success=True, message_id=f"m{len(out)}")

    monkeypatch.setattr(a, "send", fake_send)
    monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: OWNER_DIRECT)
    return out


async def _friend_runs_new(a, slash_confirm, *, session_key="sk_friend", confirm_id="7"):
    await a._on_message(dm(STRANGER, "/new"))
    slash_confirm.pending[session_key] = {"confirm_id": confirm_id, "command": "new"}
    return await a.send_slash_confirm(
        chat_id=DIRECT,
        title="/new",
        message=HOST_PROMPT,
        session_key=session_key,
        confirm_id=confirm_id,
        metadata=None,
    )


@pytest.mark.asyncio
async def test_non_owner_confirm_prompt_is_rendered_by_the_plugin_without_always(adapter, host, sent):
    a, _ = adapter
    slash_confirm, _ = host

    result = await _friend_runs_new(a, slash_confirm)

    assert result.success is True, "success tells Hermes not to send its own text fallback"
    assert len(sent) == 1
    chat_id, prompt = sent[0]
    assert chat_id == DIRECT
    assert "/approve" in prompt and "/cancel" in prompt
    assert "always" not in prompt.lower()
    assert "discards the current conversation history" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply,choice",
    [
        ("/approve", "once"),
        ("/yes", "once"),
        ("/ok", "once"),
        ("/confirm", "once"),
        ("approve", "once"),
        ("Approve once", "once"),
        ("once", "once"),
        ("!approve", "once"),
        ("/cancel", "cancel"),
        ("/no", "cancel"),
        ("/deny", "cancel"),
        ("/nevermind", "cancel"),
        ("cancel", "cancel"),
        ("no", "cancel"),
        ("nevermind", "cancel"),
    ],
)
async def test_non_owner_answers_own_new_confirm(adapter, host, sent, reply, choice):
    a, dispatched = adapter
    slash_confirm, approval = host
    await _friend_runs_new(a, slash_confirm)
    dispatched.clear()

    await a._on_message(dm(STRANGER, reply))

    assert slash_confirm.resolved == [("sk_friend", "7", choice)]
    assert dispatched == [], "the reply is consumed by the plugin, never handed to Hermes"
    assert approval.resolved == []
    assert sent[-1][0] == DIRECT
    assert sent[-1][1] in {"✨ Session reset!", "🟡 /new cancelled. Conversation unchanged."}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply", ["/always", "always", "!always", "/remember", "always approve", "/ALWAYS"]
)
async def test_non_owner_cannot_choose_always(adapter, host, sent, reply):
    a, dispatched = adapter
    slash_confirm, _ = host
    await _friend_runs_new(a, slash_confirm)
    dispatched.clear()

    await a._on_message(dm(STRANGER, reply))

    assert slash_confirm.resolved == []
    assert dispatched == [], "'always' would rewrite the owner's config.yaml"
    assert "/approve" in sent[-1][1] and "/cancel" in sent[-1][1]
    # The confirm is still open: the friend can still approve it once.
    await a._on_message(dm(STRANGER, "/approve"))
    assert slash_confirm.resolved == [("sk_friend", "7", "once")]


@pytest.mark.asyncio
async def test_non_owner_approve_never_reaches_a_dangerous_command_approval(adapter, host, sent):
    a, dispatched = adapter
    slash_confirm, approval = host
    await _friend_runs_new(a, slash_confirm)
    # Hermes would route /approve to this blocking approval instead of the confirm.
    approval.blocking.add("sk_friend")
    dispatched.clear()

    await a._on_message(dm(STRANGER, "/approve"))

    assert approval.resolved == []
    assert dispatched == []
    assert slash_confirm.resolved == [("sk_friend", "7", "once")]


@pytest.mark.asyncio
async def test_non_owner_approve_without_a_plugin_confirm_is_still_dropped(adapter, host, sent):
    a, dispatched = adapter
    slash_confirm, approval = host

    await a._on_message(dm(STRANGER, "/approve"))

    assert dispatched == []
    assert slash_confirm.resolved == [] and approval.resolved == []


@pytest.mark.asyncio
async def test_expired_confirm_is_not_resolved(adapter, host, sent):
    a, dispatched = adapter
    slash_confirm, _ = host
    await _friend_runs_new(a, slash_confirm)
    slash_confirm.pending.clear()  # timed out / superseded on the host side
    dispatched.clear()

    await a._on_message(dm(STRANGER, "/approve"))

    assert slash_confirm.resolved == []
    assert dispatched == []
    assert "/new" in sent[-1][1]


@pytest.mark.asyncio
async def test_confirm_is_answered_only_once(adapter, host, sent):
    a, dispatched = adapter
    slash_confirm, _ = host
    await _friend_runs_new(a, slash_confirm)
    await a._on_message(dm(STRANGER, "/approve"))
    dispatched.clear()

    await a._on_message(dm(STRANGER, "no"))

    assert slash_confirm.resolved == [("sk_friend", "7", "once")]
    assert [m.text for m in dispatched] == ["no"], "after the confirm, 'no' is just chat"


@pytest.mark.asyncio
async def test_owner_confirm_keeps_the_host_text_fallback(adapter, host, sent):
    a, dispatched = adapter
    await a._on_message(_frame(chat_id=OWNER_DIRECT, chat_type="direct", sender=OWNER, fragments=[text("/new")]))

    result = await a.send_slash_confirm(
        chat_id=OWNER_DIRECT, title="/new", message=HOST_PROMPT,
        session_key="sk_owner", confirm_id="1", metadata=None,
    )

    assert result.success is False, "Hermes then sends its own prompt and handles /always for the owner"
    assert sent == []


@pytest.mark.asyncio
async def test_group_confirm_keeps_the_host_text_fallback(adapter, host, sent):
    a, _ = adapter

    result = await a.send_slash_confirm(
        chat_id=GROUP, title="/new", message=HOST_PROMPT,
        session_key="sk_group", confirm_id="2", metadata={"chat_type": "group"},
    )

    assert result.success is False
    assert sent == []
