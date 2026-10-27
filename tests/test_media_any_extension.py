"""ClawChat's legacy MEDIA fallback validates paths without rebinding Hermes."""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

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


@pytest.fixture
def media_tool(old_host, monkeypatch):
    """Model the host's class-level extraction and module-global sender lookup."""
    from clawchat_gateway import host_tools_guard

    monkeypatch.setattr(host_tools_guard, "_session_turn", lambda: "owner")
    spec = importlib.util.spec_from_file_location("clawchat_media_tool_entry_test", REPO_ROOT / "__init__.py")
    plugin = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, plugin)
    spec.loader.exec_module(plugin)
    original_calls = AsyncMock(return_value={"success": True})
    media_calls = AsyncMock(return_value={"success": True})

    async def original(*args, **kwargs):
        return await original_calls(*args, **kwargs)

    def filter_media_delivery_paths(media):
        return [(safe_path, voice) for path, voice in media
                if (safe_path := old_host.validate(path))]

    module = ModuleType("tools.send_message_tool")
    module._send_to_platform = original
    module.BasePlatformAdapter = base.BasePlatformAdapter
    module.filter_media_delivery_paths = filter_media_delivery_paths
    # Defining the entry in the module's namespace makes installation replace
    # the name it actually calls, just as in the real host tool.
    exec("""
async def send_message(platform, pconfig, chat_id, message, thread_id=None):
    force_document = "[[as_document]]" in message
    media_files, cleaned_message = BasePlatformAdapter.extract_media(message)
    media_files = filter_media_delivery_paths(media_files)
    return await _send_to_platform(
        platform, pconfig, chat_id, cleaned_message, thread_id=thread_id,
        media_files=media_files, force_document=force_document,
    )
""", module.__dict__)  # noqa: S102 -- isolated host tool double
    package = ModuleType("tools")
    package.send_message_tool = module
    monkeypatch.setitem(sys.modules, "tools", package)
    monkeypatch.setitem(sys.modules, "tools.send_message_tool", module)
    monkeypatch.setattr(plugin, "_send_clawchat_media_via_live_adapter", media_calls)
    return SimpleNamespace(tool=module, install=plugin._patch_send_message_media_delivery,
                           original=original_calls, media=media_calls)


@pytest.mark.parametrize("known_media", [False, True])
async def test_tool_entry_delivers_legacy_extra_media(old_host, media_tool, known_media):
    path = old_host.paths["payload.py"]
    pdf = old_host.paths["report.pdf"]
    content = "[[audio_as_voice]][[as_document]]Caption\n"
    if known_media:
        content += f"MEDIA:{pdf}\n"
    content += f"MEDIA:{path}\nMEDIA:{path}"
    media_tool.install()

    result = await media_tool.tool.send_message("clawchat", None, "test-chat", content, thread_id="test-thread")

    assert result == {"success": True}
    media_tool.original.assert_not_awaited()
    media_tool.media.assert_awaited_once_with(
        "clawchat", None, "test-chat", "Caption", thread_id="test-thread",
        media_files=([(pdf, True)] if known_media else []) + [(path, False)], force_document=True,
    )


async def test_tool_entry_deduplicates_against_host_validated_paths(old_host, media_tool, tmp_path):
    path = old_host.paths["payload.py"]
    alias = tmp_path / "alias.pdf"
    alias.symlink_to(path)
    media_tool.install()

    await media_tool.tool.send_message("clawchat", None, "test-chat", f"MEDIA:{alias}\nMEDIA:{path}")

    assert media_tool.media.await_args.args[3] == ""
    assert media_tool.media.await_args.kwargs["media_files"] == [(path, False)]


@pytest.mark.parametrize("allowed", [False, True])
async def test_tool_entry_checks_extra_media_before_sending(old_host, media_tool, monkeypatch, allowed):
    from clawchat_gateway import host_tools_guard, local_paths

    monkeypatch.setattr(host_tools_guard, "_session_turn", lambda: "other")
    monkeypatch.setattr(local_paths, "turn_allows_local_paths", lambda: allowed)
    path = old_host.paths["payload.py"]
    media_tool.install()

    result = await media_tool.tool.send_message("clawchat", None, "test-chat", f"MEDIA:{path}")

    assert result == ({"success": True} if allowed else {"error": local_paths.LOCAL_PATH_REFUSAL})
    assert media_tool.media.await_count == int(allowed)
    media_tool.original.assert_not_awaited()


@pytest.mark.parametrize("failure", ["denied", "missing", "exception", "no-validator"])
async def test_tool_entry_keeps_unvalidated_tags(old_host, media_tool, monkeypatch, tmp_path, failure):
    path = str(tmp_path / "missing.py") if failure == "missing" else old_host.paths["denied.py"]
    if failure == "exception":
        old_host.validate.side_effect = ValueError("validation unavailable")
    elif failure == "no-validator":
        monkeypatch.delattr(base, "validate_media_delivery_path")
    content = f"Caption\nMEDIA:{path}"
    media_tool.install()

    assert await media_tool.tool.send_message("clawchat", None, "test-chat", content) == {"success": True}

    media_tool.media.assert_not_awaited()
    media_tool.original.assert_awaited_once_with(
        "clawchat", None, "test-chat", content, thread_id=None, media_files=[], force_document=False,
    )


@pytest.mark.parametrize("known_media", [False, True])
@pytest.mark.parametrize("platform_name", ["telegram", "clawchat"])
async def test_tool_entry_preserves_other_platforms_and_new_hosts(
    old_host, media_tool, monkeypatch, platform_name, known_media,
):
    from clawchat_gateway import adapter

    if platform_name == "clawchat":
        monkeypatch.setattr(base, "MEDIA_EXTENSIONLESS_TAG_RE", object(), raising=False)
    extra = Mock(side_effect=AssertionError("legacy extraction must not run"))
    monkeypatch.setattr(adapter, "_legacy_extra_media", extra)
    platform = SimpleNamespace(value=platform_name)
    path = old_host.paths["payload.py"]
    content = f"Caption\nMEDIA:{path}"
    if known_media:
        content += f"\nMEDIA:{old_host.paths['report.pdf']}"
    await media_tool.tool.send_message(platform, None, "test-chat", content)
    before = media_tool.original.await_args
    media_tool.original.reset_mock()
    media_tool.install()

    await media_tool.tool.send_message(platform, None, "test-chat", content)

    sender = media_tool.media if platform_name == "clawchat" and known_media else media_tool.original
    assert sender.await_count == 1
    assert sender.await_args == before
    assert f"MEDIA:{path}" in sender.await_args.args[3]
    extra.assert_not_called()


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
