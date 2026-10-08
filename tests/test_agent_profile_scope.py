"""Agent Profile / Owner Metadata sections describe THIS profile's agent.

A profile's ``owner.md`` can describe another agent: ``hermes profile create
--clone-all`` copies the source profile's memories, so the new profile starts
with the source agent's ``agent_*`` and ``agent_owner_*`` fields until a pull
rewrites them. The adapter belongs to one profile and knows its own identity
(token ``sub``) and owner, so fields about someone else are dropped instead of
being presented as "who you are".
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from clawchat_gateway import memory_scope
from clawchat_gateway.adapter import ClawChatAdapter


def _adapter(monkeypatch, *, user_id: str, owner_id: str, memory):
    a = ClawChatAdapter({})
    a._clawchat_config = dataclasses.replace(a._clawchat_config, user_id=user_id)
    monkeypatch.setattr(a, "_owner_user_id", lambda: owner_id)
    monkeypatch.setattr(a, "_read_memory_metadata", lambda t, i: dict(memory.get((t, i), {})))
    return a


CLONED = {
    ("owner", "owner"): {
        "agent_user_id": "usr_a",
        "agent_nickname": "Alpha",
        "agent_avatar_url": "https://example.com/alpha.png",
        "agent_bio": "Alpha's bio",
        "agent_behavior": "Alpha's behavior",
        "agent_owner_id": "usr_owner_a",
        "agent_owner_nickname": "OwnerA",
        "agent_owner_locale": "ja",
    },
    ("user", "usr_b"): {"nickname": "Beta", "avatar_url": "https://example.com/beta.png", "bio": "Beta's bio"},
}


def test_agent_profile_ignores_owner_md_about_another_agent(monkeypatch):
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory=CLONED)
    section = b._format_agent_profile_section(CLONED[("owner", "owner")]) or ""
    assert "agent_user_id: usr_b" in section
    assert "agent_nickname: Beta" in section
    assert "beta.png" in section and "Beta's bio" in section
    assert "Alpha" not in section and "alpha.png" not in section and "usr_a" not in section


def test_agent_profile_keeps_owner_md_about_itself(monkeypatch):
    own = {"agent_user_id": "usr_b", "agent_nickname": "Beta", "agent_bio": "mine"}
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory={("owner", "owner"): own})
    section = b._format_agent_profile_section(own) or ""
    assert "agent_user_id: usr_b" in section
    assert "agent_nickname: Beta" in section
    assert "agent_bio: mine" in section


def test_owner_metadata_ignores_another_owner(monkeypatch):
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory=CLONED)
    text = "\n".join(b._format_owner_metadata_sections(CLONED[("owner", "owner")]))
    assert "agent_owner_id: usr_owner_b" in text
    assert "OwnerA" not in text and "usr_owner_a" not in text
    assert "agent_owner_locale: ja" not in text


def test_agent_behavior_of_another_agent_is_not_ours(monkeypatch):
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory=CLONED)
    text = "\n".join(b._format_owner_metadata_sections(CLONED[("owner", "owner")]))
    assert "Alpha's behavior" not in text


def test_owner_metadata_keeps_the_same_owner(monkeypatch):
    md = {"agent_user_id": "usr_b", "agent_owner_id": "usr_owner_b", "agent_owner_nickname": "OwnerB"}
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory={("owner", "owner"): md})
    text = "\n".join(b._format_owner_metadata_sections(md))
    assert "agent_owner_id: usr_owner_b" in text
    assert "agent_owner_nickname: OwnerB" in text


def test_memory_scope_owner_follows_the_served_profile_home(monkeypatch, tmp_path):
    homes = {}
    for name in ("a", "b"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            f"platforms:\n  clawchat:\n    extra:\n      owner_user_id: usr_owner_{name}\n",
            encoding="utf-8",
        )
        homes[name] = home
    for var in ("CLAWCHAT_OWNER_USER_ID", "CLAWCHAT_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    import clawchat_gateway.hermes_home as hh

    current = {"home": homes["a"]}
    monkeypatch.setattr(hh, "hermes_home", lambda: Path(current["home"]))
    assert memory_scope._owner_user_id() == "usr_owner_a"
    current["home"] = homes["b"]
    assert memory_scope._owner_user_id() == "usr_owner_b"


def test_an_agt_record_id_in_owner_md_is_not_a_mismatch(monkeypatch):
    md = {"agent_id": "agt_01XYZ", "agent_nickname": "Beta"}
    b = _adapter(monkeypatch, user_id="usr_b", owner_id="usr_owner_b", memory={("owner", "owner"): md})
    section = b._format_agent_profile_section(md) or ""
    assert "agent_nickname: Beta" in section
