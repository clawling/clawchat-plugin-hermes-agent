import json
from pathlib import Path

from clawchat_gateway.liveware_sample import (
    load_intro_text,
    LIVEWARE_SAMPLE_INTRO_FALLBACK_EN,
)

TABLE = {"schema": 1, "intro": {
    "en": "EN copy", "es": "ES", "ja": "JA", "ko": "KO",
    "zh": "ZH copy", "zh_Hant": "HANT",
}}


def _app_dir(tmp_path: Path, contents: str | None) -> Path:
    if contents is not None:
        (tmp_path / "intro.i18n.json").write_text(contents, encoding="utf-8")
    return tmp_path


def test_returns_requested_language(tmp_path):
    d = _app_dir(tmp_path, json.dumps(TABLE))
    assert load_intro_text(d, "zh") == "ZH copy"
    assert load_intro_text(d, "en") == "EN copy"


def test_missing_file_falls_back(tmp_path):
    assert load_intro_text(_app_dir(tmp_path, None), "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN


def test_corrupt_json_falls_back(tmp_path):
    assert load_intro_text(_app_dir(tmp_path, "{not json"), "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN


def test_missing_or_empty_key_falls_back(tmp_path):
    d = _app_dir(tmp_path, json.dumps({"schema": 1, "intro": {"en": "EN copy", "zh": "   "}}))
    assert load_intro_text(d, "ja") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN
    assert load_intro_text(d, "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN


# A silent fallback means a mistake in the content tree ships English to every
# non-English owner with no signal anywhere. All four degradation modes must
# warn — the two below that used to fall through outside `except` were silent.


def test_warns_when_file_is_missing(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert load_intro_text(_app_dir(tmp_path, None), "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN
    assert "unreadable" in caplog.text


def test_warns_when_json_is_corrupt(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        assert load_intro_text(_app_dir(tmp_path, "{not json"), "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN
    assert "not valid JSON" in caplog.text


def test_warns_when_key_is_missing(tmp_path, caplog):
    d = _app_dir(tmp_path, json.dumps({"schema": 1, "intro": {"en": "EN copy"}}))
    with caplog.at_level("WARNING"):
        assert load_intro_text(d, "ja") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN
    assert 'has no "ja" entry' in caplog.text


def test_warns_when_value_is_empty(tmp_path, caplog):
    d = _app_dir(tmp_path, json.dumps({"schema": 1, "intro": {"en": "EN copy", "zh": "   "}}))
    with caplog.at_level("WARNING"):
        assert load_intro_text(d, "zh") == LIVEWARE_SAMPLE_INTRO_FALLBACK_EN
    assert 'has an empty "zh" entry' in caplog.text


def test_stays_quiet_on_the_happy_path(tmp_path, caplog):
    d = _app_dir(tmp_path, json.dumps(TABLE))
    with caplog.at_level("WARNING"):
        assert load_intro_text(d, "zh") == "ZH copy"
    assert caplog.text == ""
