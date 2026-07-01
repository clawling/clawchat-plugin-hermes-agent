"""Map backend owner-approval gate codes to structured tool outcomes.

Backend gate codes (confirmed live contract):
  21001 — pending owner approval (payload: {request_id, operation, expires_at})
  21003 — policy forbidden       (payload: {operation})

Returns None for any other code so callers can fall through to normal handling.
"""

from __future__ import annotations

from typing import Any


def map_gate_outcome(
    code: int | None,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    """Map a non-zero envelope code + payload to a structured gate-outcome dict.

    Args:
        code: The envelope business code from the backend response.
        data: The envelope ``data`` field from the backend response.

    Returns:
        A structured dict for code 21001 or 21003, or None for any other code.
    """
    if code == 21001:
        request_id_raw = data.get("request_id")
        operation_raw = data.get("operation")
        expires_at_raw = data.get("expires_at")
        return {
            "status": "pending_owner_approval",
            "request_id": request_id_raw if isinstance(request_id_raw, str) else "",
            "operation": operation_raw if isinstance(operation_raw, str) else "",
            "expires_at": expires_at_raw if isinstance(expires_at_raw, (int, float)) else 0,
        }
    if code == 21003:
        operation_raw = data.get("operation")
        return {
            "status": "forbidden_by_owner",
            "operation": operation_raw if isinstance(operation_raw, str) else "",
        }
    return None
