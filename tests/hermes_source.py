"""Shared lookup for tests that need a real Hermes Agent source checkout."""

from __future__ import annotations

from functools import cache
import os
from pathlib import Path
import shutil
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@cache
def main_checkout() -> Path | None:
    """Find the main working tree, including repositories used as submodules."""
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return None
    common = Path(proc.stdout.strip())
    if common.name == ".git":
        return common.parent
    # A submodule keeps its git dir under the superproject's .git/modules/.
    proc = subprocess.run(
        ["git", "config", "--file", str(common / "config"), "core.worktree"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return (common / proc.stdout.strip()).resolve()


def candidates() -> list[Path]:
    paths = []
    if source_dir := os.environ.get("HERMES_SOURCE_DIR"):
        paths.append(Path(source_dir))
    roots = [REPO_ROOT]
    main = main_checkout()
    if main is not None and main != REPO_ROOT:
        roots.append(main)
    for root in roots:
        paths.extend((root / "tmp" / "hermes", root / "tmp" / "hermes-agent"))
    return paths


def find_hermes_source(marker: str) -> Path | None:
    for candidate in candidates():
        if (candidate / marker).is_file():
            return candidate.resolve()
    return None


def require_hermes_source(marker: str) -> Path:
    source = find_hermes_source(marker)
    if source is not None:
        return source
    reason = (
        f"no Hermes source checkout containing {marker}; searched: "
        + ", ".join(str(path) for path in candidates())
    )
    if source_dir := os.environ.get("HERMES_SOURCE_DIR"):
        reason += f"; HERMES_SOURCE_DIR was set to {source_dir} but lacks {marker}"
    if os.environ.get("HERMES_SOURCE_REQUIRED") == "1":
        pytest.fail(reason)
    pytest.skip(reason, allow_module_level=True)
