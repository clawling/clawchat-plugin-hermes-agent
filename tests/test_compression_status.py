"""Compression provenance and CLI diagnostics remain read-only and JSON-safe."""

import importlib
import json
import sys
from copy import deepcopy
from types import MappingProxyType, ModuleType
from unittest.mock import Mock

import pytest

from clawchat_gateway import compression_status, config, host_version, storage
from clawchat_gateway.compression_status import describe_compression, host_version_display


OVERRIDE_NOTE = (
    "Host compression.threshold_tokens (150000) overrides "
    "platforms.clawchat.extra.session-cap-tokens (200000). "
    "The host key is global: it applies to every Hermes platform, not just ClawChat."
)
FALLBACK_NOTE = (
    "Host compression.threshold_tokens is invalid; the plugin uses its own "
    "session-cap-tokens to calculate the sediment point, while the host's own "
    "compression behavior depends on Hermes."
)
DISABLED_NOTE = (
    "Host context compression is disabled (compression.enabled: false); "
    "the compression point is not used."
)


@pytest.mark.parametrize(
    "host,extra,cap,source,notes,point",
    [
        pytest.param(None, None, 150000, "plugin_fill", [], 140000, id="no-config"),
        pytest.param({}, {}, 150000, "plugin_fill", [], 140000, id="missing-section"),
        pytest.param({"compression": {}}, {}, 150000, "plugin_fill", [], 140000, id="missing-key"),
        pytest.param({"compression": {"threshold_tokens": 150000}}, {},
                     150000, "host", [], 140000, id="host-cap-plugin-unset"),
        pytest.param({"compression": {"threshold_tokens": 150000}}, {"session-cap-tokens": 150000},
                     150000, "host", [], 140000, id="explicit-equal"),
        pytest.param({"compression": {"threshold_tokens": 150000}}, {"session-cap-tokens": 200000},
                     150000, "host", [OVERRIDE_NOTE], 140000, id="explicit-override"),
        pytest.param({}, {"session-cap-tokens": 200000},
                     200000, "plugin_fill", [], 190000, id="fill-custom"),
        pytest.param({}, {"session-cap-tokens": 1},
                     50000, "plugin_fill", [], 40000, id="cap-minimum"),
        pytest.param({}, {"session-cap-tokens": 2000000},
                     1000000, "plugin_fill", [], 990000, id="cap-maximum"),
        pytest.param({}, {"session-cap-tokens": " 200000 "},
                     200000, "plugin_fill", [], 190000, id="cap-string"),
        pytest.param({}, {"sediment-margin-tokens": 1},
                     150000, "plugin_fill", [], 148000, id="margin-minimum"),
        pytest.param({}, {"sediment-margin-tokens": 100000},
                     150000, "plugin_fill", [], 100000, id="margin-maximum"),
        pytest.param({"compression": {"threshold_tokens": 100}}, {},
                     100, "host", [], 1, id="sediment-floor"),
        pytest.param({"compression": {"threshold_tokens": 150000.9}}, {},
                     150000, "host", [], 140000, id="host-float"),
        pytest.param({"compression": {"threshold": 0.8}}, {},
                     150000, "plugin_fill", [], 140000, id="ratio-does-not-reduce-upper-bound"),
        pytest.param({"compression": {"enabled": False}}, {},
                     150000, "plugin_fill", [DISABLED_NOTE], 140000, id="disabled"),
        pytest.param({"compression": {"threshold_tokens": None, "enabled": False}}, {},
                     150000, "plugin_fallback", [FALLBACK_NOTE, DISABLED_NOTE], 140000,
                     id="invalid-disabled"),
        pytest.param({"compression": {"threshold_tokens": 150000, "enabled": False}},
                     {"session-cap-tokens": 200000}, 150000, "host",
                     [OVERRIDE_NOTE, DISABLED_NOTE], 140000, id="override-disabled"),
    ],
)
def test_compression_combinations(host, extra, cap, source, notes, point):
    original = deepcopy((host, extra))
    result = describe_compression(host, extra, host_version="0.21.7")
    assert result["threshold_tokens"] == cap
    assert result["threshold_tokens_source"] == source
    assert result["notes"] == notes
    assert result["sediment_point_tokens"] == point
    assert result["host_threshold_tokens"] == (cap if source == "host" else None)
    assert result["plugin_session_cap_set"] is (extra is not None and "session-cap-tokens" in extra)
    assert (host, extra) == original


@pytest.mark.parametrize("value", [None, "abc", "150000", 0, -1, True, False, float("nan"), float("inf")])
@pytest.mark.parametrize("extra,cap,point", [({}, 150000, 140000), ({"session-cap-tokens": 200000}, 200000, 190000)])
def test_invalid_host_key_is_present_and_never_described_as_fill(value, extra, cap, point):
    result = describe_compression({"compression": {"threshold_tokens": value}}, extra, host_version=None)
    assert result["threshold_tokens"] == cap
    assert result["threshold_tokens_source"] == "plugin_fallback"
    assert result["host_threshold_tokens"] is None
    assert result["notes"] == [FALLBACK_NOTE]
    assert result["sediment_point_tokens"] == point


@pytest.mark.parametrize("key", ["session-cap-tokens", "sediment-margin-tokens"])
@pytest.mark.parametrize("value", [None, "abc", True, False, float("nan"), float("inf")])
def test_invalid_plugin_numbers_use_defaults(key, value):
    result = describe_compression({}, {key: value}, host_version=None)
    assert result["threshold_tokens"] == 150000
    assert result["threshold_tokens_source"] == "plugin_fill"
    assert result["plugin_session_cap_tokens"] == 150000
    assert result["plugin_session_cap_set"] is (key == "session-cap-tokens")
    assert result["sediment_margin_tokens"] == 10000
    assert result["sediment_point_tokens"] == 140000
    assert result["notes"] == []


@pytest.mark.parametrize("value,expected", [
    (0.8, 0.8), (1, 1.0), (None, None), ("0.8", None), (0, None),
    (-0.1, None), (1.1, None), (True, None), (False, None),
    (float("nan"), None), (float("inf"), None),
])
def test_ratio_validation(value, expected):
    result = describe_compression({"compression": {"threshold": value}}, {}, host_version=None)
    assert result["host_threshold_ratio"] == expected
    if expected is not None:
        assert isinstance(result["host_threshold_ratio"], float)
    assert result["threshold_tokens"] == 150000
    assert result["threshold_tokens_source"] == "plugin_fill"
    assert result["sediment_point_tokens"] == 140000
    assert result["notes"] == []


@pytest.mark.parametrize("value,enabled", [(False, False), (True, True), (None, True), (0, True), ("false", True)])
def test_only_boolean_false_disables_host_compression(value, enabled):
    result = describe_compression({"compression": {"enabled": value}}, {}, host_version=None)
    assert result["host_compression_enabled"] is enabled
    assert result["notes"] == ([] if enabled else [DISABLED_NOTE])


@pytest.mark.parametrize("version", ["0.0.0", "", None, "v0.0.0", "garbage", "  v0.0.0  "])
def test_unknown_host_version(version):
    assert host_version_display(version) == "unknown (local build)"


@pytest.mark.parametrize("version", ["0.21.7", "v0.21.7", "  v0.21.7  "])
def test_known_host_version(version):
    assert host_version_display(version) == "0.21.7"


@pytest.mark.parametrize("version", [None, "0.0.0", "0.12.0", "0.21.7"])
def test_version_does_not_select_compression_behavior(version):
    result = describe_compression({}, {}, host_version=version)
    expected = describe_compression({}, {}, host_version="0.21.7")
    result.pop("host_version")
    expected.pop("host_version")
    assert result == expected


@pytest.mark.parametrize("section", [None, [], "invalid", True])
def test_invalid_compression_section_uses_defaults(section):
    assert describe_compression({"compression": section}, {}, host_version=None) == describe_compression(
        {}, {}, host_version=None,
    )


def test_accepts_read_only_mappings():
    result = describe_compression(
        MappingProxyType({"compression": MappingProxyType({"threshold_tokens": 150000})}),
        MappingProxyType({"session-cap-tokens": 200000}), host_version=None,
    )
    assert result["notes"] == [OVERRIDE_NOTE]


@pytest.fixture
def raw_config_reader(monkeypatch):
    package = ModuleType("hermes_cli")
    module = ModuleType("hermes_cli.config")
    reader = Mock(return_value={"compression": {"threshold_tokens": 150000}})
    module.read_raw_config = reader
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", module)
    return reader


def test_read_host_config_uses_raw_reader(raw_config_reader):
    assert compression_status.read_host_config() == {"compression": {"threshold_tokens": 150000}}
    raw_config_reader.assert_called_once_with()


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_read_host_config_rejects_non_dict(raw_config_reader, value):
    raw_config_reader.return_value = value
    assert compression_status.read_host_config() == {}


def test_read_host_config_failure_is_silent(raw_config_reader, capsys):
    raw_config_reader.side_effect = OSError("unreadable")
    assert compression_status.read_host_config() == {}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.fixture
def status_profile(monkeypatch, tmp_path):
    home = tmp_path / "empty-profile"
    home_module = importlib.import_module("clawchat_gateway.hermes_home")
    monkeypatch.setattr(home_module, "hermes_home", lambda: home)
    monkeypatch.setattr(compression_status, "read_host_config", lambda: {})
    monkeypatch.setattr(config, "operator_clawchat_extra", lambda: {})
    monkeypatch.setattr(host_version, "resolve_host_agent_version", lambda: "0.21.7")
    initialize = Mock(side_effect=AssertionError("status must not initialize storage"))
    monkeypatch.setattr(storage.ClawChatStore, "initialize", initialize)
    yield home
    initialize.assert_not_called()
    assert not home.exists()


def test_status_compression_snapshot(status_profile, capsys):
    from clawchat_cli import main

    assert main(["status"]) == 0
    captured = capsys.readouterr()
    actual = json.loads(captured.out)["compression"]
    expected = {
        "host_version": "0.21.7",
        "host_compression_enabled": True,
        "threshold_tokens": 150000,
        "threshold_tokens_source": "plugin_fill",
        "host_threshold_tokens": None,
        "plugin_session_cap_tokens": 150000,
        "plugin_session_cap_set": False,
        "host_threshold_ratio": None,
        "ratio_applies": (
            "Hermes compresses at the lower of threshold_tokens and the ratio threshold "
            "of the active model's window (default ratio 0.5, at least 0.75 below a 512K "
            "window); the ratio point depends on the model and is not computed here."
        ),
        "sediment_margin_tokens": 10000,
        "sediment_point_tokens": 140000,
        "notes": [],
    }
    assert actual == expected
    assert list(actual) == list(expected)
    assert captured.err == ""


@pytest.mark.parametrize("extra,notes", [({}, []), ({"session-cap-tokens": 150000}, []),
                                        ({"session-cap-tokens": 200000}, [OVERRIDE_NOTE])])
def test_status_host_cap_warning(status_profile, monkeypatch, capsys, extra, notes):
    from clawchat_cli import main

    monkeypatch.setattr(compression_status, "read_host_config",
                        lambda: {"compression": {"threshold_tokens": 150000}})
    monkeypatch.setattr(config, "operator_clawchat_extra", lambda: extra)
    assert main(["status"]) == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)["compression"]
    assert result["threshold_tokens_source"] == "host"
    assert result["notes"] == notes
    assert captured.err == (f"clawchat: {OVERRIDE_NOTE}\n" if notes else "")


def test_status_local_build(status_profile, monkeypatch, capsys):
    from clawchat_cli import main

    monkeypatch.setattr(host_version, "resolve_host_agent_version", lambda: "0.0.0")
    assert main(["status"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["compression"]["host_version"] == "unknown (local build)"
    assert captured.err == ""


def test_status_diagnostic_failure_is_json_and_silent(status_profile, monkeypatch, capsys, assert_no_secrets):
    from clawchat_cli import main

    def broken(*args, **kwargs):
        raise RuntimeError("placeholder-compression-secret")

    monkeypatch.setattr(compression_status, "describe_compression", broken)
    assert main(["status"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["compression"] == {"error": "RuntimeError"}
    assert captured.err == ""
    assert_no_secrets(captured.out, captured.err, secrets=("placeholder-compression-secret",))
