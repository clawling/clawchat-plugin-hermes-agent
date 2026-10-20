from clawchat_gateway.greeting import (
    ACTIVATION_BOOTSTRAP_PROMPT,
    FRIEND_GREETING_PROMPT,
    load_activation_bootstrap_prompt,
    load_friend_greeting_prompt,
)


def test_appends_language_line(tmp_path):
    text = load_activation_bootstrap_prompt(tmp_path, "ja")
    assert text.endswith("Reply in Japanese.")
    assert "You are now connected to a ClawChat direct conversation" in text


def test_appends_after_user_override(tmp_path):
    (tmp_path / "clawchat").mkdir()
    (tmp_path / "clawchat" / "greeting.md").write_text("MY OWN PROMPT", encoding="utf-8")
    text = load_activation_bootstrap_prompt(tmp_path, "ko")
    assert "MY OWN PROMPT" in text
    assert text.endswith("Reply in Korean.")


def test_omits_language_line_when_unknown(tmp_path):
    """An unknown owner locale must assert NO language, not English.

    The owner's locale comes off disk and may simply not have landed yet, and
    neither greeting dispatch path may grow an await to wait for it. Before
    this feature no language was asserted at all and the model inferred one
    from context; hard-asserting English at a Chinese owner is strictly worse.
    """
    text = load_activation_bootstrap_prompt(tmp_path)
    assert text == ACTIVATION_BOOTSTRAP_PROMPT
    assert "Reply in" not in text


def test_omits_language_line_over_user_override_too(tmp_path):
    (tmp_path / "clawchat").mkdir()
    (tmp_path / "clawchat" / "greeting.md").write_text("MY OWN PROMPT", encoding="utf-8")
    assert load_activation_bootstrap_prompt(tmp_path, None) == "MY OWN PROMPT"


def test_friend_greeting_appends_too(tmp_path):
    assert load_friend_greeting_prompt(tmp_path, "es").endswith("Reply in Spanish.")


def test_friend_greeting_omits_language_line_when_unknown(tmp_path):
    """Symmetric with the activation greeting — same cold-read, same rule."""
    text = load_friend_greeting_prompt(tmp_path)
    assert text == FRIEND_GREETING_PROMPT
    assert "Reply in" not in text
