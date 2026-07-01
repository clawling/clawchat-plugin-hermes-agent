"""Synthetic reasoning-turn builder for ``permission_result`` system-message receipts.

When the backend delivers a permission-request outcome as a ``sender.id="system"``
``message.send`` frame, the adapter intercepts it before ``parse_inbound_message``
drops the system message and routes it here.

The function builds a single deduped synthetic ``InboundMessage`` per unique
``request_id`` so the agent can reason about the approved/denied/expired outcome.

Wire shape (``payload.metadata`` is the authoritative discriminator):

    {
        "payload": {
            "metadata": {
                "kind": "permission_result",
                "operation": "friend.add",
                "outcome": "approved",
                "reason": "owner_allowed",
                "request_id": "prq_..."
            }
        }
    }

``outcome`` is one of ``approved``, ``denied``, ``expired``, ``failed``.

Mirrors the OpenClaw plugin's permission-result consumer so both adapters
produce equivalent reasoning turns for the same outcome.
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

from clawchat_gateway.inbound import InboundMessage

# Process-level dedup: each request_id is processed at most once per agent
# lifetime. A set is sufficient — request_ids are unique per permission request.
_seen_request_ids: set[str] = set()
_seen_lock = Lock()


def _extract_metadata(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``payload.metadata`` dict from a frame, or None if absent."""
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return None
    return metadata


def handle_permission_result(frame: dict[str, Any]) -> InboundMessage | None:
    """Build a synthetic InboundMessage for a ``permission_result`` system-message receipt.

    Discriminates on ``payload.metadata.kind == "permission_result"``.  Returns
    ``None`` on a duplicate ``request_id`` (already processed this process
    lifetime) so replayed or retried receipts collapse into one agent turn.

    The returned ``InboundMessage`` carries ``raw_message={"synthetic": True, ...}``
    matching the synthetic path used by ``build_friend_request_inbound``.
    """
    metadata = _extract_metadata(frame)
    if metadata is None:
        return None
    if metadata.get("kind") != "permission_result":
        return None

    request_id = str(metadata.get("request_id") or "")
    if not request_id:
        return None

    with _seen_lock:
        if request_id in _seen_request_ids:
            return None
        _seen_request_ids.add(request_id)

    operation = str(metadata.get("operation") or "")
    outcome = str(metadata.get("outcome") or "")
    reason = str(metadata.get("reason") or "")

    chat_id = str(frame.get("chat_id") or "")
    chat_type = str(frame.get("chat_type") or "direct")
    if chat_type not in {"direct", "group"}:
        chat_type = "direct"

    text = _build_result_text(operation=operation, outcome=outcome, reason=reason)
    now_ms = int(time.time() * 1000)
    return InboundMessage(
        chat_id=chat_id,
        chat_type=chat_type,
        sender_id="clawchat-permission-result",
        sender_name="ClawChat",
        text=text,
        raw_message={
            "synthetic": True,
            "permission_result": True,
            "request_id": request_id,
            "operation": operation,
            "outcome": outcome,
            "reason": reason,
            "trace_id": f"clawchat-hermes-permission-result-{request_id}-{now_ms}",
        },
    )


def _build_result_text(*, operation: str, outcome: str, reason: str) -> str:
    """Build the agent-facing text for a permission-request result.

    Produces wording semantically equivalent to the OpenClaw plugin's
    ``buildOutcomeNote``: one sentence naming the operation and outcome, a
    reason clause, and a resolution sentence (approved → action completed;
    non-approved → no further action taken).
    """
    outcome_label = {
        "approved": "approved",
        "denied": "denied",
        "expired": "expired",
        "failed": "failed",
    }.get(outcome, outcome or "unknown")

    resolution = (
        "The requested action has been completed."
        if outcome == "approved"
        else "No further action was taken."
    )
    parts = [
        f'Permission request for operation "{operation}" has been {outcome_label}.',
    ]
    if reason:
        parts.append(f"Reason: {reason}.")
    parts.append(resolution)
    return " ".join(parts)
