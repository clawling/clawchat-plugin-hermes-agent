"""Moments upload local images to long-lived storage before publishing.

Remote URLs pass through; invalid entries, failed uploads, and non-image
upload responses create no moment.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from clawchat_gateway import tools
from clawchat_gateway.api_client import ClawChatApiError


class FakeClient:
    def __init__(self, fail_upload: bool = False, uploaded_mime: str | None = None):
        self.uploads: list[tuple[str, str, int]] = []
        self.created: list[dict] = []
        self.fail_upload = fail_upload
        self.uploaded_mime = uploaded_mime

    async def upload_media(self, *, buffer, filename, mime):
        pytest.fail("moment images must use the long-lived upload_avatar endpoint")

    async def upload_avatar(self, *, buffer, filename, mime):
        if self.fail_upload:
            raise ClawChatApiError(kind="http", message="upload failed", status=500)
        self.uploads.append((filename, mime, len(buffer)))
        return SimpleNamespace(
            url=f"https://cdn.example.com/files/{filename}",
            mime=self.uploaded_mime if self.uploaded_mime is not None else mime,
            size=len(buffer),
        )

    async def create_moment(self, *, text, images):
        self.created.append({"text": text, "images": images})
        return {"moment_id": 1, "images": images}


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(tools, "_build_client", lambda: (c, None))
    return c


def _png(tmp_path, name="pic.png"):
    p = tmp_path / name
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    return p


def test_urls_pass_through_unchanged(client):
    urls = ["https://cdn.example.com/a.jpg", "http://cdn.example.com/b.png"]
    asyncio.run(tools.create_moment(text="hi", images=urls))
    assert client.uploads == []
    assert client.created == [{"text": "hi", "images": urls}]


def test_local_absolute_image_is_uploaded_and_replaced(client, tmp_path):
    p = _png(tmp_path)
    result = asyncio.run(
        tools.create_moment(text="hi", images=["https://cdn.example.com/a.jpg", str(p)])
    )
    assert client.uploads == [("pic.png", "image/png", p.stat().st_size)]
    assert client.created == [{
        "text": "hi",
        "images": ["https://cdn.example.com/a.jpg", "https://cdn.example.com/files/pic.png"],
    }]
    assert "error" not in result


@pytest.mark.parametrize("bad", [
    "relative/pic.png",
    "~/pic.png",
    "/definitely/not/here.png",
    "file:///tmp/pic.png",
    "ftp://example.com/a.png",
    "",
    "MEDIA:/tmp/pic.png",
])
def test_anything_else_is_rejected_and_nothing_is_created(client, bad):
    result = asyncio.run(tools.create_moment(text="hi", images=[bad]))
    assert result["error"] == "validation"
    assert client.created == []
    assert client.uploads == []


def test_a_local_non_image_file_is_rejected(client, tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("hello")
    result = asyncio.run(tools.create_moment(text="hi", images=[str(p)]))
    assert result["error"] == "validation"
    assert client.created == []


def test_a_bad_entry_rejects_the_whole_call_before_any_upload(client, tmp_path):
    p = _png(tmp_path)
    result = asyncio.run(tools.create_moment(text="hi", images=[str(p), "nope.png"]))
    assert result["error"] == "validation"
    assert client.uploads == [] and client.created == []


def test_failed_upload_creates_nothing(monkeypatch, tmp_path):
    c = FakeClient(fail_upload=True)
    monkeypatch.setattr(tools, "_build_client", lambda: (c, None))
    result = asyncio.run(tools.create_moment(text="hi", images=[str(_png(tmp_path))]))
    assert "error" in result
    assert c.created == []


@pytest.mark.parametrize("mime", ["text/plain", "application/octet-stream", ""])
def test_non_image_upload_response_creates_nothing(monkeypatch, tmp_path, mime):
    c = FakeClient(uploaded_mime=mime)
    monkeypatch.setattr(tools, "_build_client", lambda: (c, None))
    p = _png(tmp_path)
    result = asyncio.run(tools.create_moment(text="hi", images=[str(p)]))
    assert result == {
        "error": "validation",
        "message": "uploaded moment image must have an image/ MIME type",
    }
    assert c.uploads == [("pic.png", "image/png", p.stat().st_size)]
    assert c.created == []


def test_tool_description_no_longer_says_upload_first():
    from clawchat_gateway import plugin_tools

    captured: dict[str, dict] = {}

    class Ctx:
        def register_tool(self, name, _toolset, schema, *_a, **_k):
            captured[name] = schema

    plugin_tools.register_tools(Ctx())
    schema = captured["clawchat_create_moment"]
    text = schema["description"] + schema["parameters"]["properties"]["images"]["description"]
    assert "upload first" not in text.lower()
    assert "absolute" in text.lower()
    assert "about 14 days" in text
    assert "clawchat_upload_avatar_image" in text
    assert "long-lived" in text
    assert "/v1/files/upload-url" in text
    for name in (
        "clawchat_create_moment", "clawchat_update_account_profile",
        "clawchat_upload_avatar_image", "clawchat_metadata_sync", "clawchat_metadata_update",
    ):
        description = captured[name]["description"]
        assert "about 14 days" in description
        if name != "clawchat_upload_avatar_image":
            assert "clawchat_upload_avatar_image" in description
        assert "use a provided URL directly" not in description
    avatar = captured["clawchat_update_account_profile"]["parameters"]["properties"]["avatar_url"]
    metadata_avatar = captured["clawchat_metadata_update"]["parameters"]["properties"]["patch"]["properties"]["avatar_url"]
    for field in (avatar, metadata_avatar):
        assert "clawchat_upload_avatar_image" in field["description"]
        assert "about 14 days" in field["description"]
