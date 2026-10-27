"""Pin the released skills ref, default fetch URLs and bundled core version."""

import re

from clawchat_gateway import skill_update


def test_default_skills_ref_is_pinned_to_skills_v1_19_0():
    assert re.fullmatch(r"^skills-v\d+\.\d+\.\d+$", skill_update.DEFAULT_SKILLS_REF)
    assert skill_update.DEFAULT_SKILLS_REF == "skills-v1.19.0"


def test_default_manifest_url_uses_skills_v1_19_0():
    assert skill_update.manifest_url() == (
        "https://raw.githubusercontent.com/clawling/clawchat-plugin-install-cli/"
        "skills-v1.19.0/skills/manifest.json"
    )


def test_default_skill_content_url_uses_skills_v1_19_0():
    assert skill_update.skill_content_url("hermes/clawchat-core/SKILL.md") == (
        "https://raw.githubusercontent.com/clawling/clawchat-plugin-install-cli/"
        "skills-v1.19.0/skills/hermes/clawchat-core/SKILL.md"
    )


def test_bundled_clawchat_core_is_1_19_1_ahead_of_the_pinned_tag():
    path = skill_update.bundled_skills_dir() / "clawchat-core" / "SKILL.md"
    assert skill_update.skill_version(path) == "1.19.1"
