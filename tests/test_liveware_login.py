"""Tests for clawchat_liveware_login tool and handler (TDD)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clawchat_gateway import plugin_tools, tools
from clawchat_gateway.profile import ProfileConfigError


# ---------------------------------------------------------------------------
# tools.liveware_login — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liveware_login_no_cli_returns_error():
    """liveware CLI not in PATH → error envelope with descriptive message."""
    with (
        patch("clawchat_gateway.tools.load_profile_config") as mock_cfg,
        patch("shutil.which", return_value=None),
    ):
        mock_cfg.return_value = SimpleNamespace(token="tok123")
        res = await tools.liveware_login()

    assert res.get("error") is not None
    assert "liveware CLI not found in PATH" in res.get("message", "")
    # Token must never appear in result
    assert "tok123" not in json.dumps(res)


@pytest.mark.asyncio
async def test_liveware_login_config_error_returns_config_envelope():
    """ProfileConfigError → config error envelope."""
    with patch("clawchat_gateway.tools.load_profile_config", side_effect=ProfileConfigError("missing token")):
        res = await tools.liveware_login()

    assert res.get("error") == "config"
    assert "missing token" in res.get("message", "")


@pytest.mark.asyncio
async def test_liveware_login_success_returns_ok_without_token():
    """exit 0 → {'ok': True} and token is NOT in the returned dict."""
    token = "secret-bearer-token"

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"Logged in\n", b""))

    with (
        patch("clawchat_gateway.tools.load_profile_config") as mock_cfg,
        patch("shutil.which", return_value="/usr/bin/liveware"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)),
    ):
        mock_cfg.return_value = SimpleNamespace(token=token)
        res = await tools.liveware_login()

    assert res == {"ok": True}
    # Security: token must never be in the returned value
    assert token not in json.dumps(res)


@pytest.mark.asyncio
async def test_liveware_login_nonzero_exit_scrubs_token():
    """Non-zero exit code → error envelope with scrubbed output (token replaced by ***)."""
    token = "my-secret-token"

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", f"auth error: {token}".encode()))

    with (
        patch("clawchat_gateway.tools.load_profile_config") as mock_cfg,
        patch("shutil.which", return_value="/usr/bin/liveware"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)),
    ):
        mock_cfg.return_value = SimpleNamespace(token=token)
        res = await tools.liveware_login()

    assert res.get("error") is not None
    result_json = json.dumps(res)
    # Token must be scrubbed
    assert token not in result_json
    assert "***" in result_json


@pytest.mark.asyncio
async def test_liveware_login_timeout_returns_error():
    """asyncio.TimeoutError → timeout error envelope; token not in result."""
    token = "timeout-token"

    async def _mock_communicate():
        raise asyncio.TimeoutError

    fake_proc = MagicMock()
    fake_proc.kill = MagicMock()
    fake_proc.communicate = _mock_communicate

    with (
        patch("clawchat_gateway.tools.load_profile_config") as mock_cfg,
        patch("shutil.which", return_value="/usr/bin/liveware"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)),
    ):
        mock_cfg.return_value = SimpleNamespace(token=token)
        res = await tools.liveware_login()

    assert res.get("error") is not None
    assert token not in json.dumps(res)


# ---------------------------------------------------------------------------
# plugin_tools.handle_clawchat_liveware_login — handler test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_clawchat_liveware_login_success(monkeypatch):
    """Handler delegates to tools.liveware_login and JSON-serialises the result."""
    monkeypatch.setattr(tools, "liveware_login", AsyncMock(return_value={"ok": True}))

    result = await plugin_tools.handle_clawchat_liveware_login({})
    payload = json.loads(result)
    assert payload == {"ok": True}


@pytest.mark.asyncio
async def test_handle_clawchat_liveware_login_config_error(monkeypatch):
    """Handler propagates config error envelope from tools.liveware_login."""
    monkeypatch.setattr(
        tools, "liveware_login", AsyncMock(return_value={"error": "config", "message": "missing token"})
    )

    result = await plugin_tools.handle_clawchat_liveware_login({})
    payload = json.loads(result)
    assert payload["error"] == "config"


# ---------------------------------------------------------------------------
# register_tools — registration check
# ---------------------------------------------------------------------------


def test_register_tools_includes_liveware_login():
    registered: dict[str, dict] = {}

    class Ctx:
        def register_tool(self, name, _namespace, schema, handler, **_kwargs):
            registered[name] = {"schema": schema, "handler": handler}

    plugin_tools.register_tools(Ctx())

    assert "clawchat_liveware_login" in registered
    schema = registered["clawchat_liveware_login"]["schema"]
    assert schema["name"] == "clawchat_liveware_login"
    assert "description" in schema
