"""Restrict local file paths in non-owner turns and protect Hermes data."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawchat_gateway import host_tools_guard as guard
from clawchat_gateway import local_paths, media_runtime, memory_scope, storage, tools

OWNER = "usr_owner"
FRIEND = "usr_friend"
OWNER_DM = "cnv_01J9ZX3K5Q7W2M4N6P8R0T2V4X"
FRIEND_DM = "cnv_01J9ZX3K5Q7W2M4N6P8R0T2V4Y"
GROUP = "cnv_01J9ZX3K5Q7W2M4N6P8R0T2V4Z"
TURNS = {
    "friend_direct": (FRIEND_DM, "dm", FRIEND),
    "group_friend": (GROUP, "group", FRIEND),
    "group_owner": (GROUP, "group", OWNER),
    "owner_direct": (OWNER_DM, "dm", OWNER),
}
NON_OWNER_TURNS = tuple(name for name in TURNS if name != "owner_direct")
FILES = (
    "clawchat/clawchat.sqlite", "config.yaml", ".env", "auth.json",
    "memories/owner.md", "notes.txt", "picture.png",
)
TOOLS = ("send_file", "upload_avatar_image", "create_moment", "register_app", "upload_media_file")


@pytest.fixture
def policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: False)
    monkeypatch.setattr(memory_scope, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_user_id", lambda: OWNER)
    monkeypatch.setattr(guard, "_owner_direct_chat_id", lambda: OWNER_DM)
    monkeypatch.setattr(guard, "_is_known_group", lambda chat_id: chat_id == GROUP)
    monkeypatch.setattr(guard, "_config_extra", lambda: {})
    monkeypatch.setattr(guard, "_guarded_call", lambda *_: False)
    monkeypatch.setattr(guard, "_strict_unregistered", lambda *_: False)
    session = {}
    monkeypatch.setattr(guard, "_session_value", lambda key: session.get(key, ""))
    monkeypatch.setattr(memory_scope, "_session_value", lambda key: session.get(key, ""))
    for name in FILES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"data")
    calls = []

    class Client:
        async def upload_media(self, **kw):
            calls.append(("media", kw))
            return SimpleNamespace(url="https://media.example/picture.png", mime="image/png", size=12, kind="image", name="picture.png")

        async def upload_avatar(self, **kw):
            calls.append(("avatar", kw))
            return SimpleNamespace(url="https://media.example/picture.png", mime="image/png", size=12)

        async def create_moment(self, **kw):
            calls.append(("moment", kw))
            return {"id": 1}

        async def register_app(self, **kw):
            calls.append(("app", kw))
            return {"id": "sample"}

    async def send(*args, **kw):
        calls.append(("send", kw))
        return SimpleNamespace(success=True, message_id="m-sent")

    async def uploader(**kw):
        calls.append(("outbound", kw))
        return media_runtime.UploadMediaResult("https://media.example/file", kw["mime"], len(kw["buffer"]))

    monkeypatch.setattr(tools, "_build_client", lambda: (Client(), None))
    monkeypatch.setattr(tools, "get_clawchat_sender", lambda: SimpleNamespace(send=send))
    return SimpleNamespace(home=tmp_path, session=session, calls=calls, uploader=uploader)


def set_turn(policy, name):
    chat_id, chat_type, user_id = TURNS[name]
    policy.session.update({
        "HERMES_SESSION_PLATFORM": "clawchat",
        "HERMES_SESSION_CHAT_ID": chat_id,
        "HERMES_SESSION_CHAT_TYPE": chat_type,
        "HERMES_SESSION_USER_ID": user_id,
    })


def invoke(tool, path, target=FRIEND_DM):
    if tool == "send_file":
        return tools.send_file(target, path)
    if tool == "create_moment":
        return tools.create_moment(text="hello", images=["https://cdn.example/picture.png", path])
    if tool == "register_app":
        return tools.register_app("Sample", "sample", "https://app.example", icon_path=path)
    return getattr(tools, tool)(path)


def outbound(policy, references, chat_id=None):
    return media_runtime.upload_outbound_media(
        references, chat_id=chat_id, base_url="https://api.example",
        websocket_url="wss://ws.example", token="", media_local_roots=(),
        upload_file=policy.uploader,
    )


def forbid_filesystem(*_args, **_kw):
    raise AssertionError("local filesystem must not be checked in this turn")


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("filename", FILES)
def test_non_owner_tools_refuse_before_filesystem(policy, monkeypatch, turn, tool, filename):
    set_turn(policy, turn)
    path = str(policy.home / filename)
    with monkeypatch.context() as checks:
        for method in ("resolve", "exists", "is_file", "stat", "read_bytes", "open"):
            checks.setattr(Path, method, forbid_filesystem)
        checks.setattr(tools, "ensure_allowed_local_path", forbid_filesystem)
        result = asyncio.run(invoke(tool, path))
    assert result == {"error": "validation", "message": local_paths.LOCAL_PATH_REFUSAL}
    assert str(policy.home) not in str(result)
    assert policy.calls == []


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
@pytest.mark.parametrize("target", (FRIEND_DM, GROUP, None))
@pytest.mark.parametrize("file_url", (False, True), ids=("bare", "file_url"))
@pytest.mark.parametrize("filename", FILES)
def test_non_owner_attachments_drop_before_filesystem(
    policy, monkeypatch, caplog, turn, target, file_url, filename,
):
    set_turn(policy, turn)
    path = policy.home / filename
    reference = path.as_uri() if file_url else str(path)
    with monkeypatch.context() as checks:
        for method in ("resolve", "exists", "is_file", "stat", "read_bytes", "open"):
            checks.setattr(Path, method, forbid_filesystem)
        checks.setattr(media_runtime, "_load_local_media", forbid_filesystem)
        result = asyncio.run(outbound(policy, [reference], target))
    assert result == []
    assert policy.calls == []
    assert str(policy.home) not in caplog.text


@pytest.mark.parametrize("reference", ("http:private/notes.txt", "FILE:///tmp/notes.txt", "C:\\private\\notes.txt"))
def test_non_http_references_are_dropped_with_neutral_logs(policy, monkeypatch, caplog, reference):
    set_turn(policy, "friend_direct")
    monkeypatch.setattr(media_runtime, "_load_remote_media", forbid_filesystem)
    monkeypatch.setattr(media_runtime, "_load_local_media", forbid_filesystem)
    assert asyncio.run(outbound(policy, [reference], FRIEND_DM)) == []
    assert reference not in caplog.text
    assert policy.calls == []


@pytest.mark.parametrize("target", (OWNER_DM, FRIEND_DM, GROUP))
def test_owner_direct_can_send_ordinary_file_to_any_chat(policy, target):
    set_turn(policy, "owner_direct")
    result = asyncio.run(tools.send_file(target, str(policy.home / "notes.txt")))
    assert result.get("sent") is True, result
    assert len(policy.calls) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("filename", FILES[:4])
def test_owner_tools_refuse_hermes_data(policy, tool, filename):
    set_turn(policy, "owner_direct")
    result = asyncio.run(invoke(tool, str(policy.home / filename)))
    assert result["error"] == "validation"
    assert str(policy.home) not in str(result)
    assert policy.calls == []


@pytest.mark.parametrize("tool", TOOLS)
def test_owner_tools_accept_ordinary_image(policy, tool):
    set_turn(policy, "owner_direct")
    result = asyncio.run(invoke(tool, str(policy.home / "picture.png")))
    assert "error" not in result, result
    assert policy.calls


@pytest.mark.parametrize("file_url", (False, True), ids=("bare", "file_url"))
def test_unbound_delivery_to_owner_direct_drops_only_denied(policy, monkeypatch, file_url):
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    paths = [policy.home / name for name in ("notes.txt", *FILES[:4])]
    references = [path.as_uri() if file_url else str(path) for path in paths]
    fragments = asyncio.run(outbound(policy, references, OWNER_DM))
    assert [fragment["name"] for fragment in fragments] == ["notes.txt"]
    assert len(policy.calls) == 1


@pytest.mark.parametrize("turn", NON_OWNER_TURNS + ("other_platform",))
@pytest.mark.parametrize("file_url", (False, True), ids=("bare", "file_url"))
def test_non_owner_turn_cannot_route_attachments_to_owner_direct(policy, turn, file_url):
    if turn == "other_platform":
        policy.session["HERMES_SESSION_PLATFORM"] = "other_platform"
    else:
        set_turn(policy, turn)
    path = policy.home / "notes.txt"
    reference = path.as_uri() if file_url else str(path)
    assert asyncio.run(outbound(policy, [reference], OWNER_DM)) == []
    assert policy.calls == []
    assert local_paths.turn_allows_local_paths() is False
    assert guard.clawchat_pre_tool_call(tool_name="clawchat_send_file", args={"path": "notes.txt"})["action"] == "block"


def test_gateway_in_process_is_not_a_local_surface(policy, monkeypatch):
    from clawchat_gateway import terminal_send

    assert local_paths.turn_allows_local_paths() is True
    monkeypatch.setattr(terminal_send, "get_clawchat_sender", lambda: object())
    assert local_paths.turn_allows_local_paths() is False
    result = asyncio.run(tools.send_file(OWNER_DM, str(policy.home / "notes.txt")))
    assert result["message"] == local_paths.LOCAL_PATH_REFUSAL
    assert policy.calls == []


@pytest.mark.parametrize("filename", (
    "clawchat/clawchat.sqlite", "clawchat/private.txt", ".env", "config.yaml", "auth.json",
    "clawchat.sqlite", "clawchat.sqlite-wal", "clawchat.sqlite-shm", "clawchat.sqlite-journal",
))
@pytest.mark.parametrize("profile", ("", "profiles/x"), ids=("home", "named_profile"))
def test_custom_hermes_home_and_profiles_are_denied(policy, monkeypatch, filename, profile):
    custom = policy.home / "custom_location"
    path = custom / profile / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"data")
    monkeypatch.setenv("HERMES_HOME", str(custom))
    os_home = policy.home / "os_home"
    os_home.mkdir()
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("USERPROFILE", str(os_home))
    assert not (os_home / ".hermes").exists()
    with pytest.raises(ValueError) as err:
        media_runtime.ensure_allowed_local_path(str(path))
    assert str(custom) not in str(err.value)
    assert media_runtime.ensure_allowed_local_path(str(custom / "notes.txt")) == custom / "notes.txt"


@pytest.mark.parametrize("candidate", ("resolved_home", "environment", "platform_default", "host_home"))
def test_each_hermes_home_candidate_is_denied(policy, monkeypatch, candidate):
    import sys
    from clawchat_gateway import hermes_home as homes

    paths = {name: policy.home / name for name in (
        "resolved_home", "environment", "platform_default", "host_home",
    )}
    monkeypatch.setattr(homes, "hermes_home", lambda: paths["resolved_home"])
    monkeypatch.setattr(homes, "platform_default_hermes_home", lambda: paths["platform_default"])
    monkeypatch.setenv("HERMES_HOME", str(paths["environment"]))
    monkeypatch.setitem(sys.modules, "hermes_constants", SimpleNamespace(get_hermes_home=lambda: paths["host_home"]))
    path = paths[candidate] / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("data")
    with pytest.raises(ValueError):
        media_runtime.ensure_allowed_local_path(str(path))


@pytest.mark.parametrize("suffix", ("", "-wal", "-shm", "-journal"))
def test_existing_store_custom_db_is_denied_without_creating_store(policy, monkeypatch, suffix):
    db_path = policy.home / "custom_database" / "plugin.sqlite"
    path = Path(str(db_path) + suffix)
    path.parent.mkdir()
    path.write_bytes(b"data")
    monkeypatch.setattr(storage, "_stores", {db_path: SimpleNamespace(db_path=db_path)})
    monkeypatch.setattr(storage, "get_clawchat_store", forbid_filesystem)
    with pytest.raises(ValueError):
        media_runtime.ensure_allowed_local_path(str(path))
    assert len(storage._stores) == 1


def test_symlink_to_denied_database_is_refused(policy):
    set_turn(policy, "owner_direct")
    link = policy.home / "ordinary.txt"
    link.symlink_to(policy.home / "clawchat/clawchat.sqlite")
    result = asyncio.run(tools.send_file(OWNER_DM, str(link)))
    assert result["error"] == "validation"
    assert str(policy.home) not in str(result)
    assert asyncio.run(outbound(policy, [str(link)], OWNER_DM)) == []
    assert policy.calls == []


def test_linked_profile_data_is_denied(policy):
    profiles = policy.home / "profiles"
    profiles.mkdir()
    external = policy.home / "external_profile"
    (external / "clawchat").mkdir(parents=True)
    (profiles / "x").symlink_to(external, target_is_directory=True)
    for filename in ("clawchat/clawchat.sqlite", ".env", "config.yaml", "auth.json"):
        path = external / filename
        path.write_text("data")
        with pytest.raises(ValueError):
            media_runtime.ensure_allowed_local_path(str(path))


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
@pytest.mark.parametrize("field", (
    "path", "filePath", "file_path", "images", "iconPath", "icon_path", "media",
    "media_files", "attachments", "avatar", "image",
))
def test_hook_blocks_nested_local_path_fields(policy, turn, field):
    set_turn(policy, turn)
    for value in (str(policy.home / "notes.txt"), "notes.txt"):
        args = {field: [{"nested": [value]}]}
        result = guard.clawchat_pre_tool_call(tool_name="clawchat_send_file", args=args)
        assert result == {"action": "block", "message": local_paths.LOCAL_PATH_REFUSAL}


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
def test_non_owner_remote_moment_and_attachment_remain_available(policy, monkeypatch, turn):
    set_turn(policy, turn)
    url = "https://cdn.example/picture.png"
    assert guard.clawchat_pre_tool_call(tool_name="clawchat_create_moment", args={"images": [url]}) is None
    assert asyncio.run(tools.create_moment(images=[url])) == {"id": 1}
    monkeypatch.setattr(media_runtime, "_load_remote_media", lambda _: media_runtime.LoadedMedia(b"image", "remote.png", "image/png"))
    fragments = asyncio.run(outbound(policy, [str(policy.home / "notes.txt"), url], GROUP))
    assert [fragment["name"] for fragment in fragments] == ["remote.png"]
    assert [call[0] for call in policy.calls] == ["moment", "outbound"]


def test_hook_allows_owner_local_path(policy):
    set_turn(policy, "owner_direct")
    assert guard.clawchat_pre_tool_call(
        tool_name="clawchat_send_file", args={"path": str(policy.home / "notes.txt")},
    ) is None


@pytest.mark.parametrize("platform,engaged,chat_id", (
    ("telegram", False, FRIEND_DM), ("", True, ""), ("", False, FRIEND_DM),
))
def test_unknown_or_outside_session_refuses_tools(policy, monkeypatch, platform, engaged, chat_id):
    policy.session.update({"HERMES_SESSION_PLATFORM": platform, "HERMES_SESSION_CHAT_ID": chat_id})
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: engaged)
    assert local_paths.turn_allows_local_paths() is False
    assert asyncio.run(tools.send_file(OWNER_DM, str(policy.home / "notes.txt")))["message"] == local_paths.LOCAL_PATH_REFUSAL
    if platform or engaged:
        assert guard.clawchat_pre_tool_call(tool_name="clawchat_send_file", args={"path": "notes.txt"})["action"] == "block"


def test_local_operator_surface_preserves_local_file_access(policy):
    assert local_paths.turn_allows_local_paths() is True
    assert guard.clawchat_pre_tool_call(tool_name="clawchat_send_file", args={"path": "notes.txt"}) is None
    assert asyncio.run(tools.send_file(GROUP, str(policy.home / "notes.txt")))["sent"] is True


def test_context_and_destination_errors_fail_closed(policy, monkeypatch):
    def fail(*_args, **_kw):
        raise RuntimeError("context unavailable")

    monkeypatch.setattr(guard, "_session_turn", fail)
    monkeypatch.setattr(guard, "is_owner_turn", fail)
    assert local_paths.turn_allows_local_paths() is False
    assert local_paths.destination_is_owner_direct(OWNER_DM) is False
    assert guard.clawchat_pre_tool_call(tool_name="clawchat_send_file", args={"path": "notes.txt"})["action"] == "block"


def test_unbound_gateway_delivery_uses_owner_destination_only(policy, monkeypatch):
    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    assert not local_paths.turn_allows_local_paths()
    path = str(policy.home / "notes.txt")
    assert asyncio.run(outbound(policy, [path], GROUP)) == []
    assert asyncio.run(outbound(policy, [path], OWNER_DM))[0]["name"] == "notes.txt"
    assert len(policy.calls) == 1


@pytest.mark.parametrize("target", (OWNER_DM, FRIEND_DM, GROUP))
def test_adapter_passes_delivery_destination(policy, monkeypatch, target):
    from types import MethodType
    from clawchat_gateway import adapter

    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    cfg = SimpleNamespace(
        media_local_roots=(), base_url="https://api.example", websocket_url="wss://ws.example",
        token="", media_base_url="",
    )
    instance = SimpleNamespace(
        _clawchat_config=cfg,
        _build_non_media_fragments=lambda *_: [],
        _is_empty_text_response=lambda _: False,
        _extract_media_urls=lambda *_: [str(policy.home / "notes.txt")],
    )
    instance._build_media_fragments = MethodType(adapter.ClawChatAdapter._build_media_fragments, instance)

    async def upload(urls, **kw):
        assert kw["chat_id"] == target
        return await media_runtime.upload_outbound_media(urls, upload_file=policy.uploader, **kw)

    monkeypatch.setattr(adapter, "upload_outbound_media", upload)
    fragments = asyncio.run(adapter.ClawChatAdapter._build_fragments(instance, chat_id=target))
    assert any(fragment.get("name") == "notes.txt" for fragment in fragments) == (target == OWNER_DM)
    assert len(policy.calls) == (1 if target == OWNER_DM else 0)


@pytest.mark.parametrize("target", (OWNER_DM, FRIEND_DM, GROUP))
def test_standalone_passes_delivery_destination(policy, monkeypatch, target):
    from clawchat_gateway import standalone_send as standalone

    monkeypatch.setattr(memory_scope, "_session_context_engaged", lambda: True)
    cfg = SimpleNamespace(
        media_local_roots=(), base_url="https://api.example", websocket_url="wss://ws.example",
        token="", media_base_url="",
    )

    class Connection:
        config = cfg

        def __init__(self, *_a, **_kw):
            pass

        def use_sibling_connect_device_id(self, _suffix):
            return "test-device"

        async def start(self):
            pass

        async def wait_until_ready(self, **_kw):
            return True

        async def send_frame(self, *_a, **_kw):
            return True

        async def stop(self):
            pass

    async def upload(urls, **kw):
        assert kw["chat_id"] == target
        return await media_runtime.upload_outbound_media(urls, upload_file=policy.uploader, **kw)

    monkeypatch.setattr(standalone, "ClawChatConfig", SimpleNamespace(from_platform_config=lambda _: cfg))
    monkeypatch.setattr(standalone, "ClawChatConnection", Connection)
    monkeypatch.setattr(standalone, "upload_outbound_media", upload)
    result = asyncio.run(standalone.standalone_send({}, target, "", media_files=[str(policy.home / "notes.txt")]))
    assert result.get("success", False) == (target == OWNER_DM)
    assert len(policy.calls) == (1 if target == OWNER_DM else 0)


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
def test_hook_leaves_free_text_alone_but_blocks_file_uris(policy, turn):
    set_turn(policy, turn)
    for text in ("/shrug", "~ see you", "./done"):
        assert guard.clawchat_pre_tool_call(tool_name="clawchat_create_moment", args={"text": text}) is None
    result = guard.clawchat_pre_tool_call(
        tool_name="clawchat_create_moment", args={"text": "file:///tmp/notes.txt"},
    )
    assert result == {"action": "block", "message": local_paths.LOCAL_PATH_REFUSAL}


@pytest.mark.parametrize("turn", NON_OWNER_TURNS)
def test_reply_text_is_kept_when_local_attachment_is_dropped(policy, monkeypatch, turn):
    from dataclasses import replace

    from clawchat_gateway import adapter as adapter_mod

    set_turn(policy, turn)
    (policy.home / "config.yaml").write_text("{}\n")
    (policy.home / ".env").write_text("")
    chat_id = TURNS[turn][0]
    a = adapter_mod.ClawChatAdapter({})
    a._clawchat_config = replace(a._clawchat_config, user_id="usr_agent", owner_user_id=OWNER)
    frames = []

    async def send_frame(frame, *, wait_for_ack=False, **_kw):
        frames.append(frame)
        return True

    async def upload(urls, **kw):
        return await media_runtime.upload_outbound_media(urls, **{**kw, "upload_file": policy.uploader})

    monkeypatch.setattr(a._connection, "send_frame", send_frame)
    monkeypatch.setattr(a, "_claim_outbound_message", lambda **_kw: True)
    monkeypatch.setattr(adapter_mod, "upload_outbound_media", upload)
    if chat_id == GROUP:
        a._known_chat_types[GROUP] = "group"

    result = asyncio.run(a.send(
        chat_id, "hello",
        metadata={"_clawchat_immediate_media_send": True},
        media_files=[str(policy.home / "notes.txt")],
    ))
    assert result.success
    assert policy.calls == []
    fragments = [f for frame in frames for f in (frame.get("payload", {}).get("message", {}).get("body", {}).get("fragments") or [])]
    assert fragments and all(f.get("kind") == "text" for f in fragments)

    only_attachment = asyncio.run(a.send(
        chat_id, "",
        metadata={"_clawchat_immediate_media_send": True},
        media_files=[str(policy.home / "notes.txt")],
    ))
    assert not only_attachment.success
    assert str(policy.home) not in (only_attachment.error or "")

