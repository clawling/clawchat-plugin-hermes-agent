"""Local attachments require Files direct or a single permitted completion."""
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace, ModuleType
import asyncio
import json
import os
import sys
import time

import pytest

from clawchat_gateway import turn_attachments as attachments, turn_grants as turns
from clawchat_gateway import host_tools_guard as guard, local_paths, media_runtime, memory_scope, tools
from clawchat_gateway.tool_grants import resolve_turn_grants
from test_tool_grants_hook import host, context, call, TOOL
from test_turn_grants import snapshot
from test_local_path_turn_policy import policy as legacy_policy, invoke as legacy_invoke, set_turn


@pytest.fixture
def scope(host, monkeypatch, tmp_path):
    monkeypatch.setattr(attachments, "completion_hook_registered", True)
    monkeypatch.setattr(memory_scope, "_session_value", turns.session_context_value)
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "speech.mp3"
    path.write_bytes(b"speech bytes")

    @contextmanager
    def open_turn(grants=None, *, speakers=("usr_a",), key="session", chat="cnv_chat", bind=True,
                  owner_side=False):
        s = (turns.register_turn(account_id="usr_agent", chat_id=chat, speakers=("usr_owner",),
             grants={}, session_key=key, owner_side=True, owner_verified=True) if owner_side else
             snapshot(grants=grants, speakers=speakers, key=key, chat=chat))
        try:
            with host(**context(s)):
                if bind:
                    turns.bind_turn(s.nonce, "s", "t")
                yield s
        finally:
            turns.release_turn(s.nonce)
    return SimpleNamespace(open=open_turn, bind=host, path=path)


def files_rows(mode="direct", user="usr_a", kind="capability", what="files"):
    return dict(grantee_user_id=user, what_kind=kind, what=what, mode=mode)


def grants(rows, speakers=("usr_a",)):
    return resolve_turn_grants(list(speakers), "usr_owner", rows, time.time() * 1000)


def completed(path, **kw):
    return {"details": {"media": {"path": str(path)}, **kw}}


def finish(path, *, cid="c", args=None, result=None, **kw):
    attachments.post_tool_call(tool_name=TOOL, args={} if args is None else args,
        result=completed(path) if result is None else result, status="ok",
        session_id="s", turn_id="t", tool_call_id=cid, **kw)


def allow(cid="c", args=None):
    assert call(turn_id="t", tool_call_id=cid, args={} if args is None else args) is None


async def upload(path, calls, chat="cnv_chat"):
    async def uploader(**kw):
        calls.append(kw)
        return media_runtime.UploadMediaResult("https://media.example/speech.mp3", kw["mime"], len(kw["buffer"]))
    return await media_runtime.upload_outbound_media([str(path)], chat_id=chat, base_url="https://media.example",
        websocket_url="", token="", media_local_roots=(), upload_file=uploader)


@pytest.mark.parametrize("mode,exception,second,expected", [
    ("direct", None, "direct", True), ("ask", None, "direct", False),
    ("direct", "deny", "direct", False), ("direct", "ask", "direct", False),
    ("direct", None, "ask", False), ("direct", None, None, False),
    ("direct", None, "deny", False),
])
def test_files_all_direct_and_group_intersection(scope, mode, exception, second, expected):
    rows = [files_rows(mode)] + ([files_rows(second, "usr_b")] if second else [])
    if exception:
        rows.append(files_rows(exception, kind="tool", what="read_file"))
    with scope.open(grants(rows, ("usr_a", "usr_b")), speakers=("usr_a", "usr_b")):
        assert attachments.files_direct("cnv_chat") is expected
        assert local_paths.local_attachment_allowed("cnv_chat", str(scope.path)) is expected
        calls = []
        assert bool(asyncio.run(upload(scope.path, calls))) is expected
        if expected:
            assert calls[0]["buffer"] == b"speech bytes"
        assert not local_paths.local_attachment_allowed("cnv_other", str(scope.path))


def test_files_all_rule_keeps_exact_exceptions(scope):
    for exception, expected in ((None, True), ("deny", False), ("ask", False)):
        rows = [files_rows(kind="all", what="all")]
        if exception:
            rows.append(files_rows(exception, kind="tool", what="patch"))
        with scope.open(grants(rows)):
            assert attachments.files_direct() is expected


@pytest.mark.parametrize("cid", ["c", ""])
def test_artifact_current_turn_session_and_callers_only(scope, cid):
    with scope.open() as s:
        allow(cid)
        finish(scope.path, cid=cid)
        assert local_paths.local_attachment_allowed(s.chat_id, str(scope.path))
        calls = []
        assert asyncio.run(upload(scope.path, calls))
        assert calls[0]["buffer"] == b"speech bytes"
        assert not local_paths.local_attachment_allowed("cnv_other", str(scope.path))
        for key, speakers, chat in (("other", ("usr_a",), "cnv_other"),
                                    ("other", ("usr_b",), "cnv_chat")):
            with scope.open({}, key=key, speakers=speakers, chat=chat):
                assert not local_paths.local_attachment_allowed(chat, str(scope.path))
    with scope.open():
        assert not local_paths.local_attachment_allowed("cnv_chat", str(scope.path))
    assert not any(key[0] == s.nonce for key in attachments._artifacts)
    with attachments.delivery_turn(s.nonce, "usr_agent", s.chat_id):
        assert not attachments.attachment_allowed(str(scope.path), s.chat_id)


def test_completion_consumes_exact_call_and_fingerprint_once(scope):
    with scope.open():
        allow(args={"a": 1, "b": 2})
        # Wrong arguments cannot consume or borrow this permit.
        finish(scope.path, args={"a": 9, "b": 2})
        assert not attachments.artifact_identity(str(scope.path))
        finish(scope.path, args={"b": 2, "a": 1})
        assert attachments.artifact_identity(str(scope.path))
        second = scope.path.with_name("second.mp3")
        second.write_bytes(b"second")
        finish(second, args={"a": 1, "b": 2})
        assert not attachments.artifact_identity(str(second))
        finish(second, cid="different", args={"a": 1, "b": 2})
        assert not attachments.artifact_identity(str(second))


@pytest.mark.parametrize("failure", ["missing-binding", "missing-snapshot", "wrong-session", "wrong-turn",
                                      "missing-pre", "blocked", "failed", "hook-missing", "no-speakers"])
def test_completion_fails_closed(scope, monkeypatch, failure):
    with scope.open(bind=failure != "missing-binding", speakers=() if failure == "no-speakers" else ("usr_a",)) as s:
        if failure not in {"missing-pre", "no-speakers"}:
            if failure == "blocked":
                monkeypatch.setattr(guard, "_clawchat_pre_tool_decision", lambda **_: {"action": "block"})
                assert call(turn_id="t", tool_call_id="c")["action"] == "block"
            else:
                # Unbound calls remain blocked by the ordinary guard.
                call(turn_id="t", tool_call_id="c")
        if failure == "missing-snapshot":
            turns.release_turn(s.nonce)
        if failure == "hook-missing":
            monkeypatch.setattr(attachments, "completion_hook_registered", False)
        attachments.post_tool_call(tool_name=TOOL, args={}, result=completed(scope.path), tool_call_id="c",
            session_id="other" if failure == "wrong-session" else "s",
            turn_id="other" if failure == "wrong-turn" else "t",
            status="error" if failure == "failed" else "ok")
        assert not attachments.artifact_identity(str(scope.path))


@pytest.mark.parametrize("shape", ["text", "content", "legacy", "list-media", "outbound-false", "error",
    "false-ok", "false-success", "pending", "exit", "top-error", "unknown"])
def test_only_structured_successful_outputs_are_artifacts(scope, shape):
    p = str(scope.path)
    values = {
        "text": f"MEDIA:{p}", "content": {"content": [{"type": "text", "text": f"MEDIA:{p}"}]},
        "legacy": {"details": {"path": p}}, "list-media": {"details": {"media": [p]}},
        "outbound-false": {"details": {"media": {"path": p, "outbound": False}}},
        "error": completed(p, error="failed"), "false-ok": completed(p, ok=False),
        "false-success": completed(p, success=False), "pending": completed(p, status="pending"),
        "exit": completed(p, exitCode=1), "top-error": {**completed(p), "isError": True},
        "unknown": None,
    }
    with scope.open():
        allow()
        attachments.post_tool_call(tool_name=TOOL, args={}, tool_call_id="c", session_id="s", turn_id="t",
            status="ok", result=values[shape])
        assert not attachments.artifact_identity(p)
        # Even an error completion consumes the permit.
        finish(p)
        assert not attachments.artifact_identity(p)


def test_json_tool_result_is_structured_but_media_text_is_not(scope):
    with scope.open():
        allow()
        finish(scope.path, result=json.dumps(completed(scope.path)))
        assert attachments.artifact_identity(str(scope.path))
    with scope.open():
        assert not local_paths.local_attachment_allowed("cnv_chat", str(scope.path))
        assert call("send_message", turn_id="t", args={"target": "clawchat:cnv_chat",
            "message": f"MEDIA:{scope.path}"})["action"] == "block"


@pytest.mark.parametrize("kind", ["symlink", "directory-symlink", "hardlink"])
@pytest.mark.parametrize("files_direct", [False, True])
def test_linked_attachments_are_refused(scope, kind, files_direct):
    path = scope.path.with_name("linked.mp3")
    if kind == "symlink":
        path.symlink_to(scope.path)
    elif kind == "directory-symlink":
        directory = scope.path.parent / "linked-dir"
        directory.symlink_to(scope.path.parent, target_is_directory=True)
        path = directory / scope.path.name
    else:
        os.link(scope.path, path)
    with scope.open(grants([files_rows()]) if files_direct else None):
        if not files_direct:
            allow()
            finish(path)
        assert not attachments.artifact_identity(str(path))
        assert asyncio.run(upload(path, [])) == []


@pytest.mark.parametrize("change", ["replace", "write", "hardlink", "end-turn"])
@pytest.mark.parametrize("files_direct", [False, True])
def test_checked_fd_read_refuses_changes_during_read(scope, monkeypatch, change, files_direct):
    with scope.open(grants([files_rows()]) if files_direct else None) as s:
        if not files_direct:
            allow()
            finish(scope.path)
        original = attachments.os.read
        def changed(fd, size):
            data = original(fd, size)
            if change == "replace":
                replacement = scope.path.with_name("replacement")
                replacement.write_bytes(b"secret replacement")
                replacement.replace(scope.path)
            elif change == "write":
                scope.path.write_bytes(b"changed")
            elif change == "hardlink":
                os.link(scope.path, scope.path.with_name("alias"))
            else:
                turns.release_turn(s.nonce)
            return data
        monkeypatch.setattr(attachments.os, "read", changed)
        calls = []
        assert asyncio.run(upload(scope.path, calls)) == []
        assert calls == []


def test_changed_file_before_send_and_duplicate_pre_are_refused(scope):
    with scope.open():
        allow()
        allow()
        finish(scope.path)
        finish(scope.path)
        assert not attachments.artifact_identity(str(scope.path))
        allow()
        finish(scope.path)
        scope.path.write_bytes(b"new data")
        assert not attachments.artifact_identity(str(scope.path))


def test_approve_off_grants_permits_only_that_completed_call(scope, monkeypatch):
    monkeypatch.setattr(guard, "_config_extra", lambda: {"tool-grants": "off", "non-owner-host-tools": "approve"})
    monkeypatch.setattr(guard, "_host_supports_approve", lambda: True)
    with scope.open({}):
        assert call(turn_id="t", tool_call_id="approved")["action"] == "approve"
        finish(scope.path, cid="approved")
        assert attachments.artifact_identity(str(scope.path))
        assert asyncio.run(upload(scope.path, []))
        other = scope.path.with_name("unapproved.mp3")
        other.write_bytes(b"not approved")
        finish(other, cid="another")
        finish(other, cid="approved")
        assert not attachments.artifact_identity(str(other))
        assert not attachments.files_direct()
        # A denied completion cannot be retried with the same permit.
        assert call(turn_id="t", tool_call_id="denied")["action"] == "approve"
        attachments.post_tool_call(tool_name=TOOL, args={}, result=completed(other), status="blocked",
            session_id="s", turn_id="t", tool_call_id="denied")
        finish(other, cid="denied")
        assert not attachments.artifact_identity(str(other))


@pytest.mark.parametrize("available,raises", [(False, False), (True, False), (True, True)])
def test_completion_hook_registration_probes_by_name(scope, monkeypatch, available, raises):
    module = ModuleType("hermes_cli.plugins")
    module.VALID_HOOKS = {"pre_tool_call", "pre_llm_call"} | ({"post_tool_call"} if available else set())
    monkeypatch.setitem(sys.modules, "hermes_cli.plugins", module)
    monkeypatch.setattr(guard, "_install_grant_catalog_filter", lambda: True)
    recorded = {}
    def register(name, fn):
        if raises and name == "post_tool_call":
            raise ValueError("unsupported hook")
        recorded[name] = fn
    guard.register_host_tools_guard(SimpleNamespace(register_hook=register))
    assert recorded["pre_tool_call"] is guard.clawchat_pre_tool_call
    assert attachments.completion_hook_registered is (available and not raises)
    with scope.open():
        allow()
        finish(scope.path)
        assert bool(attachments.artifact_identity(str(scope.path))) is (available and not raises)
    with scope.open(grants([files_rows()])):
        assert attachments.files_direct()
        assert asyncio.run(upload(scope.path, []))


def test_owner_legacy_path_policy_is_unchanged(scope, monkeypatch):
    monkeypatch.setattr(guard, "_session_turn", lambda: "owner")
    assert local_paths.turn_allows_local_paths()
    assert local_paths.local_attachment_allowed("cnv_other", str(scope.path))


@pytest.mark.parametrize("artifact", [False, True])
def test_reply_after_context_reset_and_send_file_use_same_turn(scope, monkeypatch, artifact):
    from clawchat_gateway import adapter as adapter_mod
    from clawchat_gateway.adapter import ClawChatAdapter
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    frames, uploaded = [], []
    async def frame(value, **_):
        frames.append(value)
        return True
    async def uploader(urls, **kw):
        async def send_bytes(**data):
            uploaded.append(data["buffer"])
            return media_runtime.UploadMediaResult("https://media.example/speech.mp3", "audio/mpeg", len(data["buffer"]))
        return await media_runtime.upload_outbound_media(urls, upload_file=send_bytes, **kw)
    monkeypatch.setattr(a._connection, "send_frame", frame)
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_: True)
    monkeypatch.setattr(adapter_mod, "upload_outbound_media", uploader)
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: a)
    from test_local_path_turn_policy import FRIEND_DM
    with scope.open(None if artifact else grants([files_rows()]), chat=FRIEND_DM) as s:
        if artifact:
            allow()
            finish(scope.path)
        assert call("clawchat_send_file", turn_id="t", args={"chat_id": s.chat_id, "path": str(scope.path)}) is None
        result = asyncio.run(tools.send_file(s.chat_id, str(scope.path)))
        assert result.get("sent"), result
        assert "error" in asyncio.run(tools.send_file("cnv_other", str(scope.path)))
        assert uploaded == [b"speech bytes"]
        # Hermes invokes delivery after clearing its ContextVars; the trusted
        # reply nonce and adapter account recover only this live turn.
        with scope.open({}, key="other", chat="cnv_other"):
            result = asyncio.run(a.send(s.chat_id, "reply", reply_to=s.nonce,
                media_files=[str(scope.path)], metadata={"_clawchat_immediate_media_send": True}))
            assert result.success
        assert uploaded == [b"speech bytes", b"speech bytes"]
        a._clawchat_config = replace(a._clawchat_config, user_id="usr_other")
        asyncio.run(a.send(s.chat_id, "wrong account", reply_to=s.nonce,
            media_files=[str(scope.path)], metadata={"_clawchat_immediate_media_send": True}))
        assert len(uploaded) == 2
    assert len(frames) == 3


@pytest.mark.parametrize("completion_fields", [{}, {"status": "running"}, {"status": "ok", "error_type": "failure"},
    {"status": "ok", "error_message": "failed"}])
def test_missing_success_or_completion_error_closes_artifact_route(scope, completion_fields):
    with scope.open():
        allow()
        attachments.post_tool_call(tool_name=TOOL, args={}, result=completed(scope.path),
            session_id="s", turn_id="t", tool_call_id="c", **completion_fields)
        assert not attachments.artifact_identity(str(scope.path))


@pytest.mark.parametrize("artifact", [False, True])
@pytest.mark.parametrize("buffered", [False, True])
def test_host_media_without_reply_anchor_after_context_reset(scope, monkeypatch, artifact, buffered):
    from clawchat_gateway import adapter as adapter_mod
    from clawchat_gateway.adapter import ClawChatAdapter, _ActiveRun
    from test_local_path_turn_policy import FRIEND_DM
    a = ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id="usr_owner")
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_: True)
    monkeypatch.setattr(a, "_after_host_turn", lambda _: None)
    frames, uploads = [], []
    async def frame(value, **_):
        frames.append(value)
        return True
    async def uploader(urls, **kw):
        async def send_bytes(**data):
            uploads.append(data["buffer"])
            return media_runtime.UploadMediaResult("https://media.example/speech.mp3", "audio/mpeg", len(data["buffer"]))
        return await media_runtime.upload_outbound_media(urls, upload_file=send_bytes, **kw)
    monkeypatch.setattr(a._connection, "send_frame", frame)
    monkeypatch.setattr(adapter_mod, "upload_outbound_media", uploader)

    async def exercise():
        rules = {TOOL: "direct"} if artifact else grants([files_rows()])
        s = snapshot(grants=rules, chat=FRIEND_DM)
        event = SimpleNamespace(source=SimpleNamespace(message_id=s.nonce, chat_id=FRIEND_DM), raw_message={})
        try:
            # Host on_processing_start runs before session binding.
            await a.on_processing_start(event)
            turns.bind_turn(s.nonce, "s", "t")
            with scope.bind(**context(s)):
                if artifact:
                    allow()
                    finish(scope.path)
            assert turns.session_context_value("HERMES_SESSION_MESSAGE_ID") == ""
            if buffered:
                run = _ActiveRun(chat_id=FRIEND_DM, chat_type="direct", message_id="stream", started_order=1,
                    attachment_nonce=s.nonce, kwargs={"media_files": [str(scope.path)]})
                a._active_runs_by_id[run.message_id] = run
                result = await a.on_run_complete(FRIEND_DM, "", run.message_id)
            else:
                # Actual Hermes _deliver_media_attachments omits reply_to.
                result = await a.send_voice(FRIEND_DM, str(scope.path))
            assert result.success
            assert uploads == [b"speech bytes"]
            await a.on_processing_complete(event, "SUCCESS")
            assert attachments.current_snapshot() is None
            assert not attachments.attachment_allowed(str(scope.path), FRIEND_DM)
        finally:
            attachments.finish_processing(s.nonce)
            turns.release_turn(s.nonce)
    asyncio.run(exercise())


def test_guest_reads_use_one_descriptor_and_never_path_read_bytes(scope, monkeypatch):
    with scope.open():
        allow()
        finish(scope.path)
        original_open = attachments.os.open
        opened = []
        def open_once(path, flags, *args, **kwargs):
            opened.append(path)
            return original_open(path, flags, *args, **kwargs)
        monkeypatch.setattr(attachments.os, "open", open_once)
        monkeypatch.setattr(type(scope.path), "read_bytes", lambda *_: pytest.fail("path reopened"))
        calls = []
        assert asyncio.run(upload(scope.path, calls))
        assert opened == [str(scope.path)]
        assert calls[0]["buffer"] == b"speech bytes"


def test_declared_credentials_and_oversized_files_are_refused(scope, monkeypatch):
    with scope.open():
        credential = scope.path.parent / "home" / ".env"
        credential.parent.mkdir()
        credential.write_text("private")
        allow()
        finish(credential)
        assert not attachments.artifact_identity(str(credential))
        allow()
        finish(scope.path)
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 1)
        calls = []
        assert asyncio.run(upload(scope.path, calls)) == []
        assert calls == []


def test_turn_end_between_policy_check_and_reader_never_uses_owner_reader(scope, monkeypatch):
    with scope.open(grants([files_rows()])) as s:
        original = media_runtime.local_attachment_allowed
        def ending(chat, source=None):
            result = original(chat, source)
            if result:
                turns.release_turn(s.nonce)
            return result
        monkeypatch.setattr(media_runtime, "local_attachment_allowed", ending)
        calls = []
        assert asyncio.run(upload(scope.path, calls)) == []
        assert calls == []


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_owner_delivery_retains_legacy_behavior_with_or_without_binding(scope, bound, legacy):
    s = turns.register_turn(account_id="usr_agent", chat_id="cnv_owner", session_key="owner",
        speakers=() if legacy else ("usr_owner",), grants={}, owner_side=not legacy,
        owner_verified=not legacy, legacy_owner_tools=legacy)
    try:
        if bound:
            turns.bind_turn(s.nonce, "s", "t")
        with scope.bind(**{**context(s), "HERMES_SESSION_USER_ID": "usr_owner"}):
            assert local_paths.turn_allows_local_paths()
            assert local_paths.local_attachment_allowed("cnv_other", str(scope.path))
    finally:
        turns.release_turn(s.nonce)


def test_files_direct_keeps_remote_media_public_only(scope, monkeypatch):
    monkeypatch.setattr(local_paths.socket, "getaddrinfo", lambda *a, **kw: [
        (2, 1, 6, "", ("127.0.0.1", 80))])
    with scope.open(grants([files_rows()])):
        assert attachments.files_direct()
        assert not media_runtime.media_reference_allowed("http://example.test/x", "cnv_chat")


def _parent_swap(scope, name=".env"):
    """An allowed output and a sensitive file with the same basename."""
    sensitive = scope.path.parent / "home" / name
    sensitive.parent.mkdir(parents=True, exist_ok=True)
    sensitive.write_bytes(b"private credential")
    directory = scope.path.parent / "output"
    directory.mkdir()
    source = directory / sensitive.name
    source.write_bytes(b"public output")
    parked = directory.with_name("parked-output")

    def swap():
        directory.rename(parked)
        directory.symlink_to(sensitive.parent, target_is_directory=True)
    return source, swap


@pytest.mark.parametrize("name", [".env", "config.yaml", "auth.json", "clawchat/clawchat.sqlite"])
def test_artifact_registration_parent_swap_after_resolve_is_refused(scope, monkeypatch, name):
    source, swap = _parent_swap(scope, name)
    original = attachments._source_path
    def resolved_then_swapped(value, **kwargs):
        resolved = original(value, **kwargs)
        swap()
        return resolved
    monkeypatch.setattr(attachments, "_source_path", resolved_then_swapped)
    with scope.open() as s:
        allow()
        finish(source)
        assert str(source) not in attachments._artifacts.get(attachments._scope(s), {})


@pytest.mark.parametrize("stage", ["before-open", "final-recheck"])
@pytest.mark.parametrize("mode", ["files", "artifact"])
def test_parent_swap_after_resolve_never_uploads_sensitive_file(scope, monkeypatch, stage, mode):
    source, swap = _parent_swap(scope)
    with scope.open(grants([files_rows()]) if mode == "files" else None, owner_side=mode == "owner"):
        if mode == "artifact":
            allow()
            finish(source)
        original = attachments._source_path
        visits = []
        def resolved_then_swapped(value, **kwargs):
            resolved = original(value, **kwargs)
            visits.append(value)
            if len(visits) == (1 if stage == "before-open" else 2):
                swap()
            return resolved
        monkeypatch.setattr(attachments, "_source_path", resolved_then_swapped)
        calls = []
        assert asyncio.run(upload(source, calls)) == []
        assert calls == []
        assert len(visits) == (1 if stage == "before-open" else 2)


@pytest.mark.parametrize("mode", ["files", "artifact"])
def test_descriptor_path_unavailable_refuses_registration_and_upload(scope, monkeypatch, mode):
    rules = grants([files_rows(), files_rows(kind="tool", what=TOOL)]) if mode == "files" else None
    with scope.open(rules, owner_side=mode == "owner") as s:
        if mode == "artifact":
            allow()
            finish(scope.path)
        def unavailable(fd):
            raise ValueError("kernel lookup unavailable")
        monkeypatch.setattr(attachments, "_descriptor_path", unavailable)
        allow()
        second = scope.path.with_name("second.mp3")
        second.write_bytes(b"another output")
        finish(second)
        assert str(second) not in attachments._artifacts.get(attachments._scope(s), {})
        calls = []
        assert asyncio.run(upload(scope.path, calls)) == []
        assert calls == []


def test_linux_descriptor_lookup_failure_is_not_replaced_with_requested_path(scope, monkeypatch):
    def unavailable(_):
        raise OSError("proc unavailable")
    monkeypatch.setattr(attachments.os, "readlink", unavailable)
    with pytest.raises(ValueError, match="descriptor path is unavailable"):
        attachments._descriptor_path(123)


@pytest.mark.parametrize("platform", ["darwin", "win32", "unknown"])
def test_unsupported_or_failed_platform_descriptor_lookup_is_closed(scope, monkeypatch, platform):
    # Stub only the platform lookup; never alter pathlib's native path flavour.
    monkeypatch.setattr(attachments, "sys", SimpleNamespace(platform=platform))
    if platform == "darwin":
        module = ModuleType("fcntl")
        module.fcntl = lambda *a: (_ for _ in ()).throw(OSError("F_GETPATH unavailable"))
        monkeypatch.setitem(sys.modules, "fcntl", module)
    elif platform == "win32":
        monkeypatch.setitem(sys.modules, "msvcrt", None)
    with pytest.raises(ValueError, match="descriptor path is unavailable"):
        attachments._descriptor_path(123)


def test_open_descriptor_identity_uses_kernel_path_and_fstat(scope):
    with attachments._open_checked_file(str(scope.path)) as (fd, identity):
        info = os.fstat(fd)
        assert identity.realpath == str(scope.path.resolve())
        assert (identity.dev, identity.ino, identity.size, identity.mtime_ns, identity.ctime_ns) == (
            info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@pytest.mark.parametrize("kind", ["symlink", "directory-symlink", "hardlink"])
def test_owner_links_keep_original_reader_without_descriptor_lookup(scope, monkeypatch, kind):
    link = scope.path.with_name("owner-linked.mp3")
    if kind == "symlink":
        link.symlink_to(scope.path)
    elif kind == "directory-symlink":
        directory = scope.path.parent / "owner-linked-dir"
        directory.symlink_to(scope.path.parent, target_is_directory=True)
        link = directory / scope.path.name
    else:
        os.link(scope.path, link)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 1)
    monkeypatch.setattr(attachments, "_descriptor_path", lambda *_: pytest.fail("owner fd lookup"))
    with scope.open(owner_side=True):
        calls = []
        assert asyncio.run(upload(link, calls))
        assert calls[0]["buffer"] == b"speech bytes"


def test_macos_descriptor_path_uses_f_getpath(scope, monkeypatch):
    calls = []
    module = ModuleType("fcntl")
    def get_path(fd, command, buffer):
        calls.append((fd, command, len(buffer)))
        return os.fsencode(scope.path) + b"\0"
    module.fcntl = get_path
    monkeypatch.setitem(sys.modules, "fcntl", module)
    monkeypatch.setattr(attachments, "sys", SimpleNamespace(platform="darwin"))
    assert attachments._descriptor_path(123) == str(scope.path)
    assert calls == [(123, 50, 1024)]


@pytest.mark.parametrize("raw,expected", [
    ("\\\\?\\C:\\output\\speech.mp3", "C:\\output\\speech.mp3"),
    ("\\\\?\\UNC\\server\\share\\speech.mp3", "\\\\server\\share\\speech.mp3"),
])
def test_windows_descriptor_path_uses_final_handle_path_and_normalizes_prefix(scope, monkeypatch, raw, expected):
    import ctypes
    msvcrt = ModuleType("msvcrt")
    msvcrt.get_osfhandle = lambda fd: fd + 1000
    calls = []
    class GetPath:
        def __call__(self, handle, buffer, size, flags):
            calls.append((handle, size, flags))
            if buffer is None:
                return len(raw) + 1
            buffer.value = raw
            return len(raw)
    function = GetPath()
    monkeypatch.setitem(sys.modules, "msvcrt", msvcrt)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: SimpleNamespace(GetFinalPathNameByHandleW=function), raising=False)
    monkeypatch.setattr(attachments, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(attachments, "Path", lambda path: SimpleNamespace(is_absolute=lambda: path == expected))
    assert attachments._descriptor_path(123) == expected
    assert calls == [(1123, 0, 0), (1123, len(raw) + 1, 0)]


def test_legacy_false_reader_skips_all_guest_policy_checks(scope, monkeypatch):
    def forbidden(*_a, **_kw):
        pytest.fail("legacy read entered guest checks")
    monkeypatch.setattr(attachments, "_open_checked_file", forbidden)
    monkeypatch.setattr(attachments, "files_direct", forbidden)
    monkeypatch.setattr(attachments, "_recorded_artifact", forbidden)
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 0)
    assert attachments.read_local_attachment(str(scope.path), None, require_turn_permission=False) == b"speech bytes"


@pytest.mark.parametrize("reference", ["case", "short-name", "prefix", "different-file"])
@pytest.mark.parametrize("artifact", [False, True])
def test_windows_guest_upload_normalizes_same_file_but_refuses_other_file(scope, monkeypatch, reference, artifact):
    import ntpath
    from pathlib import Path

    alias = {
        "case": "C:\\Output\\SPEECH.MP3",
        "short-name": "C:\\OUTPUT~1\\SPEECH.MP3",
        "prefix": "\\\\?\\C:\\Output\\speech.mp3",
        "different-file": "C:\\Output\\other.mp3",
    }[reference]
    stripped = attachments._without_windows_prefix(alias)
    original_resolve = Path.resolve
    other = scope.path.with_name("other.mp3")
    other.write_bytes(b"different bytes")
    resolved_inputs = []
    def native_resolve(path, *args, **kwargs):
        if str(path) == stripped:
            resolved_inputs.append(str(path))
            # Simulate Windows resolving an 8.3 alias and preserving a different
            # spelling of the canonical file's case. The real fd stays open on
            # speech.mp3, so a genuinely different path must still fail.
            target = other if reference == "different-file" else scope.path
            return Path(str(target).upper())
        return original_resolve(path, *args, **kwargs)
    original_policy = media_runtime.ensure_allowed_local_path
    def policy(path, *args, **kwargs):
        if path == alias:
            target = other if reference == "different-file" else scope.path
            return original_policy(str(target), *args, **kwargs)
        return original_policy(path, *args, **kwargs)
    with scope.open(None if artifact else grants([files_rows()])):
        monkeypatch.setattr(attachments, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(attachments, "os", SimpleNamespace(**{**vars(os), "path": ntpath}))
        monkeypatch.setattr(Path, "resolve", native_resolve)
        monkeypatch.setattr(media_runtime, "ensure_allowed_local_path", policy)
        if artifact:
            monkeypatch.setattr(attachments, "_descriptor_path", lambda fd: str(scope.path))
            allow()
            finish(scope.path)
        monkeypatch.setattr(attachments, "_descriptor_path", lambda fd: alias)
        calls = []
        assert bool(asyncio.run(upload(scope.path, calls))) is (reference != "different-file")
        assert resolved_inputs
        if reference != "different-file":
            assert calls[0]["buffer"] == b"speech bytes"


def test_macos_normalization_resolves_var_and_private_var_on_both_sides(scope, monkeypatch):
    from pathlib import Path
    original_resolve = Path.resolve
    resolved = []
    def native_resolve(path, *args, **kwargs):
        resolved.append(str(path))
        if str(path) in {"/var/output/speech.mp3", "/private/var/output/speech.mp3"}:
            return Path("/private/var/output/speech.mp3")
        return original_resolve(path, *args, **kwargs)
    monkeypatch.setattr(attachments, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(Path, "resolve", native_resolve)
    assert attachments._normalized_path("/var/output/speech.mp3") == attachments._normalized_path("/private/var/output/speech.mp3")
    assert resolved == ["/var/output/speech.mp3", "/private/var/output/speech.mp3"]


@pytest.mark.parametrize("tool", ["upload_avatar_image", "create_moment", "register_app", "upload_media_file"])
def test_original_tool_uploads_never_enter_guest_reader(legacy_policy, monkeypatch, tool):
    set_turn(legacy_policy, "owner_direct")
    def forbidden(*_a, **_kw):
        pytest.fail("original upload entered guest reader")
    monkeypatch.setattr(attachments, "read_local_attachment", forbidden)
    monkeypatch.setattr(attachments, "_descriptor_path", forbidden)
    result = asyncio.run(legacy_invoke(tool, str(legacy_policy.home / "picture.png")))
    assert "error" not in result
    assert legacy_policy.calls
