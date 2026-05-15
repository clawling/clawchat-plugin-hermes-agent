from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dev_install_uninstalls_manifest_plugin_name():
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
    plugin_name = manifest["name"]

    dev_install = (REPO_ROOT / ".e2e" / "dev_install.md").read_text()

    assert f"hermes plugins uninstall {plugin_name}" in dev_install


def test_dev_install_uses_v012_compatible_activation_entrypoint():
    dev_install = (REPO_ROOT / ".e2e" / "dev_install.md").read_text()

    assert "/opt/data/plugins/clawchat/clawchat_cli.py activate" in dev_install
    assert "hermes clawchat activate CLAWCHAT_CODE_GOES_HERE" not in dev_install


def test_install_docs_include_v012_compatible_activation_entrypoint():
    install_doc = (REPO_ROOT / "install.md").read_text()
    readme = (REPO_ROOT / "README.md").read_text()

    assert 'python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate' in install_doc
    assert "hermes clawchat activate CLAWCHAT_CODE_GOES_HERE" in install_doc
    assert 'python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate' in readme


def test_local_start_test_removes_stale_installed_plugin_after_seed():
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
    plugin_name = manifest["name"]

    script = (REPO_ROOT / ".e2e" / "local_start_test.sh").read_text()

    assert f"./.e2e/tmp/hermes_data/plugins/{plugin_name}" in script
