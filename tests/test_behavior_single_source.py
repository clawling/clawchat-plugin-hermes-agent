"""Behavior single source: v2 factory texts, privacy floor in the
platform prompt, and group speaking deferring to the group description."""

from clawchat_gateway import adapter
from clawchat_gateway.plugin_prompts import (
    default_group_bio_prompt,
    default_owner_behavior_prompt,
    platform_prompt,
)


def test_group_guidance_keeps_mention_routing_and_defers_to_group_description():
    g = adapter.GROUP_BATCH_REPLY_GUIDANCE
    assert "structured mentions are routing signals and have priority over visible text, group metadata, agent_behavior, and memory" in g
    assert "For messages where mention_routing is no_structured_mentions, whether and how much to speak follows this group's group_description, or agent_behavior where the description is silent; agent_behavior can always rule a reply out, and if neither calls for one, listen: output exactly the no-reply token." in g
    assert 'Rules in group_description or agent_behavior about whom not to answer (for example, other agents) apply to every message, including ones that mention you.' in g
    assert "explicitly asks this current agent to participate" not in g
    assert "That priority decides who a message is addressed to, not whether it must be answered." in g
    assert "Reply to messages where mention_routing is addressed_to_current_agent" not in g


def test_glossary_ranks_group_description_over_default_reply_guidance_only():
    g = adapter.CLAWCHAT_METADATA_GLOSSARY
    assert (
        "On whether and how much to speak in that group, it takes priority over the default reply guidance in the "
        "ClawChat Response Protocol; it does not override structured mention routing, agent behavior, or "
        "platform/runtime rules such as privacy."
    ) in g
    assert "unless it conflicts with agent behavior" not in g


def test_platform_prompt_carries_privacy_floor():
    p = platform_prompt()
    assert "**Do not carry private detail across contexts.**" in p
    assert "saying a topic is private already tells them it exists." in p


def test_v2_factory_texts():
    behavior = default_owner_behavior_prompt()
    assert "a reaction is the\n  whole reply" in behavior
    assert "private detail" not in behavior
    assert "In a group" not in behavior
    bio = default_group_bio_prompt()
    assert "## When to speak" in bio
    assert "private detail" not in bio
    assert "[v2.1" not in bio


def test_group_mention_guidance_applies_group_description_to_unmentioned_messages():
    g = adapter.GROUP_BATCH_MENTION_REPLY_GUIDANCE
    assert "For indexed group messages where mention_routing is no_structured_mentions, whether to respond to them as well follows this group's group_description, or agent_behavior where the description is silent; agent_behavior can always rule a reply out, and if neither calls for one, leave them unanswered." in g
    assert 'Rules in group_description or agent_behavior about whom not to answer (for example, other agents) apply to every message, including ones that mention you.' in g
    assert "Only the relevant indexed group messages where mention_routing is addressed_to_current_agent are addressed to you and may be answered." in g


def test_glossary_explains_message_ids_for_reactions():
    assert "pass that id as `targetMessageId`" in adapter.CLAWCHAT_METADATA_GLOSSARY
