"""Import failures explain the Hermes environment and preserve their cause."""

from __future__ import annotations

import builtins
import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def isolated_activate_import(monkeypatch: pytest.MonkeyPatch):
    module_name = "clawchat_gateway.activate"
    original_module = importlib.import_module(module_name)
    try:
        with monkeypatch.context() as patch:
            patch.delitem(sys.modules, module_name)
            yield patch
    finally:
        # Restore the same module object used by other tests at collection time,
        # after restoring the real Hermes config or conftest's host double.
        assert importlib.import_module(module_name) is original_module


@pytest.mark.parametrize("missing_module", ["hermes_cli", "hermes_cli.config"])
def test_missing_hermes_module_explains_environment(isolated_activate_import, missing_module):
    patch = isolated_activate_import
    patch.delitem(sys.modules, "hermes_cli.config", raising=False)
    patch.setitem(sys.modules, missing_module, None)

    with pytest.raises(RuntimeError) as caught:
        importlib.import_module("clawchat_gateway.activate")

    message = str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, ModuleNotFoundError)
    assert cause.name in ("hermes_cli", "hermes_cli.config")
    assert f"import failed for {cause.name}." in message
    assert f"ModuleNotFoundError: {cause}" in message
    assert sys.executable in message
    assert "hermes clawchat activate <CODE>" in message
    assert "hermes -p <profile> clawchat activate <CODE>" in message
    assert "system/shell Python outside the Hermes venv" in message
    assert "too old or too new" in message
    assert "broken Hermes installation" in message
    assert "clawchat_cli.py activate <CODE>" in message
    assert "~/.hermes/hermes-agent/venv/bin/python" in message
    assert "/opt/hermes/.venv/bin/python in Docker" in message
    assert "Scripts\\python.exe on Windows" in message


def test_missing_config_helper_is_named(isolated_activate_import):
    config = ModuleType("hermes_cli.config")
    for name in (
        "get_config_path", "get_env_path", "read_raw_config", "remove_env_value", "save_config",
    ):
        setattr(config, name, lambda: None)
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.config = config
    isolated_activate_import.setitem(sys.modules, "hermes_cli", hermes_cli)
    isolated_activate_import.setitem(sys.modules, "hermes_cli.config", config)

    with pytest.raises(RuntimeError) as caught:
        importlib.import_module("clawchat_gateway.activate")

    message = str(caught.value)
    cause = caught.value.__cause__
    assert isinstance(cause, ImportError)
    assert "import failed for hermes_cli.config." in message
    assert "cannot import name 'save_env_value'" in message
    assert f"ImportError: {cause}" in message


@pytest.mark.parametrize(
    "original_error",
    [
        RuntimeError("Hermes config initialization failed"),
        ModuleNotFoundError("No module named 'hermes_dependency'", name="hermes_dependency"),
    ],
)
def test_config_import_preserves_original_exception(isolated_activate_import, original_error):
    original_import = builtins.__import__

    def import_with_broken_config(name, *args, **kwargs):
        if name == "hermes_cli.config":
            raise original_error
        return original_import(name, *args, **kwargs)

    isolated_activate_import.setattr(builtins, "__import__", import_with_broken_config)

    with pytest.raises(RuntimeError) as caught:
        importlib.import_module("clawchat_gateway.activate")

    assert caught.value.__cause__ is original_error
    message = str(caught.value)
    assert f"{type(original_error).__name__}: {original_error}" in message
    failed_module = (
        original_error.name if isinstance(original_error, ModuleNotFoundError) else "hermes_cli.config"
    )
    assert f"import failed for {failed_module}." in message
