from __future__ import annotations

import asyncio
import http.client
import json
import logging
import mimetypes
import os
import socket
import ssl
import sys
import threading
import time
import uuid
from pathlib import Path
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

from clawchat_gateway.local_paths import (
    _public_address, is_denied_hermes_path, local_attachment_allowed,
    legacy_attachment_allowed, remote_media_allowed,
)

logger = logging.getLogger("clawchat_gateway.media_runtime")

# The media CDN (Cloudflare) answers 403 to the stdlib default
# "Python-urllib/x.y" User-Agent, so every request names itself (same reason
# as liveware_cli._USER_AGENT).
_USER_AGENT = "clawchat-hermes-plugin"


def infer_media_kind_from_mime(mime: str) -> str:
    normalized = mime.split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return "file"


# Credential / system-path denylist. Outbound files may be sent from ANY
# directory (``allowed_roots`` no longer scopes delivery — parity with the
# openclaw plugin), but these locations hold secrets and must never be
# exfiltrated by a prompt-injected agent. Mirrors the Hermes host denylist in
# gateway/platforms/base.py so the send_message(media_files=…) path — which
# bypasses directory scoping in the adapter — is covered here too.
_DENIED_ABS_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/root",
)

# Denied relative to the user's HOME (e.g. ~/.ssh/id_rsa, ~/.hermes/.env).
# These carry over to Windows unchanged: Path.home() resolves to %USERPROFILE%.
_DENIED_HOME_SUBPATHS: tuple[str, ...] = (
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    ".config/gcloud",
    ".hermes/.env",
)

# Windows equivalents of the absolute prefixes above, as (env var, subpath).
# They must be resolved from the environment rather than hardcoded: the drive
# letter and profile root are not fixed. Without these the whole absolute-prefix
# layer was inert on Windows — "/etc" resolves to a drive-relative "C:\etc" that
# never exists, so nothing on that list could ever match.
#   %SystemRoot%  - the /etc + /proc + /sys analogue, and where the SAM and
#                   SYSTEM registry hives live (System32\config).
#   Credentials   - Credential Manager blobs.
#   Protect       - DPAPI master keys that decrypt the above.
_DENIED_WINDOWS_ENV_SUBPATHS: tuple[tuple[str, str], ...] = (
    ("SystemRoot", ""),
    ("WINDIR", ""),
    ("APPDATA", "Microsoft/Credentials"),
    ("LOCALAPPDATA", "Microsoft/Credentials"),
    ("APPDATA", "Microsoft/Protect"),
    ("LOCALAPPDATA", "Microsoft/Vault"),
)


def _denied_delivery_paths() -> list[Path]:
    denied = [Path(p) for p in _DENIED_ABS_PREFIXES]
    if sys.platform == "win32":
        for var, sub in _DENIED_WINDOWS_ENV_SUBPATHS:
            root = os.environ.get(var)
            if not root:
                continue
            denied.append(Path(root) / sub if sub else Path(root))
    try:
        home = Path.home()
    except (RuntimeError, OSError):
        home = None
    if home is not None:
        denied.extend(home / sub for sub in _DENIED_HOME_SUBPATHS)
    return denied


def ensure_allowed_local_path(path: str, allowed_roots: Sequence[str] = ()) -> Path:
    """Resolve ``path`` for outbound delivery, allowing any directory.

    Files may be sent from any directory; ``allowed_roots`` is accepted for
    backward compatibility but no longer restricts delivery. The credential /
    system-path and Hermes data denylist is enforced. Symlinks are resolved
    before the check so the denylist cannot be bypassed via a symlink.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        for denied in _denied_delivery_paths():
            try:
                denied_resolved = denied.expanduser().resolve()
            except (RuntimeError, OSError):
                denied_resolved = denied
            if resolved == denied_resolved or denied_resolved in resolved.parents:
                raise ValueError("path is denied for delivery")
        if is_denied_hermes_path(resolved):
            raise ValueError("path is denied for delivery")
    except Exception:  # noqa: BLE001 - resolution failures must not expose local details
        raise ValueError("path is denied for delivery") from None
    return resolved


@dataclass(frozen=True)
class UploadMediaResult:
    url: str
    mime: str
    size: int


@dataclass(frozen=True)
class LoadedMedia:
    buffer: bytes
    filename: str
    mime: str


@dataclass(frozen=True)
class DownloadedMedia:
    local_path: Path
    mime: str
    size: int
    source_url: str


def derive_base_url(*, websocket_url: str, base_url: str, media_base_url: str = "") -> str:
    if media_base_url.strip():
        return media_base_url.rstrip("/")
    parsed = urlparse(websocket_url)
    if parsed.scheme in {"ws", "wss"} and parsed.netloc:
        scheme = "https" if parsed.scheme == "wss" else "http"
        return urlunparse((scheme, parsed.netloc, "", "", "", "")).rstrip("/")

    if base_url.strip():
        return base_url.rstrip("/")

    if not parsed.scheme or not parsed.netloc:
        raise ValueError("base_url missing and websocket_url is not absolute")
    return base_url.rstrip("/")


def _guess_mime(filename: str, default: str = "application/octet-stream") -> str:
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or default


def _is_remote_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"}


def normalize_outbound_media_reference(value: str) -> str:
    normalized = value.strip()
    if not normalized.startswith("file://"):
        return normalized

    decoded = unquote(normalized[7:])
    if decoded.startswith(("http://", "https://")):
        return decoded
    for scheme in ("http", "https"):
        prefix = f"{scheme}:/"
        if decoded.startswith(prefix) and not decoded.startswith(f"{scheme}://"):
            return f"{scheme}://{decoded[len(prefix):].lstrip('/')}"
    return decoded


def media_reference_allowed(value: str, chat_id: str | None) -> bool:
    """Check the turn's policy for a local or remote reference."""
    normalized = normalize_outbound_media_reference(str(value or ""))
    if _is_remote_url(normalized):
        return remote_media_allowed(normalized, chat_id)
    return local_attachment_allowed(chat_id, normalized)


def _is_uploaded_media_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.path.startswith("/media/")


def _uploaded_media_fragment(url: str) -> dict[str, object]:
    name = Path(urlparse(url).path).name or "media"
    mime = _guess_mime(name)
    return {
        "kind": infer_media_kind_from_mime(mime),
        "url": url,
        "mime": mime,
        "size": 0,
        "name": name,
    }


def _resolve_inbound_media_url(
    url: str,
    *,
    base_url: str,
    websocket_url: str,
    media_base_url: str = "",
) -> str:
    if urlparse(url).scheme:
        if not _is_remote_url(url):
            raise InboundMediaRejected("scheme")
        return url
    resolved_base_url = derive_base_url(
        websocket_url=websocket_url, base_url=base_url, media_base_url=media_base_url
    )
    return urljoin(f"{resolved_base_url.rstrip('/')}/", url.lstrip("/"))


def _load_local_media(path: str, media_local_roots: Sequence[str], chat_id: str | None = None,
                      checked: bool = False) -> LoadedMedia:
    resolved = ensure_allowed_local_path(path, media_local_roots)
    from clawchat_gateway import turn_attachments
    snapshot = turn_attachments.current_snapshot(chat_id)
    checked = checked or bool(snapshot and not (snapshot.owner_side or snapshot.legacy_owner_tools))
    return LoadedMedia(
        buffer=(turn_attachments.read_local_attachment(path, chat_id) if checked else resolved.read_bytes()),
        filename=resolved.name,
        mime=_guess_mime(resolved.name),
    )


class RemoteMediaRejected(ValueError):
    """A remote attachment or redirect did not satisfy the turn's policy."""


def _load_remote_media(url: str) -> LoadedMedia:
    with urlopen(Request(url, headers={"User-Agent": _USER_AGENT})) as response:
        buffer = response.read()
        content_type = response.headers.get_content_type() or _guess_mime(url)
        filename = Path(urlparse(url).path).name or "upload.bin"
    return LoadedMedia(buffer=buffer, filename=filename, mime=content_type)


class _PublicHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._public_ip = ip

    def connect(self):
        self.sock = socket.create_connection((self._public_ip, self.port), self.timeout)


class _PublicHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._public_ip = ip

    def connect(self):
        sock = socket.create_connection((self._public_ip, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def _fetch_public_media(url: str, *, max_redirects: int = 5, timeout: float = 30) -> LoadedMedia:
    """Fetch directly from a checked address, checking each redirect afresh."""
    for hop in range(max_redirects + 1):
        try:
            parsed = urlparse(url)
            host = parsed.hostname or ""
            if (
                parsed.scheme not in {"http", "https"} or not host
                or host.lower().rstrip(".") == "localhost"
                or host.lower().rstrip(".").endswith(".localhost")
            ):
                raise RemoteMediaRejected("remote attachment address is not allowed")
            default_port = 443 if parsed.scheme == "https" else 80
            port = parsed.port or default_port
            addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            if not addresses or not all(_public_address(entry[4][0]) for entry in addresses):
                raise RemoteMediaRejected("remote attachment address is not allowed")
        except (OSError, ValueError):
            raise RemoteMediaRejected("remote attachment address is not allowed") from None
        connection_type = _PublicHTTPSConnection if parsed.scheme == "https" else _PublicHTTPConnection
        connection = connection_type(host, port, ip=addresses[0][4][0], timeout=timeout)
        host_header = f"[{host}]" if ":" in host else host
        if port != default_port:
            host_header = f"{host_header}:{port}"
        target = parsed.path or "/"
        if parsed.params:
            target += f";{parsed.params}"
        if parsed.query:
            target += f"?{parsed.query}"
        try:
            connection.request("GET", target, headers={"Host": host_header, "User-Agent": _USER_AGENT})
            with connection.getresponse() as response:
                if response.status in {301, 302, 303, 307, 308}:
                    if hop == max_redirects:
                        raise RemoteMediaRejected("remote attachment redirect limit reached")
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("remote attachment redirect missing location")
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise OSError("remote attachment request failed")
                return LoadedMedia(
                    buffer=response.read(),
                    filename=Path(parsed.path).name or "upload.bin",
                    mime=response.headers.get_content_type() or _guess_mime(url),
                )
        finally:
            connection.close()


def _media_log_name(url: str) -> str:
    # Only a basename; remote queries and fragments can contain private values.
    value = urlparse(url).path if _is_remote_url(url) else url
    return Path(unquote(value).replace("\\", "/")).name


def _safe_download_filename(url: str, mime: str) -> str:
    parsed_name = Path(urlparse(url).path).name
    if parsed_name:
        return parsed_name
    extension = mimetypes.guess_extension(mime.split(";", 1)[0].strip()) or ".bin"
    return f"media-{uuid.uuid4().hex}{extension}"


class InboundMediaRejected(ValueError):
    """An inbound attachment violated the download policy."""


_INBOUND_REASONS = {
    "scheme": "URL scheme is not allowed",
    "address": "destination address is not allowed",
    "dns": "destination resolution failed",
    "redirect": "redirect missing location or limit reached",
    "size": "attachment exceeds size limit",
    "deadline": "attachment deadline exceeded",
}


class _InboundMediaHTTPError(OSError):
    def __init__(self, status: int):
        self.status = status


def _inbound_log_url(url: str) -> str:
    # A non-HTTP(S) value (e.g. a data: URL) may be its own payload: log only
    # its scheme. Queries and fragments may carry signatures.
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme
        if scheme in {"http", "https"}:
            host = parsed.hostname
            port = parsed.port
            if not host:
                return "<invalid>"
            authority = f"[{host}]" if ":" in host else host
            if port is not None:
                authority += f":{port}"
            return urlunparse((scheme, authority, parsed.path, "", "", ""))[:512]
    except ValueError:
        return "<invalid>"
    if scheme and scheme not in {"http", "https"}:
        return f"{scheme[:16]}:<redacted>"
    return url.split("?", 1)[0].split("#", 1)[0][:512]


def _resolve_host(host: str, port: int):
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


_INBOUND_DNS_SLOTS = threading.BoundedSemaphore(4)


def _resolve_inbound_host(host: str, port: int, remaining: float):
    # The system resolver cannot be interrupted; its result must not outlive
    # the attachment deadline or start a late connection. Bound pending lookups
    # even when the resolver remains blocked after callers have timed out.
    slots = _INBOUND_DNS_SLOTS
    if not slots.acquire(blocking=False):
        raise InboundMediaRejected("dns")
    done = threading.Event()
    result = []

    def resolve():
        try:
            result.append(_resolve_host(host, port))
        except Exception:
            result.append(None)
        finally:
            slots.release()
            done.set()

    try:
        threading.Thread(target=resolve, daemon=True).start()
    except Exception:
        slots.release()
        raise InboundMediaRejected("dns") from None
    if not done.wait(remaining):
        raise InboundMediaRejected("deadline")
    if not result[0]:
        raise InboundMediaRejected("dns")
    return result[0]


class _InboundHTTPConnection(_PublicHTTPConnection):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float, remaining):
        super().__init__(host, port, ip=ip, timeout=timeout)
        self._remaining = remaining

    def connect(self):
        self.sock = socket.create_connection(
            (self._public_ip, self.port), min(self.timeout, self._remaining())
        )
        self._remaining()


class _InboundHTTPSConnection(_PublicHTTPSConnection):
    def __init__(self, host: str, port: int, *, ip: str, timeout: float, remaining):
        super().__init__(host, port, ip=ip, timeout=timeout)
        self._remaining = remaining

    def connect(self):
        sock = socket.create_connection(
            (self._public_ip, self.port), min(self.timeout, self._remaining())
        )
        try:
            sock.settimeout(min(self.timeout, self._remaining()))
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
            self._remaining()
        except Exception:
            sock.close()
            raise


def _connect_inbound_media(host, port, ips, *, https, timeout, remaining):
    connection_type = _InboundHTTPSConnection if https else _InboundHTTPConnection
    last_error = None
    for ip in dict.fromkeys(ips):
        remaining()
        connection = connection_type(host, port, ip=ip, timeout=timeout, remaining=remaining)
        try:
            connection.connect()
            remaining()
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
        except Exception:
            connection.close()
            raise
    if last_error is not None:
        raise last_error
    raise InboundMediaRejected("dns")


def _download_inbound_media_sync(
    *,
    url: str,
    download_dir: Path,
    allow_http: bool = False,
    allow_private_network: bool = False,
    max_bytes: int = 104857600,
    timeout_seconds: float = 30,
    deadline_seconds: float = 300,
) -> DownloadedMedia:
    source_url = url
    deadline = time.monotonic() + deadline_seconds

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise InboundMediaRejected("deadline")
        return seconds

    for hop in range(6):
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
                raise InboundMediaRejected("scheme")
            host = parsed.hostname or ""
            if not host or parsed.username is not None or parsed.password is not None:
                raise InboundMediaRejected("address")
            default_port = 443 if parsed.scheme == "https" else 80
            port = parsed.port or default_port
        except ValueError as exc:
            if isinstance(exc, InboundMediaRejected):
                raise
            raise InboundMediaRejected("address") from None
        if not allow_private_network and (
            host.lower().rstrip(".") == "localhost"
            or host.lower().rstrip(".").endswith(".localhost")
        ):
            raise InboundMediaRejected("address")
        addresses = _resolve_inbound_host(host, port, remaining())
        if not allow_private_network and not all(_public_address(a[4][0]) for a in addresses):
            raise InboundMediaRejected("address")
        # Bypass environment proxies so the checked address is the connected address.
        connection = _connect_inbound_media(
            host, port, (a[4][0] for a in addresses), https=parsed.scheme == "https",
            timeout=timeout_seconds, remaining=remaining,
        )
        partial_path = None
        watchdog = None
        try:
            sock = connection.sock

            def expire():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            # A socket timeout alone cannot bound a peer that trickles headers
            # or body bytes. Interrupt the socket at the attachment deadline.
            watchdog = threading.Timer(remaining(), expire)
            watchdog.daemon = True
            watchdog.start()
            sock.settimeout(min(timeout_seconds, remaining()))
            host_header = f"[{host}]" if ":" in host else host
            if port != default_port:
                host_header += f":{port}"
            target = urlunparse(("", "", parsed.path or "/", parsed.params, parsed.query, ""))
            connection.request("GET", target, headers={"Host": host_header, "User-Agent": _USER_AGENT})
            with connection.getresponse() as response:
                remaining()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if hop == 5 or not location:
                        raise InboundMediaRejected("redirect")
                    # Normalize alternate separators before interpreting the
                    # authority so every redirect destination is checked.
                    location = location.replace("\\", "/")
                    if urlparse(location).scheme and not _is_remote_url(location):
                        raise InboundMediaRejected("scheme")
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise _InboundMediaHTTPError(response.status)
                length = response.headers.get("Content-Length")
                if length is not None and int(length) > max_bytes:
                    raise InboundMediaRejected("size")
                mime = response.headers.get_content_type() or _guess_mime(url)
                download_dir.mkdir(parents=True, exist_ok=True)
                filename = _safe_download_filename(url, mime)
                local_path = download_dir / f"{uuid.uuid4().hex}-{filename}"
                partial_path = local_path.with_name(local_path.name + ".part")
                size = 0
                with partial_path.open("xb") as output:
                    while not response.isclosed():
                        sock.settimeout(min(timeout_seconds, remaining()))
                        chunk = response.read1(64 * 1024)
                        remaining()
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > max_bytes:
                            raise InboundMediaRejected("size")
                        output.write(chunk)
                if length is not None and size != int(length):
                    raise OSError("incomplete attachment")
                remaining()
                partial_path.replace(local_path)
                return DownloadedMedia(local_path, mime, size, source_url)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            try:
                connection.close()
            finally:
                if partial_path is not None:
                    partial_path.unlink(missing_ok=True)


def _encode_multipart(*, buffer: bytes, filename: str, mime: str) -> tuple[bytes, str]:
    boundary = f"----clawchat-{uuid.uuid4().hex}"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    footer = f"\r\n--{boundary}--\r\n".encode("utf-8")
    body = header + buffer + footer
    return body, boundary


def _upload_media_sync(
    *,
    base_url: str,
    token: str,
    buffer: bytes,
    filename: str,
    mime: str,
) -> UploadMediaResult:
    body, boundary = _encode_multipart(buffer=buffer, filename=filename, mime=mime)
    request = Request(
        f"{base_url.rstrip('/')}/media/upload",
        method="POST",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": _USER_AGENT,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    with urlopen(request) as response:
        payload = json.loads(response.read().decode("utf-8"))

    # §15.3: branch on the business `code`, not the HTTP status. `code != 0`
    # is a business error (e.g. 41501 mime not allowed, 41301 too large) and
    # `data` is absent.
    if isinstance(payload, dict) and payload.get("code") not in (0, None):
        msg = payload.get("msg")
        raise ValueError(
            f"media upload rejected code={payload.get('code')} msg={msg!r}"
        )
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ValueError(f"unexpected upload response: {payload!r}")
    url = data.get("url")
    uploaded_mime = data.get("mime")
    size = data.get("size")
    if not isinstance(url, str) or not isinstance(uploaded_mime, str):
        raise ValueError(f"upload response missing url/mime: {payload!r}")
    return UploadMediaResult(url=url, mime=uploaded_mime, size=int(size or len(buffer)))


async def upload_outbound_media(
    urls: list[str],
    *,
    base_url: str,
    websocket_url: str,
    token: str,
    media_local_roots: Sequence[str],
    upload_file=None,
    media_base_url: str = "",
    force_document: bool = False,
    chat_id: str | None = None,
    policy_dropped: list[str] | None = None,
) -> list[dict[str, object]]:
    """Upload each reference; one fragment per successful upload.

    ``force_document`` (Hermes' ``[[as_document]]``) sends every file as a
    ``file`` fragment, so an image arrives as a download, not inline.
    ``policy_dropped`` records references rejected by policy, including redirects,
    so callers can distinguish them from upload failures.
    """
    if not urls:
        return []

    resolved_base_url = derive_base_url(
        websocket_url=websocket_url, base_url=base_url, media_base_url=media_base_url
    )
    uploader = upload_file or _upload_media
    fragments: list[dict[str, object]] = []
    for raw_url in urls:
        url = normalize_outbound_media_reference(raw_url)
        if not _is_remote_url(url) and not local_attachment_allowed(chat_id, url):
            if policy_dropped is not None:
                policy_dropped.append(raw_url)
            logger.warning("clawchat outbound local media dropped name=%s", _media_log_name(url))
            continue
        try:
            if _is_remote_url(url):
                if legacy_attachment_allowed(chat_id):
                    loaded = await asyncio.to_thread(_load_remote_media, url)
                else:
                    loaded = await asyncio.to_thread(_fetch_public_media, url)
            else:
                # Losing a guest snapshot between the policy check and loading
                # must never remove the requirement for a live turn permit.
                checked = not legacy_attachment_allowed(chat_id)
                loaded = await asyncio.to_thread(
                    _load_local_media, url, media_local_roots, chat_id, checked
                )
                if checked and not local_attachment_allowed(chat_id, url):
                    raise ValueError("attachment turn ended")
            uploaded = await uploader(
                base_url=resolved_base_url,
                token=token,
                buffer=loaded.buffer,
                filename=loaded.filename,
                mime=loaded.mime,
            )
            fragments.append(
                {
                    "kind": (
                        "file" if force_document else infer_media_kind_from_mime(uploaded.mime)
                    ),
                    "url": uploaded.url,
                    "mime": uploaded.mime,
                    "size": uploaded.size,
                    "name": loaded.filename,
                }
            )
        except RemoteMediaRejected:
            if policy_dropped is not None:
                policy_dropped.append(raw_url)
            logger.warning("clawchat outbound remote media dropped name=%s", _media_log_name(url))
            continue
        except Exception as exc:  # noqa: BLE001
            # Do not drop media silently — surface the failure so an operator
            # can see why a referenced attachment never made it onto the wire.
            if _is_uploaded_media_url(url):
                logger.warning(
                    "clawchat outbound media upload failed; reusing existing url=%s error=%s",
                    urlunparse(urlparse(url)._replace(query="", fragment="")),
                    type(exc).__name__,
                )
                fragments.append(_uploaded_media_fragment(url))
            else:
                logger.warning(
                    "clawchat outbound media dropped name=%s error=%s",
                    _media_log_name(url),
                    type(exc).__name__,
                )
            continue
    return fragments


async def download_inbound_media(
    urls: list[str],
    *,
    base_url: str,
    websocket_url: str,
    download_dir: str | Path,
    download_file=None,
    media_base_url: str = "",
    allow_http: bool = False,
    allow_private_network: bool = False,
    max_bytes: int = 104857600,
    timeout_seconds: float = 30,
    deadline_seconds: float = 300,
) -> list[DownloadedMedia]:
    if not urls:
        return []

    downloader = download_file or _download_inbound_media_sync
    downloaded: list[DownloadedMedia] = []
    for url in urls:
        try:
            resolved_url = _resolve_inbound_media_url(
                url,
                base_url=base_url,
                websocket_url=websocket_url,
                media_base_url=media_base_url,
            )
            item = await asyncio.to_thread(
                downloader,
                url=resolved_url,
                download_dir=Path(download_dir),
                allow_http=allow_http,
                allow_private_network=allow_private_network,
                max_bytes=max_bytes,
                timeout_seconds=timeout_seconds,
                deadline_seconds=deadline_seconds,
            )
            downloaded.append(item)
        except Exception as exc:  # noqa: BLE001
            # Exception messages may also contain signed URLs; log only known
            # policy reasons, HTTP status codes or the failure's type.
            rejected = isinstance(exc, InboundMediaRejected)
            if rejected:
                reason = _INBOUND_REASONS.get(str(exc), "download policy violation")
            elif isinstance(exc, _InboundMediaHTTPError):
                reason = f"HTTP status {exc.status}"
            else:
                reason = type(exc).__name__
            logger.warning(
                "clawchat inbound media %s url=%s reason=%s",
                "rejected" if rejected else "download failed",
                _inbound_log_url(str(url)),
                reason,
            )
            continue
    return downloaded


async def _upload_media(
    *,
    base_url: str,
    token: str,
    buffer: bytes,
    filename: str,
    mime: str,
) -> UploadMediaResult:
    return await asyncio.to_thread(
        _upload_media_sync,
        base_url=base_url,
        token=token,
        buffer=buffer,
        filename=filename,
        mime=mime,
    )
