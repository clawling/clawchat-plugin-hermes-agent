from __future__ import annotations

import argparse
import asyncio
import sys

from clawchat_gateway.api_client import DEFAULT_BASE_URL, ClawChatApiError

activate_and_maybe_restart = None


def setup_clawchat_cli(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command")
    activate_parser = subparsers.add_parser(
        "activate",
        help="Activate ClawChat credentials from an activation code",
    )
    activate_parser.add_argument("code", help="ClawChat activation code")
    activate_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    activate_parser.add_argument(
        "--restart",
        action="store_true",
        help="Schedule a detached Hermes gateway restart after activation.",
    )
    activate_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Compatibility flag; activation does not restart by default.",
    )
    activate_parser.set_defaults(func=handle_clawchat_cli, _parser=parser)
    parser.set_defaults(_parser=parser)


def handle_clawchat_cli(args: argparse.Namespace) -> int:
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

    try:
        payload = asyncio.run(
            runner(
                args.code,
                base_url=args.base_url,
                restart=bool(args.restart and not args.no_restart),
            )
        )
    except ClawChatApiError as exc:
        details = f"{exc.kind}"
        if exc.path:
            details = f"{details} {exc.path}"
        if exc.status is not None:
            details = f"{details} status={exc.status}"
        if exc.code is not None:
            details = f"{details} code={exc.code}"
        print(f"clawchat: activation failed ({details}): {exc.message}", file=sys.stderr)
        return 1

    print(f"clawchat: activation complete for {payload['user_id']}")
    if payload.get("restart_scheduled"):
        print(
            "clawchat: Hermes restart scheduled in "
            f"{payload.get('restart_delay_seconds')}s"
        )
    return 0
