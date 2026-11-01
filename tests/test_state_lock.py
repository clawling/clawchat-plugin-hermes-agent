"""State mutations retain every concurrent writer's update."""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from clawchat_gateway import state_lock
from clawchat_gateway.state_lock import (
    StateLockTimeout, StateLockUnavailable, state_file_lock,
)

REPO = Path(__file__).resolve().parent.parent


def _child(code, home, *args):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CLAWCHAT_')}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(home),
               LOCALAPPDATA=str(home / 'AppData' / 'Local'), PYTHONPATH=str(REPO))
    return subprocess.Popen([sys.executable, '-c', code, *map(str, args)],
                            cwd=REPO, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _finish(children):
    try:
        for child in children:
            out, err = child.communicate(timeout=4)
            assert child.returncode == 0, out + err
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()


def test_reentrant_uses_one_descriptor_and_keeps_lock_file(tmp_path, monkeypatch):
    target = tmp_path / 'nested' / 'manifest.json'
    real_open = os.open
    opened = []

    def counted(*args):
        opened.append(args[0])
        return real_open(*args)

    monkeypatch.setattr(state_lock.os, 'open', counted)
    with state_file_lock(target):
        with state_file_lock(target):
            assert len(opened) == 1
    lock = target.parent / '.manifest.json.lock'
    assert lock.exists()
    if os.name != 'nt':
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_threads_wait_and_timeout(tmp_path):
    target = tmp_path / 'memory.md'
    waiting = threading.Event()
    entered = threading.Event()

    def writer(timeout):
        waiting.set()
        with state_file_lock(target, timeout=timeout):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with state_file_lock(target):
            future = pool.submit(writer, 0.1)
            with pytest.raises(StateLockTimeout):
                future.result(timeout=1)
            assert not entered.is_set()
            waiting.clear()
            future = pool.submit(writer, 1)
            assert waiting.wait(1)
            assert not entered.wait(0.05)
        future.result(timeout=1)
        assert entered.is_set()


def test_other_process_times_out_without_running_body(tmp_path):
    target = tmp_path / 'manifest.json'
    target.write_text('unchanged', encoding='utf-8')
    code = '''
import sys
from pathlib import Path
from clawchat_gateway.state_lock import state_file_lock, StateLockTimeout
path = Path(sys.argv[1])
try:
    with state_file_lock(path, timeout=0.15):
        path.write_text('lost', encoding='utf-8')
except StateLockTimeout as exc:
    assert path.name in str(exc)
    assert str(path.parent) not in str(exc)
else:
    raise AssertionError('lock body ran')
'''
    with state_file_lock(target):
        _finish([_child(code, tmp_path, target)])
    assert target.read_text(encoding='utf-8') == 'unchanged'


@pytest.mark.parametrize('failure', ['open', 'backend', 'import'])
def test_unavailable_never_runs_body(tmp_path, monkeypatch, failure):
    target = tmp_path / 'manifest.json'
    target.write_text('unchanged', encoding='utf-8')

    def unavailable(*args, **kwargs):
        if failure == 'import':
            raise ImportError('backend missing')
        raise OSError(errno.EIO, 'backend unavailable')

    with monkeypatch.context() as patch:
        if failure == 'open':
            patch.setattr(state_lock.os, 'open', unavailable)
        else:
            patch.setattr(state_lock, '_lock', unavailable)
        with pytest.raises(StateLockUnavailable, match='manifest.json') as caught:
            with state_file_lock(target):
                pytest.fail('body must not run')
        assert str(tmp_path) not in str(caught.value)
    assert target.read_text(encoding='utf-8') == 'unchanged'
    with state_file_lock(target, timeout=0.1):
        pass  # Failed acquisition releases the thread lock and descriptor.


_WORKER = '''
import sys
import time
from pathlib import Path
from clawchat_gateway import atomic_file, clawchat_memory, skill_update
from clawchat_gateway.clawchat_memory import write_clawchat_memory_body
from clawchat_gateway.skill_update import _set_local_version
from clawchat_gateway.runtime_defaults import configure_clawchat_allow_all
from clawchat_gateway.state_lock import state_file_lock
root, kind, worker = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
# Widen the lost-update window so this fails reliably without serialization.
def delayed(reader):
    def read(*args, **kwargs):
        result = reader(*args, **kwargs)
        time.sleep(0.002)
        return result
    return read
clawchat_memory._read_existing_content = delayed(clawchat_memory._read_existing_content)
skill_update.read_local_manifest = delayed(skill_update.read_local_manifest)
print('ready', flush=True)
sys.stdin.readline()
for i in range(12):
    label = f'worker-{worker}-note-{i}'
    if kind == 'memory':
        write_clawchat_memory_body(root, 'group', 'test_group', 'append', label)
    elif kind == 'manifest':
        _set_local_version(label, '1.0.0')
    elif worker == 0:
        configure_clawchat_allow_all()
    else:
        path = root / '.env'
        with state_file_lock(path):
            text = path.read_text(encoding='utf-8') if path.exists() else ''
            atomic_file.atomic_write_text(path, text + label + '=present\\n')
'''


@pytest.mark.parametrize('kind', ['memory', 'manifest', 'env'])
def test_process_updates_are_not_lost(tmp_path, kind):
    children = [_child(_WORKER, tmp_path, tmp_path, kind, worker) for worker in range(4)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == 'ready'
        for child in children:
            child.stdin.write('start\n')
            child.stdin.flush()
        _finish(children)
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.communicate()
    if kind == 'memory':
        from clawchat_gateway.clawchat_memory import read_clawchat_memory_file
        values = read_clawchat_memory_file(tmp_path, 'group', 'test_group')['body'].splitlines()
    elif kind == 'manifest':
        import json
        values = json.loads((tmp_path / 'clawchat-skills' / 'manifest.json').read_text())
    else:
        values = (tmp_path / '.env').read_text().splitlines()
        assert 'CLAWCHAT_ALLOW_ALL_USERS=true' in values
    for worker in range(0 if kind != 'env' else 1, 4):
        for i in range(12):
            label = f'worker-{worker}-note-{i}'
            assert (label + '=present' if kind == 'env' else label) in values


@pytest.mark.parametrize('operation', [
    'memory_write', 'memory_edit', 'memory_metadata', 'memory_delete',
    'manifest_set', 'skill_seed', 'skill_update', 'skill_removal', 'tombstones',
    'env_defaults', 'env_activation',
])
def test_mutators_propagate_lock_failure(tmp_path, monkeypatch, operation):
    from clawchat_gateway import activate, clawchat_memory, runtime_defaults, skill_update

    target = tmp_path / 'groups' / 'test_group.md'
    target.parent.mkdir()
    target.write_text('original', encoding='utf-8')
    env = tmp_path / '.env'
    env.write_text('OTHER=present\n', encoding='utf-8')
    monkeypatch.setattr(runtime_defaults, '_env_file', lambda: env)
    monkeypatch.setattr(activate, 'get_env_path', lambda: env)
    host_writes = []
    monkeypatch.setattr(activate, 'save_env_value', lambda *args: host_writes.append(args))
    monkeypatch.setattr(activate, 'remove_env_value', lambda *args: host_writes.append(args))

    def unavailable(*args, **kwargs):
        raise OSError(errno.EIO, 'locking unavailable')

    actions = {
        'memory_write': lambda: clawchat_memory.write_clawchat_memory_body(
            tmp_path, 'group', 'test_group', 'append', 'new'),
        'memory_edit': lambda: clawchat_memory.edit_clawchat_memory_body(
            tmp_path, 'group', 'test_group', 'original', 'new'),
        'memory_metadata': lambda: clawchat_memory.write_clawchat_metadata(
            tmp_path, 'group', 'test_group', {'group_title': 'new'}),
        'memory_delete': lambda: clawchat_memory.delete_clawchat_memory_file(
            tmp_path, 'group', 'test_group'),
        'manifest_set': lambda: skill_update._set_local_version('test-skill', '1.0.0'),
        'skill_seed': lambda: skill_update.seed_managed_skill('test-skill', tmp_path / 'bundled.md'),
        'skill_update': lambda: skill_update.apply_skill_update([]),
        'skill_removal': lambda: skill_update.apply_skill_removal([]),
        'tombstones': skill_update.apply_bundled_tombstones,
        'env_defaults': runtime_defaults.configure_clawchat_allow_all,
        'env_activation': lambda: activate._write_env_values({'OTHER': 'new', 'OLD': None}),
    }
    with monkeypatch.context() as patch:
        patch.setattr(state_lock, '_lock', unavailable)
        with pytest.raises(StateLockUnavailable):
            actions[operation]()
    assert target.read_text(encoding='utf-8') == 'original'
    assert env.read_text(encoding='utf-8') == 'OTHER=present\n'
    assert not host_writes


def test_logout_clear_fails_closed_without_lock(tmp_path, monkeypatch):
    from clawchat_gateway import activate

    env = tmp_path / '.env'
    env.write_text('OTHER=present\n', encoding='utf-8')
    monkeypatch.setattr(activate, 'get_env_path', lambda: env)
    host_writes = []
    monkeypatch.setattr(activate, 'save_env_value', lambda *args: host_writes.append(args))
    monkeypatch.setattr(activate, 'remove_env_value', lambda *args: host_writes.append(args))
    checks = []
    monkeypatch.setattr(activate, '_persisted_env_credentials_match',
                        lambda *args: checks.append(args) or True)

    def unavailable(*args, **kwargs):
        raise OSError(errno.EIO, 'locking unavailable')

    monkeypatch.setattr(state_lock, '_lock', unavailable)
    assert activate._clear_persisted_env_credentials(
        expected_access_token='a', expected_refresh_token='r') is False
    assert not checks and not host_writes


def test_logout_clear_checks_and_clears_under_one_lock(tmp_path, monkeypatch):
    from clawchat_gateway import activate

    env = tmp_path / '.env'
    monkeypatch.setattr(activate, 'get_env_path', lambda: env)
    held = []
    lock_path = tmp_path / '.env.lock'

    def match(*args):
        held.append(('check', lock_path.exists()))
        return True

    monkeypatch.setattr(activate, '_persisted_env_credentials_match', match)
    monkeypatch.setattr(activate, 'save_env_value', lambda *args: held.append(('write', args)))
    monkeypatch.setattr(activate, 'remove_env_value', lambda *args: None)
    entered = []
    real = state_lock.state_file_lock

    def spy(target, **kwargs):
        entered.append(Path(target))
        return real(target, **kwargs)

    monkeypatch.setattr(state_lock, 'state_file_lock', spy)
    assert activate._clear_persisted_env_credentials(
        expected_access_token='a', expected_refresh_token='r') is True
    assert held[0] == ('check', True)
    assert [item[0] for item in held[1:]] == ['write', 'write']
    assert entered[0] == env
