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


def _official_snapshot_fetcher():
    """Serve the bundled snapshot as the official manifest at DEFAULT_SKILLS_REF."""
    skills = skill_update.bundled_skills_dir()
    manifest = json.loads((skills / 'manifest.json').read_bytes())
    by_url = {skill_update.manifest_url(): (skills / 'manifest.json').read_bytes()}
    for skill_id, entry in manifest['skills']['hermes'].items():
        by_url[skill_update.skill_content_url(entry['path'])] = (
            skills / skill_id / 'SKILL.md').read_bytes()

    def fetch(url):
        return by_url[url]
    return fetch


def test_accepted_update_then_restart_neither_rolls_back_nor_reseeds():
    # An owner-approved update converges every managed skill to the ref's
    # manifest; the next plugin load must keep those bytes and the next check
    # must find nothing to do, so bundled and released versions cannot loop.
    fetch = _official_snapshot_fetcher()
    skill_id = 'clawchat-liveware'
    managed = skill_update.managed_skill_path(skill_id)
    managed.parent.mkdir(parents=True)
    managed.write_text(
        '---\nname: clawchat-liveware\nversion: 1.3.1\n---\n\nOld procedure.\n',
        encoding='utf-8',
    )
    skill_update.write_local_manifest({skill_id: '1.3.1'})

    first = skill_update.check_skill_update(fetcher=fetch)
    assert skill_id in {u.skill_id for u in first.updates}
    skill_update.apply_skill_update(first.updates, fetcher=fetch)
    updated = managed.read_bytes()

    for bundled_id in skill_update.bundled_skill_ids():
        skill_update.seed_managed_skill(
            bundled_id, skill_update.bundled_skills_dir() / bundled_id / 'SKILL.md')

    assert managed.read_bytes() == updated
    assert skill_update.check_skill_update(fetcher=fetch).updates == []


def test_bundled_snapshot_versions_match_their_manifest():
    skills = skill_update.bundled_skills_dir()
    manifest = json.loads((skills / 'manifest.json').read_bytes())
    for skill_id, entry in manifest['skills']['hermes'].items():
        raw = (skills / skill_id / 'SKILL.md').read_bytes()
        assert skill_update.skill_version(skills / skill_id / 'SKILL.md') == entry['version'], skill_id
        assert hashlib.sha256(raw).hexdigest() == entry['sha256'], skill_id
