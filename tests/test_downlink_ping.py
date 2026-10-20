"""The client must not ship a downlink-`ping` handler (§12).

docs/client-integration.md §12:

  "The server **never** emits a JSON-level `ping`, and an unsolicited client
   `pong` is an **unknown uplink event** — silently dropped and logged
   server-side. Do not ship a downlink-ping handler, and never send an
   unsolicited `pong`."

and the §12 checklist: "Do **not** implement a downlink `ping` handler and never
send an unsolicited `pong` — the server emits neither."

The heartbeat that actually runs is the RFC 6455 control-frame one, configured
via ping_interval/ping_timeout on the websockets client; it needs no
application code.
"""

from __future__ import annotations

import pytest

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState


class _CapturingWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def _make_conn() -> tuple[ClawChatConnection, _CapturingWs]:
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="tok_realistic_value",
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    conn = ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)
    ws = _CapturingWs()
    conn._ws = ws
    conn._state = ConnectionState.READY
    return conn, ws


@pytest.mark.asyncio
async def test_downlink_ping_is_not_answered() -> None:
    conn, ws = _make_conn()
    await conn._dispatch_inbound(
        {"version": "2", "event": "ping", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {}}
    )
    assert ws.sent == [], f"must not answer a downlink ping, sent: {ws.sent}"


@pytest.mark.asyncio
async def test_no_pong_builder_remains() -> None:
    """The only caller of build_pong_event was the banned path; it should be gone."""
    import clawchat_gateway.protocol as protocol

    assert not hasattr(protocol, "build_pong_event")


@pytest.mark.asyncio
async def test_inbound_pong_is_still_accepted() -> None:
    """§12 keeps the client-initiated probe legal, so an inbound pong must not crash."""
    conn, ws = _make_conn()
    await conn._dispatch_inbound(
        {"version": "2", "event": "pong", "trace_id": "p1", "emitted_at": 1776162600000, "payload": {}}
    )
    assert ws.sent == []
