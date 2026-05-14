from __future__ import annotations

import argparse
import asyncio

from clawchat_gateway.activate import activate_and_maybe_restart
from clawchat_gateway.api_client import DEFAULT_BASE_URL


def setup_clawchat_cli(parser: argparse.ArgumentParser) -> None:
    subparsers = parser.add_subparsers(dest="command")
    activate_parser = subparsers.add_parser(
        "activate",
        help="Activate ClawChat credentials from an activation code",
    )
    activate_parser.add_argument("code", help="ClawChat activation code")
    activate_parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    activate_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip the detached Hermes gateway restart after activation.",
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

    payload = asyncio.run(
        activate_and_maybe_restart(
            args.code,
            base_url=args.base_url,
            restart=not args.no_restart,
        )
    )
    print(f"clawchat: activation complete for {payload['user_id']}")
    if payload.get("restart_scheduled"):
        print(
            "clawchat: Hermes restart scheduled in "
            f"{payload.get('restart_delay_seconds')}s"
        )
    return 0
