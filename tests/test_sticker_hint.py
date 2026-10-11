"""Stickers: the agent learns the syntax, and a lone sticker token goes out as is.

A sticker on ClawChat is a plain text message whose whole body is
``[claw:spark.<id>]``; the client renders it as one of the built-in Spark
stickers. Hermes has no plugin-owned plain-text send tool (the reply itself
is the text, and ``clawchat_mention_message`` always carries a mention), so
the sentence lives in the platform hint, verbatim as the app and the OpenClaw
plugin teach it. The id list is the app's whole Spark pack; the app pins the
same list against its pack.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from clawchat_gateway.adapter import ClawChatAdapter
from clawchat_gateway.plugin_prompts import platform_prompt

STICKER_LINE = (
    "A lone `[claw:spark.<id>]` message is a sticker; ids: "
    "hi what ok go yay wow aww grr thx hug nice break."
)
SPARK_IDS = ["hi", "what", "ok", "go", "yay", "wow", "aww", "grr", "thx", "hug", "nice", "break"]


def test_platform_hint_carries_the_sticker_sentence_verbatim():
    assert STICKER_LINE in platform_prompt().splitlines()


def test_sticker_sentence_lists_the_whole_spark_pack():
    ids = STICKER_LINE.split("ids: ", 1)[1].rstrip(".").split()
    assert ids == SPARK_IDS


AGENT = "usr_agent"
OWNER = "usr_owner"
GROUP = "cnv_group"
DM = "cnv_dm"


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id=AGENT, owner_user_id=OWNER)
    a.frames = []

    async def send_frame(frame, **_kw):
        a.frames.append(frame)
        return True

    metadata = {
        ("group", GROUP): {"participant_ids": f"{OWNER},{AGENT},usr_bean"},
        ("owner", "owner"): {"agent_owner_nickname": "Ann", "agent_nickname": "Otter"},
        ("user", "usr_bean"): {"nickname": "Bean"},
    }
    monkeypatch.setattr(a, "_read_memory_metadata", lambda t, i: dict(metadata.get((t, i), {})))
    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    a._known_chat_types[GROUP] = "group"
    a._known_chat_types[DM] = "direct"
    return a


def _replies(adapter):
    return [f["payload"] for f in adapter.frames if f["event"] == "message.reply"]


@pytest.mark.parametrize("chat_id", [GROUP, DM])
@pytest.mark.parametrize("sticker_id", SPARK_IDS)
async def test_a_lone_sticker_token_is_sent_untouched(adapter, chat_id, sticker_id):
    token = f"[claw:spark.{sticker_id}]"
    await adapter.send(chat_id, token)
    [payload] = _replies(adapter)
    assert payload["message"]["body"]["fragments"] == [{"kind": "text", "text": token}]
    assert payload["message"]["context"]["mentions"] == []
    assert payload["message_mode"] == "normal"


@pytest.mark.parametrize("chat_id", [GROUP, DM])
async def test_a_streamed_sticker_reply_ends_as_the_lone_token(adapter, chat_id):
    first = await adapter.send(chat_id, "[claw:spark. ▉")
    await adapter.edit_message(chat_id, first.message_id, "[claw:spark.hi]", finalize=True)
    payload = _replies(adapter)[-1]
    assert payload["message"]["body"]["fragments"] == [{"kind": "text", "text": "[claw:spark.hi]"}]
    assert payload["message"]["context"]["mentions"] == []
