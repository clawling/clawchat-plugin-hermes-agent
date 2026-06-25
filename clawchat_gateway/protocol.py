from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

# Crockford base32 alphabet (ULID spec): excludes I, L, O, U.
_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_frame_id(prefix: str = "req") -> str:
    return f"{prefix}-{uuid.uuid4()}"


def _encode_base32(value: int, length: int) -> str:
    chars = [""] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _ULID_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def new_ulid() -> str:
    """Return a 26-char Crockford-base32 ULID (48-bit time + 80-bit randomness).

    Dependency-free implementation of the ULID spec. Lexicographically
    sortable by creation time, monotonic enough for client ids; collision
    probability is negligible (80 random bits per millisecond).
    """
    timestamp_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    randomness = int.from_bytes(os.urandom(10), "big")  # 80 bits
    return _encode_base32(timestamp_ms, 10) + _encode_base32(randomness, 16)


def new_message_id() -> str:
    """Mint a client message id: ``msg-`` + ULID.

    Required by ClawChat Protocol v2 (§3.1.9): every outbound
    ``message.send`` / ``message.reply`` MUST carry a client-minted
    ``payload.message_id`` so the server's ``UNIQUE(recipient, message_id)``
    absorbs bounded-timeout resends as a single coalesce (the client then
    dedupes by message_id). A bounded-timeout resend MUST reuse the same id —
    callers therefore mint once and reuse across resend attempts.
    """
    return f"msg-{new_ulid()}"


def current_time_ms() -> int:
    return int(time.time() * 1000)


def encode_frame(frame: dict[str, Any]) -> str:
    return json.dumps(frame, separators=(",", ":"), ensure_ascii=False)


def decode_frame(text: str) -> dict[str, Any]:
    obj = json.loads(text)
    if not isinstance(obj, dict):
        raise ValueError("frame must be object")
    return obj


def extract_nonce(frame: dict[str, Any]) -> str | None:
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return None
    if isinstance(payload.get("nonce"), str):
        return payload["nonce"]
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("nonce"), str):
        return data["nonce"]
    return None


def is_hello_ok(frame: dict[str, Any], expected_request_id: str) -> bool:
    if frame.get("event") == "hello-ok" and frame.get("trace_id") == expected_request_id:
        return True
    payload = frame.get("payload")
    if not isinstance(payload, dict):
        return False
    return (
        frame.get("type") == "res"
        and frame.get("requestId") == expected_request_id
        and payload.get("type") == "hello-ok"
    )


def build_connect_request(
    *,
    frame_id: str,
    token: str,
    nonce: str,
    device_id: str | None = None,
    capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "token": token,
        "nonce": nonce,
    }
    if device_id is not None:
        payload["device_id"] = device_id
    if capabilities is not None:
        payload["capabilities"] = capabilities
    now_ms = current_time_ms()
    return {
        "version": "2",
        "event": "connect",
        "trace_id": frame_id,
        "emitted_at": now_ms,
        "payload": payload,
    }


def _message_envelope(
    event: str,
    *,
    chat_id: str,
    chat_type: str,
    payload: dict[str, Any],
    emitted_at: int | None = None,
) -> dict[str, Any]:
    return {
        "version": "2",
        "event": event,
        "trace_id": new_frame_id("trace"),
        "emitted_at": emitted_at if emitted_at is not None else current_time_ms(),
        "chat_id": chat_id,
        "payload": payload,
    }


def build_message_reply_event(
    *,
    chat_id: str,
    chat_type: str,
    message_id: str,
    fragments: list[dict[str, Any]],
    reply_to_message_id: str | None = None,
    reply_preview: dict[str, Any] | None = None,
    include_message_id: bool = False,
) -> dict[str, Any]:
    context: dict[str, Any] = {"mentions": [], "reply": None}
    if reply_to_message_id:
        context["reply"] = {
            "reply_to_msg_id": reply_to_message_id,
            "reply_preview": reply_preview,
        }
    payload: dict[str, Any] = {
        "message_mode": "normal",
        "message": {
            "body": {"fragments": fragments},
            "context": context,
        },
    }
    if include_message_id:
        payload["message_id"] = message_id
    return _message_envelope(
        "message.reply",
        chat_id=chat_id,
        chat_type=chat_type,
        payload=payload,
    )


def build_message_send_event(
    *,
    chat_id: str,
    chat_type: str,
    message_id: str,
    fragments: list[dict[str, Any]],
    context_mentions: list[dict[str, Any]] | None = None,
    reply_to_message_id: str | None = None,
    reply_preview: dict[str, Any] | None = None,
    include_message_id: bool = False,
) -> dict[str, Any]:
    context: dict[str, Any] = {"mentions": context_mentions or [], "reply": None}
    if reply_to_message_id:
        context["reply"] = {
            "reply_to_msg_id": reply_to_message_id,
            "reply_preview": reply_preview,
        }
    payload: dict[str, Any] = {
        "message_mode": "normal",
        "message": {
            "body": {"fragments": fragments},
            "context": context,
        },
    }
    if include_message_id:
        payload["message_id"] = message_id
    return _message_envelope(
        "message.send",
        chat_id=chat_id,
        chat_type=chat_type,
        payload=payload,
    )


def build_typing_update_event(
    *,
    chat_id: str,
    chat_type: str,
    active: bool,
) -> dict[str, Any]:
    return {
        "version": "2",
        "event": "typing.update",
        "trace_id": new_frame_id("trace"),
        "emitted_at": current_time_ms(),
        "chat_id": chat_id,
        "payload": {"is_typing": active},
    }


def build_pong_event(*, trace_id: str, emitted_at: int) -> dict[str, Any]:
    return {
        "version": "2",
        "event": "pong",
        "trace_id": trace_id,
        "emitted_at": emitted_at,
        "payload": {},
    }


def build_offline_ack_event(*, batch_id: int) -> dict[str, Any]:
    return {
        "version": "2",
        "event": "offline.ack",
        "trace_id": new_frame_id("trace"),
        "emitted_at": current_time_ms(),
        "payload": {"batch_id": batch_id},
    }
