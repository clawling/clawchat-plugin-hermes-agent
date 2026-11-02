"""Portable, manually calculated vectors for the desktop and plugin planners."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import config as cfg
from clawchat_gateway.history_budget import (
    Entry, estimate_entry_tokens, estimate_tokens, history_tokens,
    plan_compaction, recent_window_for_injection, split_blocks,
)


VECTORS = json.loads((Path(__file__).parent / "fixtures/history_budget_vectors.json").read_text())
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize("case", VECTORS["compaction"], ids=lambda case: case["name"])
def test_compaction_vectors(case):
    entries = [Entry(**entry) for entry in case["entries"]]
    fold, window = plan_compaction(
        entries, datetime.fromisoformat(case["now"]),
        timedelta(seconds=case["recent_window_s"]), case["keep_recent_turns"],
        case["window_cap_tokens"], case["fold_all"],
    )
    assert [entry.id for entry in fold] == case["expect_fold_ids"]
    assert [entry.id for entry in window] == case["expect_window_ids"]
    assert fold + window == entries


@pytest.mark.parametrize("case", VECTORS["estimate_tokens"], ids=lambda case: case["name"])
def test_estimate_vectors(case):
    assert estimate_tokens(case["text"]) == case["tokens"]


def test_entry_overhead_and_immutable_fields():
    entry = Entry("m", "invalid", "owner", "n", "中文ab")
    assert estimate_entry_tokens(entry) == 11
    with pytest.raises(FrozenInstanceError):
        entry.text = "changed"


@pytest.mark.parametrize("opened,context,last,expected", [
    (None, None, 50, None), (12, 20, None, None), (None, 20, 50, 30),
    (12, 20, 50, 42), (12, 20, 10, 12), (0, 0, 0, 0),
])
def test_history_usage(opened, context, last, expected):
    assert history_tokens(opened, context, last) == expected


def test_split_whole_entries_at_exact_boundary():
    entries = [Entry(str(i), "invalid", "other", "n", "x") for i in range(5)]
    blocks = split_blocks(entries, 18)
    assert [[item.id for item in block] for block in blocks] == [["0", "1"], ["2", "3"], ["4"]]
    assert all(not item.truncated for block in blocks for item in block)
    assert [item.text for block in blocks for item in block] == [entry.text for entry in entries]
    assert split_blocks([]) == []


def test_oversized_entry_occupies_block_and_keeps_both_ends():
    small = Entry("small", "invalid", "other", "n", "x")
    large = Entry("large", "invalid", "owner", "n", "开始" + "中" * 300 + "结尾")
    blocks = split_blocks([small, large, small], 120)
    assert [len(block) for block in blocks] == [1, 1, 1]
    item = blocks[1][0]
    assert item.truncated
    marker = "[middle omitted — chat_history_read around=large]"
    head, tail = item.text.split(marker)
    assert head.startswith("开始") and tail.endswith("结尾")
    assert large.text.startswith(head) and large.text.endswith(tail)
    assert abs(len(head) - len(tail)) <= 1
    assert estimate_entry_tokens(item) <= 120
    assert large.text == "开始" + "中" * 300 + "结尾"
    assert not blocks[0][0].truncated and not blocks[2][0].truncated
    with pytest.raises(ValueError):
        split_blocks([small], 0)


def test_injection_with_summary_keeps_contiguous_suffix_and_at_least_one():
    entries = [Entry(str(i), "invalid", "other", "n", "x") for i in range(3)]
    assert recent_window_for_injection(entries, 18, has_summary=True, now=NOW) == entries[-2:]
    assert recent_window_for_injection(entries, 1, has_summary=True, now=NOW) == entries[-1:]
    assert recent_window_for_injection([], 1, has_summary=True, now=NOW) == []
    oversized = Entry("big", "invalid", "owner", "n", "中" * 100)
    assert recent_window_for_injection([entries[0], oversized, entries[2]], 18, has_summary=True, now=NOW) == entries[-1:]


def test_injection_without_summary_uses_planner_window():
    case = VECTORS["compaction"][2]
    entries = [Entry(**entry) for entry in case["entries"]]
    assert [entry.id for entry in recent_window_for_injection(entries, 18, now=NOW)] == ["m4", "m5"]


@pytest.fixture
def make_config(monkeypatch):
    from clawchat_gateway import storage

    monkeypatch.setattr(storage, "get_clawchat_store", lambda: SimpleNamespace(
        get_revoked_refresh_fingerprint=lambda **kwargs: None,
    ))
    return lambda extra: cfg.ClawChatConfig.from_platform_config(SimpleNamespace(extra=extra))


def test_history_settings_factory_and_account_values(make_config):
    config = make_config({})
    dm = cfg.effective_history_settings(config, "dm", False)
    group = cfg.effective_history_settings(config, "group", True)
    assert dm.recent_window == timedelta(days=3)
    assert dm.keep_recent_turns == 8 and dm.budget_compaction
    assert dm.history_budget_tokens == 25000 and dm.idle_reset == timedelta(hours=24)
    assert group.history_budget_tokens == 15000 and group.idle_reset == timedelta(hours=6)
    config = make_config({"recent-window": "2天", "history-budget-dm": 30000,
                          "history-budget-group": "20000", "keep-recent-turns": "12",
                          "idle-reset-dm": "1.5h", "idle-reset-group": "off",
                          "budget-compaction": "off"})
    dm = cfg.effective_history_settings(config, "dm", False)
    assert dm.history_budget_tokens == 30000 and dm.recent_window == timedelta(days=2)
    assert dm.keep_recent_turns == 12 and dm.idle_reset == timedelta(minutes=90)
    assert not dm.budget_compaction
    assert cfg.effective_history_settings(config, "group", True).idle_reset is None
    with pytest.raises(FrozenInstanceError):
        dm.keep_recent_turns = 2


def test_conversation_override_chain_and_chat_key_filter(make_config):
    config = make_config({
        "history-budget-dm": 30000, "history-budget-group": 20000,
        "groups": {"*": {"keep-recent-turns": 5}, "group": {
            "history-budget-group": 45000, "recent-window": "4d", "group_mode": "mention",
        }},
        "chats": {"dm": {"history-budget-dm": 35000, "idle-reset-dm": False,
                         "budget-compaction": False, "group_mode": "off"}},
    })
    group = cfg.effective_history_settings(config, "group", True)
    assert group.history_budget_tokens == 45000 and group.keep_recent_turns == 5
    assert group.recent_window == timedelta(days=4)
    dm = cfg.effective_history_settings(config, "dm", False)
    assert dm.history_budget_tokens == 35000 and dm.idle_reset is None and not dm.budget_compaction
    assert cfg.effective_history_settings(config, "missing", False).history_budget_tokens == 30000
    assert cfg.effective_history_settings(config, "dm", True).history_budget_tokens == 20000
    assert "group_mode" not in config.chats["dm"]
    assert cfg.effective_group_mode(config, "group") == "mention"


@pytest.mark.parametrize("raw,minutes", [
    ("30m", 30), ("90m", 90), ("90min", 90), ("90mins", 90),
    ("90minute", 90), ("90minutes", 90), ("90分钟", 90), ("90分", 90),
    ("1.5h", 90), ("2hr", 120), ("2hrs", 120), ("2hour", 120),
    ("2hours", 120), ("6 小时", 360), ("6时", 360), ("2D", 2880),
    ("2day", 2880), ("2days", 2880), ("2天", 2880),
    ("1.49m", 1), ("1.5m", 2), (" 1 H ", 60),
    ("1.0083333333333333h", 61),
])
def test_dart_duration_grammar(raw, minutes):
    assert cfg._parse_history_duration(raw) == timedelta(minutes=minutes)


@pytest.mark.parametrize("key,value,field,expected", [
    ("history-budget-dm", 1, "history_budget_dm", 5000),
    ("history-budget-group", 999999, "history_budget_group", 200000),
    ("keep-recent-turns", 0, "keep_recent_turns", 1),
    ("keep-recent-turns", 99, "keep_recent_turns", 50),
    ("recent-window", "30m", "recent_window", timedelta(hours=1)),
    ("recent-window", "31d", "recent_window", timedelta(days=30)),
    ("idle-reset-dm", "0h", "idle_reset_dm", timedelta(hours=1)),
    ("idle-reset-group", "999999999999999d", "idle_reset_group", timedelta(days=30)),
])
def test_setting_clamps_and_warns(make_config, caplog, key, value, field, expected):
    config = make_config({key: value})
    assert getattr(config, field) == expected
    warnings = [record.getMessage() for record in caplog.records if "history setting" in record.getMessage()]
    assert len(warnings) == 1 and key in warnings[0] and "clamped" in warnings[0]
    shown = f"{int(expected.total_seconds())}s" if isinstance(expected, timedelta) else str(expected)
    assert f"using {shown}" in warnings[0]


@pytest.mark.parametrize("key,value,field,expected", [
    ("recent-window", "off", "recent_window", timedelta(days=3)),
    ("recent-window", False, "recent_window", timedelta(days=3)),
    ("idle-reset-dm", "1w", "idle_reset_dm", timedelta(hours=24)),
    ("history-budget-dm", True, "history_budget_dm", 25000),
    ("history-budget-group", 1.5, "history_budget_group", 15000),
    ("keep-recent-turns", None, "keep_recent_turns", 8),
    ("budget-compaction", "perhaps", "budget_compaction", True),
])
def test_bad_setting_factory_fallback(make_config, caplog, key, value, field, expected):
    assert getattr(make_config({key: value}), field) == expected
    warnings = [record.getMessage() for record in caplog.records if "history setting" in record.getMessage()]
    assert len(warnings) == 1 and key in warnings[0] and "invalid" in warnings[0]


def test_bad_override_uses_factory_and_never_logs_raw_value(make_config, caplog, assert_no_secrets):
    placeholder = "placeholder-invalid-history-secret"
    config = make_config({"history-budget-dm": 40000, "chats": {
        "dm": {"history-budget-dm": placeholder},
    }})
    assert cfg.effective_history_settings(config, "dm", False).history_budget_tokens == 25000
    assert_no_secrets(caplog=caplog, secrets=(placeholder,))
    assert "history-budget-dm invalid; using 25000" in caplog.text
