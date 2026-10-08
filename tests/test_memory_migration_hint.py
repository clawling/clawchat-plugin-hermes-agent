"""After the upgrade, the owner is asked once about person facts in Hermes' global memory.

Before 0.14.0-101 Hermes wrote what it learnt about the owner and about other
people into its global ``MEMORY.md`` / ``USER.md``, which every conversation can
see — friends' direct chats and groups included. The plugin used to only log a
line about ``USER.md`` at load, which the owner never sees.

Now, once per profile, the plugin counts the entries that look like they are
about the owner or a specific person and gives the agent one maintenance turn
in the owner's direct chat: tell the owner, suggest moving them into
``owner.md`` / ``users/<id>.md``, and move nothing until the owner agrees. The
plugin itself never edits, moves or deletes those files.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from clawchat_gateway import memory_migration
from clawchat_gateway.adapter import ClawChatAdapter

OWNER_CHAT = "cnv_owner"
SEP = "\n§\n"


def _write_memories(home, *, memory=None, user=None):
    d = home / "memories"
    d.mkdir(parents=True, exist_ok=True)
    if memory is not None:
        (d / "MEMORY.md").write_text(SEP.join(memory), encoding="utf-8")
    if user is not None:
        (d / "USER.md").write_text(SEP.join(user), encoding="utf-8")


def test_scan_counts_person_entries(tmp_path):
    _write_memories(
        tmp_path,
        memory=[
            "Owner prefers short answers and green tea.",
            "The deploy script lives in scripts/deploy.sh.",
            "Ada (usr_01ada) has her birthday on May 3.",
            "主人喜欢早上开会。",
        ],
        user=["Name: Joe", "Works as an engineer in Shanghai."],
    )
    scan = memory_migration.scan_global_memory(tmp_path, names={"Ada"})
    assert scan is not None
    assert (scan.memory_person_entries, scan.memory_entries) == (3, 4)
    assert scan.user_entries == 2


def test_scan_without_person_entries_is_none(tmp_path):
    _write_memories(tmp_path, memory=["The deploy script lives in scripts/deploy.sh."])
    assert memory_migration.scan_global_memory(tmp_path, names=set()) is None


def test_scan_without_files_is_none(tmp_path):
    assert memory_migration.scan_global_memory(tmp_path, names=set()) is None


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a.dispatched = []

    async def handle_inbound(inbound):
        a.dispatched.append(inbound)

    monkeypatch.setattr(a, "_handle_inbound", handle_inbound)
    monkeypatch.setattr(a, "_owner_direct_chat_id", lambda: OWNER_CHAT)
    return a


def test_owner_is_asked_once_and_nothing_is_moved(adapter, tmp_path):
    memory = ["Owner prefers short answers.", "Ada's birthday is May 3."]
    _write_memories(tmp_path, memory=memory, user=["Name: Joe"])
    before = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "memories").iterdir()}

    asyncio.run(adapter._maybe_hint_memory_migration())

    assert len(adapter.dispatched) == 1
    turn = adapter.dispatched[0]
    assert turn.chat_id == OWNER_CHAT and turn.chat_type == "direct"
    assert "2 of 2" in turn.text and "USER.md" in turn.text
    assert "owner.md" in turn.text and "users/<usr_id>.md" in turn.text
    assert "until your owner agrees" in turn.text
    after = {p.name: p.read_text(encoding="utf-8") for p in (tmp_path / "memories").iterdir()}
    assert after == before

    asyncio.run(adapter._maybe_hint_memory_migration())
    assert len(adapter.dispatched) == 1  # once per profile


def test_nothing_to_move_sends_nothing_and_does_not_ask_later(adapter, tmp_path):
    _write_memories(tmp_path, memory=["The deploy script lives in scripts/deploy.sh."])
    asyncio.run(adapter._maybe_hint_memory_migration())
    assert adapter.dispatched == []
    _write_memories(tmp_path, memory=["Owner prefers tea."])
    asyncio.run(adapter._maybe_hint_memory_migration())
    assert adapter.dispatched == []


def test_without_an_owner_chat_it_waits_for_a_later_connection(adapter, tmp_path, monkeypatch):
    _write_memories(tmp_path, memory=["Owner prefers tea."])
    monkeypatch.setattr(adapter, "_owner_direct_chat_id", lambda: "")
    asyncio.run(adapter._maybe_hint_memory_migration())
    assert adapter.dispatched == []
    monkeypatch.setattr(adapter, "_owner_direct_chat_id", lambda: OWNER_CHAT)
    asyncio.run(adapter._maybe_hint_memory_migration())
    assert len(adapter.dispatched) == 1
