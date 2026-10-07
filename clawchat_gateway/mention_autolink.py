"""Turn "@<member name>" typed in a group message into structured mentions.

A group message whose body is one text fragment mentions nobody, however many
"@name" it contains: only a ``mention`` fragment (and its ``context.mentions``
mirror, docs/client-integration.md §7.1 / §10.2) reaches the named member as a
mention. These rules match the reference ClawChat client, so text typed by a
person and text written by an agent link the same way:

* An ``@`` starts a mention unless the character right before it is an e-mail
  local-part character (``[A-Za-z0-9._%+-]``, ASCII only), so ``alice@Bean``
  never links while ``让@大Q`` does. Only the ASCII ``@`` counts.
* Among the members whose (trimmed) name follows the ``@``, the longest wins.
  ASCII letters compare case-insensitively; at equal length an exact-case match
  beats a case-folded one. A name ending in an ASCII letter or digit must not
  run on into another one (``@Beanstalk`` does not link ``Bean``).
* Two different members tied at the same length: ambiguous, nothing links.
* The sender itself and the ``all`` sentinel are never linked.
* Text that does not link stays plain text; the ``@`` of a linked name is
  dropped from the text because the mention renders it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from clawchat_gateway.protocol import MENTION_ALL_USER_ID

_EMAIL_LOCAL_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-"
)


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _fold(text: str) -> str:
    # ASCII-only lower-casing: never changes the length.
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in text)


def _starts_mention(text: str, index: int) -> bool:
    if text[index] != "@":
        return False
    return index == 0 or text[index - 1] not in _EMAIL_LOCAL_CHARS


def has_mention_candidate(text: str) -> bool:
    """Whether ``text`` has an ``@`` that could start a mention (cheap pre-check)."""
    return any(_starts_mention(text, i) for i in range(max(0, len(text) - 1)))


def _targets(roster: Iterable[tuple[str, str]], own_user_id: str) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    for user_id, name in roster:
        user_id = (user_id or "").strip()
        name = (name or "").strip()
        if not user_id or not name or user_id == own_user_id or user_id == MENTION_ALL_USER_ID:
            continue
        targets.append((user_id, name))
    return targets


def _match_at(text: str, start: int, targets: list[tuple[str, str]]) -> tuple[str, str] | None:
    best: tuple[str, str] | None = None
    best_exact = False
    ambiguous = False
    for user_id, name in targets:
        end = start + len(name)
        if end > len(text):
            continue
        candidate = text[start:end]
        exact = candidate == name
        if not exact and _fold(candidate) != _fold(name):
            continue
        if end < len(text) and _is_ascii_alnum(name[-1]) and _is_ascii_alnum(text[end]):
            continue
        if best is None or len(name) > len(best[1]):
            best, best_exact, ambiguous = (user_id, name), exact, False
        elif len(name) == len(best[1]):
            if exact and not best_exact:
                best, best_exact, ambiguous = (user_id, name), True, False
            elif exact == best_exact and user_id != best[0]:
                ambiguous = True
    return None if ambiguous else best


def autolink_mentions(
    text: str, roster: Iterable[tuple[str, str]], own_user_id: str
) -> list[dict[str, Any]]:
    """Fragments for ``text`` with each recognised "@name" as a mention fragment.

    ``roster`` is ``(user_id, display name)`` pairs of the chat's members; one
    user may appear under several names. Returns ``[]`` for empty text and a
    single text fragment when nothing links.
    """
    if not text:
        return []
    targets = _targets(roster, own_user_id)
    if not targets:
        return [{"kind": "text", "text": text}]
    fragments: list[dict[str, Any]] = []
    cursor = 0
    i = 0
    while i < len(text):
        hit = _match_at(text, i + 1, targets) if _starts_mention(text, i) else None
        if hit is None:
            i += 1
            continue
        user_id, name = hit
        if i > cursor:
            fragments.append({"kind": "text", "text": text[cursor:i]})
        fragments.append({"kind": "mention", "user_id": user_id, "display": name})
        i = cursor = i + 1 + len(name)
    if cursor < len(text):
        fragments.append({"kind": "text", "text": text[cursor:]})
    return fragments


def mentions_in(fragments: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """The ``context.mentions`` mirror of the mention fragments: in order, one per user."""
    seen: set[str] = set()
    mentions: list[dict[str, str]] = []
    for fragment in fragments:
        if not isinstance(fragment, dict) or fragment.get("kind") != "mention":
            continue
        user_id = fragment.get("user_id")
        if not isinstance(user_id, str) or not user_id or user_id in seen:
            continue
        seen.add(user_id)
        mentions.append({"kind": "mention", "user_id": user_id, "display": str(fragment.get("display") or "")})
    return mentions
