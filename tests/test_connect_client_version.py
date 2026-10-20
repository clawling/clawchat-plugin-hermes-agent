"""The `connect` payload must carry `client_version` (§3.3 version telemetry).

docs/client-integration.md §3.3: `client_version` is optional telemetry the hub
records on its `handshake accepted` log line. The adapter reports the plugin's
single-source version constant, `clawchat_gateway.__version__`.
"""

from __future__ import annotations

import json

import pytest

from clawchat_gateway import __version__
from clawchat_gateway import connection
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection, ConnectionState
from clawchat_gateway.protocol import build_connect_request


class _CapturingWs:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:  # pragma: no cover - not exercised here
        pass


def test_build_connect_request_includes_client_version() -> None:
    frame = build_connect_request(
        frame_id="trace_1", token="t", nonce="n", client_version="1.2.3"
    )
    assert frame["payload"]["client_version"] == "1.2.3"


def test_build_connect_request_omits_empty_client_version() -> None:
    frame = build_connect_request(frame_id="trace_1", token="t", nonce="n")
    assert "client_version" not in frame["payload"]


@pytest.mark.asyncio
async def test_handshake_connect_frame_sends_plugin_version(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(connection, "hermes_home", lambda: tmp_path)
    monkeypatch.setattr(connection, "hermes_root", lambda: tmp_path)
    monkeypatch.setattr(connection, "get_clawchat_store", lambda: None)
    monkeypatch.setattr(ClawChatConnection, "_refresh_device_id", lambda _: "test-device")
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token="tok_realistic_value",
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    conn = ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)
    ws = _CapturingWs()
    conn._ws = ws
    conn._state = ConnectionState.HANDSHAKING

    await conn._handle_challenge(
        {
            "version": "2",
            "event": "connect.challenge",
            "trace_id": "c1",
            "emitted_at": 1776162600000,
            "payload": {"nonce": "abc"},
        }
    )

    connects = [json.loads(s) for s in ws.sent]
    connects = [f for f in connects if f.get("event") == "connect"]
    assert len(connects) == 1, ws.sent
    assert __version__
    assert connects[0]["payload"]["client_version"] == __version__
