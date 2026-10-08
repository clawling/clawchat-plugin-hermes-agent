"""Inbound attachment downloads identify themselves and log failures.

The media CDN (Cloudflare, e.g. an r2.dev bucket) answers 403 to the stdlib
default ``Python-urllib/x.y`` User-Agent, so every inbound image failed to
download on dev — and ``download_inbound_media`` swallowed the error, so the
agent just never saw the attachment and nothing said why.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.error import HTTPError

from clawchat_gateway import media_runtime


class _Resp:
    def __init__(self):
        from email.message import Message

        self.headers = Message()
        self.headers["Content-Type"] = "image/png"

    def read(self):
        return b"\x89PNG"

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_download_sends_a_non_default_user_agent(tmp_path, monkeypatch):
    seen = []

    def _urlopen(request, timeout=None):
        seen.append(request)
        return _Resp()

    monkeypatch.setattr(media_runtime, "urlopen", _urlopen)

    got = asyncio.run(media_runtime.download_inbound_media(
        ["https://cdn.example.test/a.png"],
        base_url="https://api.example.test",
        websocket_url="wss://api.example.test/ws",
        token="tok",
        download_dir=tmp_path,
    ))

    assert len(got) == 1
    ua = seen[0].get_header("User-agent")
    assert ua and not ua.startswith("Python-urllib")


def test_download_failure_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    def _urlopen(request, timeout=None):
        raise HTTPError(request.full_url, 403, "Forbidden", None, None)

    monkeypatch.setattr(media_runtime, "urlopen", _urlopen)

    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.media_runtime"):
        got = asyncio.run(media_runtime.download_inbound_media(
            ["https://cdn.example.test/a.png?sig=secret"],
            base_url="https://api.example.test",
            websocket_url="wss://api.example.test/ws",
            token="tok",
            download_dir=tmp_path,
        ))

    assert got == []
    text = caplog.text
    assert "403" in text
    assert "cdn.example.test/a.png" in text
    assert "sig=secret" not in text
