"""Scheduled (cron) delivery to ClawChat needs ``cron_deliver_env_var``.

Hermes pre-flights a cron job's ``deliver=<platform>`` target: a plugin
platform only counts as a valid destination when its ``PlatformEntry`` carries
``cron_deliver_env_var`` (the home-channel env var). The plugin never passed
it, so every job delivering to ClawChat ended ``blocked_config``. Activation
already writes ``CLAWCHAT_HOME_CHANNEL``.

Older hosts reject unknown ``PlatformEntry`` keys with ``TypeError``. The
fallback must be graded so a host that knows ``standalone_sender_fn`` but not
``cron_deliver_env_var`` keeps the sender: both -> without the cron var ->
without both.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ENTRY = Path(__file__).resolve().parent.parent / "__init__.py"


@pytest.fixture
def plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location("clawchat_plugin_entry_under_test", _ENTRY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    for name in (
        "_migrate_legacy_config_tokens",
        "_warn_on_shared_identity",
        "_patch_send_message_target_parser",
        "_patch_send_message_media_delivery",
        "_patch_media_delivery_accept_all_exts",
    ):
        monkeypatch.setattr(module, name, lambda: None)
    return module


class _Ctx:
    def __init__(self, known: set[str] | None) -> None:
        # ``known`` = optional keys this fake host's PlatformEntry accepts;
        # None = accepts everything (a current host).
        self.known = known
        self.calls: list[dict] = []
        self.accepted: dict | None = None

    def register_platform(self, **kwargs):
        self.calls.append(kwargs)
        optional = {"standalone_sender_fn", "cron_deliver_env_var"}
        if self.known is not None:
            unknown = (set(kwargs) & optional) - self.known
            if unknown:
                raise TypeError(f"unexpected keyword argument {sorted(unknown)[0]!r}")
        self.accepted = kwargs


def test_current_host_gets_the_cron_home_channel_and_the_sender(plugin):
    ctx = _Ctx(known=None)

    assert plugin._register_platform(ctx) is True

    assert len(ctx.calls) == 1
    assert ctx.accepted["cron_deliver_env_var"] == "CLAWCHAT_HOME_CHANNEL"
    assert ctx.accepted["standalone_sender_fn"] is plugin._clawchat_standalone_send


def test_host_without_cron_var_keeps_the_standalone_sender(plugin):
    ctx = _Ctx(known={"standalone_sender_fn"})

    plugin._register_platform(ctx)

    assert "cron_deliver_env_var" not in ctx.accepted
    assert ctx.accepted["standalone_sender_fn"] is plugin._clawchat_standalone_send


def test_oldest_host_registers_without_either(plugin):
    ctx = _Ctx(known=set())

    plugin._register_platform(ctx)

    assert ctx.accepted is not None
    assert "cron_deliver_env_var" not in ctx.accepted
    assert "standalone_sender_fn" not in ctx.accepted
    assert ctx.accepted["name"] == "clawchat"


def test_unrelated_type_error_on_the_last_attempt_propagates(plugin):
    class Broken:
        def register_platform(self, **_kwargs):
            raise TypeError("host bug")

    with pytest.raises(TypeError):
        plugin._register_platform(Broken())
