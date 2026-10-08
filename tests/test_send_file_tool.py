"""``clawchat_send_file``: the agent sends a local file into any ClawChat chat.

Hermes v0.21 no longer hands the agent its built-in ``send_message`` tool
(outbound delivery there goes through the ``hermes send`` CLI), so the
documented way to put a file into another conversation — "send this file to
group X" asked in the owner's direct chat — had no tool behind it. The plugin
now owns one. It reuses the media send path the ``send_message`` patch uses:
the live adapter's ``send`` (immediate media send, explicit-send context, so a
same-turn mention's terminal marker does not swallow it), or the standalone
sender when no gateway runs in this process. The credential / system-path
denylist applies.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway import terminal_send, tools
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.terminal_send import (
    clear_terminal_clawchat_sends_for_test,
    mark_terminal_clawchat_send,
)

GROUP = "cnv_01J9ZX3K5Q7W2M4N6P8R0T2V4X"


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
        out = []
        for url in urls:
            kind = "image" if url.endswith(".png") else "file"
            if kw.get("force_document"):
                kind = "file"
            out.append({"kind": kind, "url": f"https://media.example.com/{Path(url).name}"})
        return out

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    monkeypatch.setattr(adapter_mod, "upload_outbound_media", fake_upload)
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_kw: True)
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: a)
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


async def test_sends_the_file_into_another_chat(adapter, tmp_path):
    result = await tools.send_file(GROUP, str(_png(tmp_path)), caption="the chart")

    assert result.get("sent") is True, result
    assert result.get("chatId") == GROUP
    assert len(adapter.frames) == 1
    frags = _fragments(adapter.frames[0])
    assert any(f.get("kind") == "image" for f in frags)
    assert any(f.get("kind") == "text" and "the chart" in f.get("text", "") for f in frags)


async def test_as_document_sends_an_image_as_a_file(adapter, tmp_path):
    result = await tools.send_file(GROUP, str(_png(tmp_path)), as_document=True)

    assert result.get("sent") is True, result
    kinds = [f.get("kind") for f in _fragments(adapter.frames[0])]
    assert "file" in kinds and "image" not in kinds


async def test_a_file_after_a_same_turn_mention_is_not_swallowed(adapter, tmp_path):
    mark_terminal_clawchat_send(account_id="default", chat_id=GROUP, message_id="m-mention")

    result = await tools.send_file(GROUP, str(_png(tmp_path)))

    assert result.get("sent") is True, result
    assert len(adapter.frames) == 1, "the file must reach the group"
    # The marker still suppresses the turn's real follow-up reply.
    assert (await adapter.send(GROUP, "done!")).success
    assert len(adapter.frames) == 1


@pytest.mark.parametrize(
    "chat_id,path_kind,needle",
    [
        ("", "ok", "chat_id"),
        ("not-a-chat", "ok", "chat"),
        (GROUP, "relative", "absolute"),
        (GROUP, "missing", "not"),
        (GROUP, "dir", "file"),
    ],
)
async def test_rejects_bad_input_without_sending(adapter, tmp_path, chat_id, path_kind, needle):
    path = {
        "ok": str(_png(tmp_path)),
        "relative": "chart.png",
        "missing": str(tmp_path / "nope.png"),
        "dir": str(tmp_path),
    }[path_kind]

    result = await tools.send_file(chat_id, path)

    assert "error" in result, result
    assert needle in str(result).lower()
    assert adapter.frames == []


async def test_credential_paths_are_refused(adapter, tmp_path, monkeypatch):
    home = tmp_path / "osHome"
    (home / ".ssh").mkdir(parents=True)
    key = home / ".ssh" / "id_rsa"
    key.write_text("PRIVATE")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    result = await tools.send_file(GROUP, str(key))

    assert "error" in result
    assert "denied" in str(result).lower()
    assert adapter.frames == []


async def test_without_a_live_gateway_it_uses_the_standalone_sender(tmp_path, monkeypatch):
    seen = {}

    async def fake_standalone(pconfig, chat_id, message, **kw):
        seen.update(kw, chat_id=chat_id, message=message, pconfig=pconfig)
        return {"success": True, "message_id": "m-standalone"}

    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: None)
    monkeypatch.setattr(tools, "standalone_send", fake_standalone)

    result = await tools.send_file(GROUP, str(_png(tmp_path)), as_document=True, caption="hi")

    assert result == {"sent": True, "chatId": GROUP, "messageId": "m-standalone", "file": "chart.png"}
    assert seen["chat_id"] == GROUP
    assert seen["message"] == "hi"
    assert seen["force_document"] is True
    assert seen["media_files"] == [str(_png(tmp_path).resolve())]


def test_tool_is_registered_with_its_schema():
    from clawchat_gateway import plugin_tools

    captured: dict[str, tuple] = {}

    class Ctx:
        def register_tool(self, name, _toolset, schema, handler, *_a, **_k):
            captured[name] = (schema, handler)

    plugin_tools.register_tools(Ctx())
    schema, _handler = captured["clawchat_send_file"]
    params = schema["parameters"]
    assert set(params["properties"]) == {"chat_id", "path", "as_document", "caption"}
    assert params["required"] == ["chat_id", "path"]
    assert "send_message" not in schema["description"]


def test_handler_maps_args(monkeypatch):
    from clawchat_gateway import plugin_tools

    seen = {}

    async def fake_send_file(chat_id, path, *, as_document=False, caption=""):
        seen.update(chat_id=chat_id, path=path, as_document=as_document, caption=caption)
        return {"sent": True}

    monkeypatch.setattr(tools, "send_file", fake_send_file)
    monkeypatch.setattr(plugin_tools, "_record_tool_call", lambda **_k: None)
    out = asyncio.run(plugin_tools.handle_clawchat_send_file(
        {"chat_id": GROUP, "path": "/x/a.pdf", "as_document": True, "caption": "c"}
    ))
    assert '"sent": true' in out
    assert seen == {"chat_id": GROUP, "path": "/x/a.pdf", "as_document": True, "caption": "c"}
