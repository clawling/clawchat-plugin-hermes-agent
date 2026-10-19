"""Inbound attachment downloads identify themselves and log safe failures."""

from __future__ import annotations

import asyncio
import io
import logging
from email.message import Message
from types import SimpleNamespace

from clawchat_gateway import media_runtime


class _Resp(io.BytesIO):
    def __init__(self, status=200):
        super().__init__(b"\x89PNG")
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "image/png"

    def isclosed(self):
        return self.closed


class _Connection:
    def __init__(self, response, seen):
        self.response = response
        self.seen = seen
        self.sock = SimpleNamespace(settimeout=lambda _: None, shutdown=lambda _: None)

    def connect(self):
        pass

    def request(self, method, target, headers):
        self.seen.append(headers)

    def getresponse(self):
        return self.response

    def close(self):
        pass


def _install_connection(monkeypatch, seen, status=200):
    monkeypatch.setattr(media_runtime, "_resolve_host", lambda *_: [(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(media_runtime, "_PublicHTTPSConnection", lambda *a, **kw: _Connection(_Resp(status), seen))


def test_download_sends_a_non_default_user_agent(tmp_path, monkeypatch):
    seen = []
    _install_connection(monkeypatch, seen)
    got = asyncio.run(media_runtime.download_inbound_media(
        ["https://cdn.example.test/a.png"],
        base_url="https://api.example.test",
        websocket_url="wss://api.example.test/ws",
        download_dir=tmp_path,
    ))
    assert len(got) == 1
    ua = seen[0]["User-Agent"]
    assert ua and not ua.startswith("Python-urllib")


def test_download_failure_is_logged_not_silent(tmp_path, monkeypatch, caplog):
    _install_connection(monkeypatch, [], status=403)
    with caplog.at_level(logging.WARNING, logger="clawchat_gateway.media_runtime"):
        got = asyncio.run(media_runtime.download_inbound_media(
            ["https://cdn.example.test/a.png?sig=secret"],
            base_url="https://api.example.test",
            websocket_url="wss://api.example.test/ws",
            download_dir=tmp_path,
        ))
    assert got == []
    assert "403" in caplog.text
    assert "cdn.example.test/a.png" in caplog.text
    assert "sig=secret" not in caplog.text
