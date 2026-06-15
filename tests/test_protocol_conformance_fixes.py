"""Regression tests for the Protocol v2 conformance fixes.

Covers four spec-driven hardening changes:

- §14.1  transient (5xx / auth-backend-unavailable) ``hello-fail`` must
  backoff-reconnect with the same token rather than be treated as terminal.
- §7.4   ``message.reply`` / ``message.send`` carry the ReplyContext
  ``reply_preview`` when supplied.
- §15.3  media upload branches on the business ``code`` field, not HTTP status.
"""

from __future__ import annotations

import io
import json

import pytest

from clawchat_gateway import media_runtime
from clawchat_gateway.connection import _is_transient_auth_failure
from clawchat_gateway.protocol import (
    build_message_reply_event,
    build_message_send_event,
)


# --- §14.1: transient vs terminal hello-fail classification -----------------


@pytest.mark.parametrize(
    "reason",
    [
        "remote auth service unavailable",
        "Remote Auth Service Unavailable",
        "service unavailable",
        "the auth service unavailable right now",
        "temporarily unavailable",
    ],
)
def test_transient_auth_failures_are_retryable(reason: str) -> None:
    assert _is_transient_auth_failure(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "authentication failed",
        "nonce mismatch",
        "invalid connect event",
        "invalid connect payload",
        "",
        None,
    ],
)
def test_terminal_auth_failures_are_not_retryable(reason) -> None:
    assert _is_transient_auth_failure(reason) is False


# --- §7.4: reply_preview plumbing ------------------------------------------

_PREVIEW = {
    "id": "usr_alice",
    "nick_name": "Alice",
    "fragments": [{"kind": "text", "text": "original"}],
}


def test_reply_event_carries_reply_preview() -> None:
    frame = build_message_reply_event(
        chat_id="chat-ab",
        chat_type="direct",
        message_id="msg-AAAAAAAAAAAAAAAAAAAAAAAAAA",
        fragments=[{"kind": "text", "text": "answer"}],
        reply_to_message_id="msg-target",
        reply_preview=_PREVIEW,
    )
    reply = frame["payload"]["message"]["context"]["reply"]
    assert reply["reply_to_msg_id"] == "msg-target"
    assert reply["reply_preview"] == _PREVIEW


def test_send_event_carries_reply_preview() -> None:
    frame = build_message_send_event(
        chat_id="chat-ab",
        chat_type="direct",
        message_id="msg-BBBBBBBBBBBBBBBBBBBBBBBBBB",
        fragments=[{"kind": "text", "text": "answer"}],
        reply_to_message_id="msg-target",
        reply_preview=_PREVIEW,
    )
    reply = frame["payload"]["message"]["context"]["reply"]
    assert reply["reply_to_msg_id"] == "msg-target"
    assert reply["reply_preview"] == _PREVIEW


def test_reply_context_is_none_without_target() -> None:
    frame = build_message_reply_event(
        chat_id="chat-ab",
        chat_type="direct",
        message_id="msg-CCCCCCCCCCCCCCCCCCCCCCCCCC",
        fragments=[{"kind": "text", "text": "answer"}],
    )
    assert frame["payload"]["message"]["context"]["reply"] is None


# --- §15.3: media upload branches on the business `code` --------------------


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _patch_urlopen(monkeypatch, body: dict) -> None:
    def _fake_urlopen(_request):
        return _FakeResponse(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr(media_runtime, "urlopen", _fake_urlopen)


def test_upload_rejects_business_error_code(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, {"code": 41501, "msg": "mime type not allowed"})
    with pytest.raises(ValueError) as excinfo:
        media_runtime._upload_media_sync(
            base_url="http://host",
            token="tok",
            buffer=b"x",
            filename="a.exe",
            mime="application/x-msdownload",
        )
    assert "41501" in str(excinfo.value)


def test_upload_accepts_success_code(monkeypatch) -> None:
    _patch_urlopen(
        monkeypatch,
        {
            "code": 0,
            "msg": "ok",
            "data": {"kind": "image", "url": "https://cdn/x.png", "mime": "image/png", "size": 12},
        },
    )
    result = media_runtime._upload_media_sync(
        base_url="http://host",
        token="tok",
        buffer=b"x",
        filename="x.png",
        mime="image/png",
    )
    assert result.url == "https://cdn/x.png"
    assert result.mime == "image/png"
    assert result.size == 12
