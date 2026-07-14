"""Out-of-process ClawChat delivery (Hermes ``standalone_sender_fn``).

Implements the ``PlatformEntry.standalone_sender_fn`` contract so
``hermes send`` and ``deliver=clawchat`` cron jobs work when the gateway is
not running in the calling process: open an ephemeral WS connection,
handshake, send one ``message.send`` frame, wait for the ack, close.

ClawChat has no REST send endpoint — messages only travel over the
Protocol-v2 WebSocket — so the ephemeral connection reuses
``ClawChatConnection`` (credential loading from env/.env/SQLite, challenge
handshake, token refresh, ack tracking). The connection presents a sibling
device id (``use_sibling_connect_device_id``) so it never takes over the
socket of a gateway daemon running in another process on the same host.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import (
    HANDSHAKE_TIMEOUT_SECONDS,
    ClawChatConnection,
)
from clawchat_gateway.protocol import build_message_send_event, new_message_id

logger = logging.getLogger(__name__)

STANDALONE_DEVICE_SUFFIX = "-standalone"

# Covers the WS dial + challenge handshake, plus headroom for one reactive
# token refresh before READY.
READY_TIMEOUT_SECONDS = HANDSHAKE_TIMEOUT_SECONDS + 10.0


async def _drop_inbound(*_args: Any, **_kwargs: Any) -> None:
    """Ephemeral sessions ignore inbound traffic (fanout still reaches the
    real device; per-device cursors mean nothing is consumed on its behalf)."""
    return None


async def standalone_send(
    platform_config: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: str | None = None,
    media_files: list[str] | None = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Send one text message over an ephemeral ClawChat connection.

    Returns ``{"success": True, "message_id": ...}`` or ``{"error": str}``
    per the standalone_sender_fn contract. ``thread_id`` and
    ``force_document`` are accepted for signature parity only — ClawChat has
    no thread or document primitive. Media requires the media runtime that
    lives on the gateway adapter, so ``media_files`` is rejected here.
    """
    del thread_id, force_document
    if media_files:
        return {
            "error": (
                "ClawChat standalone delivery is text-only; media attachments "
                "need the gateway process running with ClawChat connected."
            )
        }
    target = str(chat_id or "").strip()
    if not target:
        return {"error": "ClawChat standalone send requires a chat_id."}

    config = ClawChatConfig.from_platform_config(platform_config)
    if not config.websocket_url:
        return {"error": "ClawChat is not configured (missing websocket_url)."}

    connection = ClawChatConnection(config, on_message=_drop_inbound)
    sibling_id = connection.use_sibling_connect_device_id(STANDALONE_DEVICE_SUFFIX)
    logger.info(
        "clawchat standalone send start chat_id=%s device_id=%s", target, sibling_id
    )
    try:
        await connection.start()
        ready = await connection.wait_until_ready(timeout=READY_TIMEOUT_SECONDS)
        if not ready:
            return {
                "error": (
                    "ClawChat standalone connection did not become ready within "
                    f"{READY_TIMEOUT_SECONDS:.0f}s. Is this agent activated? "
                    "Pair it with `hermes clawchat activate CODE` (or check "
                    "network reachability of the ClawChat websocket)."
                )
            }
        message_id = new_message_id()
        frame = build_message_send_event(
            chat_id=target,
            chat_type="direct",  # accepted for builder parity; not on the wire
            message_id=message_id,
            fragments=[{"kind": "text", "text": str(message or "")}],
            include_message_id=True,
        )
        try:
            sent = await connection.send_frame(
                frame,
                wait_for_ack=True,
                queue_when_unready=False,
            )
        except asyncio.TimeoutError:
            return {
                "error": (
                    "ClawChat did not acknowledge the message within "
                    f"{config.ack_timeout_ms}ms."
                )
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — contract wants {"error": str}
            return {"error": f"ClawChat standalone send failed: {exc}"}
        if not sent:
            return {"error": "ClawChat standalone send was dropped before dispatch."}
        logger.info(
            "clawchat standalone send ok chat_id=%s message_id=%s", target, message_id
        )
        return {"success": True, "message_id": message_id}
    finally:
        await connection.stop()
