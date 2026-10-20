"""The plugin relays agent-written onboarding facts (~/clawchat/onboarding.json)
on the plugin-report call; it never files a wiki report itself."""

from __future__ import annotations

import json
from pathlib import Path

from clawchat_gateway.api_client import build_plugin_report_payload
from clawchat_gateway.onboarding_report import read_onboarding_report


def _home(tmp_path: Path, payload: str | None) -> Path:
    if payload is not None:
        (tmp_path / "clawchat").mkdir(exist_ok=True)
        (tmp_path / "clawchat" / "onboarding.json").write_text(payload, encoding="utf-8")
    return tmp_path


def test_absent_file_is_none(tmp_path: Path) -> None:
    assert read_onboarding_report(_home(tmp_path, None)) is None


def test_valid_fields_are_relayed(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        json.dumps(
            {
                "wiki_report_id": "rep_01JABC",
                "capability_tier": 4,
                "capability_ceiling": 4,
                "capabilities": {"headless": True, "mcp": True, "permission_hook": False, "session_line": False},
            }
        ),
    )
    assert read_onboarding_report(home) == {
        "wiki_report_id": "rep_01JABC",
        "capability_tier": 4,
        "capability_ceiling": 4,
        "capabilities": {"headless": True, "mcp": True, "permission_hook": False, "session_line": False},
    }


def test_malformed_fields_are_dropped(tmp_path: Path) -> None:
    assert read_onboarding_report(_home(tmp_path, "{not json")) is None
    home = _home(tmp_path, json.dumps({"wiki_report_id": "ok-1", "capability_tier": 9, "capabilities": {"mcp": "yes"}}))
    assert read_onboarding_report(home) == {"wiki_report_id": "ok-1"}


def test_payload_builder_merges_onboarding() -> None:
    base = dict(device_id="d", platform="hermes", plugin_version="1", agent_version="2", runtime_name="python", runtime_version="3.12")
    assert "wiki_report_id" not in build_plugin_report_payload(**base)
    body = build_plugin_report_payload(**base, onboarding={"wiki_report_id": "rep_1", "capability_tier": 3})
    assert body["wiki_report_id"] == "rep_1"
    assert body["capability_tier"] == 3


def test_integral_float_tier_accepted_but_fractional_string_bool_dropped(tmp_path: Path) -> None:
    # Parity with the TS side (`Number.isInteger`, no separate float type):
    # a JSON `4.0` must validate the same as `4` on both plugins.
    home = _home(tmp_path, json.dumps({"capability_tier": 4.0}))
    assert read_onboarding_report(home) == {"capability_tier": 4}
    assert read_onboarding_report(_home(tmp_path, json.dumps({"capability_tier": 2.5}))) is None
    assert read_onboarding_report(_home(tmp_path, json.dumps({"capability_tier": "3"}))) is None
    assert read_onboarding_report(_home(tmp_path, json.dumps({"capability_tier": True}))) is None


def test_capability_ceiling_float_bool_string_parity(tmp_path: Path) -> None:
    # Raw JSON text, same literals as the TS suite.
    assert read_onboarding_report(_home(tmp_path, '{"capability_ceiling": 4.0}')) == {"capability_ceiling": 4}
    assert read_onboarding_report(_home(tmp_path, '{"capability_ceiling": 2.5}')) is None
    assert read_onboarding_report(_home(tmp_path, '{"capability_ceiling": "3"}')) is None
    assert read_onboarding_report(_home(tmp_path, '{"capability_ceiling": true}')) is None


def test_ceiling_below_tier_is_dropped_tier_kept(tmp_path: Path) -> None:
    # Backend 22004 rejects the whole report when ceiling < tier.
    assert read_onboarding_report(_home(tmp_path, '{"capability_tier": 3, "capability_ceiling": 2}')) == {
        "capability_tier": 3
    }
    assert read_onboarding_report(_home(tmp_path, '{"capability_tier": 3.0, "capability_ceiling": 2.0}')) == {
        "capability_tier": 3
    }
    assert read_onboarding_report(_home(tmp_path, '{"capability_tier": 3, "capability_ceiling": 3}')) == {
        "capability_tier": 3,
        "capability_ceiling": 3,
    }
    assert read_onboarding_report(_home(tmp_path, '{"capability_tier": 2, "capability_ceiling": 4.0}')) == {
        "capability_tier": 2,
        "capability_ceiling": 4,
    }
    # An invalid tier imposes no bound on the ceiling.
    assert read_onboarding_report(_home(tmp_path, '{"capability_tier": 2.5, "capability_ceiling": 1}')) == {
        "capability_ceiling": 1
    }


def test_wiki_report_id_with_trailing_newline_is_dropped(tmp_path: Path) -> None:
    # Python's `$` matches before a trailing newline. Parity with the TS reader,
    # which drops such an id (optional field: it is simply left out).
    assert read_onboarding_report(_home(tmp_path, json.dumps({"wiki_report_id": "rep_1\n"}))) is None


def test_non_object_top_level_json_is_none(tmp_path: Path) -> None:
    assert read_onboarding_report(_home(tmp_path, json.dumps(["not", "an", "object"]))) is None
    assert read_onboarding_report(_home(tmp_path, json.dumps("just a string"))) is None


def test_home_resolution_failure_returns_none(monkeypatch) -> None:
    def _boom() -> Path:
        raise RuntimeError("no home directory available")

    monkeypatch.setattr(Path, "home", _boom)
    assert read_onboarding_report() is None
