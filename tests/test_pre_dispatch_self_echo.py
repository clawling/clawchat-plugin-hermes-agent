"""``pre_gateway_dispatch`` self-echo guard under a multiplexed gateway.

One Hermes process can host several profiles, each with its own ClawChat bot
account. The runner's ``config.platforms`` is the LAUNCH profile's config, while
each profile's adapter lives in ``runner._profile_adapters[profile][platform]``
and carries that profile's token-resolved ``user_id``. The hook must judge
self-echo against the bot of the profile the event belongs to — never the
launch profile's — and must not re-parse the launch config inside another
profile's secret scope (which logs a spurious "user_id mismatch" each turn).

Runs against the host double in ``conftest.py``; no Hermes or network needed.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent

BOT_A = "usr_bot_a"
BOT_B = "usr_bot_b"
HUMAN = "usr_human"


def _load_plugin_root():
    name = "clawchat_plugin_root_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name,
        _REPO_ROOT / "__init__.py",
        submodule_search_locations=[str(_REPO_ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_root()


def _jwt(sub: str) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'none'})}.{seg({'sub': sub})}.sig"


def _platform():
    from gateway.config import Platform

    return Platform.CLAWCHAT


def _adapter(user_id: str):
    return SimpleNamespace(_clawchat_config=SimpleNamespace(user_id=user_id))


def _launch_config(user_id: str):
    # The runner's config is the LAUNCH profile's config.yaml.
    return SimpleNamespace(
        platforms={_platform(): SimpleNamespace(extra={"user_id": user_id})}
    )


def _multiplexed_runner():
    return SimpleNamespace(
        config=_launch_config(BOT_A),
        adapters={_platform(): _adapter(BOT_A)},
        _profile_adapters={"b": {_platform(): _adapter(BOT_B)}},
        _primary_profile_name="a",
    )


def _event(sender: str, profile: str | None):
    return SimpleNamespace(
        source=SimpleNamespace(
            platform=_platform(), user_id=sender, chat_id="cnv_test", profile=profile
        )
    )


@pytest.fixture
def scoped_token_b(monkeypatch):
    # Inside profile B's runtime scope the visible token is B's.
    monkeypatch.setenv("CLAWCHAT_TOKEN", _jwt(BOT_B))
    monkeypatch.delenv("CLAWCHAT_USER_ID", raising=False)


def _mismatch_logged(caplog) -> bool:
    return any("user_id mismatch" in r.getMessage() for r in caplog.records)


def test_secondary_profile_own_echo_is_skipped_without_mismatch_warning(
    scoped_token_b, caplog
):
    caplog.set_level(logging.DEBUG)
    result = plugin._clawchat_pre_gateway_dispatch(
        event=_event(BOT_B, "b"), gateway=_multiplexed_runner()
    )
    assert result == {"action": "skip", "reason": "clawchat-self-echo"}
    assert not _mismatch_logged(caplog)


def test_secondary_profile_human_message_passes(scoped_token_b, caplog):
    caplog.set_level(logging.DEBUG)
    result = plugin._clawchat_pre_gateway_dispatch(
        event=_event(HUMAN, "b"), gateway=_multiplexed_runner()
    )
    assert result is None
    assert not _mismatch_logged(caplog)


def test_secondary_profile_does_not_drop_launch_bot_messages(scoped_token_b):
    # The launch profile's bot talking to profile B (e.g. both in one group) is
    # real input for B, not B's echo.
    result = plugin._clawchat_pre_gateway_dispatch(
        event=_event(BOT_A, "b"), gateway=_multiplexed_runner()
    )
    assert result is None


def test_secondary_profile_without_adapter_defers_to_adapter_check(scoped_token_b):
    runner = _multiplexed_runner()
    runner._profile_adapters = {"b": {}}
    result = plugin._clawchat_pre_gateway_dispatch(
        event=_event(BOT_A, "b"), gateway=runner
    )
    assert result is None


def test_launch_profile_unchanged(monkeypatch):
    monkeypatch.setenv("CLAWCHAT_TOKEN", _jwt(BOT_A))
    runner = _multiplexed_runner()
    for profile in ("a", None):
        assert plugin._clawchat_pre_gateway_dispatch(
            event=_event(BOT_A, profile), gateway=runner
        ) == {"action": "skip", "reason": "clawchat-self-echo"}
        assert (
            plugin._clawchat_pre_gateway_dispatch(
                event=_event(HUMAN, profile), gateway=runner
            )
            is None
        )


def test_non_multiplexed_host_unchanged(monkeypatch):
    # Older host: no _profile_adapters, no live adapter map — config only.
    monkeypatch.setenv("CLAWCHAT_TOKEN", _jwt(BOT_A))
    runner = SimpleNamespace(config=_launch_config(BOT_A))
    assert plugin._clawchat_pre_gateway_dispatch(
        event=_event(BOT_A, None), gateway=runner
    ) == {"action": "skip", "reason": "clawchat-self-echo"}
    assert (
        plugin._clawchat_pre_gateway_dispatch(event=_event(HUMAN, None), gateway=runner)
        is None
    )
