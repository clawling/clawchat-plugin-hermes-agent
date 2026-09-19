"""Resolve the owner's reported app language into one of the six languages the
ClawChat content tree carries.

``agent_owner_locale`` comes from the owner's ClawChat app setting and is
omitted entirely when they never reported one, so every unresolvable case
lands on English rather than guessing. Mirrors ``src/owner-language.ts`` in
the openclaw plugin — keep the two in sync.

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


def language_display_name(language: str) -> str:
    # Defensive "English" default: Python has no type guarantee that `language` is
    # one of the six keys, unlike TypeScript which relies on the type system.
    # In practice, this is only ever called with resolve_owner_language()'s output.
    return _DISPLAY_NAMES.get(language, "English")
