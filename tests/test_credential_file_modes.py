"""Credential files are private on POSIX and permission failures stay visible."""
import os
from pathlib import Path

import pytest

from clawchat_gateway import activate, storage

pytestmark = pytest.mark.skipif(os.name == 'nt', reason='POSIX modes')


@pytest.mark.parametrize('existing', [False, True])
def test_data_directory_database_and_wal_modes(tmp_path, existing, monkeypatch):
    directory = tmp_path / 'data'
    if existing:
        directory.mkdir(mode=0o755)
    store = storage.ClawChatStore(directory / 'test.sqlite')
    seen = []
    original = store._chmod_private
    def inspect_modes():
        original()
        for suffix in ('', '-wal', '-shm'):
            file = Path(str(store.db_path) + suffix)
            assert file.exists()
            seen.append(file.stat().st_mode & 0o777)
    monkeypatch.setattr(store, '_chmod_private', inspect_modes)
    store.initialize()
    assert not store._disabled and store._initialized
    assert directory.stat().st_mode & 0o777 == 0o700
    assert seen == [0o600] * 3


def test_chmod_failure_warns_once_and_storage_works(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(storage, '_mode_warning_emitted', False)
    def fail(*_args, **_kwargs):
        raise PermissionError('placeholder denial')
    monkeypatch.setattr(Path, 'chmod', fail)
    store = storage.ClawChatStore(tmp_path / 'data' / 'test.sqlite')
    store.initialize()
    store._chmod_private()
    assert store._initialized and not store._disabled
    warnings = [r for r in caplog.records if 'chmod failed' in r.message]
    assert len(warnings) == 1 and warnings[0].levelname == 'WARNING'
    assert str(store.db_path.parent) in warnings[0].message
    assert store._write('test', lambda conn: conn.execute('SELECT 1').fetchone()) == (1,)


def test_windows_skips_chmod(monkeypatch, tmp_path):
    path = tmp_path / 'placeholder'
    monkeypatch.setattr(storage.os, 'name', 'nt')
    monkeypatch.setattr(Path, 'chmod', lambda *_: pytest.fail('chmod on Windows'))
    storage._private_mode(path, 0o600)
    activate._warn_open_credentials(path)


@pytest.mark.parametrize('mode, warning', [(0o644, True), (0o600, False)])
def test_activation_warns_without_changing_env_mode(tmp_path, monkeypatch, caplog, mode, warning):
    env = tmp_path / '.env'
    env.write_text('placeholder credentials')
    env.chmod(mode)
    monkeypatch.setattr(activate, '_env_mode_warned', False)
    monkeypatch.setattr(activate, '_load_config', lambda: (tmp_path / 'config.yaml', {}))
    monkeypatch.setattr(activate, '_write_config', lambda *_: None)
    writes = []
    monkeypatch.setattr(activate, '_write_env_values', lambda values: (writes.append(values), env)[1])
    for _ in range(2):
        activate.persist_activation(access_token='placeholder-access', refresh_token='placeholder-refresh',
                                    user_id='test-agent', owner_user_id='test-owner', base_url='https://example.com')
    assert len(writes) == 2 and writes[0]['CLAWCHAT_TOKEN'] == 'placeholder-access'
    assert env.stat().st_mode & 0o777 == mode
    warnings = [r for r in caplog.records if 'readable by group/others' in r.message]
    assert len(warnings) == int(warning)
    if warning:
        assert f'chmod 600 {env}' in warnings[0].message
        assert 'placeholder-access' not in caplog.text and 'placeholder-refresh' not in caplog.text


def test_missing_or_unstatable_env_is_harmless(tmp_path, monkeypatch, caplog):
    activate._warn_open_credentials(tmp_path / 'absent')
    def denied(*_args, **_kwargs):
        raise PermissionError('placeholder denial')
    monkeypatch.setattr(Path, 'stat', denied)
    activate._warn_open_credentials(tmp_path / 'absent')
    assert not caplog.text
