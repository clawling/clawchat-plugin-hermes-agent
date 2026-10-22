"""Check the real host contract in a subprocess, isolated from conftest's double."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from clawchat_gateway import adapter as adapter_mod
from test_channel_prompt_split import (
    NOW,
    dispatch,
    harness,  # noqa: F401 -- dependencies of the imported fixtures
    incoming,
    prompt_env,  # noqa: F401
    split_env,  # noqa: F401
)

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host_version",
    ["current"] + (["old"] if os.environ.get("HERMES_SOURCE_DIR_OLD") else []),
)
async def test_real_host_two_turns_keep_system_and_history_bytes(
    split_env, monkeypatch, hermes_source, tmp_path, host_version,
):
    source = hermes_source if host_version == "current" else Path(os.environ["HERMES_SOURCE_DIR_OLD"]).resolve()
    assert (source / "gateway" / "platforms" / "event.py").is_file()
    message_ids = ["msg-first", "msg-second"]
    inbounds = [
        incoming("usr_a", mid=mid, stamp=NOW + index * 120_000)
        for index, mid in enumerate(message_ids)
    ]

    async def plugin_events():
        events = []
        for inbound in inbounds:
            event = await dispatch(split_env, inbound)
            events.append({
                "channel_prompt": event.channel_prompt,
                "channel_context": getattr(event, "channel_context", None),
                "text": event.text,
            })
        return events

    split_events = await plugin_events()
    assert split_events[0]["channel_context"] != split_events[1]["channel_context"]
    for event, inbound, mid in zip(split_events, inbounds, message_ids):
        limits = split_env.adapter._non_owner_turn_section(inbound)
        assert limits and event["channel_context"].startswith(limits + "\n\n")
        assert f"message_id: {mid}" in event["channel_context"]

    # Produce the actual legacy events (the adapter renders split=False), rather
    # than moving strings between fields in the test's own rendering code.
    monkeypatch.setattr(adapter_mod, "_host_supports_channel_context", lambda: False)
    legacy_events = await plugin_events()
    assert all(not event["channel_context"] for event in legacy_events)
    event_json = json.dumps({"split": split_events, "legacy": legacy_events})
    result = run_host(source, tmp_path, "events = " + repr(event_json) + r'''
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from agent.session_persistence import _db_flush_row
from agent.turn_context import _stamp_api_content_sidecar, build_api_messages
from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from hermes_state import SessionDB

events = json.loads(events)
source = SessionSource(platform=Platform.TELEGRAM, chat_id='test-peer-chat',
                       chat_type='dm', user_id='usr_a', user_name='Test Sender')
runner = GatewayInboundMixin.__new__(GatewayInboundMixin)
runner.config = SimpleNamespace(group_sessions_per_user=False, thread_sessions_per_user=False)
# Only bypass session image buffering, attachment processing and @ expansion.
# The real prepare/prefix/media-file-note/reply-context functions stay in use.
runner._consume_pending_native_image_paths = lambda key: []
def no_media(event, pending_stt_prepared):
    assert not event.media_urls
    return [], [], [], []
runner._classify_inbound_media = no_media
def no_documents(event, text):
    assert not event.media_urls
    return text
runner._prepend_inbound_document_notes = no_documents
async def no_references(source, session_key, text):
    return text
runner._expand_inbound_context_references = no_references
# No configured channel override; combine the real platform/channel overlays.
runner._get_system_prompt_for_channel = lambda *args, **kwargs: ''

async def two_turns(name, event_rows):
    db = SessionDB(db_path=Path(name + '.db'))
    db.create_session(name, source='telegram')
    requests, systems, persisted = [], [], []
    try:
        for index, row in enumerate(event_rows):
            history = db.get_messages_as_conversation(name)
            event = MessageEvent(source=source, **row)
            text = await runner._prepare_inbound_message_text(
                event=event, source=source, history=history, session_key='test-dm',
            )
            assert event.text == row['text']
            turn = TurnRunner(runner, SimpleNamespace(
                source=source, context_prompt='Fixed platform context.',
                channel_prompt=event.channel_prompt,
            ))
            agent = SimpleNamespace(
                _current_turn_timestamp=1800000000 + index * 120,
                ephemeral_system_prompt=turn._combined_ephemeral_prompt(),
                api_mode='chat_completions',
                _copy_reasoning_content_for_api=lambda msg, api_msg: None,
                _should_sanitize_tool_calls=lambda: False,
            )
            messages = history + [{'role': 'user', 'content': text}]
            current = len(history)
            # Make the sidecar nonempty and different each turn: replay must use
            # turn one's persisted API bytes, never turn two's live hook context.
            hook = 'Host user hook for turn %d.' % (index + 1)
            _stamp_api_content_sidecar(
                agent, messages, current, '', hook, preflight_compressed=False,
            )
            request, system = build_api_messages(
                agent, messages, current_turn_user_idx=current,
                ext_prefetch_cache='', plugin_user_context=hook, moa_config=None,
                active_system_prompt='Fixed cached system prompt.',
            )
            # Materialize the same row shape as the host flush, then actually
            # reload it from SQLite at the start of the next turn.
            durable = _db_flush_row(agent, messages[current], True)
            db.append_messages_batch(name, [durable])
            persisted.append(durable)
            if index == 0:
                db.append_message(name, role='assistant', content='Fixed assistant reply.')
            requests.append(request)
            systems.append(system)
    finally:
        db.close()
    return {'requests': requests, 'systems': systems, 'persisted': persisted}

async def main():
    # Both host versions expose these helpers with the same signatures. Their
    # internal reasoning/tool-sanitization differences are outside this text case.
    return {name: await two_turns(name, rows) for name, rows in events.items()}
print(json.dumps(asyncio.run(main())))
''')
    split = result["split"]
    first, second = split["requests"]
    assert [message["role"] for message in first] == ["system", "user"]
    assert [message["role"] for message in second] == ["system", "user", "assistant", "user"]
    assert split["systems"][0].encode("utf-8") == split["systems"][1].encode("utf-8")
    assert first[0]["content"] == split["systems"][0]
    assert second[0]["content"] == split["systems"][1]
    first_user_bytes = first[1]["content"].encode("utf-8")
    assert second[1]["content"].encode("utf-8") == first_user_bytes
    durable = split["persisted"][0]
    assert durable["api_content"].encode("utf-8") == first_user_bytes
    assert durable["api_content"] != durable["content"]
    assert durable["content"] == split_events[0]["channel_context"] + "\n\n[New message]\n" + split_events[0]["text"]
    assert second[2]["content"] == "Fixed assistant reply."
    assert second[3]["content"].startswith(split_events[1]["channel_context"] + "\n\n[New message]\n")
    assert second[3]["content"].endswith("Host user hook for turn 2.")
    assert "Host user hook for turn 2." not in second[1]["content"]
    assert all("api_content" not in message for request in (first, second) for message in request)
    for system in split["systems"]:
        assert system.startswith("Fixed cached system prompt.\n\nFixed platform context.\n\n")
        assert all(mid not in system for mid in message_ids)
    # Sensitivity control: identical real host assembly, but legacy placement
    # makes the effective system change with message id/time.
    legacy = result["legacy"]
    assert legacy["systems"][0].encode("utf-8") != legacy["systems"][1].encode("utf-8")
    for index, mid in enumerate(message_ids):
        assert mid in legacy["systems"][index]
