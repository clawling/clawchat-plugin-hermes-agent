"""Find person facts left in Hermes' global memory from before per-person notes.

Up to 0.14.0-100 the plugin let Hermes keep what it learnt about the owner and
about other people in its global ``MEMORY.md`` / ``USER.md``
(``$HERMES_HOME/memories``). ``MEMORY.md`` is injected into every session — a
friend's direct chat and every group included; ``USER.md`` is no longer read
once ``memory.user_profile_enabled`` is false, so what it holds is stranded.
ClawChat notes (``owner.md``, ``users/<id>.md``) are scoped per conversation.

This module only reads and counts. It never edits, moves or deletes the files:
they are the owner's data, and moving them happens in the owner's direct chat,
by the agent, after the owner agrees (see ``build_migration_prompt``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Hermes' entry separator (tools/memory_tool_store.py ENTRY_DELIMITER).
ENTRY_DELIMITER = "\n§\n"
MARKER_FILE = ".memory-migration-hint-done"

_PERSON_PATTERNS = re.compile(
    r"\busr_[A-Za-z0-9]+"
    r"|\b(?:owner|the user|user's|he|she|his|her|him|wife|husband|son|daughter|mother|father|"
    r"friend|boss|colleague|girlfriend|boyfriend|birthday|prefers?|likes?|dislikes?|"
    r"works (?:at|as))\b"
    r"|主人|用户|他|她|喜欢|偏好|讨厌|生日|老婆|老公|妻子|丈夫|儿子|女儿|父亲|母亲|朋友|老板|同事|住在|名字",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MemoryMigrationScan:
    memory_path: Path
    user_path: Path
    memory_entries: int
    memory_person_entries: int
    user_entries: int


def split_entries(text: str) -> list[str]:
    parts = text.split(ENTRY_DELIMITER) if ENTRY_DELIMITER in text else re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def looks_like_person_entry(entry: str, names: set[str]) -> bool:
    if _PERSON_PATTERNS.search(entry):
        return True
    lowered = entry.lower()
    return any(len(name) >= 2 and name.lower() in lowered for name in names)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        return ""


def scan_global_memory(home: Path, *, names: set[str]) -> MemoryMigrationScan | None:
    """Counts of person-like entries in the global memory; ``None`` when there are none.

    Every ``USER.md`` entry counts (it is the user profile by definition); a
    ``MEMORY.md`` entry counts when it names a known person, an ``usr_`` id, or
    reads like a fact about someone.
    """
    memories = Path(home) / "memories"
    memory_path = memories / "MEMORY.md"
    user_path = memories / "USER.md"
    memory_entries = split_entries(_read(memory_path))
    user_entries = split_entries(_read(user_path))
    person = sum(1 for entry in memory_entries if looks_like_person_entry(entry, names))
    if not person and not user_entries:
        return None
    return MemoryMigrationScan(
        memory_path=memory_path,
        user_path=user_path,
        memory_entries=len(memory_entries),
        memory_person_entries=person,
        user_entries=len(user_entries),
    )


def marker_path(home: Path) -> Path:
    return Path(home) / "clawchat" / MARKER_FILE


def build_migration_prompt(scan: MemoryMigrationScan) -> str:
    found = []
    if scan.memory_person_entries:
        found.append(
            f"{scan.memory_person_entries} of {scan.memory_entries} entries in {scan.memory_path} "
            "(Hermes' global memory, shown in every conversation, including friends' direct "
            "chats and groups)"
        )
    if scan.user_entries:
        found.append(
            f"{scan.user_entries} entries in {scan.user_path} (USER.md, which Hermes no longer "
            "reads for ClawChat, so they are effectively lost)"
        )
    return "\n".join(
        [
            "[ClawChat maintenance. This is not a message from your owner.]",
            "Since this plugin version, what you learn about a person belongs in ClawChat notes "
            "that only the right conversations see: owner.md for your owner, users/<usr_id>.md "
            "for anyone else. Your older global memory still holds what looks like facts about "
            "your owner or a specific person: " + "; and ".join(found) + ".",
            "Tell your owner this in one short message, in their language: how many entries, "
            "why it matters, and offer to move them into those notes. Do not quote the entries.",
            "Ask first. Do not move, copy, edit or delete anything until your owner agrees in "
            "this chat; if they decline or do not answer, leave the files as they are. Once they "
            "agree: read each entry, append it with clawchat_memory_write to the matching note, "
            "and only after that remove it from the global memory with Hermes' memory tool.",
        ]
    )
