"""REST tool uploads go to the MEDIA host, not the API host.

``/media/upload`` is served by the media service. In deployments where the
API and media hosts or ports differ (the API port answers 404 for
``/media/upload``; the media port answers 200), the tools' REST
client posted every local moment image and ``upload_media_file`` to the API
host. The adapter's send path already resolved the media host with
``media_runtime.derive_base_url`` (explicit media base URL, else the WebSocket
host); the tools' client now resolves it the same way.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import yaml

from clawchat_gateway import api_client, tools


def _jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg(claims)}.sig"


class _Resp:
    status = 200

    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def profile(tmp_path, monkeypatch):
    def _make(extra: dict, env: dict | None = None):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        base_extra = {"user_id": "usr_bot", "base_url": "http://api.example.test:8080"}
        base_extra.update(extra)
        (home / "config.yaml").write_text(
            yaml.safe_dump({"platforms": {"clawchat": {"extra": base_extra}}}), encoding="utf-8"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        for name in (
            "CLAWCHAT_BASE_URL", "CLAWCHAT_MEDIA_BASE_URL", "CLAWCHAT_WEBSOCKET_URL",
            "CLAWCHAT_WS_URL", "CLAWCHAT_USER_ID", "CLAWCHAT_AGENT_ID",
        ):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("CLAWCHAT_TOKEN", _jwt({"sub": "usr_bot", "aid": "agt_1"}))
        for k, v in (env or {}).items():
            monkeypatch.setenv(k, v)
        return home

    return _make


@pytest.fixture
def seen_urls(monkeypatch):
    urls: list[str] = []

    def _urlopen(request, timeout=None):
        urls.append(request.full_url)
        if request.full_url.endswith("/media/upload"):
            return _Resp({"code": 0, "data": {
                "kind": "image", "url": "https://cdn.example.test/a.png",
                "name": "a.png", "mime": "image/png", "size": 4,
            }})
        return _Resp({"code": 0, "data": {"id": 1}})

    monkeypatch.setattr(api_client, "urlopen", _urlopen)
    return urls


def _png(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"\x89PNG")
    return path


def test_upload_media_file_uses_the_explicit_media_base_url(profile, seen_urls, tmp_path):
    profile({"media_base_url": "http://media.example.test:9000"})

    result = asyncio.run(tools.upload_media_file(str(_png(tmp_path))))

    assert result.get("url") == "https://cdn.example.test/a.png", result
    assert seen_urls == ["http://media.example.test:9000/media/upload"]


def test_media_base_url_env_wins_over_extra(profile, seen_urls, tmp_path):
    profile(
        {"media_base_url": "http://stale.example.test"},
        env={"CLAWCHAT_MEDIA_BASE_URL": "http://media.example.test:9000"},
    )

    asyncio.run(tools.upload_media_file(str(_png(tmp_path))))

    assert seen_urls == ["http://media.example.test:9000/media/upload"]


def test_without_media_base_url_the_websocket_host_is_used(profile, seen_urls, tmp_path):
    profile({"websocket_url": "wss://gw.example.test:8443/ws"})

    asyncio.run(tools.upload_media_file(str(_png(tmp_path))))

    assert seen_urls == ["https://gw.example.test:8443/media/upload"]


def test_moment_local_image_uploads_to_media_host_and_posts_to_api_host(profile, seen_urls, tmp_path):
    profile({"media_base_url": "http://media.example.test:9000"})

    asyncio.run(tools.create_moment(text="hi", images=[str(_png(tmp_path))]))

    assert seen_urls[0] == "http://media.example.test:9000/media/upload"
    assert all(u.startswith("http://api.example.test:8080/") for u in seen_urls[1:]), seen_urls
    assert len(seen_urls) == 2


def test_avatar_upload_stays_on_the_api_host(profile, seen_urls, tmp_path):
    profile({"media_base_url": "http://media.example.test:9000"})

    asyncio.run(tools.upload_avatar_image(str(_png(tmp_path))))

    assert seen_urls and seen_urls[0].startswith("http://api.example.test:8080/v1/files/")
