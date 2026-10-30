"""Bundled Liveware procedure updates reach older managed skill copies."""

import hashlib
import json
import os
from pathlib import Path

from clawchat_gateway import skill_update


def test_older_managed_liveware_skill_is_reseeded_on_load():
    skill_id = 'clawchat-liveware'
    bundled = skill_update.bundled_skills_dir() / skill_id / 'SKILL.md'
    version = skill_update.skill_version(bundled)
    assert version == '1.3.2'
    assert skill_update.is_version_older('1.3.1', version)
    managed = skill_update.managed_skill_path(skill_id)
    assert managed.is_relative_to(Path(os.environ['HERMES_HOME']))
    managed.parent.mkdir(parents=True)
    managed.write_text(
        '---\nname: clawchat-liveware\nversion: 1.3.1\n---\n\nOld procedure.\n',
        encoding='utf-8',
    )
    skill_update.write_local_manifest({skill_id: '1.3.1'})

    # Plugin load calls this real seeding function before registering the path.
    registered = skill_update.seed_managed_skill(skill_id, bundled)

    assert registered == managed
    assert managed.read_bytes() == bundled.read_bytes()
    assert skill_update.skill_version(managed) == version
    assert skill_update.read_local_manifest()[skill_id] == version
    assert 'clawchat_list_apps()' in managed.read_text(encoding='utf-8')


def test_bundled_liveware_manifest_matches_raw_skill_bytes():
    skills = skill_update.bundled_skills_dir()
    bundled = skills / 'clawchat-liveware' / 'SKILL.md'
    raw = bundled.read_bytes()
    manifest = json.loads((skills / 'manifest.json').read_bytes())
    entry = manifest['skills']['hermes']['clawchat-liveware']
    assert 'clawchat_list_apps()' in raw.decode('utf-8')
    assert entry['version'] == skill_update.skill_version(bundled) == '1.3.2'
    assert entry['sha256'] == hashlib.sha256(raw).hexdigest()
    assert entry['bytes'] == len(raw)
