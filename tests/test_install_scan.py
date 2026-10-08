"""The tracked tree must pass Hermes' own install-time security scan.

``hermes plugins install`` clones this repository and runs Hermes'
``tools.plugin_guard.scan_plugin`` over the clone (``plugins.scan_on_install``,
on by default). Every external plugin is ``community`` trust, and for that a
``dangerous`` verdict is a hard block that ``--force`` does not override. One
CRITICAL finding anywhere in the tree is enough, and the scanner reads tests
and docs too — a destructive shell literal in a test fixture once blocked the
install outright.

The scanner's rules change between Hermes releases, and users install with
the Hermes they have, not the newest: v0.21.6+ downgrades findings under
``tests/``, v0.21.0 (image v2026.8.31) does not, so a test literal passed the
check here and still blocked every v0.21.0 install (batch3 e2e, 2026-10-08).
So the scan also runs with the scanner as it was at each tag in
:data:`PINNED_SCANNER_TAGS` (override with ``HERMES_SCAN_TAGS``, comma
separated), read out of the same checkout with ``git show``; a tag the checkout
does not have is skipped.

This check runs the real scanner, so it needs a Hermes Agent source checkout:
``$HERMES_SOURCE_DIR``, or the ignored ``tmp/hermes`` / ``tmp/hermes-agent``
(see ``docs/hermes-source-lookup.md``). Without one it skips. The scanner runs
in a subprocess so its ``tools`` package never shadows anything in this test
session, and over a copy of exactly the tracked files — what a clone contains.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Hermes release tags whose install scanner the tracked tree must also pass.
#: v2026.8.31 is Hermes v0.21.0, the e2e image and the oldest scanner that
#: reads tests/ at full severity.
PINNED_SCANNER_TAGS = ("v2026.8.31",)
_SCANNER_FILES = ("tools/plugin_guard.py", "tools/skills_guard.py")


def _scanner_tags() -> list[str]:
    raw = os.environ.get("HERMES_SCAN_TAGS")
    if raw is not None:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return list(PINNED_SCANNER_TAGS)


def _hermes_source() -> Path | None:
    candidates = []
    if os.environ.get("HERMES_SOURCE_DIR"):
        candidates.append(Path(os.environ["HERMES_SOURCE_DIR"]))
    candidates += [REPO_ROOT / "tmp" / "hermes", REPO_ROOT / "tmp" / "hermes-agent"]
    for path in candidates:
        if (path / "tools" / "plugin_guard.py").is_file():
            return path
    return None


def _tracked_files() -> list[str] | None:
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=False
    )
    if proc.returncode != 0:
        return None
    return [p for p in proc.stdout.decode().split("\0") if p]


_SCAN = r"""
import json, sys
from pathlib import Path
from tools.plugin_guard import scan_plugin, should_allow_plugin_install
r = scan_plugin(Path(sys.argv[1]), source="install-scan-test")
allowed, reason = should_allow_plugin_install(r, force=False)
print(json.dumps({
    "verdict": r.verdict,
    "reason": reason,
    "critical": [
        f"{f.file}:{f.line} {f.pattern_id} {f.match!r}"
        for f in r.findings if f.severity == "critical"
    ],
}))
"""


def _pinned_scanner(hermes: Path, tag: str, dest: Path) -> Path | None:
    """The scanner modules as of ``tag``, alone in a fresh package dir.

    Only the two guard modules are taken; ``tools/__init__.py`` is left empty
    so nothing else of that Hermes version is imported.
    """
    if shutil.which("git") is None:
        return None
    root = dest / tag
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "__init__.py").write_text("")
    for rel in _SCANNER_FILES:
        proc = subprocess.run(
            ["git", "show", f"{tag}:{rel}"], cwd=hermes, capture_output=True, check=False
        )
        if proc.returncode != 0:
            return None
        (root / rel).write_bytes(proc.stdout)
    return root


def _copy_tracked_tree(dest: Path) -> Path | None:
    files = _tracked_files()
    if files is None:
        return None
    tree = dest / "clawchat"
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():  # deleted in the work tree but still in the index
            continue
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return tree


def _run_scan(scanner_root: Path, tree: Path) -> dict | None:
    env = {**os.environ, "PYTHONPATH": str(scanner_root)}
    proc = subprocess.run(
        [sys.executable, "-c", _SCAN, str(tree)],
        cwd=scanner_root, env=env, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("tag", _scanner_tags() or [pytest.param("", marks=pytest.mark.skip(reason="no pinned tags"))])
def test_tracked_tree_passes_the_install_scanner_of_older_hermes(tmp_path, tag):
    hermes = _hermes_source()
    if hermes is None:
        pytest.skip("no Hermes source checkout (set HERMES_SOURCE_DIR or use tmp/hermes)")
    scanner = _pinned_scanner(hermes, tag, tmp_path / "scanners")
    if scanner is None:
        pytest.skip(f"Hermes checkout has no {tag} scanner (not a git checkout, or tag missing)")
    tree = _copy_tracked_tree(tmp_path)
    if tree is None:
        pytest.skip("not a git checkout")
    result = _run_scan(scanner, tree)
    if result is None:
        pytest.skip(f"{tag} scanner not importable here")

    assert result["critical"] == [], (tag, result["critical"])
    assert result["verdict"] != "dangerous", (tag, result["reason"])


def test_tracked_tree_is_not_blocked_by_the_hermes_install_scanner(tmp_path):
    hermes = _hermes_source()
    if hermes is None:
        pytest.skip("no Hermes source checkout (set HERMES_SOURCE_DIR or use tmp/hermes)")
    files = _tracked_files()
    if files is None:
        pytest.skip("not a git checkout")

    tree = tmp_path / "clawchat"
    for rel in files:
        src = REPO_ROOT / rel
        if not src.is_file():  # deleted in the work tree but still in the index
            continue
        dst = tree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    env = {**os.environ, "PYTHONPATH": str(hermes)}
    proc = subprocess.run(
        [sys.executable, "-c", _SCAN, str(tree)],
        cwd=hermes, env=env, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"Hermes scanner not importable here: {proc.stderr.strip()[-300:]}")
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert result["critical"] == [], result["critical"]
    assert result["verdict"] != "dangerous", result["reason"]
