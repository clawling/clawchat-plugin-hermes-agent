"""Resolve the owner's reported app language into one of the six languages the
ClawChat content tree carries.

``agent_owner_locale`` comes from the owner's ClawChat app setting and is
omitted entirely when they never reported one, so every unresolvable case
lands on English rather than guessing. Mirrors ``src/owner-language.ts`` in
the openclaw plugin — keep the two in sync.

Callers on the *greeting* path want the opposite of that fallback and use
:func:`resolve_owner_language_if_known` instead — see its docstring.

Note: ``language_display_name()`` includes a defensive default that TypeScript's
type system prevents, documented at that function.
"""

from __future__ import annotations

OWNER_LANGUAGES: tuple[str, ...] = ("en", "es", "ja", "ko", "zh", "zh_Hant")

_DISPLAY_NAMES = {
    "en": "English",
    "es": "Spanish",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Simplified Chinese",
    "zh_Hant": "Traditional Chinese",
}

# Traditional-Chinese regions must be tested before the bare ``zh`` prefix,
# because they also start with "zh".
_TRADITIONAL_PREFIXES = ("zh-hant", "zh-tw", "zh-hk", "zh-mo")


def resolve_owner_language(locale: str | None) -> str:
    tag = str(locale or "").strip().lower().replace("_", "-")
    if not tag:
        return "en"
    if tag.startswith(_TRADITIONAL_PREFIXES):
        return "zh_Hant"
    for prefix, language in (("zh", "zh"), ("ja", "ja"), ("ko", "ko"), ("es", "es")):
        if tag.startswith(prefix):
            return language
    return "en"


def resolve_owner_language_if_known(locale: str | None) -> str | None:
    """Resolve a locale ONLY when one was actually reported; ``None`` otherwise.

    :func:`resolve_owner_language` must answer with a language because the
    Liveware intro lookup has to pick one copy and ``en`` is the documented
    default. The greeting path is the opposite case: the language line is an
    explicit assertion to the model, and the owner metadata may not have landed
    yet (the friend-greeting dispatch reads it synchronously and must not grow
    an await). Collapsing "unknown" into ``en`` there tells a Chinese owner's
    agent to reply in English. Callers on the greeting path use this and omit
    the line entirely when it returns ``None``.

    Mirrors ``resolveOwnerLanguageIfKnown`` in the openclaw plugin.
    """
    return resolve_owner_language(locale) if str(locale or "").strip() else None


def language_display_name(language: str) -> str:
    # Defensive "English" default: Python has no type guarantee that `language` is
    # one of the six keys, unlike TypeScript which relies on the type system.
    # In practice, this is only ever called with resolve_owner_language()'s output.
    return _DISPLAY_NAMES.get(language, "English")
