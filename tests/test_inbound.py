from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.inbound import parse_inbound_message


def test_group_message_requires_mention_in_mention_mode():
    cfg = ClawChatConfig(
        websocket_url="wss://x",
        token="t",
        user_id="bot",
        group_mode="mention",
    )
    env = {
        "chat_id": "g1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {"mentions": [{"id": "other"}]},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is None


def test_reply_preview_is_preserved():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {
                    "reply": {
                        "id": "u2",
                        "nick_name": "bob",
                        "fragments": [{"kind": "text", "text": "old"}],
                    }
                },
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound.reply_preview["id"] == "u2"
    assert inbound.text == "hello"


def test_group_message_accepts_matching_mention_in_mention_mode():
    cfg = ClawChatConfig(
        websocket_url="wss://x",
        token="t",
        user_id="bot",
        group_mode="mention",
    )
    env = {
        "chat_id": "g1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {"mentions": [{"id": "bot"}]},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.text == "hello"


def test_group_mode_all_accepts_group_message_without_mention():
    cfg = ClawChatConfig(
        websocket_url="wss://x",
        token="t",
        user_id="bot",
        group_mode="all",
    )
    env = {
        "chat_id": "g1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {"mentions": [{"id": "other"}]},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.chat_type == "group"
    assert inbound.text == "hello"


def test_default_group_mode_accepts_group_message_without_mention():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "room1",
        "chat_type": "group",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "context": {"mentions": []},
                "fragments": [{"kind": "text", "text": "hello"}],
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.chat_type == "group"


def test_mixed_text_and_media_fragments_render_placeholders_and_media_urls():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "fragments": [
                    {"kind": "text", "text": "hello"},
                    {"kind": "image", "url": "https://cdn.example/img.png", "name": "img.png"},
                    {"kind": "file", "url": "https://cdn.example/doc.pdf", "name": "doc.pdf"},
                ],
                "context": {},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.text == "\n".join(
        [
            "hello",
            "![img.png](https://cdn.example/img.png)",
            "[doc.pdf](https://cdn.example/doc.pdf)",
        ]
    )
    assert inbound.media_urls == [
        "https://cdn.example/img.png",
        "https://cdn.example/doc.pdf",
    ]


def test_message_body_string_is_parsed_as_text():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "body": "hello from body",
                "context": {},
                "streaming": False,
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.text == "hello from body"


def test_message_body_dict_text_is_parsed_as_text():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "body": {"text": "hello from dict body"},
                "context": {},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.text == "hello from dict body"


def test_message_body_list_accepts_type_content_fragments():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "chat_id": "u1",
        "chat_type": "direct",
        "sender": {"id": "u1", "nick_name": "alice"},
        "payload": {
            "message": {
                "body": [
                    {"type": "text", "content": "hello"},
                    {"type": "image", "url": "https://cdn.example/img.png", "name": "img.png"},
                ],
                "context": {},
            }
        },
    }

    inbound = parse_inbound_message(env, cfg)

    assert inbound is not None
    assert inbound.text == "hello\n![img.png](https://cdn.example/img.png)"
    assert inbound.media_urls == ["https://cdn.example/img.png"]


def test_parse_inbound_message_returns_none_for_truthy_non_mapping_payload():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")

    assert parse_inbound_message({"payload": "bad", "sender": {"id": "u1"}}, cfg) is None


def test_parse_inbound_message_returns_none_for_truthy_non_mapping_message():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "payload": {"message": "bad"},
        "sender": {"id": "u1", "nick_name": "alice"},
    }

    assert parse_inbound_message(env, cfg) is None


def test_parse_inbound_message_returns_none_for_truthy_non_mapping_context():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot", group_mode="all")
    env = {
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": "bad",
            }
        },
        "sender": {"id": "u1", "nick_name": "alice"},
    }

    assert parse_inbound_message(env, cfg) is None


def test_parse_inbound_message_returns_none_for_truthy_non_mapping_sender():
    cfg = ClawChatConfig(websocket_url="wss://x", token="t", user_id="bot")
    env = {
        "payload": {
            "message": {
                "fragments": [{"kind": "text", "text": "hello"}],
                "context": {},
            }
        },
        "sender": "bad",
    }

    assert parse_inbound_message(env, cfg) is None
