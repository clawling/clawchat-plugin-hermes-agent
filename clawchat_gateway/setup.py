from __future__ import annotations

import asyncio

from clawchat_gateway.activate import activate_and_maybe_restart
from clawchat_gateway.api_client import DEFAULT_BASE_URL


def _prompt(default: str, label: str) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def setup_clawchat_platform() -> None:
    print()
    print("  --- ClawChat Setup ---")
    code = input("ClawChat activation code: ").strip()
    if not code:
        print("No activation code entered; ClawChat setup skipped.")
        return

    base_url = _prompt(DEFAULT_BASE_URL, "ClawChat API base URL")
    result = asyncio.run(
        activate_and_maybe_restart(
            code,
            base_url=base_url,
            restart=False,
        )
    )

    print("ClawChat activation complete.")
    print(f"  User ID: {result.get('user_id')}")
    print(f"  Base URL: {result.get('base_url')}")
    print(f"  WebSocket URL: {result.get('websocket_url')}")
    print(
        "Hermes gateway setup will handle the final gateway service step "
        "after you finish."
    )
