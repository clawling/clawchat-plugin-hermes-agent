"""Hermes runtime notices must not reach the chat as if the agent said them.

What a user sees in ClawChat may only be the agent's reply. Three Hermes
notices kept leaking through the plain ``send()`` path:

* ``ℹ️ Context compression deferred — summary still streaming. …``;
* ``💾 Self-improvement review: …`` — Hermes' own Discord adapter filters it;
* ``⚠ Stream stalled mid tool-call (…); the action was not executed. …`` —
  either alone, or appended to the partial reply text with a blank line.
"""

from __future__ import annotations

import pytest

from clawchat_gateway.adapter import ClawChatAdapter

STALLED = (
    "⚠ Stream stalled mid tool-call (terminal, web_search); the action was not "
    "executed. Ask me to retry if you want to continue."
)


@pytest.fixture
def adapter(monkeypatch) -> ClawChatAdapter:
    a = ClawChatAdapter({})
    monkeypatch.setattr(a, "_runtime_status_messages_enabled", lambda: False)
    return a


@pytest.mark.parametrize(
    "notice",
    [
        "ℹ️ Context compression deferred — summary still streaming. Will retry next turn.",
        "ℹ Context compression deferred — summary still streaming.",
        "💾 Self-improvement review: Memory updated",
        "  💾 Self-improvement review: Skill 'release-notes' patched",
        STALLED,
        "\n\n" + STALLED,
    ],
)
def test_notice_is_suppressed(adapter, notice):
    assert adapter._should_suppress_runtime_status_message(notice) is True


@pytest.mark.parametrize(
    "reply",
    [
        "Self-improvement review: this is a normal heading the agent wrote",
        "💾 Saved your file.",
        "Context compression deferred is a Hermes log line, here is what it means.",
        "The stream stalled yesterday, but it works now.",
    ],
)
def test_ordinary_reply_is_not_suppressed(adapter, reply):
    assert adapter._should_suppress_runtime_status_message(reply) is False


def test_notices_shown_when_owner_opted_into_runtime_status(monkeypatch):
    a = ClawChatAdapter({})
    monkeypatch.setattr(a, "_runtime_status_messages_enabled", lambda: True)
    assert a._should_suppress_runtime_status_message(
        "💾 Self-improvement review: Memory updated"
    ) is False


def test_stalled_tail_is_stripped_from_a_partial_reply(adapter):
    content = "Here is what I found so far." + "\n\n" + STALLED
    assert adapter._filter_output_content(content) == "Here is what I found so far."


def test_stalled_warning_in_the_middle_of_a_stitched_reply_is_stripped(adapter):
    content = "Part one." + "\n\n" + STALLED + "\n\nPart two after the retry."
    assert adapter._filter_output_content(content) == "Part one.\n\nPart two after the retry."


def test_stalled_tail_kept_when_owner_opted_into_runtime_status(monkeypatch):
    a = ClawChatAdapter({})
    monkeypatch.setattr(a, "_runtime_status_messages_enabled", lambda: True)
    content = "Partial." + "\n\n" + STALLED
    assert "Stream stalled" in a._filter_output_content(content)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "notice",
    [
        "ℹ️ Context compression deferred — summary still streaming. …",
        "💾 Self-improvement review: Memory updated",
        STALLED,
    ],
)
async def test_send_drops_the_notice_without_a_frame(adapter, monkeypatch, notice):
    frames: list = []

    async def record(frame, **_kwargs):
        frames.append(frame)
        return True

    monkeypatch.setattr(adapter._connection, "send_frame", record)

    result = await adapter.send("cnv_group_1", notice, metadata={"chat_type": "group"})

    assert result.success is True
    assert frames == []


# Hermes sends the shutdown / restart notice to every chat with a running agent
# through plain send(); it is gateway lifecycle, not the agent's reply.
GATEWAY_SHUTDOWN = "⚠️ Gateway shutting down — Your current task will be interrupted."
GATEWAY_RESTART = (
    "⚠️ Gateway restarting — Your current task will be interrupted. "
    "Send any message after restart and I'll try to resume where you left off."
)


@pytest.mark.parametrize(
    "notice",
    [
        GATEWAY_SHUTDOWN,
        GATEWAY_RESTART,
        GATEWAY_SHUTDOWN.replace("️", ""),
        GATEWAY_RESTART.replace("️", ""),
    ],
)
def test_gateway_lifecycle_notice_is_suppressed(adapter, notice):
    assert adapter._should_suppress_runtime_status_message(notice) is True


def test_gateway_lifecycle_reply_text_is_not_suppressed(adapter):
    assert adapter._should_suppress_runtime_status_message(
        "Gateway restarting is safe: your sessions survive it."
    ) is False


def test_gateway_lifecycle_notice_shown_when_owner_opted_in(monkeypatch):
    a = ClawChatAdapter({})
    monkeypatch.setattr(a, "_runtime_status_messages_enabled", lambda: True)
    assert a._should_suppress_runtime_status_message(GATEWAY_RESTART) is False
