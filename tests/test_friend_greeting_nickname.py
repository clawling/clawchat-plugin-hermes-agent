"""The greeting to a new friend knows the friend's nickname.

The synthetic friend-greeting turn used to carry ``sender_name=""``, and the
friend's profile is otherwise fetched only when they first write: a brand-new
friend has no cached nickname yet, so the prompt showed their ``usr_…`` id and
the agent greeted an id. (The greeting prompt itself tells the agent not to
call tools just to greet, so it could not look the name up either.)

Now the greeting turn first refreshes the friend's profile, bounded by a
timeout, and resolves ``sender_name`` from it. A slow or failing lookup never
blocks or drops the greeting: it goes out as before.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clawchat_gateway import adapter as adapter_mod
from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.storage import ClawChatStore

OWNER = "usr_owner"
FRIEND = "usr_friend"
CONV = "cnv_friend"


@pytest.fixture
def adapter(tmp_path) -> ClawChatAdapter:
    store = ClawChatStore(tmp_path / "clawchat.db")
    store.initialize()
    a = ClawChatAdapter({})
    a._store = store
    a._clawchat_config = ClawChatConfig(websocket_url="wss://x", owner_user_id=OWNER)
    a._test_inbounds = []  # type: ignore[attr-defined]
    a._test_cache = {}  # type: ignore[attr-defined]

    async def capture_inbound(inbound):
        a._test_inbounds.append(inbound)

    async def fake_rest(call):
        class _Client:
            async def get_direct_conversation(self, peer_id: str) -> dict:
                return {"conversation": {"id": CONV, "type": "direct"}}

        return await call(_Client())

    a._handle_inbound = capture_inbound  # type: ignore[method-assign]
    a._rest_with_auth_retry = fake_rest  # type: ignore[method-assign]
    a._sender_metadata = lambda inbound: dict(a._test_cache.get(inbound.sender_id, {}))  # type: ignore[method-assign]
    return a


def _signal() -> dict[str, Any]:
    return {
        "version": "2",
        "event": "notify.signal",
        "trace_id": "t",
        "emitted_at": 0,
        "payload": {
            "type": "friend.added",
            "entity_id": FRIEND,
            "event_id": "evt_1",
            "version": 1,
            "message_id": f"notify:friend.added:{FRIEND}",
        },
    }


async def _greet(adapter) -> Any:
    await adapter._on_notify_signal(_signal())
    await asyncio.gather(*list(adapter._friend_greeting_tasks))
    assert len(adapter._test_inbounds) == 1
    return adapter._test_inbounds[0]


@pytest.mark.asyncio
async def test_greeting_refreshes_the_friend_and_uses_their_nickname(adapter) -> None:
    refreshed: list[str] = []

    async def refresh(user_id: str) -> bool:
        refreshed.append(user_id)
        adapter._test_cache[user_id] = {"nickname": "Ada"}
        return True

    adapter._refresh_user_profile = refresh  # type: ignore[method-assign]
    inbound = await _greet(adapter)
    assert refreshed == [FRIEND]
    assert inbound.sender_name == "Ada"


@pytest.mark.asyncio
async def test_a_slow_profile_lookup_does_not_hold_the_greeting(adapter, monkeypatch) -> None:
    monkeypatch.setattr(adapter_mod, "FRIEND_GREETING_PROFILE_TIMEOUT_SECONDS", 0.05)

    async def hang(_user_id: str) -> bool:
        await asyncio.sleep(30)
        return True

    adapter._refresh_user_profile = hang  # type: ignore[method-assign]
    inbound = await asyncio.wait_for(_greet(adapter), timeout=5)
    assert inbound.chat_id == CONV
    assert inbound.raw_message.get("friend_greeting") is True


@pytest.mark.asyncio
async def test_a_failed_profile_lookup_still_greets(adapter) -> None:
    async def boom(_user_id: str) -> bool:
        raise RuntimeError("profile service down")

    adapter._refresh_user_profile = boom  # type: ignore[method-assign]
    inbound = await _greet(adapter)
    assert inbound.chat_id == CONV
