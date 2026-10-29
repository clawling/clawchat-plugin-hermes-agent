"""Keep each profile's plugin copy a virtual Hermes workspace member."""

import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


ROOT = Path(__file__).resolve().parent.parent
OLD_BUILD_SYSTEM = """\
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

"""
HOST_IMPORT_SCRIPT = """\
import importlib.metadata
import importlib.util
from pathlib import Path
import sys
import types

plugin_dir = Path(sys.argv[1]).resolve()
namespace = types.ModuleType("hermes_plugins")
namespace.__path__ = [str(plugin_dir.parent)]
sys.modules["hermes_plugins"] = namespace
spec = importlib.util.spec_from_file_location(
    "hermes_plugins.clawchat",
    plugin_dir / "__init__.py",
    submodule_search_locations=[str(plugin_dir)],
)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

import clawchat_gateway

print(Path(clawchat_gateway.__file__).resolve())
try:
    importlib.metadata.distribution("clawchat-gateway")
except importlib.metadata.PackageNotFoundError:
    print("distribution found: False")
else:
    print("distribution found: True")
"""


def _member_name(pyproject_doc, plugin_dir: Path) -> str:
    """Mirrors Hermes PM's workspace-member naming, 0.22+."""
    if (
        "build-system" not in pyproject_doc
        and pyproject_doc.get("tool", {}).get("uv", {}).get("package") is not True
    ):
        resolved = plugin_dir.resolve()
        suffix = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:16]
        return f"hermes-plugin-{resolved.name}-{suffix}"
    return pyproject_doc["project"]["name"]


def _copy_pyproject(plugin_dir: Path, *, old: bool = False) -> dict:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if old:
        text = OLD_BUILD_SYSTEM + text
    path = plugin_dir / "pyproject.toml"
    path.write_text(text, encoding="utf-8")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_repo_is_virtual_with_runtime_dependencies():
    with (ROOT / "pyproject.toml").open("rb") as stream:
        doc = tomllib.load(stream)

    assert "build-system" not in doc
    assert doc.get("tool", {}).get("uv", {}).get("package") is not True
    assert doc["tool"]["uv"]["package"] is False
    assert any(spec.startswith("websockets") for spec in doc["project"]["dependencies"])


def test_multi_profile_copies_have_distinct_virtual_names(tmp_path):
    dirs = [
        tmp_path / "home" / "plugins" / "clawchat",
        tmp_path / "home" / "profiles" / "coder" / "plugins" / "clawchat",
    ]
    names = [_member_name(_copy_pyproject(path), path) for path in dirs]

    assert len(set(names)) == 2
    assert all(name.startswith("hermes-plugin-clawchat-") for name in names)
    assert "clawchat-gateway" not in names


def test_single_profile_upgrade_changes_name_and_preserves_dependencies(tmp_path):
    plugin_dir = tmp_path / "home" / "plugins" / "clawchat"
    old_doc = _copy_pyproject(plugin_dir, old=True)
    new_doc = _copy_pyproject(plugin_dir)
    suffix = hashlib.sha256(str(plugin_dir.resolve()).encode("utf-8")).hexdigest()[:16]

    assert _member_name(old_doc, plugin_dir) == "clawchat-gateway"
    assert _member_name(new_doc, plugin_dir) == f"hermes-plugin-clawchat-{suffix}"
    assert old_doc["project"]["dependencies"] == new_doc["project"]["dependencies"]


def test_existing_duplicates_require_fewer_than_two_old_copies(tmp_path):
    default = tmp_path / "home" / "plugins" / "clawchat"
    coder = tmp_path / "home" / "profiles" / "coder" / "plugins" / "clawchat"
    writer = tmp_path / "home" / "profiles" / "writer" / "plugins" / "clawchat"
    default_old = _member_name(_copy_pyproject(default, old=True), default)
    coder_old = _member_name(_copy_pyproject(coder, old=True), coder)
    writer_old = _member_name(_copy_pyproject(writer, old=True), writer)

    assert default_old == coder_old == "clawchat-gateway"
    default_new = _member_name(_copy_pyproject(default), default)
    assert len({default_new, coder_old}) == 2
    assert len({default_new, coder_old, writer_old}) == 2  # Three profiles still collide.


def test_collision_recovery_removes_old_copies_before_updating(tmp_path):
    def _collides(copies):
        names = []
        for profile, version in copies.items():
            home = tmp_path / "home"
            if profile != "default":
                home = home / "profiles" / profile
            plugin_dir = home / "plugins" / "clawchat"
            doc = _copy_pyproject(plugin_dir, old=version == "old")
            names.append(_member_name(doc, plugin_dir))
        return len(names) != len(set(names))

    copies = {"default": "old", "coder": "old", "writer": "old"}
    assert _collides(copies)
    copies["default"] = "new"
    assert _collides(copies)  # Updating one of three cannot pass the solve.

    two_copies = {"default": "old", "coder": "old"}
    assert _collides(two_copies)
    two_copies["default"] = "new"
    assert not _collides(two_copies)

    copies = {"default": "old", "coder": "old", "writer": "old"}
    del copies["coder"]
    assert _collides(copies)  # Remove can proceed without solving the workspace.
    del copies["writer"]
    assert not _collides(copies)
    copies["default"] = "new"
    assert not _collides(copies)
    for profile in ("coder", "writer"):
        copies[profile] = "new"
        assert not _collides(copies)


def test_host_import_from_copied_tree_without_installing_distribution(tmp_path):
    plugin_dir = tmp_path / "plugins" / "clawchat"
    plugin_dir.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(ROOT / name, plugin_dir / name)
    for name in ("clawchat_gateway", "prompts", "skills"):
        source = ROOT / name
        if source.exists():
            shutil.copytree(
                source,
                plugin_dir / name,
                ignore=shutil.ignore_patterns("tests", ".git", ".venv", "__pycache__"),
            )

    result = subprocess.run(
        [sys.executable, "-I", "-c", HOST_IMPORT_SCRIPT, str(plugin_dir)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    imported_path = Path(result.stdout.splitlines()[0])
    assert imported_path.is_relative_to(plugin_dir.resolve())
    assert imported_path == plugin_dir.resolve() / "clawchat_gateway" / "__init__.py"


def test_runtime_does_not_read_own_distribution_metadata():
    # Match both qualified calls and functions imported from importlib.metadata.
    lookup = re.compile(
        r"""\b(?:version|distribution|metadata|requires|files|from_name)\s*\(\s*"""
        r"""(?:distribution_name\s*=\s*)?["']clawchat[-_]gateway["']"""
    )
    paths = sorted((ROOT / "clawchat_gateway").rglob("*.py"))
    paths.extend([ROOT / "__init__.py", ROOT / "clawchat_cli.py"])

    for path in paths:
        assert not lookup.search(path.read_text(encoding="utf-8")), (
            f"{path.relative_to(ROOT)} requires installed plugin distribution metadata; "
            "Hermes imports the plugin directly from its source directory"
        )
