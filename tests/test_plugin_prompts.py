from __future__ import annotations

from pathlib import Path

import clawchat_gateway.plugin_prompts as plugin_prompts


def test_prompt_loader_exposes_only_shipped_prompt_files(tmp_path: Path) -> None:
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "platform.md").write_text("platform prompt\n", encoding="utf-8")
    (prompts_dir / "default-owner-behavior.md").write_text("owner default\n", encoding="utf-8")
    (prompts_dir / "default-group-bio.md").write_text("group default\n", encoding="utf-8")
    (prompts_dir / "user.md").write_text("legacy user prompt\n", encoding="utf-8")
    (prompts_dir / "group.md").write_text("legacy group prompt\n", encoding="utf-8")

    prompts = plugin_prompts.load_clawchat_prompts_from_root(tmp_path)

    assert prompts == {
        "platform": "platform prompt",
        "default_owner_behavior": "owner default",
        "default_group_bio": "group default",
    }
    assert not hasattr(plugin_prompts, "user_prompt")
    assert not hasattr(plugin_prompts, "group_prompt")
    assert not hasattr(plugin_prompts, "mode_prompt")


def test_clawchat_skill_routes_natural_language_output_visibility() -> None:
    skill = (Path(__file__).parents[1] / "skills" / "clawchat" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "## Output Visibility" in skill
    assert "quiet mode" in skill
    assert "`/clawchat-output minimal`" in skill
    assert "conversation mode" in skill
    assert "`/clawchat-output normal`" in skill
    assert "dev mode" in skill
    assert "`/clawchat-output full`" in skill
