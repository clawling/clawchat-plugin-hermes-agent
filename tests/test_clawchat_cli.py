from __future__ import annotations

import argparse

from clawchat_gateway.api_client import DEFAULT_BASE_URL
from clawchat_gateway.cli import handle_clawchat_cli, setup_clawchat_cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes clawchat")
    setup_clawchat_cli(parser)
    return parser


def test_setup_clawchat_cli_parses_activate_command_defaults() -> None:
    args = _parser().parse_args(["activate", "ABC123"])

    assert args.command == "activate"
    assert args.code == "ABC123"
    assert args.base_url == DEFAULT_BASE_URL
    assert args.no_restart is False
    assert args.func is handle_clawchat_cli


def test_setup_clawchat_cli_parses_activate_options() -> None:
    args = _parser().parse_args(
        ["activate", "ABC123", "--base-url", "https://chat.example", "--no-restart"]
    )

    assert args.command == "activate"
    assert args.code == "ABC123"
    assert args.base_url == "https://chat.example"
    assert args.no_restart is True


def test_handle_clawchat_cli_activates_and_prints_restart(monkeypatch, capsys) -> None:
    from clawchat_gateway import cli as cli_mod

    calls = []

    async def fake_activate_and_maybe_restart(code: str, *, base_url: str, restart: bool):
        calls.append({"code": code, "base_url": base_url, "restart": restart})
        return {
            "user_id": "user-123",
            "restart_scheduled": True,
            "restart_delay_seconds": 2,
        }

    monkeypatch.setattr(
        cli_mod,
        "activate_and_maybe_restart",
        fake_activate_and_maybe_restart,
    )
    args = _parser().parse_args(["activate", "ABC123", "--base-url", "https://chat.example"])

    assert handle_clawchat_cli(args) == 0

    assert calls == [
        {"code": "ABC123", "base_url": "https://chat.example", "restart": True}
    ]
    assert capsys.readouterr().out.splitlines() == [
        "clawchat: activation complete for user-123",
        "clawchat: Hermes restart scheduled in 2s",
    ]


def test_handle_clawchat_cli_honors_no_restart(monkeypatch, capsys) -> None:
    from clawchat_gateway import cli as cli_mod

    calls = []

    async def fake_activate_and_maybe_restart(code: str, *, base_url: str, restart: bool):
        calls.append({"code": code, "base_url": base_url, "restart": restart})
        return {"user_id": "user-456"}

    monkeypatch.setattr(
        cli_mod,
        "activate_and_maybe_restart",
        fake_activate_and_maybe_restart,
    )
    args = _parser().parse_args(["activate", "ABC123", "--no-restart"])

    assert handle_clawchat_cli(args) == 0

    assert calls == [{"code": "ABC123", "base_url": DEFAULT_BASE_URL, "restart": False}]
    assert capsys.readouterr().out.splitlines() == [
        "clawchat: activation complete for user-456",
    ]
