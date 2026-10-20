import pytest
from clawchat_gateway.owner_language import (
    language_display_name,
    resolve_owner_language,
    resolve_owner_language_if_known,
)


@pytest.mark.parametrize("locale,expected", [
    ("en-US", "en"), ("es-MX", "es"), ("ja-JP", "ja"), ("ko-KR", "ko"), ("zh-CN", "zh"),
    ("zh-Hant", "zh_Hant"), ("zh-TW", "zh_Hant"), ("zh-HK", "zh_Hant"), ("zh-MO", "zh_Hant"),
    ("zh-Hans-CN", "zh"), ("  ZH_hant  ", "zh_Hant"), ("JA", "ja"),
    (None, "en"), ("", "en"), ("   ", "en"), ("de-DE", "en"),
])
def test_resolve_owner_language(locale, expected):
    assert resolve_owner_language(locale) == expected


def test_display_names():
    assert language_display_name("zh") == "Simplified Chinese"
    assert language_display_name("zh_Hant") == "Traditional Chinese"
    assert language_display_name("ja") == "Japanese"


def test_display_names_unknown_locale():
    # Python's defensive default (TypeScript relies on type system instead)
    assert language_display_name("de") == "English"
    assert language_display_name("") == "English"


# The greeting path's resolver. Unlike resolve_owner_language it must NOT
# collapse "we have no idea" into "en": that turns an owner locale that has
# not landed yet into a hard "Reply in English." assertion at an owner who
# never said they speak English.
@pytest.mark.parametrize("locale", [None, "", "   "])
def test_resolve_owner_language_if_known_returns_none_when_unreported(locale):
    assert resolve_owner_language_if_known(locale) is None


@pytest.mark.parametrize("locale,expected", [
    ("ja-JP", "ja"), ("zh-TW", "zh_Hant"),
    # An unrecognised-but-present locale is still a real report, so "en" here
    # is a resolution, not a guess.
    ("de-DE", "en"),
])
def test_resolve_owner_language_if_known_matches_resolver(locale, expected):
    assert resolve_owner_language_if_known(locale) == expected
