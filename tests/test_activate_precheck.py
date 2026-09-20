"""Activation pre-checks the code (non-consuming) before /v1/agents/connect.

Transport failure degrades to "no pre-check"; only an explicit
``pairable: false`` refuses. ``bound_agent: true`` (the owner's reconnect
prompt) counts as the restore intent.
"""

from __future__ import annotations

import json

import pytest

from clawchat_gateway.activate import PrecheckOutcome, evaluate_precheck
from clawchat_gateway.api_client import ClawChatApiClient
from clawchat_gateway.onboarding import RECONNECT_GUIDE_URL


def test_evaluate_precheck_none_means_skip() -> None:
    out = evaluate_precheck(None)
    assert out == PrecheckOutcome(pairable=True, bound_agent=False, refusal="")


def test_evaluate_precheck_bound_code_is_restore_intent() -> None:
    out = evaluate_precheck({"pairable": True, "status": "pending", "bound_agent": True})
    assert out.bound_agent is True
    assert out.refusal == ""


def test_evaluate_precheck_spent_code_points_at_reconnect_page() -> None:
    out = evaluate_precheck({"pairable": False, "status": "paired"})
    assert out.pairable is False
    assert RECONNECT_GUIDE_URL in out.refusal
    assert "reconnect prompt from the ClawChat app" in out.refusal
    assert "--repair" not in out.refusal


def test_evaluate_precheck_expired_asks_for_a_fresh_code() -> None:
    out = evaluate_precheck({"pairable": False, "status": "expired"})
    assert "fresh code" in out.refusal


def test_evaluate_precheck_owner_mismatch() -> None:
    out = evaluate_precheck({"pairable": False, "status": "pending", "user_id_status": "owner_mismatch"})
    assert "different ClawChat account" in out.refusal


@pytest.mark.asyncio
async def test_agents_connect_check_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_call_json(self, method, path, *, body=None, extra_headers=None, **_kw):
        seen["method"] = method
        seen["path"] = path
        seen["body"] = json.loads(body.decode("utf-8"))
        return {"pairable": True, "status": "pending"}

    monkeypatch.setattr(ClawChatApiClient, "_call_json", fake_call_json)
    client = ClawChatApiClient(base_url="https://app.clawling.com", device_id="hermes-test")
    res = await client.agents_connect_check(
        code="K7RM4TQP",
        user_id="usr_1",
        context={"agent_kind": "hermes", "lane": "self", "os": "linux"},
    )
    assert res["pairable"] is True
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/agents/connect/check"
    assert seen["body"] == {
        "code": "K7RM4TQP",
        "platform": "hermes",
        "plugin_version": seen["body"]["plugin_version"],
        "user_id": "usr_1",
        "agent_kind": "hermes",
        "lane": "self",
        "os": "linux",
    }


@pytest.mark.asyncio
async def test_agents_connect_carries_context(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_call_json(self, method, path, *, body=None, extra_headers=None, **_kw):
        seen["body"] = json.loads(body.decode("utf-8"))
        return {"agent": {}, "access_token": "t"}

    monkeypatch.setattr(ClawChatApiClient, "_call_json", fake_call_json)
    client = ClawChatApiClient(base_url="https://app.clawling.com", device_id="hermes-test")
    await client.agents_connect(code="K7RM4TQP", context={"agent_kind": "hermes", "lane": "self"})
    assert seen["body"]["agent_kind"] == "hermes"
    assert seen["body"]["lane"] == "self"


def test_evaluate_precheck_empty_200_data_degrades_to_proceed() -> None:
    # Parity with the TS pre-check: a 200 with empty data says nothing about
    # the code, so it is treated like an unreachable endpoint.
    assert evaluate_precheck({}) == PrecheckOutcome(pairable=True, bound_agent=False, refusal="")


def test_evaluate_precheck_bound_code_for_a_different_agent() -> None:
    out = evaluate_precheck(
        {"pairable": False, "status": "pending", "bound_agent": True, "user_id_status": "owner_mismatch"}
    )
    assert out.pairable is False
    assert "reconnect prompt for a different agent" in out.refusal
    assert "different ClawChat account" not in out.refusal


def test_evaluate_precheck_invalid_user_id_has_its_own_message() -> None:
    out = evaluate_precheck({"pairable": False, "status": "pending", "user_id_status": "invalid"})
    assert "not a valid ClawChat user id" in out.refusal
    assert "--new-account" in out.refusal
    assert "fresh code" not in out.refusal


# --- --new-account must not be blocked by a verdict on the id it drops ------
# End-to-end through the real activate() coroutine, seamed at
# ClawChatApiClient._call_json like tests/test_activate_device_id.py.


def _wire_activate(monkeypatch: pytest.MonkeyPatch, check_response):
    import clawchat_gateway.activate as activate_mod
    from clawchat_gateway import device_id as dev

    dev._host_device_id.cache_clear()
    monkeypatch.setattr(dev, "_env", lambda _name: "")
    monkeypatch.setattr(dev, "_mac_platform_uuid", lambda: "")
    monkeypatch.setattr(dev, "_machine_id", lambda: "hermes-machine-abc123")
    monkeypatch.setattr(dev, "_profile_name", lambda: "work")
    config = {
        "platforms": {
            "clawchat": {
                "extra": {
                    "user_id": "usr_other_owner",
                    "agent_id": "agt_other",
                    "base_url": "https://app.clawling.com",
                    "profile": "work",
                }
            }
        }
    }
    monkeypatch.setattr(activate_mod, "_load_config", lambda: ("config.yaml", config))
    monkeypatch.setattr(activate_mod, "_active_profile_name", lambda: "work")
    monkeypatch.setattr(activate_mod, "_get_env", lambda _name: "")
    monkeypatch.setattr(activate_mod, "_write_env_values", lambda values: "/tmp/.env")
    monkeypatch.setattr(activate_mod, "_write_config", lambda *_a, **_k: None)

    class _Store:
        def get_activation_credentials(self, *, platform: str, account_id: str):
            return None

        def upsert_activation(self, **kwargs: object) -> bool:
            return True

    monkeypatch.setattr(activate_mod, "get_clawchat_store", lambda: _Store())

    calls: list[tuple[str, dict]] = []

    async def fake_call_json(self, method, path, *, body=None, extra_headers=None, **_kw):
        sent = json.loads(body.decode("utf-8"))
        calls.append((path, sent))
        if path == "/v1/agents/connect/check":
            return check_response(sent)
        assert path == "/v1/agents/connect"
        return {
            "agent": {"id": "agt_new", "user_id": "usr_new", "owner_id": "usr_owner"},
            "conversation": {"id": "conv_1"},
            "access_token": "tok_new",
            "refresh_token": "rtok_new",
        }

    monkeypatch.setattr(ClawChatApiClient, "_call_json", fake_call_json)
    return activate_mod, calls


def _mismatch_when_user_id_sent(sent: dict) -> dict:
    # Mirrors the backend: an id the code's owner does not own makes the check
    # unpairable; the same code checked without a user_id is fine.
    if "user_id" in sent:
        return {"pairable": False, "status": "pending", "user_id_status": "owner_mismatch"}
    return {"pairable": True, "status": "pending"}


@pytest.mark.asyncio
async def test_new_account_is_not_blocked_by_owner_mismatch_on_the_dropped_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activate_mod, calls = _wire_activate(monkeypatch, _mismatch_when_user_id_sent)
    payload = await activate_mod.activate(
        "K7RM4TQP", base_url="https://app.clawling.com", new_account=True
    )
    assert [p for p, _ in calls] == ["/v1/agents/connect/check", "/v1/agents/connect"]
    assert "user_id" not in calls[0][1]
    assert "user_id" not in calls[1][1]
    assert payload


@pytest.mark.asyncio
async def test_without_new_account_owner_mismatch_still_refuses_pointing_at_new_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clawchat_gateway.api_client import ClawChatApiError

    activate_mod, calls = _wire_activate(monkeypatch, _mismatch_when_user_id_sent)
    with pytest.raises(ClawChatApiError, match="--new-account"):
        await activate_mod.activate("K7RM4TQP", base_url="https://app.clawling.com")
    assert [p for p, _ in calls] == ["/v1/agents/connect/check"]
    assert calls[0][1]["user_id"] == "usr_other_owner"


@pytest.mark.asyncio
async def test_new_account_refuses_a_bound_code_and_never_calls_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clawchat_gateway.api_client import ClawChatApiError

    # Backend: no user_id sent -> pairable, bound_agent (it only flags a
    # mismatch when a user_id is sent); /connect would restore the bound agent.
    activate_mod, calls = _wire_activate(
        monkeypatch, lambda _sent: {"pairable": True, "status": "pending", "bound_agent": True}
    )
    with pytest.raises(ClawChatApiError, match="cannot create a new agent"):
        await activate_mod.activate("K7RM4TQP", base_url="https://app.clawling.com", new_account=True)
    assert [p for p, _ in calls] == ["/v1/agents/connect/check"]
    assert "user_id" not in calls[0][1]


def test_bound_code_new_identity_refusal_wording() -> None:
    from clawchat_gateway.activate import BOUND_CODE_NEW_IDENTITY_REFUSAL as msg

    assert "reconnect code bound to an existing agent" in msg
    assert "cannot create a new agent" in msg
    assert "normal connect code" in msg
    assert "http" not in msg
    assert "--" not in msg
