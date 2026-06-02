from __future__ import annotations

from clawchat_gateway.api_client import ClawChatApiError
from clawchat_gateway.tools import (
    CODE_PENDING_APPROVAL,
    CODE_POLICY_FORBIDDEN,
    _api_error,
)


def test_pending_approval_maps_to_clear_non_retryable_result():
    err = ClawChatApiError(
        kind="api",
        message="pending approval",
        status=403,
        path="/v1/moments",
        code=CODE_PENDING_APPROVAL,
    )

    result = _api_error(err)

    assert result["error"] == "permission"
    assert result["retryable"] is False
    assert result["status"] == "pending"
    assert "approval" in result["message"].lower()
    assert "not failed" in result["message"].lower()
    assert "do not retry" in result["message"].lower()
    assert "wait" in result["message"].lower()
    assert result["meta"]["code"] == CODE_PENDING_APPROVAL


def test_pending_approval_includes_request_id_when_available():
    err = ClawChatApiError(
        kind="api",
        message="pending approval",
        status=403,
        path="/v1/moments",
        code=CODE_PENDING_APPROVAL,
    )
    # ClawChatApiError is a frozen dataclass; the mapping reads request_id defensively
    # from an optional payload attribute when one is carried.
    object.__setattr__(err, "data", {"request_id": "req_123", "status": "pending"})

    result = _api_error(err)

    assert result["request_id"] == "req_123"
    assert "req_123" in result["message"]


def test_policy_forbidden_maps_to_clear_non_retryable_result():
    err = ClawChatApiError(
        kind="api",
        message="policy forbidden",
        status=403,
        path="/v1/moments",
        code=CODE_POLICY_FORBIDDEN,
    )

    result = _api_error(err)

    assert result["error"] == "permission"
    assert result["retryable"] is False
    assert result["status"] == "forbidden"
    assert "policy_forbidden" in result["message"].lower()
    assert "do not retry" in result["message"].lower()
    assert result["meta"]["code"] == CODE_POLICY_FORBIDDEN


def test_other_codes_are_unchanged():
    err = ClawChatApiError(
        kind="api",
        message="boom",
        status=500,
        path="/v1/moments",
        code=50000,
    )

    result = _api_error(err)

    assert result == {
        "error": "api",
        "message": "boom",
        "meta": {"status": 500, "path": "/v1/moments", "code": 50000},
    }
    assert "retryable" not in result
