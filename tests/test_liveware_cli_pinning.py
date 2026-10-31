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


def test_stream_verified_install(cache, monkeypatch):
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
    assert list(cache.parent.iterdir()) == [cache]
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


@pytest.mark.parametrize('damage', ['missing', 'binary', 'not-executable'])
def test_invalid_cache_is_replaced(cache, monkeypatch, damage):
    if damage == 'not-executable' and os.name == 'nt':
        pytest.skip('POSIX executable mode required')
    fetch(monkeypatch, [Response(BODY), Response(BODY)])
    cli.ensure_liveware_cli()
    if damage == 'missing':
        cache.unlink()
    elif damage == 'not-executable':
        cache.chmod(0o600)
    else:
        cache.write_bytes(b'tampered-placeholder-binary')
    assert cli.resolve_liveware_path() is None
    cli.ensure_liveware_cli()
    assert cache.read_bytes() == BODY and cli.resolve_liveware_path() == str(cache)


def test_different_pins_reject_and_replace_each_others_binary(cache, monkeypatch):
    other_body = b'obvious-placeholder-other-cli-pin'
    pin_a = hashlib.sha256(BODY).hexdigest()
    pin_b = hashlib.sha256(other_body).hexdigest()
    calls = fetch(monkeypatch, [Response(other_body), Response(BODY), Response(other_body)])
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {ASSET: pin_b})
    cli.ensure_liveware_cli()
    assert cli.resolve_liveware_path() == str(cache)
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {ASSET: pin_a})
    assert cli.resolve_liveware_path() is None
    cli.ensure_liveware_cli()
    assert cache.read_bytes() == BODY
    assert cli.resolve_liveware_path() == str(cache)
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {ASSET: pin_b})
    assert cli.resolve_liveware_path() is None
    cli.ensure_liveware_cli()
    assert cache.read_bytes() == other_body
    assert cli.resolve_liveware_path() == str(cache)
    monkeypatch.setattr(cli, 'LIVEWARE_CLI_SHA256', {ASSET: pin_a})
    assert cli.resolve_liveware_path() is None
    assert len(calls) == 3


@pytest.mark.skipif(os.name == 'nt', reason='independent POSIX lock descriptions required')
def test_install_lock_is_held_during_download(cache, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Blocked(Response):
        def read(self, size):
            entered.set()
            assert release.wait(2)
            return super().read(size)
    fetch(monkeypatch, [Blocked(BODY)])
    monkeypatch.setattr(cli, '_INSTALL_LOCK_TIMEOUT', 0.03)
    installer = threading.Thread(target=cli.ensure_liveware_cli)
    installer.start()
    try:
        assert entered.wait(2)
        assert cli.resolve_liveware_path() is None
        fd = os.open(cache.parent / '.install.lock', os.O_RDWR)
        try:
            assert os.fstat(fd).st_mode & 0o777 == 0o600
            with pytest.raises(TimeoutError, match='install lock timed out'):
                cli._lock_install_file(fd)
        finally:
            os.close(fd)
    finally:
        release.set()
        installer.join(2)
    assert not installer.is_alive()
    assert cache.read_bytes() == BODY
    assert cli.resolve_liveware_path() == str(cache)
    fd = os.open(cache.parent / '.install.lock', os.O_RDWR)
    try:
        cli._lock_install_file(fd)
        cli._lock_install_file(fd, release=True)
    finally:
        os.close(fd)


@pytest.mark.skipif(os.name == 'nt', reason='independent POSIX lock descriptions required')
def test_install_lock_timeout_skips_download(cache, monkeypatch, caplog):
    cache.write_bytes(b'previous-placeholder')
    calls = fetch(monkeypatch, [])
    monkeypatch.setattr(cli, '_INSTALL_LOCK_TIMEOUT', 0.03)
    fd = os.open(cache.parent / '.install.lock', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        cli._lock_install_file(fd)
        cli.ensure_liveware_cli()
    finally:
        cli._lock_install_file(fd, release=True)
        os.close(fd)
    assert not calls
    assert cache.read_bytes() == b'previous-placeholder'
    assert cli.resolve_liveware_path() is None
    assert 'WARNING' in caplog.text and 'install lock timed out' in caplog.text


def test_cache_is_rechecked_after_acquiring_install_lock(cache, monkeypatch):
    cache.write_bytes(b'previous-placeholder')
    calls = fetch(monkeypatch, [])
    lock = cli._lock_install_file
    acquired = []
    def lock_after_other_install(fd, *, release=False):
        lock(fd, release=release)
        if not release:
            acquired.append(True)
            cache.write_bytes(BODY)
            cache.chmod(0o755)
    monkeypatch.setattr(cli, '_lock_install_file', lock_after_other_install)
    cli.ensure_liveware_cli()
    assert acquired == [True] and not calls
    assert cli.resolve_liveware_path() == str(cache)


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
    from hermes_source import require_hermes_source
    source = require_hermes_source('tools/plugin_guard.py')
    scanner = source if tag is None else scan._pinned_scanner(source, tag, tmp_path / 'scanners')
    if scanner is None:
        pytest.skip('pinned scanner tag unavailable locally')
    tree = scan._copy_tracked_tree(tmp_path)
    assert tree is not None
    # Include allowlisted new tests even before the author stages them.
    for name in ('test_liveware_cli_pinning.py', 'test_credential_file_modes.py',
                 'test_refresh_redaction.py', 'test_token_refresh_s_fixes.py',
                 'test_liveware_intro_retry.py', 'test_liveware_skill_reseed.py'):
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


def test_access_time_change_during_hash_does_not_invalidate(cache, monkeypatch):
    cache.write_bytes(BODY)
    cache.chmod(0o755)
    real_stat = cli.Path.stat
    calls = {'n': 0}

    def bumping_stat(self, *args, **kwargs):
        info = real_stat(self, *args, **kwargs)
        if self == cache:
            calls['n'] += 1
            values = list(info)
            values[7] += calls['n']  # st_atime moves on every read, as with atime updates
            return os.stat_result(values, {
                name: getattr(info, name) for name in ('st_atime_ns', 'st_mtime_ns', 'st_ctime_ns')
            })
        return info

    monkeypatch.setattr(cli.Path, 'stat', bumping_stat)
    assert cli._verified_local(cache, ASSET, cached=False)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX rename semantics')
def test_external_same_size_same_mtime_replacement_is_rehashed(cache):
    cache.write_bytes(BODY)
    cache.chmod(0o755)
    assert cli.resolve_liveware_path() == str(cache)
    original = cache.stat()
    other = cache.with_name('swap')
    other.write_bytes(b'X' * len(BODY))  # same length, wrong bytes
    other.chmod(0o755)
    os.utime(other, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(other, cache)
    assert cache.stat().st_size == original.st_size
    assert cache.stat().st_mtime_ns == original.st_mtime_ns
    assert cli.resolve_liveware_path() is None
