"""First-message greeting to a newly added non-owner friend.

Before this feature, ``friend.added`` was a pure awareness event: the server
already creates the direct conversation inside the accept transaction, but the
signal only carries the counterparty ``usr_…`` id, nothing in the plugin could
translate that into a ``cnv_…`` id, and no turn was ever produced. The agent
therefore never spoke first to anyone but its owner.

Two pieces are tested here:

* ``ClawChatApiClient.get_direct_conversation`` — the REST wrapper for
  ``POST /v1/conversations/direct``, also exposed as the
  ``clawchat_get_direct_conversation`` tool.
* ``ClawChatAdapter._on_notify_signal`` — a non-owner ``friend.added`` now
  resolves the direct conversation and feeds one synthetic greeting turn into
  ``_handle_inbound`` addressed to that conversation. Owner events, replays of
  the same ``event_id``, a disabled ``friend_greeting`` flag, and a failed
  conversation lookup all produce no turn.

The adapter tests run against a real ``ClawChatStore`` on a real SQLite file so
the replay dedupe is proven by the ledger, not by a mock.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clawchat_gateway import api_client
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.api_client import ClawChatApiClient, ClawChatApiError
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.greeting import (
    FRIEND_GREETING_PROMPT,
    load_friend_greeting_prompt,
)
from clawchat_gateway.storage import ClawChatStore

OWNER = "usr_owner00000000000000000000"
FRIEND = "usr_friend0000000000000000000"
CONV = "cnv_01HZZZZZZZZZZZZZZZZZZZZZZZ"


# ---------------------------------------------------------------------------
# api_client.get_direct_conversation
# ---------------------------------------------------------------------------


class _FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_get_direct_conversation_posts_peer_id_and_returns_conversation(monkeypatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        seen["method"] = request.get_method()
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["content_type"] = request.get_header("Content-type")
        return _FakeResponse(
            json.dumps({"code": 0, "data": {"conversation": {"id": CONV, "type": "direct"}}}).encode("utf-8")
        )

    monkeypatch.setattr(api_client, "urlopen", fake_urlopen)
    client = ClawChatApiClient(base_url="https://api.example.invalid", token="t", user_id="usr_1")

    result = await client.get_direct_conversation(FRIEND)

    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.example.invalid/v1/conversations/direct"
    assert seen["body"] == {"peer_id": FRIEND}
    assert seen["content_type"] == "application/json"
    assert result == {"conversation": {"id": CONV, "type": "direct"}}


@pytest.mark.asyncio
async def test_get_direct_conversation_rejects_blank_peer_id_before_network(monkeypatch) -> None:
    def must_not_be_called(request, timeout=None):  # pragma: no cover - guard
        raise AssertionError("no HTTP request expected for a blank peer_id")

    monkeypatch.setattr(api_client, "urlopen", must_not_be_called)
    client = ClawChatApiClient(base_url="https://api.example.invalid", token="t", user_id="usr_1")

    with pytest.raises(ClawChatApiError) as excinfo:
        await client.get_direct_conversation("   ")
    assert excinfo.value.kind == "validation"


# ---------------------------------------------------------------------------
# greeting.load_friend_greeting_prompt
# ---------------------------------------------------------------------------


def test_friend_greeting_prompt_falls_back_when_override_missing(tmp_path) -> None:
    assert load_friend_greeting_prompt(home_dir=tmp_path, language="en") == (
        f"{FRIEND_GREETING_PROMPT}\n\nReply in English."
    )


def test_friend_greeting_prompt_falls_back_when_override_blank(tmp_path) -> None:
    (tmp_path / "clawchat").mkdir()
    (tmp_path / "clawchat" / "friend-greeting.md").write_text("  \n\t\n", encoding="utf-8")
    assert load_friend_greeting_prompt(home_dir=tmp_path, language="en") == (
        f"{FRIEND_GREETING_PROMPT}\n\nReply in English."
    )


def test_friend_greeting_prompt_uses_override_when_present(tmp_path) -> None:
    (tmp_path / "clawchat").mkdir()
    (tmp_path / "clawchat" / "friend-greeting.md").write_text("  Say hi in French.  \n", encoding="utf-8")
    # The language instruction is appended even over a user override — the
    # override says what to say, not which language, so it is not a full
    # replacement of the built-in text.
    assert load_friend_greeting_prompt(home_dir=tmp_path, language="en") == (
        "Say hi in French.\n\nReply in English."
    )


def test_friend_greeting_prompt_is_distinct_from_owner_activation_prompt() -> None:
    from clawchat_gateway.greeting import ACTIVATION_BOOTSTRAP_PROMPT

    assert FRIEND_GREETING_PROMPT != ACTIVATION_BOOTSTRAP_PROMPT
    assert "owner" in FRIEND_GREETING_PROMPT.lower()


# ---------------------------------------------------------------------------
# config.friend_greeting
# ---------------------------------------------------------------------------


def test_config_friend_greeting_defaults_on() -> None:
    cfg = ClawChatConfig.from_platform_config(SimpleNamespace(extra={"websocket_url": "wss://x"}))
    assert cfg.friend_greeting is True


def test_config_friend_greeting_can_be_disabled() -> None:
    cfg = ClawChatConfig.from_platform_config(
        SimpleNamespace(extra={"websocket_url": "wss://x", "friend_greeting": False})
    )
    assert cfg.friend_greeting is False


# ---------------------------------------------------------------------------
# adapter: friend.added -> one greeting turn
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path) -> ClawChatStore:
    s = ClawChatStore(tmp_path / "clawchat.db")
    s.initialize()
    assert not s._disabled, "test store failed to initialize; the rest is meaningless"
    return s


@pytest.fixture
def adapter(store, tmp_path, monkeypatch) -> ClawChatAdapter:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    a = ClawChatAdapter({})
    a._store = store
    monkeypatch.setattr(a, "_owner_locale", lambda: None)
    # awareness_note is off by default, so _handle_inbound only ever sees the
    # greeting turn. The config dataclass is frozen: tests that flip flags use
    # dataclasses.replace.
    a._clawchat_config = ClawChatConfig(websocket_url="wss://x", owner_user_id=OWNER)

    inbounds: list[Any] = []

    async def capture_inbound(inbound):
        inbounds.append(inbound)

    a._handle_inbound = capture_inbound  # type: ignore[method-assign]
    a._test_inbounds = inbounds  # type: ignore[attr-defined]

    lookups: list[str] = []

    async def fake_rest(call):
        class _Client:
            async def get_direct_conversation(self, peer_id: str) -> dict:
                lookups.append(peer_id)
                return {"conversation": {"id": CONV, "type": "direct"}}

        return await call(_Client())

    a._rest_with_auth_retry = fake_rest  # type: ignore[method-assign]
    a._test_lookups = lookups  # type: ignore[attr-defined]
    return a


def _friend_added(entity_id: str, event_id: str = "evt_1") -> dict[str, Any]:
    return {
        "version": "2",
        "event": "notify.signal",
        "trace_id": "t",
        "emitted_at": 0,
        "payload": {
            "type": "friend.added",
            "entity_id": entity_id,
            "event_id": event_id,
            "version": 1,
            "message_id": f"notify:friend.added:{entity_id}",
        },
    }


async def _drain(adapter: ClawChatAdapter) -> None:
    await asyncio.gather(*list(adapter._friend_greeting_tasks))


@pytest.mark.asyncio
async def test_non_owner_friend_added_greets_in_resolved_direct_conversation(adapter) -> None:
    await adapter._on_notify_signal(_friend_added(FRIEND))
    await _drain(adapter)

    assert adapter._test_lookups == [FRIEND]
    assert len(adapter._test_inbounds) == 1
    inbound = adapter._test_inbounds[0]
    assert inbound.chat_id == CONV
    assert inbound.chat_type == "direct"
    assert inbound.sender_id == FRIEND
    # No owner locale recorded in this store, so nothing is known about the
    # owner's language — the greeting must carry NO language line rather than
    # hard-asserting English at someone who may not speak it.
    assert inbound.text == FRIEND_GREETING_PROMPT
    assert "Reply in" not in inbound.text
    assert inbound.raw_message.get("synthetic") is True
    assert inbound.raw_message.get("friend_greeting") is True


@pytest.mark.asyncio
async def test_non_owner_friend_added_greets_in_owners_resolved_language(adapter) -> None:
    # The recipient is the new friend, not the owner — but the greeting uses
    # the OWNER's reported locale anyway (see docs/configuration.md): friends
    # are usually in the same language circle, and the backend exposes
    # `locale` only on the owner profile endpoint.
    adapter._owner_locale = lambda: "ja-JP"  # type: ignore[method-assign]

    await adapter._on_notify_signal(_friend_added(FRIEND))
    await _drain(adapter)

    assert len(adapter._test_inbounds) == 1
    assert adapter._test_inbounds[0].text.endswith("Reply in Japanese.")


@pytest.mark.asyncio
async def test_owner_friend_added_does_not_greet(adapter) -> None:
    await adapter._on_notify_signal(_friend_added(OWNER))
    await _drain(adapter)

    assert adapter._test_lookups == []
    assert adapter._test_inbounds == []


@pytest.mark.asyncio
async def test_replayed_event_id_greets_only_once(adapter) -> None:
    await adapter._on_notify_signal(_friend_added(FRIEND, event_id="evt_same"))
    await _drain(adapter)
    await adapter._on_notify_signal(_friend_added(FRIEND, event_id="evt_same"))
    await _drain(adapter)

    assert len(adapter._test_inbounds) == 1


@pytest.mark.asyncio
async def test_re_added_friend_with_new_event_id_greets_again(adapter) -> None:
    await adapter._on_notify_signal(_friend_added(FRIEND, event_id="evt_a"))
    await _drain(adapter)
    await adapter._on_notify_signal(_friend_added(FRIEND, event_id="evt_b"))
    await _drain(adapter)

    assert len(adapter._test_inbounds) == 2


@pytest.mark.asyncio
async def test_friend_greeting_disabled_produces_no_turn(adapter) -> None:
    adapter._clawchat_config = dataclasses.replace(adapter._clawchat_config, friend_greeting=False)

    await adapter._on_notify_signal(_friend_added(FRIEND))
    await _drain(adapter)

    assert adapter._test_lookups == []
    assert adapter._test_inbounds == []


@pytest.mark.asyncio
async def test_conversation_lookup_failure_is_swallowed(adapter) -> None:
    async def failing_rest(call):
        raise ClawChatApiError("api", "peer must be friend", status=200, code=19012)

    adapter._rest_with_auth_retry = failing_rest  # type: ignore[method-assign]

    await adapter._on_notify_signal(_friend_added(FRIEND))
    await _drain(adapter)

    assert adapter._test_inbounds == []


@pytest.mark.asyncio
async def test_friend_added_without_event_id_still_greets(adapter) -> None:
    frame = _friend_added(FRIEND)
    del frame["payload"]["event_id"]

    await adapter._on_notify_signal(frame)
    await _drain(adapter)

    assert len(adapter._test_inbounds) == 1


# ---------------------------------------------------------------------------
# tools.get_direct_conversation + clawchat_get_direct_conversation registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_get_direct_conversation_returns_conversation(monkeypatch) -> None:
    from clawchat_gateway import tools

    class _Client:
        async def get_direct_conversation(self, peer_id: str) -> dict:
            assert peer_id == FRIEND
            return {"conversation": {"id": CONV, "type": "direct"}}

    monkeypatch.setattr(tools, "_build_client", lambda: (_Client(), None))

    result = await tools.get_direct_conversation(f"  {FRIEND}  ")

    assert result == {"conversation": {"id": CONV, "type": "direct"}}


@pytest.mark.asyncio
async def test_tool_get_direct_conversation_requires_user_id(monkeypatch) -> None:
    from clawchat_gateway import tools

    def must_not_build():  # pragma: no cover - guard
        raise AssertionError("client must not be built for an empty userId")

    monkeypatch.setattr(tools, "_build_client", must_not_build)

    result = await tools.get_direct_conversation("")

    assert result["error"] == "validation"
    assert "userId" in result["message"]


@pytest.mark.asyncio
async def test_tool_get_direct_conversation_surfaces_not_friend_api_error(monkeypatch) -> None:
    from clawchat_gateway import tools

    class _Client:
        async def get_direct_conversation(self, peer_id: str) -> dict:
            raise ClawChatApiError("api", "peer must be friend", status=200, path="/v1/conversations/direct", code=19012)

    monkeypatch.setattr(tools, "_build_client", lambda: (_Client(), None))

    result = await tools.get_direct_conversation(FRIEND)

    assert result["error"] == "api"
    assert result["meta"]["code"] == 19012


def test_get_direct_conversation_tool_is_registered() -> None:
    from clawchat_gateway import plugin_tools

    registered: dict[str, dict] = {}

    class _Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            registered[name] = {"schema": schema, "handler": handler, "kw": kw}

    plugin_tools.register_tools(_Ctx())

    entry = registered["clawchat_get_direct_conversation"]
    assert entry["schema"]["parameters"]["required"] == ["userId"]
    assert entry["kw"].get("is_async") is True
    assert "cnv_" in entry["schema"]["description"]


def test_get_direct_conversation_tool_is_declared_in_manifest() -> None:
    import re
    from pathlib import Path

    manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
    assert re.search(r"^\s*-\s*clawchat_get_direct_conversation\s*$", manifest, re.M)
