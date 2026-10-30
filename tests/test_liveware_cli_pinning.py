"""Only shipped, verified CLI bytes can enter the executable cache."""
import hashlib
import io
import os
import threading
from pathlib import Path

import pytest

from clawchat_gateway import liveware_cli as cli

BODY = b'obvious-placeholder-liveware-binary'
ASSET = 'liveware-linux-amd64'


class Response(io.BytesIO):
    status = 200


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(cli.shutil, 'which', lambda _: None)
    monkeypatch.setattr(cli, 'platform_assets', lambda: ASSET)
    monkeypatch.setattr(cli, 'liveware_binary_name', lambda: 'liveware')
    monkeypatch.setattr(cli, '_verified', None)
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {ASSET: hashlib.sha256(BODY).hexdigest()})
    cli.liveware_dir().mkdir(parents=True)
    return cli.liveware_dir() / 'liveware'


def fetch(monkeypatch, steps):
    calls = []
    def urlopen(request, timeout):
        calls.append((request, timeout))
        step = steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step
    monkeypatch.setattr(cli.urllib.request, 'urlopen', urlopen)
    return calls


def test_shipped_pins_and_windows_arm64():
    assert cli.LIVEWARE_CLI_VERSION == '0.0.47-20260921'
    assert len(cli.LIVEWARE_CLI_SHA256) == 6
    assert all(len(bytes.fromhex(value)) == 32 for value in cli.LIVEWARE_CLI_SHA256.values())
    assert cli.platform_assets('windows', 'arm64') == 'liveware-windows-arm64.exe'
    assert cli.platform_assets('unsupported', 'arm64') is None


def test_stream_verified_install_and_marker(cache, monkeypatch):
    seen_modes = []
    class Checked(Response):
        def read(self, size):
            assert size == 64 * 1024
            temps = [p for p in cache.parent.iterdir() if p != cache]
            seen_modes.extend(p.stat().st_mode & 0o777 for p in temps)
            return super().read(size)
    calls = fetch(monkeypatch, [Checked(BODY)])
    cli.ensure_liveware_cli()
    assert cache.read_bytes() == BODY
    if os.name != 'nt':
        assert seen_modes and set(seen_modes) == {0o600}
        assert cache.stat().st_mode & 0o777 == 0o755
    assert cli._marker(cache).read_text().strip() == f'{cli.LIVEWARE_CLI_VERSION} {hashlib.sha256(BODY).hexdigest()}'
    assert cli.resolve_liveware_path() == str(cache)
    cli.ensure_liveware_cli()
    assert len(calls) == 1 and calls[0][1] == 60


@pytest.mark.parametrize('failure', ['network', 'timeout', 'status', 'empty', 'digest'])
def test_retry_once_then_install(cache, monkeypatch, caplog, failure):
    bad = {'network': OSError('placeholder network error'), 'timeout': TimeoutError(),
           'status': Response(BODY), 'empty': Response(b''), 'digest': Response(b'wrong') }[failure]
    if failure == 'status':
        bad.status = 503
    calls = fetch(monkeypatch, [bad, Response(BODY)])
    sleeps = []
    cli._download(cli.LIVEWARE_BASE_URL + ASSET, cache, sleep=sleeps.append)
    assert len(calls) == 2 and sleeps == [2.0]
    assert cache.read_bytes() == BODY
    assert sorted(p.name for p in cache.parent.iterdir()) == ['liveware', 'liveware.version']
    if failure == 'digest':
        assert ASSET in caplog.text and 'expected=' in caplog.text and 'actual=' in caplog.text


def test_bad_download_never_executes_or_overwrites(cache, monkeypatch):
    cache.write_bytes(b'previous-placeholder')
    fetch(monkeypatch, [Response(b'wrong'), Response(b'wrong')])
    with pytest.raises(RuntimeError, match='digest mismatch'):
        cli._download(cli.LIVEWARE_BASE_URL + ASSET, cache, sleep=lambda _: None)
    assert cache.read_bytes() == b'previous-placeholder'
    assert list(cache.parent.iterdir()) == [cache]
    assert cli.resolve_liveware_path() is None


@pytest.mark.parametrize('damage', ['missing-marker', 'version', 'digest', 'binary', 'invalid-marker'])
def test_invalid_cache_is_replaced(cache, monkeypatch, damage):
    fetch(monkeypatch, [Response(BODY), Response(BODY)])
    cli.ensure_liveware_cli()
    if damage == 'missing-marker':
        cli._marker(cache).unlink()
    elif damage == 'version':
        cli._marker(cache).write_text('outdated ' + hashlib.sha256(BODY).hexdigest())
    elif damage == 'digest':
        cli._marker(cache).write_text(cli.LIVEWARE_CLI_VERSION + ' wrong')
    elif damage == 'invalid-marker':
        cli._marker(cache).write_bytes(b'\xff')
    else:
        cache.write_bytes(b'tampered-placeholder-binary')
    assert cli.resolve_liveware_path() is None
    cli.ensure_liveware_cli()
    assert cache.read_bytes() == BODY and cli.resolve_liveware_path() == str(cache)


def test_resolve_memoizes_by_metadata(cache, monkeypatch):
    fetch(monkeypatch, [Response(BODY)])
    cli.ensure_liveware_cli()
    original = cli.hashlib.file_digest
    hashes = []
    def digest(*args):
        hashes.append(1)
        return original(*args)
    monkeypatch.setattr(cli.hashlib, 'file_digest', digest)
    assert cli.resolve_liveware_path() == str(cache)
    assert cli.resolve_liveware_path() == str(cache)
    assert len(hashes) == 1
    info = cache.stat()
    os.utime(cache, ns=(info.st_atime_ns, info.st_mtime_ns + 1))
    assert cli.resolve_liveware_path() == str(cache)
    assert len(hashes) == 2


def test_path_wins_and_unpinned_asset_never_downloads(cache, monkeypatch, caplog):
    monkeypatch.setattr(cli.urllib.request, 'urlopen', lambda *_a, **_k: pytest.fail('network forbidden'))
    cache.write_bytes(b'old')
    monkeypatch.setattr(cli.shutil, 'which', lambda _: '/placeholder/liveware')
    cli.ensure_liveware_cli()
    assert cli.resolve_liveware_path() == 'liveware' and cache.read_bytes() == b'old'
    monkeypatch.setattr(cli.shutil, 'which', lambda _: None)
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {})
    cli.ensure_liveware_cli()
    cli._download(cli.LIVEWARE_BASE_URL + ASSET, cache)
    assert 'no pinned' in caplog.text and cli.resolve_liveware_path() is None


def test_size_cap_cleans_temp(cache, monkeypatch):
    monkeypatch.setattr(cli, '_DOWNLOAD_MAX_BYTES', 2)
    fetch(monkeypatch, [Response(BODY), Response(BODY)])
    with pytest.raises(RuntimeError, match='size cap'):
        cli._download(cli.LIVEWARE_BASE_URL + ASSET, cache, sleep=lambda _: None)
    assert not list(cache.parent.iterdir())


def test_total_deadline_fences_late_worker(cache, monkeypatch):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    class Blocked(Response):
        def read(self, size):
            entered.set()
            release.wait(2)
            return super().read(size)
        def __exit__(self, *args):
            try:
                return super().__exit__(*args)
            finally:
                exited.set()
    fetch(monkeypatch, [Blocked(BODY)])
    monkeypatch.setattr(cli, '_DOWNLOAD_DEADLINE', 0.1)
    try:
        with pytest.raises(TimeoutError, match='deadline'):
            cli._download(cli.LIVEWARE_BASE_URL + ASSET, cache)
        assert entered.is_set() and not list(cache.parent.iterdir())
    finally:
        release.set()
        assert exited.wait(2)
    # Synchronize cleanup by waiting for the worker's temp-file deletion.
    for thread in threading.enumerate():
        if thread.name == 'clawchat-liveware-download':
            thread.join(2)
    assert not list(cache.parent.iterdir())


def test_ensure_never_raises_on_install_failure(cache, monkeypatch, caplog):
    monkeypatch.setattr(cli, '_download', lambda *_: (_ for _ in ()).throw(OSError('placeholder failure')))
    cli.ensure_liveware_cli()
    assert 'download skipped' in caplog.text


@pytest.mark.parametrize('tag', [None, 'v2026.8.31', 'v2026.9.11', 'v2026.9.21', 'v0.21.6', 'v0.22.5'])
def test_new_payload_passes_install_scanner(tmp_path, tag):
    import shutil
    import test_install_scan as scan
    source = scan._hermes_source()
    if source is None:
        pytest.skip('no local Hermes scanner checkout')
    scanner = source if tag is None else scan._pinned_scanner(source, tag, tmp_path / 'scanners')
    if scanner is None:
        pytest.skip('pinned scanner tag unavailable locally')
    tree = scan._copy_tracked_tree(tmp_path)
    assert tree is not None
    # Include allowlisted new tests even before the author stages them.
    for name in ('test_liveware_cli_pinning.py', 'test_credential_file_modes.py',
                 'test_refresh_redaction.py', 'test_token_refresh_s_fixes.py',
                 'test_liveware_intro_retry.py'):
        shutil.copy2(Path(__file__).with_name(name), tree / 'tests' / name)
    result = scan._run_scan(scanner, tree)
    assert result is not None and not result['critical']
    if tag is None or tag in scan.HIGH_CLEAN_SCANNER_TAGS:
        assert not result['high']


async def test_background_api_starts_once_and_signals_completion(monkeypatch):
    monkeypatch.setattr(cli, '_started', False)
    monkeypatch.setattr(cli, '_done_event', threading.Event())
    installs, threads = [], []
    monkeypatch.setattr(cli, 'ensure_liveware_cli', lambda: installs.append(True))
    class Thread:
        def __init__(self, **kwargs):
            threads.append(kwargs)
        def start(self):
            threads[-1]['target']()
    monkeypatch.setattr(cli.threading, 'Thread', Thread)
    await cli.wait_liveware_cli_ready()
    cli.ensure_liveware_cli_background()
    cli.ensure_liveware_cli_background()
    await cli.wait_liveware_cli_ready()
    assert installs == [True] and len(threads) == 1
    assert threads[0]['daemon'] is True and cli._done_event.is_set()
