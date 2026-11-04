"""Shared lookup for tests that need a real Hermes Agent source checkout."""

from __future__ import annotations

from functools import cache
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_HOST_SUBPROCESS = "HERMES_REAL_HOST_SUBPROCESS"


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


def _output_tail(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        output = output.decode(errors="replace")
    return "\n".join((output or "").splitlines()[-80:])


def _run_on_real_host(marker: str, *args: str) -> subprocess.CompletedProcess[str]:
    if os.environ.get(REAL_HOST_SUBPROCESS) == "1":
        pytest.fail("real Hermes host unavailable in its subprocess; refusing recursive retry", pytrace=False)
    source = require_hermes_source(marker)
    pythonpath = os.pathsep.join((str(source), str(REPO_ROOT)))
    if inherited := os.environ.get("PYTHONPATH"):
        pythonpath += os.pathsep + inherited
    try:
        result = subprocess.run(
            [sys.executable, *args], cwd=REPO_ROOT,
            env={**os.environ, "PYTHONPATH": pythonpath, REAL_HOST_SUBPROCESS: "1"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            check=False, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"real Hermes subprocess timed out after 600s:\n{_output_tail(exc.stdout)}", pytrace=False)
    if result.returncode != 0:
        pytest.fail(
            f"real Hermes subprocess exited {result.returncode}:\n{_output_tail(result.stdout)}",
            pytrace=False,
        )
    return result


def run_pytest_on_real_host(test_path: Path, marker: str, *pytest_args: str) -> None:
    """Run actual host tests outside the session's doubles; all-skipped is failure."""
    result = _run_on_real_host(
        marker, "-m", "pytest", str(test_path), "-p", "no:cacheprovider", *pytest_args,
    )
    if re.search(r"\b[1-9]\d* passed\b", result.stdout) is None:
        pytest.fail(
            f"real Hermes subprocess reported no passed tests:\n{_output_tail(result.stdout)}",
            pytrace=False,
        )


def run_python_on_real_host(script: str, marker: str) -> None:
    """Run host assertions in a fresh interpreter with the shared source lookup."""
    _run_on_real_host(marker, "-c", script)
