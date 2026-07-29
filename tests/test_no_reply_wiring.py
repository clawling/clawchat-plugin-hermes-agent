import sys
from types import ModuleType

# adapter.py imports the Hermes host `gateway` package, absent in the plugin's
# own test env. Stub the minimal surface it touches (mirrors test_reaction.py
# / test_duplicate_send.py / test_adapter_dispatch.py).
if "gateway" not in sys.modules:
    _gateway = ModuleType("gateway")
    _gateway_config = ModuleType("gateway.config")
    _gateway_platforms = ModuleType("gateway.platforms")
    _gateway_base = ModuleType("gateway.platforms.base")

    class _Platform(str):
        CLAWCHAT = "clawchat"

    class _BasePlatformAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    class _MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MessageType:
        TEXT = "text"

    class _SendResult:
        def __init__(self, success, error=None, message_id=None):
            self.success = success
            self.error = error
            self.message_id = message_id

    _gateway_config.Platform = _Platform
    _gateway_base.BasePlatformAdapter = _BasePlatformAdapter
    _gateway_base.MessageEvent = _MessageEvent
    _gateway_base.MessageType = _MessageType
    _gateway_base.SendResult = _SendResult
    _gateway_platforms.base = _gateway_base
    _gateway.config = _gateway_config
    _gateway.platforms = _gateway_platforms

    sys.modules["gateway"] = _gateway
    sys.modules["gateway.config"] = _gateway_config
    sys.modules["gateway.platforms"] = _gateway_platforms
    sys.modules["gateway.platforms.base"] = _gateway_base

from clawchat_gateway.adapter import ClawChatAdapter


def _adapter() -> ClawChatAdapter:
    # These helpers are pure — bypass __init__ so the test needs no config.
    return ClawChatAdapter.__new__(ClawChatAdapter)


def test_noop_detects_token_embedded_in_prose():
    assert _adapter()._is_noop_response_text("好的,我不打扰了 <clawchat:no-reply/>") is True


def test_noop_detects_host_bare_marker():
    assert _adapter()._is_noop_response_text("NO_REPLY") is True


def test_noop_still_detects_legacy_empty_token():
    assert _adapter()._is_noop_response_text('""') is True


def test_noop_rejects_ordinary_prose():
    assert _adapter()._is_noop_response_text("there is no reply from the server") is False


def test_pure_silent_response_across_multiple_text_fragments():
    fragments = [
        {"kind": "text", "text": "好的 "},
        {"kind": "text", "text": "<clawchat:no-reply/>"},
    ]
    assert _adapter()._is_pure_silent_response(fragments) is True


def test_pure_silent_response_false_when_a_media_fragment_present():
    fragments = [
        {"kind": "text", "text": "<clawchat:no-reply/>"},
        {"kind": "image", "url": "https://example.com/a.png"},
    ]
    assert _adapter()._is_pure_silent_response(fragments) is False


def test_strip_fragments_removes_token_keeps_media():
    fragments = [
        {"kind": "text", "text": "看这个 <clawchat:no-reply/>"},
        {"kind": "image", "url": "https://example.com/a.png"},
    ]
    assert _adapter()._strip_no_reply_from_fragments(fragments) == [
        {"kind": "text", "text": "看这个"},
        {"kind": "image", "url": "https://example.com/a.png"},
    ]


def test_strip_fragments_drops_text_fragment_that_becomes_empty():
    fragments = [
        {"kind": "text", "text": "<clawchat:no-reply/>"},
        {"kind": "image", "url": "https://example.com/a.png"},
    ]
    assert _adapter()._strip_no_reply_from_fragments(fragments) == [
        {"kind": "image", "url": "https://example.com/a.png"},
    ]


def test_prefix_guard_holds_partial_token():
    assert _adapter()._is_no_reply_token_prefix("<clawchat:no-re") is True


def test_prefix_guard_ignores_complete_token():
    assert _adapter()._is_no_reply_token_prefix("<clawchat:no-reply/>") is False
