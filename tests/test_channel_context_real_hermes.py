"""Check the real host contract in a subprocess, isolated from conftest's double."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def hermes_source():
    candidates = []
    if os.environ.get("HERMES_SOURCE_DIR"):
        candidates.append(Path(os.environ["HERMES_SOURCE_DIR"]))
    candidates += [REPO_ROOT / "tmp" / "hermes-agent", REPO_ROOT / "tmp" / "hermes"]
    for candidate in candidates:
        if (candidate / "gateway" / "platforms" / "event.py").is_file():
            return candidate.resolve()
    pytest.skip("no Hermes source checkout (set HERMES_SOURCE_DIR or use tmp/hermes-agent / tmp/hermes)")


def run_host(source, tmp_path, script):
    # Do not let the pytest process's gateway double mask real host imports.
    wrapped = "try:\n" + "\n".join("    " + line for line in script.splitlines()) + "\n"
    wrapped += (
        "except ModuleNotFoundError as exc:\n"
        "    import json\n"
        "    print(json.dumps({'missingDependency': exc.name}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", wrapped], cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(source), str(REPO_ROOT)))},
        capture_output=True, text=True, check=False, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    if "missingDependency" in payload:
        pytest.skip(f"real Hermes import needs missing dependency: {payload['missingDependency']}")
    return payload


def test_real_message_event_declares_supported_channel_context(hermes_source, tmp_path):
    result = run_host(hermes_source, tmp_path, """
import json
from dataclasses import fields
from pathlib import Path
import gateway.platforms.event as event_module
from clawchat_gateway import adapter
event_type = event_module.MessageEvent
adapter.MessageEvent = event_type
print(json.dumps({
    'eventFile': str(Path(event_module.__file__).resolve()),
    'declared': 'channel_context' in {field.name for field in fields(event_type)},
    'supported': adapter._host_supports_channel_context(),
}))
""")
    assert result["eventFile"] == str(hermes_source / "gateway" / "platforms" / "event.py")
    assert result["declared"] is True
    assert result["supported"] is True


def test_real_inbound_prefixes_context_before_new_message(hermes_source, tmp_path):
    result = run_host(hermes_source, tmp_path, """
import json
from types import SimpleNamespace
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.session import SessionSource
runner = GatewayInboundMixin.__new__(GatewayInboundMixin)
runner.config = SimpleNamespace(group_sessions_per_user=False, thread_sessions_per_user=False)
context = 'Plugin turn limits\\n\\n## ClawChat Turn Metadata\\nsent_at: null'
text = 'Current message body'
results = []
for chat_type, user_name in [('dm', 'Test Sender'), ('group', None), ('group', 'Test Sender')]:
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='test-chat',
                           chat_type=chat_type, user_id='test-sender', user_name=user_name)
    event = MessageEvent(text=text, source=source, channel_context=context)
    actual = runner._prefix_inbound_sender_context(event, source, text)
    trigger = '[Test Sender] ' + text if chat_type == 'group' and user_name else text
    assert actual == context + '\\n\\n[New message]\\n' + trigger
    assert event.text == text
    results.append(actual)
print(json.dumps({'prefixed': results}))
""")
    assert len(result["prefixed"]) == 3
