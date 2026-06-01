from __future__ import annotations

import argparse
import io
import shlex
from contextlib import redirect_stderr

from clawchat_gateway.activate import activate_and_maybe_restart
from clawchat_gateway.api_client import DEFAULT_BASE_URL
from clawchat_gateway.output_visibility import apply_output_visibility


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="/clawchat-activate",
        add_help=False,
        exit_on_error=False,
    )
    parser.add_argument("code", help="ClawChat activation code")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Skip the detached Hermes gateway restart after activation.",
    )
    return parser


def _usage(message: str | None = None) -> str:
    lines = ["usage: /clawchat-activate CODE [--base-url URL] [--no-restart]"]
    if message:
        lines.append(message)
    return "\n".join(lines)


def _output_usage(message: str | None = None) -> str:
    lines = ["usage: /clawchat-output minimal|normal|full"]
    if message:
        lines.append(message)
    return "\n".join(lines)


def _parse(raw_args: str) -> argparse.Namespace | str:
    try:
        argv = shlex.split(raw_args or "")
    except ValueError as exc:
        return _usage(str(exc))
    if not argv:
        return _usage("missing activation code")

    parser = _parser()
    stderr = io.StringIO()
    try:
        with redirect_stderr(stderr):
            return parser.parse_args(argv)
    except (argparse.ArgumentError, SystemExit) as exc:
        detail = str(exc) if isinstance(exc, argparse.ArgumentError) else stderr.getvalue().strip()
        return _usage(detail or "invalid arguments")


async def handle_clawchat_activate_command(raw_args: str) -> str:
    args = _parse(raw_args)
    if isinstance(args, str):
        return args

    payload = await activate_and_maybe_restart(
        args.code,
        base_url=args.base_url,
        restart=not args.no_restart,
    )
    lines = [f"clawchat: activation complete for {payload['user_id']}"]
    if payload.get("restart_scheduled"):
        lines.append(
            "clawchat: Hermes restart scheduled in "
            f"{payload.get('restart_delay_seconds')}s"
        )
    return "\n".join(lines)


async def handle_clawchat_output_command(raw_args: str) -> str:
    try:
        argv = shlex.split(raw_args or "")
    except ValueError as exc:
        return _output_usage(str(exc))
    if len(argv) != 1:
        return _output_usage("expected exactly one visibility mode")

    try:
        result = apply_output_visibility(argv[0])
    except ValueError as exc:
        return _output_usage(str(exc))

    mode = result["mode"]
    runtime_status = "on" if result["runtime_status_messages"] else "off"
    return (
        f"clawchat: output visibility set to {mode}\n"
        f"clawchat: runtime status messages {runtime_status}"
    )
