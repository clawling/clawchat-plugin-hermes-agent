"""State publication syncs bytes and permissions before replacing durable names."""

import errno
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from clawchat_gateway import atomic_file as af


def test_write_chmod_file_sync_replace_directory_sync(tmp_path, monkeypatch):
    target = tmp_path / "state"
    events = []
    fsync, replace, chmod, os_open = os.fsync, os.replace, os.chmod, os.open

    def record_chmod(path, mode):
        assert Path(path).read_bytes() == b"complete\n"
        events.extend(["write complete", "chmod"])
        return chmod(path, mode)

    def record_sync(fd):
        events.append("dir sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file sync")
        return fsync(fd)

    def record_replace(source, dest):
        events.append("replace")
        return replace(source, dest)

    def record_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            events.append("dir open")
        return os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(af.os, "chmod", record_chmod)
    monkeypatch.setattr(af.os, "fsync", record_sync)
    monkeypatch.setattr(af.os, "replace", record_replace)
    monkeypatch.setattr(af.os, "open", record_open)
    af.atomic_write_text(target, "complete\n", mode=0o600)
    assert target.read_bytes() == b"complete\n"
    expected = ["write complete", "chmod", "file sync", "replace", "dir open", "dir sync"]
    if os.name == "nt":
        expected = ["file sync", "replace"]
    assert events == expected


@pytest.mark.parametrize("failure", ["fsync", "replace", "interrupt"])
def test_prepublication_failure_preserves_target_and_cleans_temp(tmp_path, monkeypatch, failure):
    target = tmp_path / "state"
    target.write_bytes(b"previous")
    error = KeyboardInterrupt() if failure == "interrupt" else OSError(errno.EIO, "placeholder failure")

    def fail(*args):
        raise error

    monkeypatch.setattr(af.os, "replace" if failure == "replace" else "fsync", fail)
    with pytest.raises(type(error)) as raised:
        af.atomic_write_bytes(target, b"replacement")
    assert raised.value is error
    assert target.read_bytes() == b"previous"
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_default_mode_preserves_existing_and_new_is_private(tmp_path):
    existing, new = tmp_path / "existing", tmp_path / "new"
    existing.write_bytes(b"old")
    existing.chmod(0o640)
    af.atomic_write_bytes(existing, b"new")
    af.atomic_write_bytes(new, b"new")
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    af.atomic_write_bytes(existing, b"explicit private mode", mode=0o600)
    assert stat.S_IMODE(existing.stat().st_mode) == 0o600


@pytest.mark.parametrize("newline,expected", [("", b"a\nb\r\n"), ("\r\n", b"a\r\nb\r\r\n")])
def test_text_newline_bytes(tmp_path, newline, expected):
    path = tmp_path / "nested" / "state"
    af.atomic_write_text(path, "a\nb\r\n", newline=newline)
    assert path.read_bytes() == expected


@pytest.mark.skipif(os.name == "nt", reason="Windows directory fsync is intentionally skipped")
@pytest.mark.parametrize("operation", ["open", "fsync"])
@pytest.mark.parametrize("code", [errno.EINVAL, errno.EIO])
def test_directory_sync_only_swallows_unsupported(tmp_path, monkeypatch, operation, code):
    closed = []
    close = os.close

    def record_close(fd):
        closed.append(fd)
        close(fd)

    def fail(*args):
        raise OSError(code, "placeholder failure")

    monkeypatch.setattr(af.os, operation, fail)
    monkeypatch.setattr(af.os, "close", record_close)
    if code == errno.EINVAL:
        af.fsync_dir(tmp_path)
    else:
        with pytest.raises(OSError) as raised:
            af.fsync_dir(tmp_path)
        assert raised.value.errno == errno.EIO
    assert bool(closed) == (operation == "fsync")


def test_postpublication_directory_failure_is_reported(tmp_path, monkeypatch):
    target = tmp_path / "state"
    target.write_bytes(b"old")

    def fail(path):
        raise OSError(errno.EIO, "placeholder directory failure")

    monkeypatch.setattr(af, "fsync_dir", fail)
    with pytest.raises(OSError):
        af.atomic_write_bytes(target, b"published")
    assert target.read_bytes() == b"published"
    assert list(tmp_path.iterdir()) == [target]


@pytest.fixture
def writes(monkeypatch):
    calls, syncs = [], []
    write, fsync, sync_dir = af.atomic_write_text, af.os.fsync, af.fsync_dir

    def record_write(path, text, **kwargs):
        calls.append((Path(path), text, kwargs))
        return write(path, text, **kwargs)

    def record_sync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            syncs.append("file")
        return fsync(fd)

    def record_dir(path):
        syncs.append(("dir", Path(path)))
        return sync_dir(path)

    monkeypatch.setattr(af, "atomic_write_text", record_write)
    monkeypatch.setattr(af.os, "fsync", record_sync)
    monkeypatch.setattr(af, "fsync_dir", record_dir)
    return calls, syncs


@pytest.mark.parametrize("module,function", [("skill_update", "atomic_write_text"), ("clawchat_memory", "_atomic_write")])
def test_legacy_text_helpers_delegate(tmp_path, writes, module, function):
    import importlib

    path = tmp_path / "nested" / "state"
    getattr(importlib.import_module(f"clawchat_gateway.{module}"), function)(path, "raw\n")
    calls, syncs = writes
    assert calls == [(path, "raw\n", {"newline": ""})]
    assert syncs == ["file", ("dir", path.parent)]


def test_superseded_state_delegates_and_reports_failure(tmp_path, writes, monkeypatch):
    from clawchat_gateway import install_identity as identity

    value = {"reason": "placeholder"}
    assert identity._write_superseded_unlocked(tmp_path, value)
    calls, syncs = writes
    path = identity.superseded_path(tmp_path)
    assert calls == [(path, json.dumps(value, ensure_ascii=True), {"mode": 0o600})]
    assert syncs == ["file", ("dir", path.parent)]
    assert json.loads(path.read_text()) == value

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "placeholder failure")

    monkeypatch.setattr(af, "atomic_write_text", fail)
    assert identity._write_superseded_unlocked(tmp_path, value) is False


def test_reclaim_generation_delegates_and_propagates_failure(tmp_path, writes, monkeypatch):
    from clawchat_gateway import install_identity as identity

    identity._bump_reclaim_generation_unlocked(tmp_path)
    calls, syncs = writes
    path = identity._reclaim_generation_path(tmp_path)
    assert calls[0][0] == path
    assert calls[0][2] == {"encoding": "ascii", "mode": 0o600}
    assert path.read_text().endswith("\n")
    assert len(path.read_text().strip()) == 32
    assert syncs == ["file", ("dir", path.parent)]

    def fail(*args, **kwargs):
        raise OSError(errno.EIO, "placeholder failure")

    monkeypatch.setattr(af, "atomic_write_text", fail)
    with pytest.raises(OSError):
        identity._bump_reclaim_generation_unlocked(tmp_path)


def test_seed_is_create_only_and_syncs_directory_after_link(tmp_path, monkeypatch):
    from clawchat_gateway import install_identity as identity

    path = tmp_path / "clawchat" / "install_seed"
    events = []
    link, fsync, sync_dir = os.link, os.fsync, af.fsync_dir

    def record_link(source, destination):
        events.append("link")
        return link(source, destination)

    def record_sync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("file sync")
        return fsync(fd)

    def record_dir(parent):
        assert parent == path.parent
        events.append("dir sync")
        return sync_dir(parent)

    monkeypatch.setattr(identity.os, "link", record_link)
    monkeypatch.setattr(identity.os, "fsync", record_sync)
    monkeypatch.setattr(af, "fsync_dir", record_dir)
    identity._publish_seed(path)
    original = path.read_bytes()
    identity._publish_seed(path)
    assert path.read_bytes() == original
    assert events == ["file sync", "link", "dir sync"] * 2
    assert list(path.parent.iterdir()) == [path]


async def test_adapter_migration_marker_uses_helper(tmp_path, writes, monkeypatch):
    from clawchat_gateway import adapter

    monkeypatch.setattr(adapter, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(adapter.memory_migration, "scan_global_memory", lambda *a, **k: None)
    fake = SimpleNamespace(_await_owner_metadata_refreshed=AsyncMock(), _known_person_names=lambda: [])
    await adapter.ClawChatAdapter._maybe_hint_memory_migration(fake)
    marker = adapter.memory_migration.marker_path(tmp_path)
    calls, syncs = writes
    assert calls[0][0] == marker
    assert marker.read_text().isdigit()
    assert syncs == ["file", ("dir", marker.parent)]


def test_runtime_defaults_preserves_env_content_and_mode(tmp_path, writes, monkeypatch):
    from clawchat_gateway import runtime_defaults as defaults

    path = tmp_path / ".env"
    path.write_text("OTHER=value\nCLAWCHAT_ALLOW_ALL_USERS=false\n")
    if os.name != "nt":
        path.chmod(0o640)
    monkeypatch.setattr(defaults, "_env_file", lambda: path)
    assert defaults.configure_clawchat_allow_all()
    assert path.read_bytes() == b"OTHER=value\nCLAWCHAT_ALLOW_ALL_USERS=true\n"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert writes[0][0][2] == {"newline": ""}
    assert writes[1] == ["file", ("dir", path.parent)]
    assert not defaults.configure_clawchat_allow_all()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
def test_binary_download_syncs_permissions_before_publish(tmp_path, monkeypatch):
    from clawchat_gateway import liveware_cli as cli

    dest = tmp_path / "liveware"
    events = []
    fsync, replace, chmod = os.fsync, os.replace, os.chmod

    class InlineWorker:
        def __init__(self, *, target, args, **kwargs):
            self.run = lambda: target(*args)

        def start(self):
            self.run()

    def stream_download(fd, url, expected, deadline, timeout, cancelled, monotonic, result):
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"verified placeholder binary")
            stream.flush()
            os.fsync(stream.fileno())
        result.set_result(None)

    def record_sync(fd):
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            events.append("file sync")
        return fsync(fd)

    def record_chmod(path, mode):
        events.append("chmod")
        return chmod(path, mode)

    def record_replace(source, target):
        events.append("replace")
        return replace(source, target)

    sync_dir = af.fsync_dir

    def record_dir(path):
        assert path == tmp_path
        events.append("dir sync")
        return sync_dir(path)

    monkeypatch.setattr(cli.threading, "Thread", InlineWorker)
    monkeypatch.setattr(cli, "_stream_download", stream_download)
    monkeypatch.setattr(cli, "LIVEWARE_CLI_SHA256", {"placeholder": "placeholder-digest"})
    monkeypatch.setattr(cli.os, "fsync", record_sync)
    monkeypatch.setattr(cli.os, "chmod", record_chmod)
    monkeypatch.setattr(cli.os, "replace", record_replace)
    monkeypatch.setattr(af, "fsync_dir", record_dir)
    cli._download("https://example.invalid/placeholder", dest, monotonic=lambda: 0)
    assert events == ["file sync", "chmod", "file sync", "replace", "dir sync"]
    assert dest.read_bytes() == b"verified placeholder binary"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755


@pytest.mark.parametrize("fail_swap", [False, True])
def test_sample_syncs_user_data_and_rolls_back_failed_swap(tmp_path, writes, monkeypatch, fail_swap):
    from clawchat_gateway import liveware_sample as sample

    app = tmp_path / "app"
    app.mkdir()
    (app / "demo.js").write_bytes(b"old app")
    (app / "state.json").write_bytes(b"user data")
    stale = tmp_path / ".app.old"
    stale.mkdir()
    (stale / "demo.js").write_bytes(b"stale backup")
    if os.name != "nt":
        (app / "state.json").chmod(0o640)
    monkeypatch.setattr(sample, "_fetch_verified", lambda *a: b"manifest")
    monkeypatch.setattr(sample, "parse_livewares_manifest", lambda raw: SimpleNamespace(version="new", files=[]))
    monkeypatch.setattr(sample, "_local_install_matches", lambda *a: False)
    monkeypatch.setattr(sample, "_fetch_sample_files", lambda *a: [("demo.js", b"new app")])
    replace = Path.replace
    publications = []

    def record_replace(source, dest):
        publications.append(source.name)
        if source.name == ".app.tmp" and fail_swap:
            raise OSError(errno.EIO, "placeholder swap failure")
        return replace(source, dest)

    monkeypatch.setattr(Path, "replace", record_replace)
    if fail_swap:
        with pytest.raises(OSError):
            sample.download_liveware_sample(fetch=lambda url: b"", sample_root=tmp_path)
        assert (app / "demo.js").read_bytes() == b"old app"
        assert publications == ["app", ".app.tmp", ".app.old"]
    else:
        assert sample.download_liveware_sample(fetch=lambda url: b"", sample_root=tmp_path) == ("new", app)
        assert (app / "demo.js").read_bytes() == b"new app"
        assert publications == ["app", ".app.tmp"]
    assert (app / "state.json").read_bytes() == b"user data"
    if os.name != "nt":
        assert stat.S_IMODE((app / "state.json").stat().st_mode) == 0o640
    assert not (tmp_path / ".app.tmp").exists()
    assert not (tmp_path / ".app.old").exists()
    expected = ["file", "file", ("dir", tmp_path / ".app.tmp")]
    if not fail_swap:
        expected.append(("dir", tmp_path))
    assert writes[1] == expected


def test_sample_restores_retained_app_after_crash_between_swaps(tmp_path, monkeypatch):
    from clawchat_gateway import liveware_sample as sample

    retained = tmp_path / ".app.old"
    retained.mkdir()
    (retained / "demo.js").write_bytes(b"old app")
    (retained / "state.json").write_bytes(b"user data")
    monkeypatch.setattr(sample, "_fetch_verified", lambda *a: b"manifest")
    monkeypatch.setattr(sample, "parse_livewares_manifest", lambda raw: SimpleNamespace(version="new", files=[]))
    monkeypatch.setattr(sample, "_local_install_matches", lambda *a: False)
    monkeypatch.setattr(sample, "_fetch_sample_files", lambda *a: [("demo.js", b"new app")])
    app = tmp_path / "app"
    assert sample.download_liveware_sample(fetch=lambda url: b"", sample_root=tmp_path) == ("new", app)
    assert (app / "demo.js").read_bytes() == b"new app"
    assert (app / "state.json").read_bytes() == b"user data"
    assert not retained.exists()


def test_sample_restore_syncs_parent_before_matching_short_circuit(tmp_path, monkeypatch):
    from clawchat_gateway import liveware_sample as sample

    retained = tmp_path / ".app.old"
    retained.mkdir()
    (retained / "state.json").write_bytes(b"user data")
    events = []
    monkeypatch.setattr(sample, "_fetch_verified", lambda *a: b"manifest")
    monkeypatch.setattr(sample, "parse_livewares_manifest", lambda raw: SimpleNamespace(version="same", files=[]))

    def matches(app_dir, manifest):
        events.append("match")
        return True

    def sync_dir(path):
        events.append(("dir", path))

    monkeypatch.setattr(sample, "_local_install_matches", matches)
    monkeypatch.setattr(sample, "_fetch_sample_files", lambda *a: pytest.fail("matching install must not fetch"))
    monkeypatch.setattr(af, "fsync_dir", sync_dir)
    app = tmp_path / "app"
    assert sample.download_liveware_sample(fetch=lambda url: b"", sample_root=tmp_path) == ("same", app)
    assert (app / "state.json").read_bytes() == b"user data"
    assert not retained.exists()
    assert events == [("dir", tmp_path), "match"]


def test_sample_restore_directory_sync_failure_propagates(tmp_path, monkeypatch):
    from clawchat_gateway import liveware_sample as sample

    (tmp_path / ".app.old").mkdir()
    monkeypatch.setattr(sample, "_fetch_verified", lambda *a: b"manifest")
    monkeypatch.setattr(sample, "parse_livewares_manifest", lambda raw: SimpleNamespace(version="same", files=[]))
    monkeypatch.setattr(sample, "_local_install_matches", lambda *a: True)

    def failing(path):
        raise OSError(errno.EIO, "placeholder directory sync failure")

    monkeypatch.setattr(af, "fsync_dir", failing)
    with pytest.raises(OSError):
        sample.download_liveware_sample(fetch=lambda url: b"", sample_root=tmp_path)
