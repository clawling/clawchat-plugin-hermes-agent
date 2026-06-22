"""Tests for GroupSettingsCache and get_my_group_settings (Task 10 TDD).

Three cache cases (mirrors Task 6 / TS plugin parity):
  1. Backend row overrides static fallback.
  2. Stale (lower version) row is ignored.
  3. Unknown chat falls back to static_fallback.

Plus a thin parse test for get_my_group_settings 404 -> [].
"""

from __future__ import annotations

import json

import pytest

from clawchat_gateway.group_settings import (
    EffectiveSettings,
    GroupSettings,
    GroupSettingsCache,
)

STATIC = EffectiveSettings(muted=False, reply_mode="all", batch_delay_seconds=10)


# ---------------------------------------------------------------------------
# Cache: three canonical cases
# ---------------------------------------------------------------------------


def test_backend_overrides_static() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 2)])
    assert c.effective("c1", STATIC) == EffectiveSettings(True, "mention", 30)


def test_ignores_stale_version() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "all", 10, 5)])
    # older version must NOT overwrite
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 3)])
    assert c.effective("c1", STATIC).muted is True


def test_ignores_strictly_lower_version_boundary() -> None:
    """version 4 < 5 => ignored (boundary: strict-less-than is ignored)."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 20, 5)])
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 4)])
    assert c.effective("c1", STATIC).muted is True
    assert c.effective("c1", STATIC).reply_mode == "mention"


def test_accepts_equal_version() -> None:
    """version == cached => accepted (>= semantics)."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 20, 5)])
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 5)])
    assert c.effective("c1", STATIC).muted is False


def test_unknown_chat_falls_back() -> None:
    c = GroupSettingsCache()
    assert c.effective("c1", STATIC) == STATIC


def test_multiple_conversations_independent() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([
        GroupSettings("c1", True, "mention", 30, 1),
        GroupSettings("c2", False, "all", 5, 1),
    ])
    assert c.effective("c1", STATIC) == EffectiveSettings(True, "mention", 30)
    assert c.effective("c2", STATIC) == EffectiveSettings(False, "all", 5)
    assert c.effective("c3", STATIC) == STATIC


def test_apply_fetched_empty_list_is_noop() -> None:
    """Empty fetch is treated as a no-op (404 / no-endpoint safety), NOT a wipe."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 3)])
    c.apply_fetched([])
    assert c.effective("c1", STATIC).muted is True


def test_full_fetch_clears_rows_absent_from_fresh_fetch() -> None:
    """A non-empty full fetch is a replacement set: a conversation omitted from the
    fresh fetch must fall back to static (not keep its stale cached override)."""
    c = GroupSettingsCache()
    c.apply_fetched([
        GroupSettings("c1", True, "mention", 30, 1),
        GroupSettings("c2", True, "all", 10, 1),
    ])
    # Fresh fetch no longer includes c1 (e.g. agent left that group, row deleted).
    c.apply_fetched([GroupSettings("c2", True, "all", 10, 2)])
    assert c.effective("c1", STATIC) == STATIC, "omitted row must be cleared"
    assert c.effective("c2", STATIC).muted is True


def test_full_fetch_replacement_preserves_version_monotonicity() -> None:
    """A row whose fetched version is strictly older than the cached one is kept
    (guards a refresh racing behind a newer config-changed signal)."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 5)])
    # Stale fetch (older version) for c1 alongside a fresh c2.
    c.apply_fetched([
        GroupSettings("c1", False, "all", 10, 3),
        GroupSettings("c2", False, "all", 5, 1),
    ])
    assert c.effective("c1", STATIC).muted is True, "stale version must not overwrite"
    assert c.effective("c1", STATIC).reply_mode == "mention"
    assert c.effective("c2", STATIC) == EffectiveSettings(False, "all", 5)


# ---------------------------------------------------------------------------
# get_my_group_settings: 404 -> [] and happy-path parse
# ---------------------------------------------------------------------------


def _patch_urlopen_status(monkeypatch, status: int, payload: dict | None = None):
    """Monkey-patch api_client.urlopen to return a fake HTTP response."""
    import clawchat_gateway.api_client as api_client_mod
    from urllib.error import HTTPError
    import io

    if status == 404:
        def fake_urlopen(request, timeout=None):
            raise HTTPError(
                url=request.full_url,
                code=404,
                msg="Not Found",
                hdrs={},  # type: ignore[arg-type]
                fp=io.BytesIO(b'{"code":404,"msg":"not found","data":{}}'),
            )
    else:
        raw = json.dumps(payload).encode("utf-8")

        class _FakeResp:
            status = 200

            def read(self):
                return raw

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            return _FakeResp()

    monkeypatch.setattr(api_client_mod, "urlopen", fake_urlopen)


@pytest.mark.asyncio
async def test_get_my_group_settings_404_returns_empty(monkeypatch) -> None:
    from clawchat_gateway.api_client import ClawChatApiClient

    _patch_urlopen_status(monkeypatch, 404)
    client = ClawChatApiClient(
        base_url="https://api.test", token="tok", device_id="dev-1"
    )
    result = await client.get_my_group_settings()
    assert result == []


@pytest.mark.asyncio
async def test_get_my_group_settings_parses_rows(monkeypatch) -> None:
    from clawchat_gateway.api_client import ClawChatApiClient

    payload = {
        "code": 0,
        "data": {
            "settings": [
                {
                    "conversation_id": "grp_abc",
                    "agent_id": "agt_xyz",
                    "muted": True,
                    "reply_mode": "mention",
                    "batch_delay_seconds": 15,
                    "version": 7,
                },
                {
                    "conversation_id": "grp_def",
                    "agent_id": "agt_xyz",
                    "muted": False,
                    "reply_mode": "all",
                    "batch_delay_seconds": 5,
                    "version": 2,
                },
            ]
        },
        "msg": "",
    }
    _patch_urlopen_status(monkeypatch, 200, payload)
    client = ClawChatApiClient(
        base_url="https://api.test", token="tok", device_id="dev-1"
    )
    result = await client.get_my_group_settings()
    assert len(result) == 2
    assert result[0].conversation_id == "grp_abc"
    assert result[0].muted is True
    assert result[0].reply_mode == "mention"
    assert result[0].batch_delay_seconds == 15
    assert result[0].version == 7
    assert result[1].conversation_id == "grp_def"
    assert result[1].muted is False
