"""Original test names must stay selectable when a real-host module is proxied."""
import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).parent
REPO = TESTS.parent


def _collect(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-p", "no:cacheprovider", *args],
        cwd=REPO, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    assert result.returncode == 0, result.stdout
    return result.stdout


def test_class_method_node_id_selects_original_name():
    out = _collect("tests/test_multiplex_isolation.py::Isolation::test_sender_and_media_routing")
    assert "test_sender_and_media_routing" in out
    assert "test_connections_coexist" not in out


def test_function_node_id_and_k_select_original_name():
    out = _collect("tests/test_orchestrate_real_host_context.py::test_platform_placeholder_registration_is_scoped")
    assert "test_platform_placeholder_registration_is_scoped" in out
    out = _collect("tests/test_orchestrate_real_host_context.py", "-k", "placeholder_registration")
    assert "test_platform_placeholder_registration_is_scoped" in out
    assert "test_real_dispatch" not in out
    assert "test_real_host_subprocess" not in out
