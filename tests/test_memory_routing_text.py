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
