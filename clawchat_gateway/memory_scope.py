"""Which ClawChat notes the conversation behind a tool call may read.

A memory tool's result goes into the calling session's history. A group is one
Hermes session for everyone in it, kept across turns, so a note read or found
there can be surfaced to anyone in the group later — whatever the reply that
triggered the read chose to say. The memory tools therefore judge each read by
the conversation the call comes from, taken from the host's session context
(``HERMES_SESSION_PLATFORM`` / ``_CHAT_ID`` / ``_CHAT_TYPE`` / ``_USER_ID``;
task-local ContextVars on current Hermes, ``os.environ`` on older ones):

* ``local`` — no gateway session in this process (``hermes chat``, the profile
  CLI): every note. The operator there owns the files anyway.
* ``owner_direct`` — the owner's direct chat: every note.
* ``direct`` — anyone else's direct chat: only the note about that person
  (``users/<their id>.md``). Never ``owner.md``, another person's note or any
  group's note, for the reason below: the friend in this chat is the only one
  present.
* ``group`` — a ClawChat group: that group's note and the notes of its members
  (the participant list cached in the group note's metadata, refreshed on every
  group message; the group's recent speakers only when no list is cached).
  Never ``owner.md``, another group's note, or a note about a non-member: a
  person's note can hold what they said elsewhere, so it may only come up where
  that person is present.

Whatever cannot be placed — another platform, a missing chat id, a chat type
the plugin never sets, a host "dm" for a chat the plugin knows as a group, or a
gateway process whose call carries no session — is a group with no known
members, so it reads nothing.

Appends are not judged here: the routing rule sends a fact about the owner to
``owner.md`` from any conversation, and adding text reveals nothing. Replacing
or editing a note reads or wipes what is there, so the tools only allow those
on notes the conversation may read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clawchat_gateway.clawchat_memory import read_clawchat_memory_file
from clawchat_gateway.storage import get_clawchat_store

ACCOUNT_ID = "default"
DIRECT_CHAT_TYPES = frozenset({"dm", "direct"})
SPEAKER_FALLBACK_ROWS = 200

# Surfaces that are the agent's own machine, not a chat (mirrors the host's
# NON_MESSAGING_SESSION_SURFACES for the ones that matter here).
_LOCAL_SURFACES = frozenset({"", "cli", "local", "tui", "desktop", "tool"})


@dataclass(frozen=True)
class MemoryScope:
    kind: str  # "local" | "owner_direct" | "direct" | "group"
    chat_id: str = ""
    members: frozenset[str] = frozenset()

    @property
    def restricted(self) -> bool:
        return self.kind in {"direct", "group"}

    def can_read(self, target_type: str, target_id: str) -> bool:
        if self.kind in {"local", "owner_direct"}:
            return True
        if target_type == "owner":
            return False
        if self.kind == "direct":
            return target_type == "user" and target_id in self.members
        if target_type == "group":
            return bool(self.chat_id) and target_id == self.chat_id
        if target_type == "user":
            return target_id in self.members
        return False

    def refusal(self, target_type: str, target_id: str) -> dict[str, Any]:
        if target_type == "owner":
            message = (
                "owner.md can only be read in your owner's direct chat. This conversation is "
                "not that chat, and a tool result here stays in this conversation's history "
                "where others can see it. Do not try to get the owner's notes another way."
            )
        elif self.kind == "direct":
            message = (
                f"{'groups' if target_type == 'group' else 'users'}/{target_id}.md is not readable in this "
                "direct chat: here you can only read the note about the person you are talking to, "
                "because another person's or a group's note can hold what was said where they were "
                "not present."
            )
        elif target_type == "group":
            message = (
                f"groups/{target_id}.md is another group's note; in a group you can only read "
                "that group's own note."
            )
        else:
            message = (
                f"users/{target_id}.md is about someone who is not a member of this group; in a "
                "group you can only read the notes of its members."
            )
        return {"error": "not_readable_here", "code": "memory_scope", "message": message}

    def note(self) -> str | None:
        """What a restricted search left out, for the model (no counts)."""
        if self.kind == "direct":
            return (
                "In this direct chat only the note about the person you are talking to is searched; "
                "owner.md, other people's notes and group notes are left out."
            )
        if self.kind == "group":
            return (
                "In a group only that group's note and its members' notes are searched; "
                "owner.md, other groups' notes and notes about non-members are left out."
            )
        return None


def _session_value(name: str) -> str:
    try:
        from gateway.session_context import get_session_env

        value = get_session_env(name, "")
    except Exception:  # noqa: BLE001 - older/absent host module: plain env
        value = os.getenv(name, "")
    return str(value or "").strip()


def _host_session_context_engaged() -> bool:
    try:
        from gateway.session_context import session_context_engaged
    except Exception:  # noqa: BLE001 - host without the latch
        return False
    try:
        return bool(session_context_engaged())
    except Exception:  # noqa: BLE001
        return True


# Indirection so tests can stand in for the host latch.
_session_context_engaged = _host_session_context_engaged


def _config_extra() -> dict[str, Any]:
    try:
        import yaml

        from clawchat_gateway.hermes_home import hermes_home

        path = Path(hermes_home()) / "config.yaml"
        if not path.exists():
            return {}
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        extra = ((loaded.get("platforms") or {}).get("clawchat") or {}).get("extra") or {}
        return extra if isinstance(extra, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _owner_user_id() -> str:
    from clawchat_gateway.config import ClawChatConfig

    try:
        owner = ClawChatConfig.from_platform_config(SimpleNamespace(extra=_config_extra())).owner_user_id
    except Exception:  # noqa: BLE001
        owner = ""
    if owner:
        return str(owner)
    try:
        owner = get_clawchat_store().get_activation_owner_user_id(platform="hermes", account_id=ACCOUNT_ID)
    except Exception:  # noqa: BLE001
        owner = ""
    return str(owner or "")


def _group_participants(root: Path, chat_id: str) -> tuple[bool, frozenset[str]]:
    """(known as a group, cached participant ids) from the group note's metadata."""
    try:
        memory = read_clawchat_memory_file(root, "group", chat_id)
    except Exception:  # noqa: BLE001 - malformed id / unsafe path: not a known group
        return False, frozenset()
    metadata = memory.get("metadata") if isinstance(memory.get("metadata"), dict) else {}
    raw = str(metadata.get("participant_ids") or "")
    ids = frozenset(value.strip() for value in raw.split(",") if value.strip())
    return bool(ids), ids


def _recent_speakers(chat_id: str) -> frozenset[str]:
    try:
        rows = get_clawchat_store().list_recent_group_transcript(ACCOUNT_ID, chat_id, SPEAKER_FALLBACK_ROWS)
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(
        str(row.get("sender_id") or "")
        for row in rows or []
        if row.get("direction") == "inbound" and row.get("sender_id")
    )


_NOTHING = MemoryScope(kind="group")


def resolve_memory_scope(root: Path | str | None) -> MemoryScope:
    platform = _session_value("HERMES_SESSION_PLATFORM").lower()
    if not platform:
        source = _session_value("HERMES_SESSION_SOURCE").lower()
        host_platform = os.getenv("HERMES_PLATFORM", "").strip().lower()
        if source in _LOCAL_SURFACES and host_platform in _LOCAL_SURFACES and not _session_context_engaged():
            return MemoryScope(kind="local")
        return _NOTHING
    if platform != "clawchat":
        return _NOTHING
    chat_id = _session_value("HERMES_SESSION_CHAT_ID")
    chat_type = _session_value("HERMES_SESSION_CHAT_TYPE").lower()
    if not chat_id or root is None:
        return _NOTHING
    known_group, participants = _group_participants(Path(root), chat_id)
    if chat_type in DIRECT_CHAT_TYPES and not known_group:
        owner = _owner_user_id()
        user_id = _session_value("HERMES_SESSION_USER_ID")
        if owner and user_id == owner:
            return MemoryScope(kind="owner_direct", chat_id=chat_id)
        return MemoryScope(kind="direct", chat_id=chat_id, members=frozenset({user_id}) if user_id else frozenset())
    if chat_type != "group" and not known_group:
        return _NOTHING
    members = participants or _recent_speakers(chat_id)
    return MemoryScope(kind="group", chat_id=chat_id, members=members)
