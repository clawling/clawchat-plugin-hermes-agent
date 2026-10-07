"""Every `liveware` process the plugin starts names this profile's account.

The liveware CLI keeps every login in ONE file under the OS home
(``~/.clawling/liveware.json``), keyed by account: ``login`` names the account
after the JWT ``aid`` claim (lowercased), and the first account to log in
becomes the default. A command without ``--account`` acts as that default
account. Two Hermes profiles on one host (each its own ClawChat agent) share
that file, so without ``--account`` the second profile's tunnels, apps and
agent daemon all ran as whichever agent logged in first.

The account cannot be injected through the environment: Hermes strips
``*_TOKEN`` variables from child processes, and the CLI reads no account
variable. So the plugin appends ``--account <agent id, lowercased>`` to every
CLI invocation, and tells the agent its account name in the
``clawchat_liveware_login`` result for the commands it runs itself.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from clawchat_gateway import liveware_sample as ls
from clawchat_gateway import tools
from clawchat_gateway.liveware_cli import liveware_account_args, liveware_account_name

ACCOUNT = "agt_01abc"


class _Proc:
    def __init__(self, rc=0, out=b"", err=b""):
        self.returncode, self._out, self._err = rc, out, err
        self.stdout = None
        self.stderr = None
        self.pid = 4242

    async def communicate(self):
        return self._out, self._err

    def kill(self):
        pass


def _recorder(out=b""):
    calls: list[tuple] = []

    def _exec(*argv, **_kw):
        calls.append(argv)
        return _Proc(0, out)

    return calls, _exec


def test_account_name_is_the_lowercased_agent_id():
    assert liveware_account_name("AGT_01ABC") == "agt_01abc"
    assert liveware_account_name("  agt_x  ") == "agt_x"
    assert liveware_account_name("") is None
    assert liveware_account_name(None) is None


def test_account_args():
    assert liveware_account_args("agt_x") == ["--account", "agt_x"]
    assert liveware_account_args(None) == []
    assert liveware_account_args("") == []


@pytest.mark.parametrize(
    "call, expected_head",
    [
        (lambda ex: ls.liveware_login(liveware_path="lw", token="t", exec=ex, account=ACCOUNT),
         ("lw", "login", "--access-token", "t")),
        (lambda ex: ls.liveware_app_create(liveware_path="lw", name="Sample", exec=ex, account=ACCOUNT),
         ("lw", "app", "create", "Sample")),
        (lambda ex: ls.liveware_app_find_by_name(liveware_path="lw", name="Sample", exec=ex, account=ACCOUNT),
         ("lw", "app", "list")),
        (lambda ex: ls.liveware_agent_is_running(liveware_path="lw", exec=ex, account=ACCOUNT),
         ("lw", "status")),
        (lambda ex: ls.tunnel_bind(liveware_path="lw", app_id="app-1", port=3001, exec=ex, account=ACCOUNT),
         ("lw", "tunnel", "bind", "app-1", "http://127.0.0.1:3001")),
    ],
)
def test_one_shot_cli_calls_append_the_account(call, expected_head):
    out = b"app id: app-1\nhttps://x.example.com\n"
    calls, ex = _recorder(out)
    try:
        asyncio.run(call(ex))
    except ls.LivewareSampleError:
        pass  # output parsing is not under test here; the argv is
    assert len(calls) == 1
    argv = calls[0]
    assert argv[: len(expected_head)] == expected_head
    assert argv[-2:] == ("--account", ACCOUNT)


def test_cli_calls_without_an_account_are_unchanged():
    calls, ex = _recorder()
    asyncio.run(ls.liveware_login(liveware_path="lw", token="t", exec=ex))
    assert calls == [("lw", "login", "--access-token", "t")]


def test_tunnel_agent_daemon_appends_the_account(monkeypatch):
    calls: list[tuple] = []

    def _spawn(*argv, **_kw):
        calls.append(argv)
        return _Proc()

    async def _ready(*_a, **_k):
        return "relay grpc control connected"

    monkeypatch.setattr(ls, "_read_until", _ready)
    monkeypatch.setattr(ls, "_drain_pipes", lambda proc: asyncio.sleep(0))
    proc, drain = asyncio.run(_start(ls.start_tunnel_agent, _spawn))
    assert calls == [("lw", "agent", "--account", ACCOUNT)]


async def _start(fn, spawn):
    proc, drain = await fn(liveware_path="lw", spawn=spawn, account=ACCOUNT)
    drain.close() if asyncio.iscoroutine(drain) else None
    return proc, drain


def test_supervisor_passes_the_account_to_every_cli_step(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    def rec(name, result=None):
        async def _f(**kw):
            seen[name] = kw.get("account", "<missing>")
            return result
        return _f

    async def _server(**_kw):
        return _Proc(), 3001, asyncio.sleep(0)

    async def _agent(**kw):
        seen["start_tunnel_agent"] = kw.get("account", "<missing>")
        return _Proc(), asyncio.sleep(0)

    monkeypatch.setattr(ls, "liveware_login", rec("liveware_login"))
    monkeypatch.setattr(ls, "liveware_app_find_by_name", rec("liveware_app_find_by_name"))
    monkeypatch.setattr(ls, "liveware_app_create", rec("liveware_app_create", "app-1"))
    monkeypatch.setattr(ls, "tunnel_bind", rec("tunnel_bind", "https://pub.example.com"))
    monkeypatch.setattr(ls, "liveware_agent_is_running", rec("liveware_agent_is_running", False))
    monkeypatch.setattr(ls, "start_tunnel_agent", _agent)
    monkeypatch.setattr(ls, "start_sample_server", _server)

    class _Store:
        def upsert_liveware_sample(self, **_kw):
            pass

    async def _list_apps():
        return {"apps": []}

    async def _register_app(**_kw):
        return {}

    async def _notify(_t):
        return True

    deps = ls.LivewareSampleDeps(
        platform="clawchat", account_id="default", enabled=True, store=_Store(),
        sample_root=tmp_path, resolve_token=lambda: "tok",
        resolve_liveware_path=lambda: "lw", list_apps=_list_apps,
        register_app=_register_app, notify_owner=_notify,
        resolve_liveware_account=lambda: "AGT_01ABC",
    )
    sup = ls.LivewareSampleSupervisor(deps)

    async def _download(_v):
        return "1.0.0", tmp_path

    async def _no_intro():
        return None

    monkeypatch.setattr(sup, "_download", _download)
    monkeypatch.setattr(sup, "_deliver_intro", _no_intro)
    monkeypatch.setattr(sup, "_watch_child", lambda proc: None)

    async def _run():
        await sup._bootstrap()
        await sup.stop()

    asyncio.run(_run())
    assert seen == {
        "liveware_login": ACCOUNT,
        "liveware_app_find_by_name": ACCOUNT,
        "liveware_app_create": ACCOUNT,
        "tunnel_bind": ACCOUNT,
        "liveware_agent_is_running": ACCOUNT,
        "start_tunnel_agent": ACCOUNT,
    }


def test_login_tool_names_the_account_and_tells_the_agent_to_use_it(monkeypatch):
    calls: list[tuple] = []

    async def _exec(*argv, **_kw):
        calls.append(argv)
        return _Proc(0)

    monkeypatch.setattr(
        tools, "load_profile_config",
        lambda: SimpleNamespace(token="tok-secret", agent_id="AGT_01ABC"),
    )
    monkeypatch.setattr(tools, "resolve_liveware_path", lambda: "lw")
    monkeypatch.setattr(tools.asyncio, "create_subprocess_exec", _exec)

    result = asyncio.run(tools.liveware_login())

    assert calls == [("lw", "login", "--access-token", "tok-secret", "--account", ACCOUNT)]
    assert result["ok"] is True
    assert result["account"] == ACCOUNT
    assert f"--account {ACCOUNT}" in result["instructions"]
    assert "tok-secret" not in repr(result)
