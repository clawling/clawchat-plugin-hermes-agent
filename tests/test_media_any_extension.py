"""ClawChat's legacy MEDIA fallback validates paths without rebinding Hermes."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.platforms import base
from clawchat_gateway.adapter import ClawChatAdapter


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def old_host(monkeypatch, tmp_path):
    """Install only test-local host methods, restored even with a real host loaded."""
    adapter = base.BasePlatformAdapter
    monkeypatch.delattr(base, "MEDIA_EXTENSIONLESS_TAG_RE", raising=False)
    for name in (
        "validate_media_delivery_path", "_mask_protected_spans", "_mask_json_string_media",
    ):
        monkeypatch.delattr(adapter, name, raising=False)

    def extract(content):
        media = []

        def take(match):
            media.append((match.group("path"), "[[audio_as_voice]]" in content))
            return ""

        cleaned = re.sub(r"MEDIA:(?P<path>/[^\s]+\.(?:png|pdf))\b", take, content)
        cleaned = cleaned.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "")
        return media, cleaned

    def validate(path):
        candidate = Path(path)
        if candidate.is_file() and candidate.name != "denied.py":
            return str(candidate.resolve())
        return None

    validator = Mock(side_effect=validate)
    monkeypatch.setattr(adapter, "extract_media", staticmethod(extract), raising=False)
    monkeypatch.setattr(base, "validate_media_delivery_path", validator, raising=False)
    paths = {}
    for name in ("payload.py", "report.pdf", "denied.py"):
        path = tmp_path / name
        path.write_text("test attachment\n", encoding="utf-8")
        paths[name] = str(path)
    return SimpleNamespace(extract=extract, validate=validator, paths=paths)


@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("instance_call", [False, True])
def test_old_host_delivers_other_extensions(old_host, voice, instance_call):
    path = old_host.paths["payload.py"]
    content = ("[[audio_as_voice]]\n" if voice else "") + f"Ready\nMEDIA:{path}"
    adapter = object.__new__(ClawChatAdapter) if instance_call else ClawChatAdapter

    media, cleaned = adapter.extract_media(content)

    assert media == [(path, voice)]
    assert cleaned.strip() == "Ready"
    old_host.validate.assert_called_once_with(path)


def test_known_extension_and_repeated_paths_are_not_duplicated(old_host):
    pdf = old_host.paths["report.pdf"]
    code = old_host.paths["payload.py"]
    media, cleaned = ClawChatAdapter.extract_media(f"MEDIA:{pdf}\nMEDIA:{code}\nMEDIA:{code}")
    assert media == [(pdf, False), (code, False)]
    assert cleaned == ""


def test_path_already_returned_by_host_is_not_added_again(old_host, monkeypatch):
    path = old_host.paths["payload.py"]
    monkeypatch.setattr(base.BasePlatformAdapter, "extract_media", staticmethod(
        lambda content: ([(path, False)], content)
    ))
    assert ClawChatAdapter.extract_media(f"MEDIA:{path}") == ([(path, False)], "")


@pytest.mark.parametrize("failure", ["missing", "denied", "false", "exception"])
def test_failed_validation_keeps_tag(old_host, tmp_path, failure):
    path = str(tmp_path / "missing.py") if failure == "missing" else old_host.paths["denied.py"]
    if failure == "false":
        old_host.validate.side_effect = None
        old_host.validate.return_value = False
    elif failure == "exception":
        old_host.validate.side_effect = ValueError("validation unavailable")
    content = f"Keep this\nMEDIA:{path}"
    assert ClawChatAdapter.extract_media(content) == ([], content)


def test_missing_validator_keeps_tag(old_host, monkeypatch):
    monkeypatch.delattr(base, "validate_media_delivery_path")
    content = f"MEDIA:{old_host.paths['payload.py']}"
    assert ClawChatAdapter.extract_media(content) == ([], content)


def test_class_static_validator_is_supported(old_host, monkeypatch):
    monkeypatch.delattr(base, "validate_media_delivery_path")
    monkeypatch.setattr(base.BasePlatformAdapter, "validate_media_delivery_path",
                        staticmethod(old_host.validate), raising=False)
    path = old_host.paths["payload.py"]
    assert ClawChatAdapter.extract_media(f"MEDIA:{path}") == ([(path, False)], "")


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoted_home_path_is_normalized(old_host, monkeypatch, tmp_path, quote):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "with spaces.py"
    path.write_text("test attachment", encoding="utf-8")
    content = f"MEDIA:{quote}~/with spaces.py,.;:)}}]{quote}"
    assert ClawChatAdapter.extract_media(content) == ([(str(path), False)], "")
    old_host.validate.assert_called_once_with(str(path))


def test_validator_returned_path_is_used(old_host, tmp_path):
    alias = tmp_path / "alias.py"
    alias.symlink_to(old_host.paths["payload.py"])
    assert ClawChatAdapter.extract_media(f"MEDIA:{alias}") == (
        [(old_host.paths["payload.py"], False)], "",
    )


def test_host_masks_preserve_examples_and_offsets(old_host, monkeypatch):
    path = old_host.paths["payload.py"]
    pdf = old_host.paths["report.pdf"]
    calls = []

    def mask(content, pattern, name):
        calls.append(name)
        return re.sub(pattern, lambda m: re.sub(r"[^\n]", " ", m.group()), content)

    monkeypatch.setattr(base.BasePlatformAdapter, "_mask_protected_spans", staticmethod(
        lambda text: mask(text, r"(?ms)```.*?```|`[^`\n]+`|^>[^\n]*", "protected")
    ), raising=False)
    monkeypatch.setattr(base.BasePlatformAdapter, "_mask_json_string_media", staticmethod(
        lambda text: mask(text, r'\{"result": "[^"\n]*"\}', "json")
    ), raising=False)
    examples = f'```\nMEDIA:{path}\n```\n`MEDIA:{path}`\n> MEDIA:{path}\n{{"result": "MEDIA:{path}"}}'
    content = f"[[audio_as_voice]]\nMEDIA:{pdf}\n{examples}\nMEDIA:{path}"

    media, cleaned = ClawChatAdapter.extract_media(content)

    assert media == [(pdf, True), (path, True)]
    assert examples in cleaned
    assert cleaned.count(f"MEDIA:{path}") == 4
    assert calls == ["protected", "json"]
    old_host.validate.assert_called_once_with(path)


def test_new_host_result_is_returned_unchanged(old_host, monkeypatch):
    content = f"MEDIA:{old_host.paths['payload.py']}\n\n\n"
    expected = old_host.extract(content)
    extract = Mock(return_value=expected)
    monkeypatch.setattr(base.BasePlatformAdapter, "extract_media", staticmethod(extract))
    monkeypatch.setattr(base, "MEDIA_EXTENSIONLESS_TAG_RE", object(), raising=False)

    assert ClawChatAdapter.extract_media(content) is expected
    assert expected == ([], content)
    extract.assert_called_once_with(content)
    old_host.validate.assert_not_called()


@pytest.mark.parametrize("entrypoint", ["_register_platform", "register"])
def test_registration_leaves_shared_media_state_unchanged(old_host, monkeypatch, entrypoint):
    sentinel = object()
    monkeypatch.setattr(base, "MEDIA_TAG_CLEANUP_RE", sentinel, raising=False)
    assert not hasattr(base, "_clawchat_all_exts_patch")
    original = base.BasePlatformAdapter.extract_media
    spec = importlib.util.spec_from_file_location("clawchat_media_entry_test", REPO_ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)

    assert base.MEDIA_TAG_CLEANUP_RE is sentinel
    assert base.BasePlatformAdapter.extract_media is original
    for name in (
        "_migrate_legacy_config_tokens", "_ensure_host_defaults", "_warn_on_shared_identity",
        "_patch_send_message_target_parser", "_patch_send_message_media_delivery",
        "_configure_runtime_defaults", "_start_liveware_cli_download", "_register_skill",
        "_register_cli_commands", "_register_commands", "_register_llm_context_debug_hooks",
        "_register_session_usage_hook", "_register_host_tools_guard",
    ):
        monkeypatch.setattr(plugin, name, lambda *_args: None)
    from clawchat_gateway import plugin_tools
    monkeypatch.setattr(plugin_tools, "register_tools", lambda _ctx: None)
    ctx = SimpleNamespace(register_platform=Mock(), register_hook=Mock())

    getattr(plugin, entrypoint)(ctx)

    ctx.register_platform.assert_called_once()
    assert base.MEDIA_TAG_CLEANUP_RE is sentinel
    assert not hasattr(base, "_clawchat_all_exts_patch")
    assert base.BasePlatformAdapter.extract_media is original
    content = f"MEDIA:{old_host.paths['payload.py']}"
    assert base.BasePlatformAdapter.extract_media(content) == ([], content)
    assert ClawChatAdapter.extract_media(content)[0] == [(old_host.paths["payload.py"], False)]


@pytest.fixture
def hermes_source():
    candidates = []
    if os.environ.get("HERMES_SOURCE_DIR"):
        candidates.append(Path(os.environ["HERMES_SOURCE_DIR"]))
    candidates += [REPO_ROOT / "tmp" / "hermes-agent", REPO_ROOT / "tmp" / "hermes"]
    for candidate in candidates:
        if (candidate / "hermes_cli" / "plugin_validate_core_override.py").is_file():
            return candidate.resolve()
    pytest.skip("no Hermes core override validator source (set HERMES_SOURCE_DIR)")


def test_real_host_reports_no_shared_media_override(hermes_source, tmp_path):
    script = """
import json
import sys
from pathlib import Path
try:
    from hermes_cli.plugin_validate_core_override import core_override_findings
except ModuleNotFoundError as exc:
    print(json.dumps({'missingDependency': exc.name}))
else:
    print(json.dumps({'findings': core_override_findings(Path(sys.argv[1]))}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(REPO_ROOT)], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(hermes_source), str(REPO_ROOT)))},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if "missingDependency" in payload:
        pytest.skip(f"real Hermes validator needs missing dependency: {payload['missingDependency']}")
    findings = "\n".join(payload["findings"])
    assert "MEDIA_TAG_CLEANUP_RE" not in findings
    assert "_clawchat_all_exts_patch" not in findings
