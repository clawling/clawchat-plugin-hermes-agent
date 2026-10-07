"""Appending a note paragraph that is already in the file must not add it again.

Several conversations (or one conversation after its context was compressed)
can each "discover" the same fact and append it. The append path compares by
paragraph (blank-line separated, surrounding whitespace ignored), skips the
paragraphs already present, and says so in the tool result so the model knows
nothing was lost.
"""

from __future__ import annotations

import asyncio

from clawchat_gateway import tools
from clawchat_gateway.clawchat_memory import (
    read_clawchat_memory_file,
    write_clawchat_memory_body,
)


def _body(root, target_type="group", target_id="cnv_g"):
    return read_clawchat_memory_file(root, target_type, target_id)["body"]


def test_identical_paragraph_is_skipped(tmp_path):
    write_clawchat_memory_body(tmp_path, "group", "cnv_g", "append", "Rule: no ads.")
    result = write_clawchat_memory_body(tmp_path, "group", "cnv_g", "append", "Rule: no ads.\n")
    assert _body(tmp_path).count("Rule: no ads.") == 1
    assert result["appended"] is False
    assert result["skipped_duplicate_paragraphs"] == 1


def test_only_new_paragraphs_are_appended(tmp_path):
    write_clawchat_memory_body(tmp_path, "group", "cnv_g", "append", "Rule: no ads.\n\nMeets Fridays.")
    result = write_clawchat_memory_body(
        tmp_path, "group", "cnv_g", "append", "  Meets Fridays.  \n\nTopic: hiking."
    )
    body = _body(tmp_path)
    assert body.count("Meets Fridays.") == 1
    assert "Topic: hiking." in body
    assert result["appended"] is True
    assert result["skipped_duplicate_paragraphs"] == 1


def test_similar_but_different_paragraph_is_kept(tmp_path):
    write_clawchat_memory_body(tmp_path, "user", "usr_a", "append", "Likes tea.")
    result = write_clawchat_memory_body(tmp_path, "user", "usr_a", "append", "Likes green tea.")
    assert "Likes green tea." in _body(tmp_path, "user", "usr_a")
    assert result["skipped_duplicate_paragraphs"] == 0


def test_replace_is_not_deduplicated(tmp_path):
    write_clawchat_memory_body(tmp_path, "owner", "owner", "append", "A.")
    result = write_clawchat_memory_body(tmp_path, "owner", "owner", "replace", "A.\n\nA.")
    assert _body(tmp_path, "owner", "owner") == "A.\n\nA."
    assert result["skipped_duplicate_paragraphs"] == 0


def test_tool_result_reports_skip(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "_resolve_memory_root", lambda: (tmp_path, None))
    first = asyncio.run(tools.memory_write("group", "cnv_g", mode="append", content="Rule: no ads."))
    assert first["ok"] is True
    second = asyncio.run(tools.memory_write("group", "cnv_g", mode="append", content="Rule: no ads."))
    assert second["ok"] is True
    assert second["skippedDuplicateParagraphs"] == 1
    assert "already" in second["note"]
