"""From a direct chat, the agent can send a file into a group.

The route is Hermes' own ``send_message`` tool with a ``MEDIA:<path>`` marker
and a ``clawchat:cnv_…`` target: the plugin's ``send_message`` patch delivers
ClawChat media through the live adapter (or the standalone sender when no
gateway runs in-process).

Two things used to break it:

* **The terminal-send marker swallowed it.** ``clawchat_mention_message`` marks
  the chat it posted into so the turn's normal follow-up reply there is
  suppressed (the mention already was the reply). ``send()`` consumed that
  marker on ANY send to the chat, so "mention the group, then send it the file"
  returned success and dropped the file. An explicit send — a media send or a
  ``send_message`` call — is never a follow-up reply: it is delivered and the
  marker stays for the real follow-up.
* **``[[as_document]]`` was lost** on the ``send_message`` route and on
  ``send_document``: an image always went out as an inline image. It now goes
  out as a ``file`` fragment.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway import media_runtime
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.terminal_send import (
    clear_terminal_clawchat_sends_for_test,
    explicit_tool_send,
    mark_terminal_clawchat_send,
)

GROUP = "cnv_group"
_ENTRY = Path(__file__).resolve().parent.parent / "__init__.py"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    clear_terminal_clawchat_sends_for_test()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    a.frames = []

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        a.frames.append(frame)
        return True

    async def fake_upload(urls, **kw):
        kinds = []
        for url in urls:
            kind = "image" if url.endswith(".png") else "file"
            if kw.get("force_document"):
                kind = "file"
            kinds.append({"kind": kind, "url": f"https://media.example.com/{Path(url).name}"})
        return kinds

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    monkeypatch.setattr(adapter_mod, "upload_outbound_media", fake_upload)
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_kw: True)
    a._known_chat_types[GROUP] = "group"
    yield a
    clear_terminal_clawchat_sends_for_test()


def _fragments(frame):
    payload = frame["payload"]
    body = payload.get("message", {}).get("body") or payload.get("message", {})
    return body.get("fragments") or payload.get("fragments") or []


def _png(tmp_path):
    p = tmp_path / "chart.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    return p


async def test_media_send_after_a_mention_is_delivered_not_swallowed(adapter, tmp_path):
    mark_terminal_clawchat_send(account_id="default", chat_id=GROUP, message_id="m-mention")
    result = await adapter.send(
        GROUP, "here it is",
        metadata={"_clawchat_immediate_media_send": True},
        media_files=[str(_png(tmp_path))],
        _clawchat_media_files_validated=True,
    )
    assert result.success
    assert len(adapter.frames) == 1, "the file must reach the group"
    assert any(f.get("kind") == "image" for f in _fragments(adapter.frames[0]))
    # The marker still suppresses the turn's real follow-up reply.
    assert (await adapter.send(GROUP, "done!")).success
    assert len(adapter.frames) == 1


async def test_send_message_text_after_a_mention_is_delivered(adapter):
    mark_terminal_clawchat_send(account_id="default", chat_id=GROUP, message_id="m-mention")
    with explicit_tool_send():
        result = await adapter.send(GROUP, "a separate note")
    assert result.success
    assert len(adapter.frames) == 1


async def test_follow_up_reply_after_a_mention_is_still_suppressed(adapter):
    mark_terminal_clawchat_send(account_id="default", chat_id=GROUP, message_id="m-mention")
    assert (await adapter.send(GROUP, "duplicate of the mention")).success
    assert adapter.frames == []


async def test_send_document_sends_an_image_as_a_file(adapter, tmp_path):
    result = await adapter.send_document(GROUP, str(_png(tmp_path)))
    assert result.success
    kinds = [f.get("kind") for f in _fragments(adapter.frames[0])]
    assert "file" in kinds and "image" not in kinds


def test_upload_outbound_media_force_document_makes_a_file_fragment(tmp_path, monkeypatch):
    p = _png(tmp_path)

    async def uploader(**kw):
        from types import SimpleNamespace
        return SimpleNamespace(url="https://m.example.com/x.png", mime="image/png", size=12)

    def run(force):
        return asyncio.run(media_runtime.upload_outbound_media(
            [str(p)], base_url="https://app.example.com", websocket_url="",
            token="t", media_local_roots=(str(tmp_path),), upload_file=uploader,
            force_document=force,
        ))

    assert run(False)[0]["kind"] == "image"
    assert run(True)[0]["kind"] == "file"


@pytest.fixture
def plugin(monkeypatch):
    spec = importlib.util.spec_from_file_location("clawchat_plugin_entry_cross_chat", _ENTRY)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_send_message_media_patch_passes_force_document_and_marks_explicit(plugin, monkeypatch):
    seen = {}

    class Sender:
        async def send(self, **kw):
            from clawchat_gateway.terminal_send import is_explicit_tool_send
            seen.update(kw)
            seen["explicit"] = is_explicit_tool_send()
            from types import SimpleNamespace
            return SimpleNamespace(success=True, message_id="m1", error=None)

    import clawchat_gateway.terminal_send as ts
    monkeypatch.setattr(ts, "get_clawchat_sender", lambda: Sender())
    result = asyncio.run(plugin._send_clawchat_media_via_live_adapter(
        "clawchat", None, GROUP, "caption",
        media_files=[("/tmp/x.png", False)], force_document=True,
    ))
    assert result == {"success": True, "message_id": "m1"}
    assert seen["_clawchat_force_document"] is True
    assert seen["explicit"] is True


def test_send_message_text_patch_marks_the_send_explicit(plugin, monkeypatch):
    import types

    seen = {}

    async def original(platform, pconfig, chat_id, message, thread_id=None,
                       media_files=None, force_document=False, **_kw):
        from clawchat_gateway.terminal_send import is_explicit_tool_send
        seen["explicit"] = is_explicit_tool_send()
        return {"success": True}

    fake_tool = types.SimpleNamespace(_send_to_platform=original)
    fake_pkg = types.ModuleType("tools")
    fake_pkg.send_message_tool = fake_tool
    monkeypatch.setitem(sys.modules, "tools", fake_pkg)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", fake_tool)
    plugin._patch_send_message_media_delivery()
    platform = types.SimpleNamespace(value="clawchat")
    asyncio.run(fake_tool._send_to_platform(platform, None, GROUP, "hi"))
    assert seen["explicit"] is True
