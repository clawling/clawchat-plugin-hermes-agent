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


# --- A non-owner's message never controls the gateway -----------------------
#
# Hermes reads some plain text as gateway control: "always" / "approve" /
# "cancel" answer a pending slash-confirm, "yes" / "always" approve a blocking
# dangerous-command approval while the agent is busy, and "restart gateway" in a
# direct chat is rewritten to /restart. The allow-list above only sees slash
# commands. On hosts whose MessageEvent has ``allow_gateway_control`` (Hermes
# 0.20.1+), a non-owner's event carries False unless it is a command the plugin
# already allowed, and Hermes keeps it conversational. On older hosts the plugin
# drops the dangerous plain-text forms itself.

import types as _types

import clawchat_gateway.adapter as adapter_mod


@pytest.fixture
def events(adapter, monkeypatch) -> list:
    """Run the real _handle_inbound and capture the MessageEvent Hermes gets."""
    a, _ = adapter
    captured: list = []

    async def handle_message(event):
        captured.append(event)

    async def no_consent(_inbound):
        return False

    async def no_media(_inbound):
        return []

    async def no_metadata(_chat_id):
        return None

    monkeypatch.setattr(a, "_handle_inbound", ClawChatAdapter._handle_inbound.__get__(a))
    monkeypatch.setattr(a, "handle_message", handle_message, raising=False)
    monkeypatch.setattr(a, "build_source", lambda **kw: _types.SimpleNamespace(**kw), raising=False)
    monkeypatch.setattr(a, "_maybe_consume_skill_update_consent", no_consent)
    monkeypatch.setattr(a, "_download_inbound_media", no_media)
    monkeypatch.setattr(a, "_ensure_group_participants_metadata", no_metadata)
    return captured


@pytest.fixture
def new_host(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_host_supports_gateway_control", lambda: True)


@pytest.fixture
def old_host(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_host_supports_gateway_control", lambda: False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    ["hello", "always", "!always", "Always approve", "approve", "yes", "restart gateway", "/etc/hosts"],
)
async def test_non_owner_dm_text_cannot_control_the_gateway(adapter, events, new_host, body):
    a, _ = adapter

    await a._on_message(dm(STRANGER, body))

    assert [e.text for e in events] == [body], "still delivered to the agent as chat"
    assert events[0].allow_gateway_control is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/new", "/compress", "/help"])
async def test_non_owner_dm_allowed_command_keeps_gateway_control(adapter, events, new_host, body):
    a, _ = adapter

    await a._on_message(dm(STRANGER, body))

    assert events[0].allow_gateway_control is True, "Hermes only runs a command it may treat as one"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["always", "/restart", "hello"])
async def test_owner_dm_keeps_gateway_control(adapter, events, new_host, body):
    a, _ = adapter

    await a._on_message(dm(OWNER, body))

    assert events[0].allow_gateway_control is True


@pytest.mark.asyncio
async def test_old_host_gets_no_unknown_field(adapter, events, old_host):
    a, _ = adapter

    await a._on_message(dm(STRANGER, "hello"))

    assert not hasattr(events[0], "allow_gateway_control"), "older MessageEvent rejects the kwarg"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        "always", " Always ", "!always", "/always", "always approve", "/remember",
        "restart gateway", "please restart the gateway!", "Restart Hermes",
    ],
)
async def test_old_host_drops_non_owner_control_text(adapter, events, old_host, body):
    a, _ = adapter

    await a._on_message(dm(STRANGER, body))

    assert events == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["always", "restart gateway"])
async def test_old_host_owner_text_is_untouched(adapter, events, old_host, body):
    a, _ = adapter

    await a._on_message(dm(OWNER, body))

    assert [e.text for e in events] == [body]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["yes", "OK", "y", "approve", "👍", "session", "approve always"])
async def test_old_host_non_owner_cannot_approve_a_blocking_command(
    adapter, events, old_host, host, monkeypatch, body
):
    a, _ = adapter
    _, approval = host

    async def sent_frame(_frame, **_kwargs):
        return True

    monkeypatch.setattr(a._connection, "send_frame", sent_frame)
    await a._on_message(dm(STRANGER, "run the cleanup"))
    events.clear()
    # The agent hit a dangerous command in the friend's session; Hermes asks there.
    await a.send_exec_approval(
        chat_id=DIRECT, command="run-cleanup --everything", session_key="sk_friend",
        metadata={"chat_type": "direct"},
    )
    approval.blocking.add("sk_friend")

    await a._on_message(dm(STRANGER, body))
    assert events == [], "Hermes would route this to /approve while the approval blocks"

    approval.blocking.clear()
    await a._on_message(dm(STRANGER, body))
    assert [e.text for e in events] == [body], "with nothing blocking it is ordinary chat"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["/always", "/remember", "/ALWAYS"])
async def test_non_owner_group_always_dropped_even_when_commands_are_open(adapter, events, body):
    a, _ = adapter
    a._clawchat_config = replace(a._clawchat_config, group_command_mode="all")
    set_group(a, reply_mode="all")

    await a._on_message(group(STRANGER, text(body)))
    await a._on_message(group(STRANGER, mention(AGENT, "Helper"), text(" " + body)))

    assert events == []


@pytest.mark.asyncio
async def test_owner_group_always_still_dispatches(adapter, events):
    a, _ = adapter
    set_group(a, reply_mode="all")

    await a._on_message(group(OWNER, text("/always")))

    assert [e.text for e in events] == ["/always"]


@pytest.mark.asyncio
async def test_group_gateway_control_follows_the_senders(adapter, events, new_host):
    a, _ = adapter
    a._clawchat_config = replace(a._clawchat_config, group_command_mode="all")
    set_group(a, reply_mode="all")

    await a._on_message(group(STRANGER, text("/new")))  # allowed by group_command_mode
    await a._on_message(group(OWNER, text("/new")))
    assert [e.allow_gateway_control for e in events] == [True, True]
    events.clear()

    await a._on_message(group(STRANGER, mention(AGENT, "Helper"), text(" always")))
    await a._on_message(group(OWNER, mention(AGENT, "Helper"), text(" hi")))
    assert [e.allow_gateway_control for e in events] == [False, True]


# --- A dangerous-command approval in a non-owner's direct chat --------------
#
# When the agent hits a dangerous command inside a friend's session, Hermes
# queues the approval in tools.approval and then calls send_exec_approval for
# that friend's chat. The friend cannot approve (their "yes"/"/approve" never
# reaches tools.approval), so waiting only blocks their session for the whole
# approval timeout, after which a queued "yes" reads as consent to the agent.
# The plugin therefore denies that approval right away through the host's own
# resolve path (the one /deny uses), with a policy reason the agent sees, and
# tells the friend it was declined: no command text, no /approve choices.
# Hosts without that path keep the old behaviour: the note, then the timeout.


def _frame_text(frame: dict) -> str:
    fragments = frame["payload"]["message"]["body"]["fragments"]
    return "".join(f.get("text", "") for f in fragments if isinstance(f, dict))


class FakeApprovalQueue:
    """tools.approval as of Hermes v0.18+: resolve_gateway_approval takes reason."""

    def __init__(self) -> None:
        self.pending: dict = {}
        self.resolved: list = []

    def has_blocking_approval(self, session_key):
        return bool(self.pending.get(session_key))

    def resolve_gateway_approval(
        self, session_key, choice, resolve_all=False, reason=None, request_id=None
    ):
        queue = self.pending.get(session_key) or []
        if not queue:
            return 0
        count = len(queue) if resolve_all else 1
        del queue[:count]
        self.resolved.append((session_key, choice, resolve_all, reason))
        return count


class OldApprovalQueue(FakeApprovalQueue):
    """tools.approval as of Hermes v0.12: no reason parameter."""

    def resolve_gateway_approval(self, session_key, choice, resolve_all=False):
        return super().resolve_gateway_approval(session_key, choice, resolve_all)


def _install_approval(monkeypatch, approval) -> None:
    tools_pkg = types.ModuleType("tools")
    tools_pkg.approval = approval
    monkeypatch.setitem(sys.modules, "tools", tools_pkg)
    monkeypatch.setitem(sys.modules, "tools.approval", approval)


@pytest.fixture
def approval_queue(monkeypatch) -> FakeApprovalQueue:
    approval = FakeApprovalQueue()
    _install_approval(monkeypatch, approval)
    return approval


@pytest.fixture
def frames(adapter, monkeypatch) -> list:
    a, _ = adapter
    out: list = []

    async def capture(frame, **_kwargs):
        out.append(frame)
        return True

    monkeypatch.setattr(a._connection, "send_frame", capture)
    monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: OWNER_DIRECT)
    return out


def _assert_no_prompt(body: str) -> None:
    for leaked in ("/approve", "/always", "/deny", "run-cleanup"):
        assert leaked not in body, f"{leaked!r} must not be shown to a non-owner"


@pytest.mark.asyncio
async def test_non_owner_dm_exec_approval_is_denied_immediately(adapter, frames, approval_queue):
    a, _ = adapter
    await a._on_message(dm(STRANGER, "run the cleanup"))
    approval_queue.pending["sk_friend"] = [{"command": "run-cleanup --everything"}]

    result = await a.send_exec_approval(
        chat_id=DIRECT, command="run-cleanup --everything", session_key="sk_friend",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True, "a failure would make Hermes resend its full text prompt"
    assert len(approval_queue.resolved) == 1
    session_key, choice, resolve_all, reason = approval_queue.resolved[0]
    assert (session_key, choice, resolve_all) == ("sk_friend", "deny", False)
    assert reason and "owner" in reason.lower() and "not retry" in reason.lower()
    assert not approval_queue.has_blocking_approval("sk_friend")
    assert [f["chat_id"] for f in frames] == [DIRECT]
    body = _frame_text(frames[0])
    assert "owner" in body.lower() and "declined" in body.lower()
    assert "minutes" not in body, "nothing is left waiting for a timeout"
    _assert_no_prompt(body)


@pytest.mark.asyncio
async def test_non_owner_dm_exec_approval_denied_on_a_host_without_reason(
    adapter, frames, monkeypatch
):
    approval = OldApprovalQueue()
    _install_approval(monkeypatch, approval)
    a, _ = adapter
    await a._on_message(dm(STRANGER, "run the cleanup"))
    approval.pending["sk_friend"] = [{"command": "run-cleanup --everything"}]

    result = await a.send_exec_approval(
        chat_id=DIRECT, command="run-cleanup --everything", session_key="sk_friend",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert approval.resolved == [("sk_friend", "deny", False, None)]
    body = _frame_text(frames[0])
    assert "declined" in body.lower()
    _assert_no_prompt(body)


@pytest.mark.asyncio
async def test_non_owner_dm_exec_approval_falls_back_without_the_host_api(
    adapter, frames, monkeypatch
):
    # A host whose tools.approval lacks the resolve path (or whose approval is
    # not queued there): keep the note and leave the host's timeout in charge.
    _install_approval(monkeypatch, types.ModuleType("tools.approval"))
    a, _ = adapter
    await a._on_message(dm(STRANGER, "run the cleanup"))

    result = await a.send_exec_approval(
        chat_id=DIRECT, command="run-cleanup --everything", session_key="sk_friend",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert [f["chat_id"] for f in frames] == [DIRECT]
    body = _frame_text(frames[0])
    assert "owner" in body.lower() and "minutes" in body
    assert "declined" not in body.lower()
    _assert_no_prompt(body)
    assert a._direct_exec_approval_sessions[DIRECT] == "sk_friend"


@pytest.mark.asyncio
async def test_non_owner_dm_exec_approval_falls_back_when_nothing_is_queued(
    adapter, frames, approval_queue
):
    a, _ = adapter
    await a._on_message(dm(STRANGER, "run the cleanup"))

    await a.send_exec_approval(
        chat_id=DIRECT, command="run-cleanup --everything", session_key="sk_friend",
        metadata={"chat_type": "direct"},
    )

    assert approval_queue.resolved == []
    body = _frame_text(frames[0])
    assert "minutes" in body and "declined" not in body.lower()
    _assert_no_prompt(body)


@pytest.mark.asyncio
async def test_owner_dm_exec_approval_keeps_the_full_prompt(adapter, frames, approval_queue):
    a, _ = adapter
    await a._on_message(
        _frame(chat_id=OWNER_DIRECT, chat_type="direct", sender=OWNER, fragments=[text("go")])
    )
    approval_queue.pending["sk_owner"] = [{"command": "run-cleanup --everything"}]

    result = await a.send_exec_approval(
        chat_id=OWNER_DIRECT, command="run-cleanup --everything", session_key="sk_owner",
        metadata={"chat_type": "direct"},
    )

    assert result.success is True
    assert approval_queue.resolved == []
    assert approval_queue.has_blocking_approval("sk_owner")
    body = _frame_text(frames[0])
    assert "/approve" in body and "run-cleanup" in body


@pytest.mark.asyncio
async def test_group_exec_approval_still_goes_to_the_owner(adapter, frames, approval_queue):
    a, _ = adapter
    set_group(a, reply_mode="all")
    await a._on_message(group(STRANGER, mention(AGENT, "Helper"), text(" run the cleanup")))
    approval_queue.pending["sk_group"] = [{"command": "run-cleanup --everything"}]

    result = await a.send_exec_approval(
        chat_id=GROUP, command="run-cleanup --everything", session_key="sk_group",
        metadata={"chat_type": "group"},
    )

    assert result.success is True
    assert approval_queue.resolved == []
    assert approval_queue.has_blocking_approval("sk_group")
    assert [f["chat_id"] for f in frames] == [OWNER_DIRECT]
    body = _frame_text(frames[0])
    assert "run-cleanup" in body and "/approve" in body
