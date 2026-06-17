from __future__ import annotations

from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import parse_inbound_message


def _config() -> ClawChatConfig:
    return ClawChatConfig(
        websocket_url="wss://example.test/ws",
        user_id="agt_self",
        owner_user_id="usr_owner",
    )


def _system_frame() -> dict:
    return {
        "event": "message.send",
        "chat_id": "cnv_1",
        "chat_type": "group",
        "sender": {"id": "system", "nick_name": "System"},
        "payload": {
            "message_id": "sys-1",
            "message": {
                "body": {"fragments": [{"kind": "text", "text": "Alice 加入了群聊"}]},
                "context": {},
            },
        },
    }


def test_system_message_is_dropped() -> None:
    assert parse_inbound_message(_system_frame(), _config()) is None
