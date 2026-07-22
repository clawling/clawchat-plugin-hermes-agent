from __future__ import annotations

from pathlib import Path

from clawchat_gateway.storage import ClawChatStore


def test_relocates_legacy_default_db_and_wal(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    legacy = home / "clawchat.sqlite"
    legacy.write_bytes(b"OLD-DB")
    (home / "clawchat.sqlite-wal").write_bytes(b"OLD-WAL")

    target = home / "clawchat" / "clawchat.sqlite"
    store = ClawChatStore(target)
    store._relocate_legacy_db()

    assert target.read_bytes() == b"OLD-DB"
    assert (target.parent / "clawchat.sqlite-wal").read_bytes() == b"OLD-WAL"
    assert not legacy.exists()
    assert store.db_path == target


def test_no_legacy_leaves_named_profile_fresh(tmp_path):
    target = (
        tmp_path / ".hermes" / "profiles" / "coder" / "clawchat" / "clawchat-coder.sqlite"
    )
    store = ClawChatStore(target)
    store._relocate_legacy_db()
    assert not target.exists()  # nothing to relocate; initialize() creates it later
    assert store.db_path == target


def test_existing_target_is_not_overwritten(tmp_path):
    home = tmp_path / ".hermes"
    (home / "clawchat").mkdir(parents=True)
    legacy = home / "clawchat.sqlite"
    legacy.write_bytes(b"OLD")
    target = home / "clawchat" / "clawchat.sqlite"
    target.write_bytes(b"NEW")

    store = ClawChatStore(target)
    store._relocate_legacy_db()

    assert target.read_bytes() == b"NEW"
    assert legacy.read_bytes() == b"OLD"  # left untouched


def test_relocation_failure_falls_back_to_legacy(monkeypatch, tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir(parents=True)
    legacy = home / "clawchat.sqlite"
    legacy.write_bytes(b"OLD")
    target = home / "clawchat" / "clawchat.sqlite"
    store = ClawChatStore(target)

    def boom(self, _dst):
        raise OSError("cross-device move blocked")

    monkeypatch.setattr(Path, "replace", boom)
    store._relocate_legacy_db()

    assert store.db_path == legacy  # opened in place, no empty DB created
    assert not target.exists()
    assert legacy.read_bytes() == b"OLD"
