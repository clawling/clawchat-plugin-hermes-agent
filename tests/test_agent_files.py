"""The agent's own files live in its profile: ``$HERMES_HOME/clawchat/``.

``greeting.md``, ``friend-greeting.md`` and ``onboarding.json`` used to be read
from ``~/clawchat/``, under the OS home, which every Hermes profile on the host
shares. Two profiles are two ClawChat agents, so one agent's greeting and
onboarding facts (capability tier, wiki report id) became every agent's.

Now each is read from the profile home first. The old location is still read
for the DEFAULT profile only, so a file written there before this change keeps
working; a named profile never reads it, since there it is another agent's.
The agent is given the absolute directory in its owner's direct chat, because
``~`` in the agent's own shell is not the gateway's home (Hermes points a
container's subprocess ``HOME`` at ``$HERMES_HOME/home``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clawchat_gateway import agent_files
from clawchat_gateway.greeting import (
    ACTIVATION_BOOTSTRAP_PROMPT,
    FRIEND_GREETING_PROMPT,
    load_activation_bootstrap_prompt,
    load_friend_greeting_prompt,
)
from clawchat_gateway.onboarding_report import read_onboarding_report


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fake OS home holding the default profile and one named profile."""
    os_home = tmp_path / "osuser"
    default_home = os_home / ".hermes"
    named_home = default_home / "profiles" / "coder"
    for d in (os_home, default_home, named_home):
        d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", lambda: os_home)
    return os_home, default_home, named_home


def _use(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))


def _write(base: Path, name: str, text: str) -> None:
    (base / "clawchat").mkdir(parents=True, exist_ok=True)
    (base / "clawchat" / name).write_text(text, encoding="utf-8")


def test_files_dir_is_under_the_profile_home(homes, monkeypatch):
    _, default_home, named_home = homes
    _use(monkeypatch, named_home)
    assert agent_files.agent_files_dir() == named_home / "clawchat"
    _use(monkeypatch, default_home)
    assert agent_files.agent_files_dir() == default_home / "clawchat"
    assert agent_files.agent_files_dir().is_absolute()


def test_profile_file_wins(homes, monkeypatch):
    os_home, default_home, _ = homes
    _use(monkeypatch, default_home)
    _write(os_home, "greeting.md", "legacy")
    _write(default_home, "greeting.md", "profile")
    assert load_activation_bootstrap_prompt() == "profile"


def test_default_profile_falls_back_to_the_legacy_home_file(homes, monkeypatch):
    os_home, default_home, _ = homes
    _use(monkeypatch, default_home)
    _write(os_home, "greeting.md", "legacy greeting")
    _write(os_home, "friend-greeting.md", "legacy friend")
    _write(os_home, "onboarding.json", json.dumps({"capability_tier": 3}))
    assert load_activation_bootstrap_prompt() == "legacy greeting"
    assert load_friend_greeting_prompt() == "legacy friend"
    assert read_onboarding_report() == {"capability_tier": 3}


def test_named_profile_never_reads_the_legacy_home_file(homes, monkeypatch):
    os_home, _, named_home = homes
    _use(monkeypatch, named_home)
    _write(os_home, "greeting.md", "someone else's greeting")
    _write(os_home, "friend-greeting.md", "someone else's friend greeting")
    _write(os_home, "onboarding.json", json.dumps({"wiki_report_id": "rep-other"}))
    assert load_activation_bootstrap_prompt() == ACTIVATION_BOOTSTRAP_PROMPT
    assert load_friend_greeting_prompt() == FRIEND_GREETING_PROMPT
    assert read_onboarding_report() is None


def test_named_profile_reads_its_own_files(homes, monkeypatch):
    _, _, named_home = homes
    _use(monkeypatch, named_home)
    _write(named_home, "friend-greeting.md", "mine")
    _write(named_home, "onboarding.json", json.dumps({"wiki_report_id": "rep-mine"}))
    assert load_friend_greeting_prompt() == "mine"
    assert read_onboarding_report() == {"wiki_report_id": "rep-mine"}


def test_an_empty_profile_file_resets_without_falling_back(homes, monkeypatch):
    # "Empty the file" is the documented reset, and it must win over a legacy
    # file the agent may not even know exists.
    os_home, default_home, _ = homes
    _use(monkeypatch, default_home)
    _write(os_home, "greeting.md", "legacy")
    _write(default_home, "greeting.md", "   \n")
    assert load_activation_bootstrap_prompt() == ACTIVATION_BOOTSTRAP_PROMPT


def test_owner_direct_chat_prompt_names_the_absolute_files_dir(homes, monkeypatch):
    from clawchat_gateway.adapter import ClawChatAdapter
    from clawchat_gateway.inbound import InboundMessage

    _, _, named_home = homes
    _use(monkeypatch, named_home)
    a = ClawChatAdapter({})
    monkeypatch.setattr(a, "_owner_user_id", lambda: "usr_owner")
    owner_dm = InboundMessage(
        chat_id="cnv_dm", chat_type="direct", sender_id="usr_owner", sender_name="Owner",
        text="hi", raw_message={},
    )
    stranger_dm = InboundMessage(
        chat_id="cnv_x", chat_type="direct", sender_id="usr_stranger", sender_name="S",
        text="hi", raw_message={},
    )
    expected = str(named_home / "clawchat")
    assert expected in (a._compose_channel_prompt(owner_dm) or "")
    assert expected not in (a._compose_channel_prompt(stranger_dm) or "")
