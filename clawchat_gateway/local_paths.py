"""Local file delivery policy shared by tools, hooks and reply attachments."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

LOCAL_PATH_REFUSAL = (
    "Local files can't be sent or uploaded in this conversation. "
    "This turn needs direct Files access or an authorized tool artifact."
)
ATTACHMENT_REFUSAL = "Attachments can't be sent from this conversation to other platforms."


def _public_address(ip: str) -> bool:
    """Whether an address is globally routable and unicast."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast


def _session_bound() -> bool:
    from clawchat_gateway import memory_scope

    try:
        return any(
            memory_scope._session_value(f"HERMES_SESSION_{key}")
            for key in ("PLATFORM", "CHAT_ID", "CHAT_TYPE", "USER_ID")
        )
    except memory_scope.SessionContextUnavailable:
        return True


def _gateway_in_process() -> bool:
    """A ClawChat adapter runs in this process, so this is not a local CLI."""
    from clawchat_gateway.terminal_send import get_clawchat_sender

    return get_clawchat_sender() is not None


def turn_allows_local_paths() -> bool:
    from clawchat_gateway.turn_attachments import files_direct
    return files_direct() or _legacy_local_paths()


def _legacy_local_paths() -> bool:
    """The owner's direct-chat turn, or a local operator surface (no gateway)."""
    try:
        from clawchat_gateway import host_tools_guard, memory_scope
        from clawchat_gateway import turn_attachments, turn_grants

        nonce = turn_grants.session_context_value("HERMES_SESSION_MESSAGE_ID")
        pending = turn_grants.effective_snapshot(nonce)
        if pending is not None:
            snapshot = turn_attachments.current_snapshot()
            if snapshot is None:
                # Ordinary owner delivery predates execution bindings. Keep
                # it on older hosts, without enabling any guest permission.
                return bool((pending.owner_side or pending.legacy_owner_tools)
                            and pending.chat_id == turn_grants.session_context_value("HERMES_SESSION_CHAT_ID")
                            and host_tools_guard._session_turn() == "owner")
            return bool(snapshot and (snapshot.owner_side or snapshot.legacy_owner_tools))
        if host_tools_guard._session_turn() == "owner":
            return True
        return (
            memory_scope.is_local_surface()
            and not _session_bound()
            and not _gateway_in_process()
        )
    except Exception:  # noqa: BLE001 - unknown context is not an owner turn
        return False


def local_attachment_allowed(chat_id: str | None, source: str | None = None) -> bool:
    """Whether a local attachment may be delivered into ``chat_id``.

    Reply delivery can carry an explicit, process-local execution nonce after
    Hermes clears session context. Without that binding only legacy owner
    delivery remains available.
    """
    try:
        from clawchat_gateway import turn_attachments
        if turn_attachments.attachment_allowed(source, chat_id):
            return True
        return legacy_attachment_allowed(chat_id)
    except Exception:  # noqa: BLE001 - fail closed
        return False


def legacy_attachment_allowed(chat_id: str | None) -> bool:
    """Original owner/local policy, also used for unrestricted remote fetching."""
    try:
        from clawchat_gateway import turn_attachments
        snapshot = turn_attachments.current_snapshot()
        if snapshot and not (snapshot.owner_side or snapshot.legacy_owner_tools):
            return False
        if _legacy_local_paths():
            return True
        if _session_bound():
            return False
        return bool(chat_id) and destination_is_owner_direct(str(chat_id))
    except Exception:  # noqa: BLE001 - fail closed
        return False


def remote_media_allowed(url: str, chat_id: str | None) -> bool:
    """Non-owner attachments may only be fetched from public addresses."""
    if legacy_attachment_allowed(chat_id):
        return True
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host == "localhost" or host.endswith(".localhost"):
            return False
        addresses = socket.getaddrinfo(
            host, parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
        if not addresses:
            return False
        return all(_public_address(entry[4][0]) for entry in addresses)
    except (OSError, ValueError):
        return False


def destination_is_owner_direct(chat_id: str) -> bool:
    try:
        from clawchat_gateway import host_tools_guard, memory_scope

        return host_tools_guard.is_owner_turn(
            chat_id, "", "", owner_user_id=memory_scope._owner_user_id
        )
    except Exception:  # noqa: BLE001 - fail closed
        return False


_DB_SUFFIXES = ("", "-wal", "-shm", "-journal")
_HOME_FILES = frozenset({".env", "config.yaml", "auth.json"}) | frozenset(
    "clawchat.sqlite" + suffix for suffix in _DB_SUFFIXES
)


def is_denied_hermes_path(resolved: Path) -> bool:
    """Check resolved Hermes homes and existing stores without opening a store."""
    from clawchat_gateway import storage
    from clawchat_gateway.hermes_home import hermes_home, platform_default_hermes_home

    candidates = [hermes_home(), platform_default_hermes_home()]
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        candidates.append(Path(configured))
    try:
        from hermes_constants import get_hermes_home
    except ImportError:  # standalone: the host module is optional
        pass
    else:
        candidates.append(Path(get_hermes_home()))
    homes = {Path(home).expanduser().resolve() for home in candidates}
    for home in homes:
        roots = [home]
        # Resolve named profiles too, including profiles linked outside home.
        try:
            roots.extend((home / "profiles").iterdir())
        except (FileNotFoundError, NotADirectoryError):
            pass
        try:
            relative = resolved.relative_to(home)
        except ValueError:
            pass
        else:
            if len(relative.parts) >= 3 and relative.parts[0] == "profiles":
                roots.append(home / "profiles" / relative.parts[1])
        for root in roots:
            data_dir = (root / "clawchat").resolve()
            if resolved == data_dir or data_dir in resolved.parents:
                return True
            if any(resolved == (root / name).resolve() for name in _HOME_FILES):
                return True
    with storage._store_lock:
        db_paths = [store.db_path for store in storage._stores.values()]
    return any(
        resolved == Path(str(db_path) + suffix).expanduser().resolve()
        for db_path in db_paths
        for suffix in _DB_SUFFIXES
    )
