from __future__ import annotations

import argparse
import asyncio
import sys

from clawchat_gateway.api_client import ClawChatApiError, format_connect_guidance, redact_display_text
from clawchat_gateway.config import resolve_activation_base_url
from clawchat_gateway.restart import format_restart_lines

activate_and_maybe_restart = None


def _status_user_id() -> str | None:
    """Resolve the current profile identity without initializing its database."""
    from types import SimpleNamespace
    from clawchat_gateway.config import ClawChatConfig, operator_clawchat_extra
    from clawchat_gateway.storage import ClawChatStore

    try:
        extra = operator_clawchat_extra() or {}
        cfg = ClawChatConfig.from_platform_config(
            SimpleNamespace(extra=extra), operator_extra=extra, read_only=True,
        )
        if cfg.user_id:
            return cfg.user_id
    except Exception:  # noqa: BLE001 — status remains available with unreadable config
        pass
    try:
        return ClawChatStore().get_activation_user_id(platform="hermes", account_id="default")
    except Exception:  # noqa: BLE001 — unknown identity keeps the fail-closed view
        return None


def setup_clawchat_cli(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command")
    activate_parser = subparsers.add_parser(
        "activate",
        help="Activate ClawChat credentials from an activation code",
    )
    activate_parser.add_argument("code", help="ClawChat activation code")
    activate_parser.add_argument(
        "--restart",
        action="store_true",
        help="Compatibility flag; activation restarts by default.",
    )
    activate_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip the detached Hermes gateway restart after activation.",
    )
    activate_parser.add_argument(
        "--new-account",
        action="store_true",
        help=(
            "Replace this profile's ClawChat identity with a brand-new agent "
            "instead of re-pairing the existing one."
        ),
    )
    activate_parser.add_argument(
        "--repair",
        action="store_true",
        help="Re-pair the agent this profile already holds (keeps its identity).",
    )
    activate_parser.set_defaults(func=handle_clawchat_cli, _parser=parser)
    for command in ("reclaim", "status"):
        sub = subparsers.add_parser(command, help=f"ClawChat installation {command}")
        sub.set_defaults(func=handle_clawchat_cli, _parser=parser)
    parser.set_defaults(_parser=parser)


def format_activation_failure(exc: ClawChatApiError) -> list[str]:
    """The ``activation failed (...)`` line, then any server guidance lines.

    The first line is unchanged from older releases so anything parsing it keeps
    working; ``next_action`` / ``hint`` lines follow only when the server sent them.
    """
    details = f"{exc.kind}"
    if exc.path:
        details = f"{details} {redact_display_text(exc.path)}"
    if exc.status is not None:
        details = f"{details} status={exc.status}"
    if exc.code is not None:
        details = f"{details} code={exc.code}"
    return [
        f"clawchat: activation failed ({details}): {redact_display_text(exc.message)}",
        *(redact_display_text(line) for line in format_connect_guidance(exc)),
    ]


def handle_clawchat_cli(args: argparse.Namespace) -> int:
    command = getattr(args, "command", None)
    if command in ("reclaim", "status"):
        import json
        from clawchat_gateway.hermes_home import hermes_home
        from clawchat_gateway.install_identity import clear_superseded, install_status, reclaim_message

        home = hermes_home()
        if command == "status":
            from clawchat_gateway.host_version import unsupported_host_message

            message = unsupported_host_message()
            if message is not None:
                print(message, file=sys.stderr)
            print(json.dumps(install_status(home, _status_user_id()), indent=2, ensure_ascii=False))
            return 0
        try:
            cleared = clear_superseded(home)
        except OSError as exc:
            print(f"clawchat: reclaim failed ({type(exc).__name__})", file=sys.stderr)
            return 1
        from clawchat_gateway.connection import ClawChatConnection
        ClawChatConnection.wake_reclaimed(home)
        print(reclaim_message(cleared))
        return 0
    if getattr(args, "command", None) != "activate":
        parser = getattr(args, "_parser", None)
        if parser is not None:
            parser.print_help()
        else:
            print(f"clawchat: unknown command: {getattr(args, 'command', None) or ''}".rstrip())
        return 2

    runner = activate_and_maybe_restart
    if runner is None:
        from clawchat_gateway.activate import activate_and_maybe_restart as runner

    from clawchat_gateway.activate import ExistingActivationError

    try:
        payload = asyncio.run(
            runner(
                args.code,
                base_url=resolve_activation_base_url(),
                restart=not args.no_restart,
                new_account=args.new_account,
                repair=args.repair,
            )
        )
    except ExistingActivationError as exc:
        # A precondition failure, not a server rejection: the connect code was
        # never spent, so the operator can still redeem it the right way.
        print(f"clawchat: activation refused — {redact_display_text(exc)}", file=sys.stderr)
        return 1
    except ClawChatApiError as exc:
        print("\n".join(redact_display_text(line) for line in format_activation_failure(exc)), file=sys.stderr)
        return 1

    print(f"clawchat: activation complete for {payload['user_id']}")
    for line in format_restart_lines(payload):
        print(line)
    return 0
