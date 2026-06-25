from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

logger = logging.getLogger("clawchat_gateway.media_runtime")


def infer_media_kind_from_mime(mime: str) -> str:
    normalized = mime.split(";", 1)[0].strip().lower()
    if normalized.startswith("image/"):
        return "image"
    if normalized.startswith("audio/"):
        return "audio"
    if normalized.startswith("video/"):
        return "video"
    return "file"


def ensure_allowed_local_path(path: str, allowed_roots: Sequence[str]) -> Path:
    resolved = Path(path).expanduser().resolve()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not roots:
        raise ValueError("no allowed roots configured")
    if not any(root == resolved or root in resolved.parents for root in roots):
        raise ValueError(f"path not under allowed roots: {resolved}")
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
    if _is_remote_url(url):
        return url
    resolved_base_url = derive_base_url(
        websocket_url=websocket_url, base_url=base_url, media_base_url=media_base_url
    )
    return urljoin(f"{resolved_base_url.rstrip('/')}/", url.lstrip("/"))


def _load_local_media(path: str, media_local_roots: Sequence[str]) -> LoadedMedia:
    resolved = ensure_allowed_local_path(path, media_local_roots)
    return LoadedMedia(
        buffer=resolved.read_bytes(),
        filename=resolved.name,
        mime=_guess_mime(resolved.name),
    )


def _load_remote_media(url: str) -> LoadedMedia:
    with urlopen(url) as response:
        buffer = response.read()
        content_type = response.headers.get_content_type() or _guess_mime(url)
        filename = Path(urlparse(url).path).name or "upload.bin"
    return LoadedMedia(buffer=buffer, filename=filename, mime=content_type)


def _safe_download_filename(url: str, mime: str) -> str:
    parsed_name = Path(urlparse(url).path).name
    if parsed_name:
        return parsed_name
    extension = mimetypes.guess_extension(mime.split(";", 1)[0].strip()) or ".bin"
    return f"media-{uuid.uuid4().hex}{extension}"


def _download_inbound_media_sync(
    *,
    url: str,
    token: str,
    download_dir: Path,
) -> DownloadedMedia:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(url, headers=headers)
    with urlopen(request) as response:
        buffer = response.read()
        mime = response.headers.get_content_type() or _guess_mime(url)
    download_dir.mkdir(parents=True, exist_ok=True)
    filename = _safe_download_filename(url, mime)
    local_path = download_dir / f"{uuid.uuid4().hex}-{filename}"
    local_path.write_bytes(buffer)
    return DownloadedMedia(
        local_path=local_path,
        mime=mime,
        size=len(buffer),
        source_url=url,
    )


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
) -> list[dict[str, object]]:
    if not urls:
        return []

    resolved_base_url = derive_base_url(
        websocket_url=websocket_url, base_url=base_url, media_base_url=media_base_url
    )
    uploader = upload_file or _upload_media
    fragments: list[dict[str, object]] = []
    for raw_url in urls:
        url = normalize_outbound_media_reference(raw_url)
        try:
            if _is_remote_url(url):
                loaded = await asyncio.to_thread(_load_remote_media, url)
            else:
                loaded = await asyncio.to_thread(
                    _load_local_media, url, media_local_roots
                )
            uploaded = await uploader(
                base_url=resolved_base_url,
                token=token,
                buffer=loaded.buffer,
                filename=loaded.filename,
                mime=loaded.mime,
            )
            fragments.append(
                {
                    "kind": infer_media_kind_from_mime(uploaded.mime),
                    "url": uploaded.url,
                    "mime": uploaded.mime,
                    "size": uploaded.size,
                    "name": loaded.filename,
                }
            )
        except Exception as exc:  # noqa: BLE001
            # Do not drop media silently — surface the failure so an operator
            # can see why a referenced attachment never made it onto the wire.
            if _is_uploaded_media_url(url):
                logger.warning(
                    "clawchat outbound media upload failed; reusing existing url=%s error=%s",
                    url,
                    exc,
                )
                fragments.append(_uploaded_media_fragment(url))
            else:
                logger.warning(
                    "clawchat outbound media dropped url=%s error=%s",
                    url,
                    exc,
                )
            continue
    return fragments


async def download_inbound_media(
    urls: list[str],
    *,
    base_url: str,
    websocket_url: str,
    token: str,
    download_dir: str | Path,
    download_file=None,
    media_base_url: str = "",
) -> list[DownloadedMedia]:
    if not urls:
        return []

    target_dir = Path(download_dir)
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
                token=token,
                download_dir=target_dir,
            )
            downloaded.append(item)
        except Exception:
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
