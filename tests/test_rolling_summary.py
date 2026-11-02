from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import os
import re
import time

import pytest

from clawchat_gateway import atomic_file, rolling_summary as summary
from clawchat_gateway.history_budget import BlockItem


VALID = "## Now\nIn progress.\n## Decided\nMeet at noon.\n## Open\n(none)\n## Index\n10-30 · owner · meeting · id m1"
EXAMPLE = "(e.g. 10-30 · owner · server address and login · id m123)"


@pytest.mark.parametrize("reply", [None, "", "## Now\n## Decided\n## Open", "## Decided\n## Now\n## Open\n## Index"])
def test_missing_or_unordered_headings_fail(reply):
    assert summary.parse_rolling_summary(reply) is None


def test_parser_starts_at_first_now_and_accepts_trailing_heading_spaces():
    assert summary.parse_rolling_summary("Preface\n\n" + VALID + "\n\n") == VALID
    assert summary.parse_rolling_summary(VALID.replace("## Open", "## Open  ")) == VALID.replace("## Open", "## Open  ")


@pytest.mark.parametrize("line", [EXAMPLE, "  " + EXAMPLE + "  ", "- " + EXAMPLE, "* " + EXAMPLE, "+ " + EXAMPLE])
def test_only_the_exact_template_line_is_removed(line):
    assert summary.parse_rolling_summary(VALID + "\n" + line) == VALID


def test_owner_parenthetical_lines_and_non_exact_example_survive():
    owner_lines = "\n(owner asked to wait)\n（先确认日期）\n" + EXAMPLE + " keep this\n(e.g. another example)"
    assert summary.parse_rolling_summary(VALID + owner_lines) == VALID + owner_lines


def test_over_limit_discards_oldest_index_lines_first():
    old = "old index clue " * 1000
    parsed = summary.parse_rolling_summary(VALID + "\n" + old + "\nnewest clue · id m9")
    assert parsed == VALID[:VALID.index("10-30")] + "newest clue · id m9"


def test_over_limit_removes_index_before_truncating_body_by_codepoint():
    reply = "## Now\n" + "😀" * 11000 + "\n## Decided\n(none)\n## Open\n(none)\n## Index\nold clue"
    parsed = summary.parse_rolling_summary(reply)
    assert len(parsed) == 10000
    assert parsed == reply[:9998].rstrip() + " …"
    assert "old clue" not in parsed
    parsed.encode("utf-8")


def test_build_messages_keeps_previous_summary_and_complete_body_in_user_role():
    text = "message body\n" + "detail " * 100
    block = [BlockItem("m1", "2026-10-30T14:03:00", "owner", "Owner", text),
             BlockItem("m2", "invalid", "other", "Guest\nName", "second body")]
    messages = summary.build_summary_messages(VALID, block)
    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[0]["content"] == summary.SUMMARY_SYSTEM_PROMPT
    assert VALID in messages[1]["content"] and VALID not in messages[0]["content"]
    assert f"- id m1, 10-30 14:03, Owner: {text}" in messages[1]["content"]
    assert "- id m2, Guest Name: second body" in messages[1]["content"]
    assert "ClawChat Conversation Summary" not in summary.build_summary_messages(None, block)[1]["content"]
    assert EXAMPLE in messages[0]["content"]
    assert "If a previous summary is given, start from it and fold in the new messages" in messages[0]["content"]
    assert "Clues only:" in messages[0]["content"] and "Facts only," in messages[0]["content"]
    assert "under 800 words" in messages[0]["content"]
    assert "Nothing from this turn is sent" not in messages[0]["content"]


def publish(directory, text=VALID, **kwargs):
    return summary.publish_summary(directory, text, **(dict(
        through="m1", boundary=None, as_of="2026-10-31T00:00:00Z",
    ) | kwargs))


def test_publish_and_read_header_roundtrip(tmp_path):
    ref = publish(tmp_path, through="message with --> delimiter", boundary="m0")
    assert re.fullmatch(r"\d{8}T\d{12}Z-[a-f0-9]{32}\.md", ref)
    header, body = summary.read_summary(tmp_path, ref)
    assert header == dict(as_of="2026-10-31T00:00:00Z", through="message with --> delimiter", boundary="m0")
    assert body == VALID
    first = (tmp_path / ref).read_text().splitlines()[0]
    assert first.startswith("<!-- clawchat-summary as_of=")
    assert first.count("-->") == 1
    assert not list(tmp_path.glob("*.tmp"))
    if os.name != "nt":
        assert (tmp_path / ref).stat().st_mode & 0o777 == 0o600


def test_datetime_as_of_is_stored_as_utc(tmp_path):
    value = datetime(2026, 10, 31, tzinfo=timezone.utc)
    ref = publish(tmp_path, as_of=value)
    assert summary.read_summary(tmp_path, ref)[0]["as_of"] == value.isoformat()


@pytest.fixture
def colliding_names(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 31, tzinfo=timezone.utc)

    monkeypatch.setattr(summary, "datetime", FrozenDatetime)
    monkeypatch.setattr(summary.secrets, "token_hex", lambda n: "a" * (n * 2))


def test_existing_target_never_overwritten(tmp_path, colliding_names):
    ref = publish(tmp_path, "first")
    before = (tmp_path / ref).read_bytes()
    with pytest.raises(FileExistsError):
        publish(tmp_path, "second")
    assert (tmp_path / ref).read_bytes() == before


def test_competing_publishers_cannot_replace_same_ref(tmp_path, colliding_names):
    def attempt(text):
        try:
            return publish(tmp_path, text), text
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["first", "second"]))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    ref, text = winners[0]
    assert summary.read_summary(tmp_path, ref)[1] == text


@pytest.mark.parametrize("failure", ["helper", "file_sync", "rename", "directory_sync"])
def test_failed_publication_leaves_no_new_formal_file(tmp_path, monkeypatch, failure):
    old_ref = publish(tmp_path)
    old_bytes = (tmp_path / old_ref).read_bytes()

    def fail(*args, **kwargs):
        raise OSError("simulated publication failure")

    if failure == "helper":
        monkeypatch.setattr(atomic_file, "atomic_write_text", fail)
    elif failure == "file_sync":
        monkeypatch.setattr(atomic_file.os, "fsync", fail)
    elif failure == "rename":
        monkeypatch.setattr(atomic_file.os, "replace", fail)
    else:
        monkeypatch.setattr(atomic_file, "fsync_dir", fail)
    with pytest.raises(OSError):
        publish(tmp_path, "new")
    assert list(tmp_path.glob("*.md")) == [tmp_path / old_ref]
    assert (tmp_path / old_ref).read_bytes() == old_bytes
    assert not list(tmp_path.glob("*.tmp"))


def test_read_rejects_missing_malformed_and_escaping_refs(tmp_path):
    assert summary.read_summary(tmp_path, "../outside.md") is None
    ref = "20261031T000000000000Z-" + "b" * 32 + ".md"
    assert summary.read_summary(tmp_path, ref) is None
    (tmp_path / ref).write_text("no header\nbody")
    assert summary.read_summary(tmp_path, ref) is None
    (tmp_path / ref).write_bytes(b"\xff")
    assert summary.read_summary(tmp_path, ref) is None


def test_cleanup_protects_current_previous_recent_and_unrelated_files(tmp_path):
    current, previous, orphan, recent = [publish(tmp_path) for _ in range(4)]
    old_time = time.time() - 7200
    for ref in (current, previous, orphan):
        os.utime(tmp_path / ref, (old_time, old_time))
    unrelated = tmp_path / "unrelated.md"
    unrelated.write_text("keep")
    os.utime(unrelated, (old_time, old_time))
    summary.cleanup_summaries(tmp_path, {current, previous})
    assert not (tmp_path / orphan).exists()
    assert all((tmp_path / ref).exists() for ref in (current, previous, recent))
    assert unrelated.exists()
    summary.cleanup_summaries(tmp_path / "absent", set())


def test_directory_segments_are_utf8_percent_escaped(tmp_path):
    path = summary.summary_dir(tmp_path, "account/a", "group:房间/a%")
    assert path == tmp_path / "account%2Fa" / "group%3A%E6%88%BF%E9%97%B4%2Fa%25"
    assert summary.summary_dir(tmp_path, "..", ".") == tmp_path / "%2E%2E" / "%2E"
    assert summary.summary_dir(tmp_path, "account_1", "chat.A-2") == tmp_path / "account_1" / "chat.A-2"
    with pytest.raises(ValueError):
        summary.summary_dir(tmp_path, "account", "")
