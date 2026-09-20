"""activate.py must resolve the SAME device id an existing pairing already
connects with — never handing the backend's redeem safety gate
(``paired_device_id``, keyed on device id alone) or the message hub's
per-device delivery cursor a different id on ``--repair`` / the bound-agent
auto-repair.

Only a genuinely fresh pairing (no existing identity at all, or an explicit
``--new-account``) may get the new per-profile ``get_device_id()`` id.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import clawchat_gateway.activate as activate_mod
from clawchat_gateway import device_id as dev
from clawchat_gateway.api_client import AGENT_NOT_FOUND_CODE, ClawChatApiClient, ClawChatApiError


class _StubStore:
    def __init__(self, device_id: str | None) -> None:
        self._device_id = device_id

    def get_activation_credentials(self, *, platform: str, account_id: str):
        if self._device_id is None:
            return None
        return SimpleNamespace(device_id=self._device_id)


@pytest.fixture(autouse=True)
def _clear_cache():
    dev._host_device_id.cache_clear()
    yield
    dev._host_device_id.cache_clear()


def test_repair_with_stored_row_device_id_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(activate_mod, "get_clawchat_store", lambda: _StubStore("hermes-legacy-row-id"))
    monkeypatch.setattr(activate_mod, "_get_env", lambda _name: "")
    result = activate_mod._resolve_activation_device_id(existing_user_id="usr_1", new_account=False)
    assert result == "hermes-legacy-row-id"


def test_repair_with_no_row_but_a_token_falls_back_to_legacy_host_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named profile ("work"), not "default": on the default profile
    get_device_id() == legacy_host_device_id() byte-for-byte, so a regression
    that returned get_device_id() here would still pass. The `!=
    dev.get_device_id()` assertion is what actually catches that."""
    monkeypatch.setattr(activate_mod, "get_clawchat_store", lambda: _StubStore(None))
    monkeypatch.setattr(
        activate_mod,
        "_get_env",
        lambda name: "some-opaque-token" if name == "CLAWCHAT_TOKEN" else "",
    )
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-legacy")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    result = activate_mod._resolve_activation_device_id(existing_user_id="usr_1", new_account=False)
    assert result == "hermes-machine-legacy"
    assert result == dev.legacy_host_device_id()
    assert result != dev.get_device_id()


def test_repair_with_no_row_and_no_token_falls_back_to_a_fresh_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No stored row, no token at all: nothing to replay, so this degrades to
    the fresh-pairing (per-profile) derivation rather than crashing or
    returning "" — the genuinely-unresolvable edge of the "existing identity
    present but no local credentials survive" shape (see
    docs/activation.md's cloned-profile discussion).

    Named profile ("work"), not "default": on the default profile the fresh
    derivation and the legacy id are byte-identical, so this wouldn't actually
    prove "fresh, per-profile" behaviour without it — see the explicit `-p`
    and `!= legacy_host_device_id()` assertions below."""
    monkeypatch.setattr(activate_mod, "get_clawchat_store", lambda: _StubStore(None))
    monkeypatch.setattr(activate_mod, "_get_env", lambda _name: "")
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    result = activate_mod._resolve_activation_device_id(existing_user_id="usr_1", new_account=False)
    assert result == dev.get_device_id()
    assert result.startswith("hermes-machine-abc123-p")
    assert result != dev.legacy_host_device_id()


def test_fresh_activation_on_named_profile_gets_the_per_profile_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    result = activate_mod._resolve_activation_device_id(existing_user_id="", new_account=False)
    assert result.startswith("hermes-machine-abc123-p")
    assert result == dev.get_device_id()


def test_new_account_gets_the_per_profile_id_even_with_an_existing_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--new-account must win over a replayed existing_user_id: it is an
    explicit "give me a NEW agent" request, so the stored row (if any) must
    not leak into the new agent's device id."""
    monkeypatch.setattr(activate_mod, "get_clawchat_store", lambda: _StubStore("hermes-legacy-row-id"))
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    result = activate_mod._resolve_activation_device_id(existing_user_id="usr_1", new_account=True)
    assert result.startswith("hermes-machine-abc123-p")
    assert result != "hermes-legacy-row-id"


# --- AGENT_NOT_FOUND_CODE (16001) retry --------------------------------------
# Task 3 review fix round 2, controller-ruled addition: the replayed identity
# turned out not to exist on the backend, so `activate()` retries as a fresh
# pairing (user_id=None) — but it must mint a NEW per-profile device id for
# that fresh pairing, never reuse the stale identity's resolved id (which
# could otherwise collide with, say, the default profile's agent on this
# same host if that one paired under the legacy host id — rule 5).
#
# Seam: monkeypatch `ClawChatApiClient._call_json` (the same seam
# tests/test_activate_precheck.py already uses) rather than reaching into
# `agents_connect_with_retry` — it's the lowest-level hook already exercised
# elsewhere in this repo's tests, so it drives the real `activate()` coroutine
# end-to-end without inventing a new mocking pattern.


def _config_with_stale_identity() -> dict:
    return {
        "platforms": {
            "clawchat": {
                "extra": {
                    "user_id": "usr_stale",
                    "base_url": "https://app.clawling.com",
                    "profile": "work",
                }
            }
        }
    }


@pytest.mark.asyncio
async def test_agent_not_found_retry_mints_a_fresh_per_profile_device_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")

    monkeypatch.setattr(
        activate_mod,
        "_load_config",
        lambda: ("config.yaml", _config_with_stale_identity()),
    )
    monkeypatch.setattr(activate_mod, "_active_profile_name", lambda: "work")
    monkeypatch.setattr(activate_mod, "_get_env", lambda _name: "")
    monkeypatch.setattr(activate_mod, "_write_env_values", lambda values: "/tmp/.env")
    monkeypatch.setattr(activate_mod, "_write_config", lambda *_a, **_k: None)

    upserts: list[dict[str, object]] = []

    class _UpsertStore(_StubStore):
        def upsert_activation(self, **kwargs: object) -> bool:
            upserts.append(kwargs)
            return True

    monkeypatch.setattr(
        activate_mod, "get_clawchat_store", lambda: _UpsertStore("hermes-stale-row-id")
    )

    seen_devices: list[str] = []

    async def fake_call_json(self, method, path, *, body=None, extra_headers=None, **_kw):
        if path == "/v1/agents/connect/check":
            # Precheck is telemetry-only; a failure here just skips it.
            raise RuntimeError("no precheck in this test")
        assert path == "/v1/agents/connect"
        seen_devices.append(self._device_id)
        sent = json.loads(body.decode("utf-8"))
        if "user_id" in sent:
            # First attempt: replays the stale identity. The backend no
            # longer has an agent for it.
            raise ClawChatApiError("api", "agent not found", code=AGENT_NOT_FOUND_CODE)
        # Retry: a genuinely fresh pairing.
        return {
            "agent": {"id": "agt_new", "user_id": "usr_new", "owner_id": "usr_owner"},
            "conversation": {"id": "conv_1"},
            "access_token": "tok_new",
            "refresh_token": "rtok_new",
        }

    monkeypatch.setattr(ClawChatApiClient, "_call_json", fake_call_json)

    await activate_mod.activate(
        "CODE1234", base_url="https://app.clawling.com", new_account=False, repair=True
    )

    fresh_id = dev.get_device_id()
    assert seen_devices == ["hermes-stale-row-id", fresh_id], (
        "the first attempt must present the stale identity's own id (rule 3/4), "
        "and the retry — a brand-new agent — must present the NEW per-profile id "
        "(rule 5), never the stale one"
    )
    assert upserts, "expected the successful retry to persist via upsert_activation"
    assert upserts[-1]["device_id"] == fresh_id
    assert upserts[-1]["device_id"] != "hermes-stale-row-id"
