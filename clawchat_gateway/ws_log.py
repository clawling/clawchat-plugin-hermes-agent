from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def optional_field(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def format_ws_log(
    *,
    event: str,
    account_id: str,
    attempt: int,
    reconnect_count: int,
    state: str,
    action: str,
    fields: Iterable[tuple[str, Any]] = (),
) -> str:
    ordered: list[tuple[str, Any]] = [
        ("event", event),
        ("account_id", account_id),
        ("attempt", attempt),
        ("reconnect_count", reconnect_count),
        ("state", state),
        ("action", action),
        *list(fields),
    ]
    return "clawchat.ws " + " ".join(
        f"{key}={optional_field(value)}" for key, value in ordered
    )
