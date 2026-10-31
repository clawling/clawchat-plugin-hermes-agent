"""YAML reads follow Hermes' parser without requiring PyYAML at import time.

Pin host delegation, old-host PyYAML precedence, ruamel's YAML 1.1 policy,
missing-reader diagnostics, and the six config/frontmatter read paths.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from clawchat_gateway import _yaml
from hermes_source import require_hermes_source

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_TEXT = "platforms:\n  clawchat:\n    extra:\n      user_id: usr_yaml\n      flag: y\n"
EXTRA = {"user_id": "usr_yaml", "flag": "y"}


@pytest.fixture(autouse=True)
def yaml_env():
    _yaml._reset_backend_for_tests()
    try:
        with pytest.MonkeyPatch.context() as patches:
            yield patches
    finally:
        _yaml._reset_backend_for_tests()


@pytest.fixture
def pyyaml_backend(yaml_env):
    pytest.importorskip("yaml")
    yaml_env.setitem(sys.modules, "hermes_yaml", None)
    yaml_env.setitem(sys.modules, "ruamel.yaml", None)
    assert _yaml.backend_name() == "pyyaml"


@pytest.fixture
def config_path(tmp_path, yaml_env):
    yaml_env.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG_TEXT, encoding="utf-8")
    return path


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The upstream Hermes checkout is a read-only reference.
    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(sys, "dont_write_bytecode", True)
        spec.loader.exec_module(module)
    return module


def test_pyyaml_only_preserves_old_host_policy(pyyaml_backend):
    assert _yaml.safe_load("a: y") == {"a": "y"}
    assert _yaml.safe_load(b"a: yes") == {"a": True}
    assert _yaml.safe_load("a: 1\na: 2") == {"a": 2}


def test_pyyaml_precedes_installed_ruamel(yaml_env):
    pytest.importorskip("yaml")
    pytest.importorskip("ruamel.yaml")
    yaml_env.setitem(sys.modules, "hermes_yaml", None)
    assert _yaml.backend_name() == "pyyaml"
    assert _yaml.safe_load("a: y") == {"a": "y"}


def test_ruamel_only_uses_host_yaml_11_policy(yaml_env):
    pytest.importorskip("ruamel.yaml")
    yaml_error = importlib.import_module("ruamel.yaml.error").YAMLError
    yaml_env.setitem(sys.modules, "hermes_yaml", None)
    yaml_env.setitem(sys.modules, "yaml", None)
    assert _yaml.backend_name() == "ruamel"
    assert _yaml.safe_load("a: y") == {"a": True}
    assert _yaml.safe_load(b"a: off") == {"a": False}
    with pytest.raises(yaml_error) as error:
        _yaml.safe_load("a: 1\na: 2")
    assert type(error.value).__name__ == "DuplicateKeyError"
    assert _yaml.safe_load("[{url: http://h}]") == [{"url": "http://h"}]


def test_ruamel_retries_with_fresh_pure_parser(yaml_env):
    ruamel = pytest.importorskip("ruamel.yaml")
    yaml_error = importlib.import_module("ruamel.yaml.error").YAMLError
    yaml_env.setitem(sys.modules, "hermes_yaml", None)
    yaml_env.setitem(sys.modules, "yaml", None)
    parsers = []
    document = "[{url: http://h}]"

    class Parser:
        def __init__(self, *, typ, pure):
            assert typ == "safe"
            self.pure = pure
            parsers.append(self)

        def load(self, stream):
            assert self.version == (1, 1)
            assert stream == document
            if not self.pure:
                raise yaml_error("C parser rejection")
            return [{"url": "http://h"}]

    yaml_env.setattr(ruamel, "YAML", Parser)
    for _ in range(2):
        assert _yaml.safe_load(document) == [{"url": "http://h"}]
    assert [parser.pure for parser in parsers] == [False, True, False, True]
    assert len({id(parser) for parser in parsers}) == 4


def test_hermes_yaml_delegation_and_success_cache(yaml_env):
    host = types.ModuleType("hermes_yaml")
    calls = []
    sentinel = object()

    def load(stream):
        calls.append(stream)
        return sentinel

    host.safe_load = load
    yaml_env.setitem(sys.modules, "hermes_yaml", host)
    assert _yaml.backend_name() == "hermes_yaml"
    # A successful selection stays cached, even if imports later become unavailable.
    yaml_env.setitem(sys.modules, "hermes_yaml", None)
    assert _yaml.safe_load("a: y") is sentinel
    assert _yaml.safe_load(b"a: n") is sentinel
    assert calls == ["a: y", b"a: n"]


def test_real_hermes_yaml_policy(yaml_env):
    pytest.importorskip("ruamel.yaml")
    source = require_hermes_source("hermes_yaml.py") / "hermes_yaml.py"
    host = _load_module(source, "_clawchat_test_hermes_yaml")
    yaml_error = importlib.import_module("ruamel.yaml.error").YAMLError
    yaml_env.setitem(sys.modules, "hermes_yaml", host)
    assert _yaml.backend_name() == "hermes_yaml"
    assert _yaml.safe_load("a: y\nb: n") == {"a": True, "b": False}
    with pytest.raises(yaml_error):
        _yaml.safe_load("a: 1\na: 2")


@pytest.mark.parametrize("broken_name", ["hermes_yaml", "yaml"])
def test_non_import_error_during_import_falls_back(yaml_env, broken_name, caplog):
    if broken_name == "hermes_yaml":
        pytest.importorskip("yaml")
        expected = "pyyaml"
    else:
        pytest.importorskip("ruamel.yaml")
        yaml_env.setitem(sys.modules, "hermes_yaml", None)
        expected = "ruamel"
    real_import = importlib.import_module

    def broken_import(name, *args, **kwargs):
        if name == broken_name:
            raise RuntimeError("broken backend initialization")
        return real_import(name, *args, **kwargs)

    yaml_env.setattr(importlib, "import_module", broken_import)
    with caplog.at_level(logging.DEBUG, logger=_yaml.__name__):
        assert _yaml.backend_name() == expected
    assert "broken backend initialization" in caplog.text


def test_missing_backends_warn_once_and_retry_after_repair(yaml_env, caplog):
    for name in ("hermes_yaml", "yaml", "ruamel.yaml"):
        yaml_env.setitem(sys.modules, name, None)
    with caplog.at_level(logging.WARNING, logger=_yaml.__name__):
        for read in (lambda: _yaml.safe_load("a: 1"), _yaml.backend_name):
            with pytest.raises(_yaml.YamlUnavailableError) as error:
                read()
            for text in ("hermes_yaml", "PyYAML", "ruamel.yaml", sys.executable, "upgrade Hermes Agent"):
                assert text in str(error.value)
    warnings = [record for record in caplog.records if record.name == _yaml.__name__]
    assert len(warnings) == 1
    host = types.ModuleType("hermes_yaml")
    host.safe_load = lambda stream: {"repaired": True}
    yaml_env.setitem(sys.modules, "hermes_yaml", host)
    assert _yaml.safe_load("a: 1") == {"repaired": True}
    assert _yaml.backend_name() == "hermes_yaml"


def test_modules_import_without_any_yaml_library():
    # A fresh interpreter: reloading modules in-process would mint new exception
    # classes that other already-imported modules do not catch.
    script = (
        "import sys\n"
        "for name in ('hermes_yaml', 'yaml', 'ruamel', 'ruamel.yaml'):\n"
        "    sys.modules[name] = None\n"
        "import clawchat_gateway.profile, clawchat_gateway.restart\n"
        "import clawchat_gateway.profile_collision, clawchat_gateway.memory_scope\n"
        "import clawchat_gateway.skill_update\n"
        "from clawchat_gateway import _yaml\n"
        "try:\n"
        "    _yaml.safe_load('a: 1')\n"
        "except _yaml.YamlUnavailableError:\n"
        "    print('unavailable')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unavailable"


def test_missing_yaml_keeps_each_read_path_error_semantics(yaml_env, config_path):
    from clawchat_gateway import memory_scope, profile, profile_collision, restart, skill_update

    for name in ("hermes_yaml", "yaml", "ruamel.yaml"):
        yaml_env.setitem(sys.modules, name, None)
    with pytest.raises(_yaml.YamlUnavailableError):
        _yaml.safe_load(CONFIG_TEXT)
    with pytest.raises(profile.ProfileConfigError, match="ClawChat needs hermes_yaml, PyYAML or ruamel.yaml"):
        profile._load_yaml(config_path)
    assert restart._read_yaml(config_path) == {}
    assert profile_collision._clawchat_extra(config_path) == {}
    assert memory_scope._config_extra() == {}
    assert skill_update.parse_frontmatter("---\nversion: 1.2.3\n---\n") == {}
    entrypoint = _load_module(REPO_ROOT / "__init__.py", "_clawchat_yaml_entrypoint")
    assert entrypoint._clawchat_home_extra() == {}


def test_entrypoint_reads_config_extra(pyyaml_backend, config_path):
    entrypoint = _load_module(REPO_ROOT / "__init__.py", "_clawchat_yaml_entrypoint")
    assert entrypoint._clawchat_home_extra() == EXTRA


def test_profile_reads_config_extra(pyyaml_backend, config_path):
    from clawchat_gateway import profile

    assert profile._load_yaml(config_path)["platforms"]["clawchat"]["extra"] == EXTRA


def test_memory_scope_reads_config_extra(pyyaml_backend, config_path):
    from clawchat_gateway import memory_scope

    assert memory_scope._config_extra() == EXTRA


def test_restart_reads_config_extra(pyyaml_backend, config_path):
    from clawchat_gateway import restart

    assert restart._read_yaml(config_path)["platforms"]["clawchat"]["extra"] == EXTRA


def test_profile_collision_reads_config_extra(pyyaml_backend, config_path):
    from clawchat_gateway import profile_collision

    assert profile_collision._clawchat_extra(config_path) == EXTRA


def test_skill_update_reads_frontmatter_version(pyyaml_backend, tmp_path):
    from clawchat_gateway import skill_update

    path = tmp_path / "SKILL.md"
    path.write_text("---\nversion: 1.2.3\n---\nSkill body\n", encoding="utf-8")
    assert skill_update.skill_version(path) == "1.2.3"
