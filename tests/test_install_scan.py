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

The newer scanners in :data:`HIGH_CLEAN_SCANNER_TAGS` (override with
``HERMES_HIGH_CLEAN_SCAN_TAGS``, comma separated) and the checkout's HEAD
scanner must report zero high findings as well as zero critical findings.

This check runs the real scanner, so it needs a Hermes Agent source checkout:
``$HERMES_SOURCE_DIR``, or the ignored ``tmp/hermes`` / ``tmp/hermes-agent``
of this checkout or, from a linked worktree, of the main checkout (see
``docs/hermes-source-lookup.md``). The shared ``hermes_source.py`` helper lists
searched paths when skipping; ``HERMES_SOURCE_REQUIRED=1`` fails instead.
The scanner runs
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

from hermes_source import require_hermes_source

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Hermes release tags whose install scanner the tracked tree must also pass.
#: v2026.8.31 is Hermes v0.21.0, the e2e image and the oldest scanner that
#: reads tests/ at full severity. v2026.9.11 is Hermes v0.21.2, the last whose
#: ``dns_exfil`` rule fires on the prose word "host" followed by any ``$`` on
#: the line (fixed upstream in v0.21.3).
PINNED_SCANNER_TAGS = ("v2026.8.31", "v2026.9.11")
#: A single high finding is a *caution* verdict: the install stops until the
#: owner approves it. v2026.9.21 carries the scanner of the v2026.9.21 /
#: v2026.9.24 releases, v0.21.6 the first that steps findings under tests/
#: down, and v0.22.5 is the newest Hermes release. The scanner of v2026.9.14
#: and older still rates two lines of the bundled skills high; those lines come
#: from the shared skill source, not from this repository.
HIGH_CLEAN_SCANNER_TAGS = ("v2026.9.21", "v0.21.6", "v0.22.5")


def _scanner_tags(
    env_var: str = "HERMES_SCAN_TAGS",
    defaults: tuple[str, ...] = PINNED_SCANNER_TAGS,
) -> list[str]:
    raw = os.environ.get(env_var)
    if raw is not None:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return list(defaults)


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
    "high": [
        (f.file, f.pattern_id)
        for f in r.findings if f.severity == "high"
    ],
}))
"""


def _pinned_scanner(hermes: Path, tag: str, dest: Path) -> Path | None:
    """The scanner modules as of ``tag``, alone in a fresh package dir.

    All guard modules under ``tools/`` are taken; ``tools/__init__.py`` is left empty
    so nothing else of that Hermes version is imported.
    """
    if shutil.which("git") is None:
        return None
    listing = subprocess.run(
        ["git", "ls-tree", "--name-only", tag, "tools/"],
        cwd=hermes, capture_output=True, text=True, check=False,
    )
    if listing.returncode != 0:
        return None
    modules = [
        rel for rel in listing.stdout.splitlines()
        if "guard" in Path(rel).name and rel.endswith(".py")
    ]
    if not {"tools/plugin_guard.py", "tools/skills_guard.py"}.issubset(modules):
        return None
    root = dest / tag
    (root / "tools").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "__init__.py").write_text("")
    for rel in modules:
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
    hermes = require_hermes_source("tools/plugin_guard.py")
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


@pytest.mark.parametrize(
    "tag",
    _scanner_tags("HERMES_HIGH_CLEAN_SCAN_TAGS", HIGH_CLEAN_SCANNER_TAGS)
    or [pytest.param("", marks=pytest.mark.skip(reason="no high-clean pinned tags"))],
)
def test_tracked_tree_has_no_high_findings(tmp_path, tag):
    hermes = require_hermes_source("tools/plugin_guard.py")
    scanner = _pinned_scanner(hermes, tag, tmp_path / "scanners")
    if scanner is None:
        pytest.skip(f"Hermes checkout has no {tag} scanner (not a git checkout, or tag missing)")
    tree = _copy_tracked_tree(tmp_path)
    if tree is None:
        pytest.skip("not a git checkout")
    result = _run_scan(scanner, tree)
    assert result is not None, f"{tag} scanner not importable here"

    assert result["critical"] == [], (tag, result["critical"])
    assert result["verdict"] != "dangerous", (tag, result["reason"])
    assert result["high"] == [], (tag, result["high"])


def test_tracked_tree_is_not_blocked_by_the_hermes_install_scanner(tmp_path):
    hermes = require_hermes_source("tools/plugin_guard.py")
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
    assert result["high"] == [], result["high"]
    assert result["verdict"] != "dangerous", result["reason"]
