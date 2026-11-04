"""New state writes require the atomic helper or a counted, reasoned exception."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tests" / "check_state_writes.py"
_spec = importlib.util.spec_from_file_location("state_write_gate", SCRIPT)
gate = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = gate
_spec.loader.exec_module(gate)


def tree(tmp_path, source):
    directory = tmp_path / "clawchat_gateway"
    directory.mkdir()
    (directory / "example.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_repository_writes_are_all_registered():
    assert gate.check(ROOT) == []


def test_new_write_is_rejected(tmp_path):
    root = tree(tmp_path, 'from pathlib import Path\nPath(x).write_text("y")\n')
    errors = gate.check(root, allowlist=[])
    assert len(errors) == 1
    assert "unregistered state write: clawchat_gateway/example.py:2" in errors[0]
    assert "Path(x).write_text in <module>" in errors[0]


def test_stale_allowlist_entry_is_rejected(tmp_path):
    root = tree(tmp_path, "pass\n")
    entry = ("clawchat_gateway/example.py", "missing", "path.write_text", 1, "C", "C: debug snapshot")
    errors = gate.check(root, allowlist=[entry])
    assert len(errors) == 1 and "stale allowlist entry" in errors[0]


def test_repeated_key_counts_cannot_drift(tmp_path):
    root = tree(tmp_path, 'def save():\n    path.write_bytes(b"a")\n    path.write_bytes(b"b")\n')
    entry = ("clawchat_gateway/example.py", "save", "path.write_bytes", 1, "C", "C: disposable output")
    assert "count exceeded" in gate.check(root, allowlist=[entry])[0]
    assert gate.check(root, allowlist=[(*entry[:3], 2, *entry[4:])]) == []
    assert "stale allowlist entry" in gate.check(root, allowlist=[(*entry[:3], 3, *entry[4:])])[0]


def test_string_replace_is_ignored_and_path_replace_is_found(tmp_path):
    root = tree(tmp_path, 'text.replace("a", "b")\nPath("a").replace(x)\n')
    findings = gate.scan(root)
    assert len(findings) == 1
    assert findings[0].key == ("clawchat_gateway/example.py", "<module>", "Path('a').replace")
    assert findings[0].line == 2


def test_write_patterns_and_qualified_names(tmp_path):
    root = tree(tmp_path, '''
class Store:
    async def save(self):
        open(p, "wb")
        open(p, mode="a")
        p.open("x")
        p.open(mode="r+")
        os.fdopen(fd, "w")
        os.fdopen(fd, mode="a")
        os.open(p, os.O_RDONLY | os.O_CREAT)
        os.open(p, flags=O_RDWR)
        os.replace(a, b)
        os.rename(a, b)
        os.link(a, b)
        os.symlink(a, b)
        p.rename(b)
        p.write_text("a")
        p.write_bytes(b"a")
        shutil.move(a, b)
        shutil.copy(a, b)
        shutil.copy2(a, b)
        shutil.copyfile(a, b)
        shutil.copytree(a, b)
        open(p)
        p.open("rb")
        os.open(p, os.O_RDONLY)
        tempfile.mkstemp()
        tempfile.NamedTemporaryFile(mode="w")
        tempfile.mkdtemp()
''')
    findings = gate.scan(root)
    assert len(findings) == 22
    assert {f.qualname for f in findings} == {"Store.save"}
    assert {"tempfile.mkstemp", "tempfile.NamedTemporaryFile"} <= {f.func_text for f in findings}
    assert "tempfile.mkdtemp" not in {f.func_text for f in findings}


@pytest.mark.parametrize("source", [
    "import tempfile\nfd, name = tempfile.mkstemp()\n",
    "import tempfile\nwith tempfile.NamedTemporaryFile('w', delete=False) as f:\n    f.write('y')\n",
    "from tempfile import mkstemp\nfd, name = mkstemp()\n",
])
def test_temp_file_writes_are_rejected(tmp_path, source):
    errors = gate.check(tree(tmp_path, source), allowlist=[])
    assert len(errors) == 1 and "unregistered state write" in errors[0]


def test_only_production_paths_are_scanned(tmp_path):
    root = tree(tmp_path, "pass")
    for directory in ("tests", "scripts"):
        (root / directory).mkdir()
        (root / directory / "ignored.py").write_text('open(p, "w")')
    (root / "clawchat_gateway" / "atomic_file.py").write_text('open(p, "w")')
    (root / "__init__.py").write_text('open(p, "w")')
    (root / "clawchat_cli.py").write_text('open(p, "w")')
    assert {f.path for f in gate.scan(root)} == {"__init__.py", "clawchat_cli.py"}


def test_script_accepts_current_tree():
    result = subprocess.run([sys.executable, str(SCRIPT)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "findings, all registered" in result.stdout
