"""The rule for where a remembered fact goes is in text the model sees every turn.

Hermes' own ``memory`` tool is always present and tells the model to save "who
the user is". Without a stronger, always-present counter-rule the model keeps
putting ClawChat people into MEMORY.md. The rule therefore lives in the two
places that are in every turn: the platform hint (``prompts/platform.md``) and
the ``clawchat_memory_write`` tool description.
"""

from __future__ import annotations

from clawchat_gateway import plugin_tools
from clawchat_gateway.plugin_prompts import platform_prompt


class _Ctx:
    def __init__(self) -> None:
        self.tools: dict[str, dict] = {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = schema


def _write_description() -> str:
    ctx = _Ctx()
    plugin_tools.register_tools(ctx)
    return ctx.tools["clawchat_memory_write"]["description"]


def _assert_routing(text: str) -> None:
    assert "targetType=user" in text
    assert "targetType=group" in text
    assert "targetType=owner" in text
    assert "MEMORY.md" in text
    assert "clawchat_memory_read" in text


def test_platform_prompt_routes_people_and_groups_to_clawchat_notes():
    prompt = platform_prompt()
    _assert_routing(prompt)
    assert "not instructions" in prompt
    # The privacy floor is still there.
    assert "**Do not carry private detail across contexts.**" in prompt


def test_memory_write_description_carries_the_routing_rule():
    _assert_routing(_write_description())


# --- The rule is also in every turn's injected context -----------------------
#
# The platform hint is part of the session's system prompt, which Hermes builds
# once per session and reuses. A session that started before the hint carried
# the routing paragraph never sees it, so the model can keep saving people into
# Hermes' memory, whose MEMORY.md / USER.md go into the system prompt of every
# conversation (including groups). A one-line reminder in the per-turn channel
# prompt reaches old and new sessions alike.

import pytest  # noqa: E402

from clawchat_gateway.adapter import MEMORY_ROUTING_REMINDER, ClawChatAdapter  # noqa: E402
from clawchat_gateway.inbound import InboundMessage  # noqa: E402


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return ClawChatAdapter({})


def _turn(chat_type):
    return InboundMessage(
        chat_id="cnv_x",
        chat_type=chat_type,
        sender_id="usr_someone",
        sender_name="",
        text="hi",
        raw_message={"payload": {"message_id": "m1"}},
    )


def test_reminder_is_one_line_naming_the_tool_and_the_forbidden_stores():
    assert "\n" not in MEMORY_ROUTING_REMINDER.strip()
    for needle in ("clawchat_memory_write", "MEMORY.md", "USER.md", "memory"):
        assert needle in MEMORY_ROUTING_REMINDER
    for target in ("user", "group", "owner"):
        assert target in MEMORY_ROUTING_REMINDER


@pytest.mark.parametrize("chat_type", ["direct", "group"])
def test_every_turn_carries_the_reminder(adapter, chat_type):
    parts = adapter._compose_channel_prompt_parts(_turn(chat_type))
    found = [p for p in parts if p["id"] == "memory-routing"]
    assert found and found[0]["content"] == MEMORY_ROUTING_REMINDER
    assert MEMORY_ROUTING_REMINDER in adapter._compose_channel_prompt(_turn(chat_type))
