"""Plugin environment mutations compose with the host's independent file lock."""

from __future__ import annotations

import errno
import os
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import activate, config, runtime_defaults
from clawchat_gateway.state_lock import (
    StateLockError,
    StateLockTimeout,
    env_file_lock,
    env_lock_path,
    state_file_lock,
)


@contextmanager
def host_env_lock(env_path, *, timeout=0.5):
    """Open a fresh descriptor and lock the host's byte range or flock inode."""
    lock_path = env_path.with_name(env_path.name + ".lock")
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt" and lock_path.stat().st_size == 0:
            try:
                lock_file.write(b" ")
                lock_file.flush()
            except PermissionError:
                pass  # Another Windows holder may already lock byte zero.

        def kernel_lock(*, release=False):
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(
                    lock_file.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1,
                )
            else:
                import fcntl

                fcntl.flock(
                    lock_file.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB,
                )

        deadline = time.monotonic() + timeout
        while True:
            try:
                kernel_lock()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("host environment lock timed out") from exc
                time.sleep(0.01)
        try:
            yield
        finally:
            kernel_lock(release=True)


def read_env(env_path):
    return dict(line.split("=", 1) for line in env_path.read_text(encoding="utf-8").splitlines())


@pytest.fixture
def host_writers(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    monkeypatch.setattr(activate, "get_env_path", lambda: env_path)
    monkeypatch.setattr(config, "hermes_home", lambda: tmp_path)

    def update(key, value):
        with host_env_lock(env_path):
            values = read_env(env_path) if env_path.exists() else {}
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
            env_path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")

    monkeypatch.setattr(activate, "save_env_value", update)
    monkeypatch.setattr(activate, "remove_env_value", lambda key: update(key, None))
    return env_path


def test_plugin_lock_nests_with_host_lock(tmp_path):
    env_path = tmp_path / ".env"
    assert env_lock_path(env_path) == tmp_path / ".clawchat-env.lock"
    assert env_lock_path(env_path) != env_path.with_name(env_path.name + ".lock")
    with env_file_lock(env_path):
        assert env_lock_path(env_path).exists()
        with env_file_lock(env_path):
            with host_env_lock(env_path):
                pass


def test_activation_writes_and_removes_values_with_host_lock(host_writers):
    host_writers.write_text("OTHER=present\nOLD=remove-me\n", encoding="utf-8")
    assert activate._write_env_values({
        "CLAWCHAT_TOKEN": "placeholder-activation-access", "OLD": None,
    }) == host_writers
    assert read_env(host_writers) == {"OTHER": "present", "CLAWCHAT_TOKEN": "placeholder-activation-access"}


def test_token_rotation_writes_env_with_host_lock(host_writers, monkeypatch):
    host_writers.write_text(
        "CLAWCHAT_TOKEN=placeholder-old-access\nCLAWCHAT_REFRESH_TOKEN=placeholder-old-refresh\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(activate, "get_clawchat_store", lambda: SimpleNamespace(
        update_activation_tokens=lambda **kwargs: True,
    ))
    rotated = ("placeholder-rotated-access", "placeholder-rotated-refresh")
    old = ("placeholder-old-access", "placeholder-old-refresh")
    assert activate.persist_rotated_tokens(
        access_token=rotated[0], refresh_token=rotated[1],
        expected_access_token=old[0], expected_refresh_token=old[1],
        seed_if_missing=True,
    ) is True
    assert read_env(host_writers) == {
        "CLAWCHAT_TOKEN": "placeholder-rotated-access", "CLAWCHAT_REFRESH_TOKEN": "placeholder-rotated-refresh",
    }


@pytest.mark.parametrize("matched_clear", [False, True])
def test_logout_clears_credentials_with_host_lock(host_writers, matched_clear):
    host_writers.write_text(
        "OTHER=present\nCLAWCHAT_TOKEN=placeholder-old-access\nCLAWCHAT_REFRESH_TOKEN=placeholder-old-refresh\n",
        encoding="utf-8",
    )
    if matched_clear:
        assert activate._match_and_clear_env_credentials(
            "placeholder-old-access", "placeholder-old-refresh",
        ) == (True, True)
    else:
        old = ("placeholder-old-access", "placeholder-old-refresh")
        assert activate._clear_persisted_env_credentials(
            expected_access_token=old[0], expected_refresh_token=old[1],
        ) is True
    assert read_env(host_writers) == {"OTHER": "present", "CLAWCHAT_TOKEN": "", "CLAWCHAT_REFRESH_TOKEN": ""}


def test_allow_all_holds_plugin_then_host_lock(tmp_path, monkeypatch):
    from hermes_cli import config as host_config

    env_path = tmp_path / ".env"
    env_path.write_text("OTHER=present\nCLAWCHAT_ALLOW_ALL_USERS=false\n", encoding="utf-8")
    monkeypatch.setattr(runtime_defaults, "_env_file", lambda: env_path)
    held = []

    @contextmanager
    def checked_lock(path):
        assert path == env_path
        assert env_lock_path(path).exists()
        with host_env_lock(path):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    write = runtime_defaults.atomic_file.atomic_write_text
    read = Path.read_text

    def checked_read(path, *args, **kwargs):
        assert held
        return read(path, *args, **kwargs)

    def checked_write(*args, **kwargs):
        assert held
        return write(*args, **kwargs)

    monkeypatch.setattr(host_config, "_env_write_lock", checked_lock, raising=False)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", checked_read)
        patch.setattr(runtime_defaults.atomic_file, "atomic_write_text", checked_write)
        assert runtime_defaults.configure_clawchat_allow_all() is True
        assert runtime_defaults.configure_clawchat_allow_all() is False
    assert read_env(env_path) == {"OTHER": "present", "CLAWCHAT_ALLOW_ALL_USERS": "true"}


def test_allow_all_host_lock_timeout_preserves_env(tmp_path, monkeypatch):
    from hermes_cli import config as host_config

    env_path = tmp_path / ".env"
    original = "OTHER=present\nCLAWCHAT_ALLOW_ALL_USERS=false\n"
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(runtime_defaults, "_env_file", lambda: env_path)
    monkeypatch.setattr(host_config, "_env_write_lock", host_env_lock, raising=False)
    with host_env_lock(env_path):
        with pytest.raises(StateLockError) as caught:
            runtime_defaults.configure_clawchat_allow_all()
        assert isinstance(caught.value, StateLockTimeout)
        assert isinstance(caught.value.__cause__, TimeoutError)
    assert env_path.read_text(encoding="utf-8") == original
    # The failed host acquisition also releases the outer plugin lock.
    assert runtime_defaults.configure_clawchat_allow_all() is True


@pytest.mark.parametrize("missing", ["import", "attribute"])
def test_allow_all_supports_hosts_without_env_lock(tmp_path, monkeypatch, missing):
    import builtins
    from hermes_cli import config as host_config

    env_path = tmp_path / ".env"
    monkeypatch.setattr(runtime_defaults, "_env_file", lambda: env_path)
    if missing == "attribute":
        monkeypatch.delattr(host_config, "_env_write_lock", raising=False)
    else:
        real_import = builtins.__import__

        def import_without_host(name, *args, **kwargs):
            if name == "hermes_cli":
                raise ImportError("host config unavailable")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", import_without_host)
    assert runtime_defaults.configure_clawchat_allow_all() is True
    assert read_env(env_path) == {"CLAWCHAT_ALLOW_ALL_USERS": "true"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock conflicts across same-process descriptors")
def test_old_plugin_lock_name_conflicts_with_host(tmp_path):
    env_path = tmp_path / ".env"
    with state_file_lock(env_path):
        with pytest.raises(TimeoutError):
            with host_env_lock(env_path):
                pytest.fail("the old shared lock name must conflict")


def test_real_host_save_env_value_under_plugin_lock(monkeypatch):
    from conftest import _USING_HERMES_CLI_DOUBLE
    from hermes_cli import config as host_config

    if _USING_HERMES_CLI_DOUBLE or not hasattr(host_config, "_env_write_lock"):
        pytest.skip("real Hermes environment lock unavailable")
    env_path = Path(host_config.get_env_path())
    assert env_path.parent.resolve() == Path(os.environ["HERMES_HOME"]).resolve()
    key = "CLAWCHAT_ENV_LOCK_TEST"
    monkeypatch.delenv(key, raising=False)
    with env_file_lock(env_path):
        host_config.save_env_value(key, "placeholder-real-host-value")
    assert "placeholder-real-host-value" in env_path.read_text(encoding="utf-8")
