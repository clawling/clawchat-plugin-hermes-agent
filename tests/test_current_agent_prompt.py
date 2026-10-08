"""The channel prompt names the agent the turn belongs to.

Without it a model in a group with a second agent cannot tell which
participant it is: on hosts with strong stay-silent group wording it answered
an owner @ with no reply. Mirrors the OpenClaw plugin's
``ClawChat Current Agent`` section and the ``current_agent`` participant label.
"""

from __future__ import annotations

import dataclasses

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.inbound import InboundMessage


def _adapter(monkeypatch, *, user_id: str, memory: dict[tuple[str, str], dict[str, str]]):
    a = ClawChatAdapter({})
    a._clawchat_config = dataclasses.replace(a._clawchat_config, user_id=user_id)
    monkeypatch.setattr(a, "_owner_user_id", lambda: "usr_owner")
    monkeypatch.setattr(
        a, "_read_memory_metadata", lambda t, i: dict(memory.get((t, i), {}))
    )
    return a


def _dm(sender: str = "usr_owner") -> InboundMessage:
    return InboundMessage(
        chat_id="cnv_dm", chat_type="direct", sender_id=sender, sender_name="Owner",
        text="hi", raw_message={},
    )


def _group() -> InboundMessage:
    return InboundMessage(
        chat_id="cnv_g", chat_type="group", sender_id="usr_owner", sender_name="Owner",
        text="@Bot hi", raw_message={},
    )


def _section(prompt: str, title: str) -> str:
    start = prompt.index(f"## {title}\n")
    end = prompt.find("\n## ", start + 1)
    return prompt[start:] if end == -1 else prompt[start:end]


GROUP_MEMORY = {
    ("group", "cnv_g"): {
        "participant_ids": "usr_owner,usr_self,usr_peer",
        "group_owner_id": "usr_owner",
    },
    ("user", "usr_peer"): {"nickname": "PeerBot", "profile_type": "agent"},
}


def test_direct_prompt_names_the_current_agent(monkeypatch):
    memory = {("owner", "owner"): {"agent_user_id": "usr_self", "agent_nickname": "Bot"}}
    prompt = _adapter(monkeypatch, user_id="usr_self", memory=memory)._compose_channel_prompt(_dm())
    section = _section(prompt or "", "ClawChat Current Agent")
    assert "current_agent_id: usr_self" in section
    assert "current_agent_nickname: Bot" in section
    assert "You are usr_self (Bot)." in section
    assert "mentions_current_agent=true" in section


def test_stranger_direct_prompt_names_the_current_agent_too(monkeypatch):
    memory = {("owner", "owner"): {"agent_user_id": "usr_self", "agent_nickname": "Bot"}}
    prompt = _adapter(monkeypatch, user_id="usr_self", memory=memory)._compose_channel_prompt(
        _dm("usr_stranger")
    )
    assert "## ClawChat Current Agent" in (prompt or "")


def test_group_prompt_labels_the_current_agent_participant(monkeypatch):
    memory = {
        **GROUP_MEMORY,
        ("owner", "owner"): {"agent_user_id": "usr_self", "agent_nickname": "Bot"},
    }
    prompt = _adapter(monkeypatch, user_id="usr_self", memory=memory)._compose_channel_prompt(_group())
    assert "You are usr_self (Bot)." in _section(prompt or "", "ClawChat Current Agent")
    participants = _section(prompt or "", "ClawChat Group Participants")
    assert "usr_self: Bot (agent, current_agent)" in participants
    assert "usr_peer: PeerBot (agent)" in participants
    assert "current_agent" not in participants.split("usr_peer", 1)[1]


def test_no_nickname_names_the_id_only(monkeypatch):
    prompt = _adapter(monkeypatch, user_id="usr_self", memory={})._compose_channel_prompt(_dm())
    section = _section(prompt or "", "ClawChat Current Agent")
    assert "current_agent_id: usr_self" in section
    assert "current_agent_nickname" not in section
    assert "You are usr_self." in section


def test_second_profile_never_borrows_the_default_profiles_identity(monkeypatch):
    # A profile's owner.md can still be another profile's (--clone-all copies
    # memories). The turn's own identity is this
    # adapter's connection (token sub), and another agent's nickname must not
    # be shown as ours.
    default_owner = {("owner", "owner"): {"agent_user_id": "usr_a", "agent_nickname": "Alpha"}}
    memory = {
        **default_owner,
        **GROUP_MEMORY,
        ("group", "cnv_g"): {"participant_ids": "usr_owner,usr_a,usr_b", "group_owner_id": "usr_owner"},
        ("user", "usr_a"): {"nickname": "Alpha", "profile_type": "agent"},
        ("user", "usr_b"): {"nickname": "Beta", "profile_type": "agent"},
    }
    a = _adapter(monkeypatch, user_id="usr_a", memory=memory)
    b = _adapter(monkeypatch, user_id="usr_b", memory=memory)

    prompt_a = a._compose_channel_prompt(_group()) or ""
    prompt_b = b._compose_channel_prompt(_group()) or ""

    assert "You are usr_a (Alpha)." in _section(prompt_a, "ClawChat Current Agent")
    assert "usr_a: Alpha (agent, current_agent)" in prompt_a
    assert "usr_b: Beta (agent)\n" in prompt_a + "\n"

    section_b = _section(prompt_b, "ClawChat Current Agent")
    assert "current_agent_id: usr_b" in section_b
    assert "You are usr_b (Beta)." in section_b
    assert "Alpha" not in section_b
    assert "usr_b: Beta (agent, current_agent)" in prompt_b
    assert "usr_a: Alpha (agent)" in prompt_b
