# API Client — `clawchat_gateway/api_client.py`

Thin HTTP client for ClawChat REST endpoints, using `urllib.request` in a worker thread (`asyncio.to_thread`). No external HTTP library dependency.

## Constants

| Name | Value |
|---|---|
| `DEFAULT_BASE_URL` | `"https://app.clawling.com"` |
| `DEFAULT_WEBSOCKET_URL` | `"wss://app.clawling.com/ws"` |
| `AGENTS_CONNECT_PLATFORM` | `"hermes"` |
| `AGENTS_CONNECT_TYPE` | `"clawbot"` |
| `DEFAULT_REQUEST_TIMEOUT` | `30.0` (seconds; passed to `urlopen` for every request) |

## Exceptions

### `ClawChatApiError`

```python
@dataclass(frozen=True)
class ClawChatApiError(Exception):
    kind: str          # "validation" | "transport" | "auth" | "api"
    message: str
    status: int | None = None
    path: str | None = None
    code: int | None = None

    def __str__(self) -> str: return self.message
```

- `kind` categorises the failure: `validation` (bad input), `transport` (network / JSON decode / envelope shape), `auth` (HTTP 401/403), `api` (non-zero business `code` from server).
- `code` is the server-side response `code` field (0 means success; anything else is an error).

## Value objects

### `UploadResult`

```python
@dataclass(frozen=True)
class UploadResult:
    url: str
    size: int
    mime: str
```

## `ClawChatApiClient`

### Construction

```python
ClawChatApiClient(*, base_url: str, token: str = "", user_id: str = "", device_id: str | None = None)
```

- Validates that `base_url` starts with `http://` or `https://` (raises `ClawChatApiError("validation", ...)` otherwise).
- `device_id` defaults to `device_id.get_device_id()`.
- `base_url` is stored rstripped of trailing `/`.

### API methods

| Method | Signature | Endpoint | Notes |
|---|---|---|---|
| `get_my_profile` | `async () -> dict` | `GET /v1/users/me` | Returns `data` object from the envelope. |
| `get_user_info` | `async (user_id: str) -> dict` | `GET /v1/users/{user_id}` | Raises `validation` if `user_id` is blank. |
| `list_friends` | `async (*, page: int = 1, page_size: int = 20) -> dict` | `GET /v1/friendships` | `page` / `page_size` are accepted for existing callers, but the current Apifox endpoint is unpaginated and receives no query string. |
| `update_my_profile` | `async (*, nickname=None, avatar_url=None, bio=None) -> dict` | `PATCH /v1/users/me` | At least one field required; sends a JSON body. |
| `agents_connect` | `async (*, code: str, tools: list[str] \| None = None) -> dict` | `POST /v1/agents/connect` | Body: `{code, platform: "hermes", type: "clawbot", tools?: [...]}`. |
| `upload_media` | `async (*, buffer: bytes, filename: str, mime: str = "application/octet-stream") -> UploadResult` | `POST /media/upload` | Used by outbound message media. |
| `upload_avatar` | `async (*, buffer: bytes, filename: str, mime: str = "application/octet-stream") -> UploadResult` | `POST /v1/files/upload-url` | Avatar uploads go to a different endpoint. |

### Internal mechanics

| Method | Purpose |
|---|---|
| `async _upload(path, *, buffer, filename, mime) -> UploadResult` | Build a `multipart/form-data` body with a single `file` field; POST; validate response has `url` and `mime` strings; coerce `size` to int (default to `len(buffer)`). |
| `async _call_json(method, path, *, body=None, extra_headers=None) -> dict` | Offload `_call_json_sync` to a thread. |
| `_call_json_sync(method, path, body, extra_headers) -> dict` | Build a `Request`, call `urlopen` with `timeout=DEFAULT_REQUEST_TIMEOUT`, decode JSON, enforce envelope `{code: 0, data: <dict>}`. Raises `ClawChatApiError` on transport / non-zero code / missing `data`. For HTTP 401/403 the kind is `"auth"`. The 30s timeout exists so a stalled backend fails fast — without it the activate flow could hang the entire chat-tool turn waiting on a half-open TCP connection, blowing past the terminal-tool's 60s approval timeout in non-interactive sessions. The 30s ceiling is also large enough to tolerate typical 20 MB media uploads on a >10 Mbps link. |
| `_headers(extra_headers, body) -> dict` | Always emits `authorization: Bearer <token>` and `x-device-id`. Adds `content-length` if `body` is present. Extra headers override defaults. |

### Response envelope contract

The server is expected to return JSON of shape:

```json
{"code": 0, "msg": "...", "data": { ... }}
```

- Non-zero `code` → `ClawChatApiError(kind="api" or "auth", ..., code=code)`.
- Missing / non-dict `data` → `ClawChatApiError(kind="transport")`.
- `msg`/`message` from the response (if present) becomes the error `message`.

### Upload response contract

`POST /media/upload` and `POST /v1/files/upload-url` return a `data` object with at minimum `url` (str), `mime` (str), and optionally `size` (int). Apifox REST paths are used with the required `/v1` prefix except for the legacy `/media/upload` endpoint.
