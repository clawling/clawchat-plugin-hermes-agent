"""Hermes' built-in memory must stop treating every ClawChat sender as "the user".

Hermes' ``memory`` tool, its ``USER PROFILE`` block and its background memory
review all assume one user per agent. A ClawChat agent talks to many people, so
following those defaults puts every person it meets into the global
``USER.md`` / ``MEMORY.md``. Two host keys turn that off:

* ``memory.user_profile_enabled: false`` — no USER.md target, no USER PROFILE
  block (``memory_enabled`` stays on: MEMORY.md is still the global notebook);
* ``memory.nudge_interval: 0`` — no background memory review (the skill review
  is a separate knob and stays on).

Activation writes both (overwrite). Plugin load fills them in only when missing,
so an existing install picks them up on its next start but an operator's own
value is never replaced.
"""

from __future__ import annotations


import pytest

from clawchat_gateway import activate


@pytest.fixture
def fake_config(monkeypatch, tmp_path):
    state: dict = {"config": {}, "writes": 0}

    def load():
        return tmp_path / "config.yaml", state["config"]

    def write(_path, config):
        state["config"] = config
        state["writes"] += 1

    monkeypatch.setattr(activate, "_load_config", load)
    monkeypatch.setattr(activate, "_write_config", write)
    monkeypatch.setattr(activate, "_write_env_values", lambda values: tmp_path / ".env")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return state


def test_activation_writes_memory_defaults(fake_config):
    fake_config["config"] = {"memory": {"user_profile_enabled": True, "nudge_interval": 10}}
    activate.persist_activation(
        access_token="tok",
        user_id="usr_agent",
        owner_user_id="usr_owner",
        refresh_token=None,
        base_url="https://example.invalid",
    )
    memory = fake_config["config"]["memory"]
    assert memory["user_profile_enabled"] is False
    assert memory["nudge_interval"] == 0
    # MEMORY.md stays the global notebook.
    assert "memory_enabled" not in memory or memory["memory_enabled"] is True


def test_load_fills_missing_memory_keys(fake_config):
    fake_config["config"] = {"platforms": {"clawchat": {"extra": {}}}}
    activate.ensure_clawchat_host_defaults_on_load()
    memory = fake_config["config"]["memory"]
    assert memory == {"user_profile_enabled": False, "nudge_interval": 0}
    assert fake_config["writes"] == 1


def test_load_keeps_operator_values(fake_config):
    fake_config["config"] = {
        "memory": {"user_profile_enabled": True, "nudge_interval": 5},
        "compression": {"threshold_tokens": 90000},
    }
    activate.ensure_clawchat_host_defaults_on_load()
    assert fake_config["config"]["memory"] == {"user_profile_enabled": True, "nudge_interval": 5}


def test_load_is_idempotent(fake_config):
    fake_config["config"] = {}
    activate.ensure_clawchat_host_defaults_on_load()
    writes = fake_config["writes"]
    activate.ensure_clawchat_host_defaults_on_load()
    assert fake_config["writes"] == writes


def test_load_never_raises(monkeypatch):
    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(activate, "_load_config", boom)
    activate.ensure_clawchat_host_defaults_on_load()


def test_load_leaves_existing_user_md_alone(fake_config, tmp_path):
    # The owner is asked about these entries in their direct chat instead
    # (tests/test_memory_migration_hint.py); load never touches the file.
    memories = tmp_path / "memories"
    memories.mkdir()
    content = "Ada likes tea\n§\nBo is a designer\n"
    (memories / "USER.md").write_text(content, encoding="utf-8")
    fake_config["config"] = {}
    activate.ensure_clawchat_host_defaults_on_load()
    assert (memories / "USER.md").read_text(encoding="utf-8") == content


def test_plugin_load_runs_host_defaults(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    entry = Path(__file__).resolve().parent.parent / "__init__.py"
    spec = importlib.util.spec_from_file_location("clawchat_entry_memory_defaults", entry)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    calls: list[str] = []
    monkeypatch.setattr(activate, "ensure_clawchat_host_defaults_on_load", lambda: calls.append("x"))
    for name in (
        "_migrate_legacy_config_tokens",
        "_warn_on_shared_identity",
        "_patch_send_message_target_parser",
        "_patch_send_message_media_delivery",
        "_patch_media_delivery_accept_all_exts",
    ):
        monkeypatch.setattr(module, name, lambda: None)

    class Ctx:
        def register_platform(self, **_kwargs):
            return None

    module._register_platform(Ctx())
    assert calls == ["x"]
