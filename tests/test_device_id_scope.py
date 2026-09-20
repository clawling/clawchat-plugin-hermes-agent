"""Device id is agent-scoped: one id per Hermes profile on the same host.

Two profiles used to share the host fingerprint. Backend structures are keyed
on (user_id, device_id), so that was not a takeover — but the redeem safety
gate (`paired_device_id`) and the plugin-report row are keyed on device_id
alone, and two agents behind one id collide there. Existing pairings must keep
whatever id they connected with.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from clawchat_gateway import device_id as dev
from clawchat_gateway.config import ClawChatConfig
from clawchat_gateway.connection import ClawChatConnection


@pytest.fixture(autouse=True)
def _clear_cache():
    dev._host_device_id.cache_clear()
    yield
    dev._host_device_id.cache_clear()


def test_default_profile_keeps_the_legacy_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_profile_name", lambda: "default")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    assert dev.get_device_id() == "hermes-machine-abc123"
    assert dev.get_device_id() == dev.legacy_host_device_id()


def test_named_profile_gets_its_own_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    work = dev.get_device_id()
    dev._host_device_id.cache_clear()
    monkeypatch.setattr(dev, "_profile_name", lambda: "home")
    home = dev.get_device_id()
    assert work != home
    assert work.startswith("hermes-machine-abc123-p") and home.startswith("hermes-machine-abc123-p")
    assert dev.legacy_host_device_id() == "hermes-machine-abc123"


def test_pinned_env_wins_regardless_of_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev, "_env", lambda name: "hermes-pinned-1" if name == "CLAWCHAT_DEVICE_ID" else "")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    assert dev.get_device_id() == "hermes-pinned-1"


def test_scope_is_stable_and_transport_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dev, "_profile_name", lambda: "工作 profile")
    scope = dev.profile_scope()
    assert scope == dev.profile_scope()
    assert len(scope) == 12 and all(c in "0123456789abcdef" for c in scope)


# --- ClawChatConnection._resolve_device_id -----------------------------------
# Task 3 review fix round 1, finding 2: the compatibility rules were only
# exercised at the `device_id.py` unit level; nothing drove
# `connection.py::_resolve_device_id` itself through its token/row branches.


def _fake_jwt(payload: dict[str, object] | None) -> str:
    """A JWT shaped enough for ``_jwt_claim`` to decode: header.payload.sig."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    if payload is None:
        body = "not-valid-base64-json!!!"
    else:
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


class _StubStore:
    """Minimal ``get_activation_credentials`` double, per the reviewer's seam."""

    def __init__(self, device_id: str | None) -> None:
        self._device_id = device_id

    def get_activation_credentials(self, *, platform: str, account_id: str):
        if self._device_id is None:
            return None
        return SimpleNamespace(device_id=self._device_id)


def _make_conn(*, token: str, stored_device_id: str | None) -> ClawChatConnection:
    cfg = ClawChatConfig(
        websocket_url="wss://example.invalid/ws",
        token=token,
        user_id="usr_1",
        owner_user_id="usr_owner",
    )
    conn = ClawChatConnection(cfg, on_message=lambda *_a, **_k: None)
    conn._store = _StubStore(stored_device_id)
    return conn


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("plain-opaque-token-no-dots", id="opaque_token_no_did"),
        pytest.param(_fake_jwt(None), id="corrupt_jwt_payload"),
        pytest.param(_fake_jwt({"sub": "usr_1"}), id="jwt_without_did_claim"),
    ],
)
def test_resolve_device_id_falls_back_to_legacy_host_id_when_paired_without_a_record(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """A token exists (this profile IS paired) but neither the activations row
    nor the token itself yields a device id: must present the LEGACY host id,
    never the new per-profile derivation — rule 4.

    Uses a NAMED profile ("work"), not "default": on the default profile
    get_device_id() and legacy_host_device_id() are byte-identical, so a
    regression that returns get_device_id() here would still pass. The extra
    ``!= dev.get_device_id()`` assertion is what actually catches that."""
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    conn = _make_conn(token=token, stored_device_id=None)
    resolved, _durable = conn._resolve_device_id()
    assert resolved == dev.legacy_host_device_id()
    assert resolved != dev.get_device_id()


def test_resolve_device_id_uses_the_jwt_did_claim_when_present() -> None:
    token = _fake_jwt({"sub": "usr_1", "did": "hermes-did-from-token"})
    conn = _make_conn(token=token, stored_device_id=None)
    resolved, durable = conn._resolve_device_id()
    assert resolved == "hermes-did-from-token"
    assert durable is True


def test_resolve_device_id_stored_row_wins_over_a_did_bearing_token() -> None:
    token = _fake_jwt({"sub": "usr_1", "did": "hermes-did-from-token"})
    conn = _make_conn(token=token, stored_device_id="hermes-stored-row-id")
    resolved, durable = conn._resolve_device_id()
    assert resolved == "hermes-stored-row-id"
    assert durable is True


@pytest.mark.parametrize("token", ["", "   "], ids=["empty_token", "whitespace_token"])
def test_resolve_device_id_with_no_token_at_all_gets_the_per_profile_id(
    monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """No stored row and no token whatsoever: a truly unpaired process — rule
    5, this is the ONLY branch that may hand out the new per-profile id."""
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    conn = _make_conn(token=token, stored_device_id=None)
    resolved, _durable = conn._resolve_device_id()
    assert resolved == dev.get_device_id()
    assert resolved.startswith("hermes-machine-abc123-p")
