"""Tests for GroupSettingsCache and get_my_group_settings.

Sequence-ordered, authoritative-vs-not cache semantics (mirrors the OpenClaw
plugin's ``GroupSettingsCache`` parity, Task 6 + group-governance migration):

  1. An authoritative full pull REPLACES the cache (prunes vanished rows).
  2. Cross-pull ordering is by a monotonic fetch SEQUENCE assigned at the call
     site — a pull whose sequence <= the last applied one is dropped wholesale
     (this gates EMPTY pulls too: a stale "clear all" can never overtake a newer
     snapshot). ``version`` is only an in-snapshot tiebreaker.
  3. An authoritative HTTP 200 with an EMPTY list means "zero overrides" and
     CLEARS the cache.
  4. A non-authoritative outcome (404 / endpoint-absent / non-2xx / network
     error) is a NO-OP at the call site — it must never reach ``apply_fetched``.
  5. Unknown chat falls back to ``static_fallback``.

Plus parse tests for ``get_my_group_settings`` distinguishing an authoritative
200 (incl. 200-empty) from a non-authoritative 404.
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
# Cache: canonical cases (sequence-ordered)
# ---------------------------------------------------------------------------


def test_backend_overrides_static() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 2)], sequence=1)
    assert c.effective("c1", STATIC) == EffectiveSettings(True, "mention", 30)


def test_ignores_stale_sequence() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "all", 10, 5)], sequence=2)
    # A later-arriving pull tagged with an older sequence must be dropped wholesale.
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 3)], sequence=1)
    assert c.effective("c1", STATIC).muted is True


def test_ignores_equal_sequence() -> None:
    """A pull with sequence == last-applied is a re-delivery and is dropped."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 20, 5)], sequence=3)
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 9)], sequence=3)
    assert c.effective("c1", STATIC).muted is True
    assert c.effective("c1", STATIC).reply_mode == "mention"


def test_accepts_higher_sequence() -> None:
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 20, 5)], sequence=1)
    c.apply_fetched([GroupSettings("c1", False, "all", 10, 1)], sequence=2)
    # Newer sequence wins regardless of row version (version is not a cross-pull guard).
    assert c.effective("c1", STATIC).muted is False


def test_unknown_chat_falls_back() -> None:
    c = GroupSettingsCache()
    assert c.effective("c1", STATIC) == STATIC


def test_multiple_conversations_independent() -> None:
    c = GroupSettingsCache()
    c.apply_fetched(
        [
            GroupSettings("c1", True, "mention", 30, 1),
            GroupSettings("c2", False, "all", 5, 1),
        ],
        sequence=1,
    )
    assert c.effective("c1", STATIC) == EffectiveSettings(True, "mention", 30)
    assert c.effective("c2", STATIC) == EffectiveSettings(False, "all", 5)
    assert c.effective("c3", STATIC) == STATIC


def test_authoritative_empty_pull_clears_cache() -> None:
    """An authoritative 200 with an EMPTY list means "zero overrides" => clear all.

    This replaces the old ``test_apply_fetched_empty_list_is_noop``: the 404 /
    no-endpoint safety now lives at the CALL SITE (non-authoritative outcomes
    never reach ``apply_fetched``), so an empty list that *does* reach the cache
    is an authoritative "all overrides cleared" and must prune.
    """
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 3)], sequence=1)
    c.apply_fetched([], sequence=2)
    assert c.effective("c1", STATIC) == STATIC, "authoritative empty pull must clear"


def test_stale_empty_pull_does_not_wipe_newer_snapshot() -> None:
    """A late EMPTY pull with a LOWER sequence must not wipe a newer snapshot."""
    c = GroupSettingsCache()
    # Newer non-empty snapshot applied first (higher sequence).
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 5)], sequence=2)
    # A stale empty "clear all" lands late with a lower sequence — ignored.
    c.apply_fetched([], sequence=1)
    assert c.effective("c1", STATIC).muted is True


def test_full_fetch_clears_rows_absent_from_fresh_fetch() -> None:
    """A full pull is a replacement set: a conversation omitted from the fresh
    pull must fall back to static (not keep its stale cached override)."""
    c = GroupSettingsCache()
    c.apply_fetched(
        [
            GroupSettings("c1", True, "mention", 30, 1),
            GroupSettings("c2", True, "all", 10, 1),
        ],
        sequence=1,
    )
    # Fresh pull no longer includes c1 (e.g. agent left that group, row deleted).
    c.apply_fetched([GroupSettings("c2", True, "all", 10, 2)], sequence=2)
    assert c.effective("c1", STATIC) == STATIC, "omitted row must be cleared"
    assert c.effective("c2", STATIC).muted is True


def test_stale_nonempty_pull_does_not_roll_back() -> None:
    """An out-of-order older non-empty snapshot (lower sequence) is dropped."""
    c = GroupSettingsCache()
    c.apply_fetched([GroupSettings("c1", True, "mention", 30, 5)], sequence=3)
    c.apply_fetched([GroupSettings("c2", True, "all", 20, 3)], sequence=2)
    assert c.effective("c1", STATIC).muted is True
    assert c.effective("c2", STATIC) == STATIC


def test_in_snapshot_version_tiebreaker() -> None:
    """Within ONE snapshot a repeated conversation keeps the highest version."""
    c = GroupSettingsCache()
    c.apply_fetched(
        [
            GroupSettings("c1", False, "all", 10, 1),
            GroupSettings("c1", True, "mention", 30, 5),
            GroupSettings("c1", False, "all", 10, 3),
        ],
        sequence=1,
    )
    assert c.effective("c1", STATIC) == EffectiveSettings(True, "mention", 30)


# ---------------------------------------------------------------------------
# get_my_group_settings: authoritative-vs-not parse
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
async def test_get_my_group_settings_404_is_non_authoritative(monkeypatch) -> None:
    """HTTP 404 (older backend, no endpoint) => non-authoritative no-op result."""
    from clawchat_gateway.api_client import ClawChatApiClient

    _patch_urlopen_status(monkeypatch, 404)
    client = ClawChatApiClient(
        base_url="https://api.test", token="tok", device_id="dev-1"
    )
    result = await client.get_my_group_settings()
    assert result.authoritative is False
    assert result.rows == []


@pytest.mark.asyncio
async def test_get_my_group_settings_200_empty_is_authoritative(monkeypatch) -> None:
    """HTTP 200 with an empty list => authoritative "zero overrides"."""
    from clawchat_gateway.api_client import ClawChatApiClient

    payload = {"code": 0, "data": {"settings": []}, "msg": ""}
    _patch_urlopen_status(monkeypatch, 200, payload)
    client = ClawChatApiClient(
        base_url="https://api.test", token="tok", device_id="dev-1"
    )
    result = await client.get_my_group_settings()
    assert result.authoritative is True
    assert result.rows == []


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
    assert result.authoritative is True
    rows = result.rows
    assert len(rows) == 2
    assert rows[0].conversation_id == "grp_abc"
    assert rows[0].muted is True
    assert rows[0].reply_mode == "mention"
    assert rows[0].batch_delay_seconds == 15
    assert rows[0].version == 7
    assert rows[1].conversation_id == "grp_def"
    assert rows[1].muted is False
