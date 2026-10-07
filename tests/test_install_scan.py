"""The tracked tree must pass Hermes' own install-time security scan.

``hermes plugins install`` clones this repository and runs Hermes'
``tools.plugin_guard.scan_plugin`` over the clone (``plugins.scan_on_install``,
on by default). Every external plugin is ``community`` trust, and for that a
``dangerous`` verdict is a hard block that ``--force`` does not override. One
CRITICAL finding anywhere in the tree is enough, and the scanner reads tests
and docs too — a destructive shell literal in a test fixture once blocked the
install outright.

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
