from __future__ import annotations


def test_setup_clawchat_platform_activates_without_restart(monkeypatch, capsys):
    from clawchat_gateway import setup as setup_mod

    prompts: list[str] = []
    answers = iter(["ABC123", ""])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    async def fake_activate_and_maybe_restart(code: str, *, base_url: str, restart: bool):
        assert code == "ABC123"
        assert base_url == setup_mod.DEFAULT_BASE_URL
        assert restart is False
        return {
            "ok": True,
            "user_id": "agent-1",
            "base_url": base_url,
            "websocket_url": "ws://company.newbaselab.com:10086/ws",
            "token": "***",
            "refresh_token": "***",
        }

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(
        setup_mod,
        "activate_and_maybe_restart",
        fake_activate_and_maybe_restart,
    )

    setup_mod.setup_clawchat_platform()

    out = capsys.readouterr().out
    assert "ClawChat activation complete" in out
    assert "agent-1" in out
    assert "Hermes gateway setup will handle the final gateway service step" in out
    assert "will offer to restart" not in out
    assert prompts[0].startswith("ClawChat activation code")
    assert prompts[1].startswith("ClawChat API base URL")


def test_setup_clawchat_platform_exits_on_missing_code(monkeypatch, capsys):
    from clawchat_gateway import setup as setup_mod

    monkeypatch.setattr("builtins.input", lambda prompt: "")

    setup_mod.setup_clawchat_platform()

    out = capsys.readouterr().out
    assert "No activation code entered" in out
