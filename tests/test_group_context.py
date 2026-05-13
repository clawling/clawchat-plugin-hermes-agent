from clawchat_gateway.group_context import (
    DEFAULT_GROUP_COVENANT,
    build_group_channel_prompt,
    format_group_covenant_prompt,
)


def test_format_group_covenant_prompt_ignores_blank_text():
    assert format_group_covenant_prompt("") is None
    assert format_group_covenant_prompt("   ") is None


def test_format_group_covenant_prompt_wraps_text():
    prompt = format_group_covenant_prompt(
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )

    assert prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )


def test_build_group_channel_prompt_uses_default_covenant(monkeypatch):
    monkeypatch.setattr(
        "clawchat_gateway.group_context.DEFAULT_GROUP_COVENANT",
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。",
    )

    prompt = build_group_channel_prompt()

    assert prompt == (
        "ClawChat group covenant:\n"
        "群里陈平安是落魄山共和国的国王。它的位置在卡拉米星球的洪荒大陆上。"
    )


def test_default_group_covenant_contains_e2e_fixture_text():
    assert "群里陈平安是落魄山共和国的国王" in DEFAULT_GROUP_COVENANT
    assert "卡拉米星球的洪荒大陆" in DEFAULT_GROUP_COVENANT
