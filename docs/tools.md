# Tools — `clawchat_gateway/tools.py`

Single source of truth for the account/profile/media/search/moment REST handlers. Both the Hermes-facing wrappers in `clawchat_gateway/plugin_tools.py` and the `profile.py` CLI subcommands call into this module. Handlers return result dicts and **never raise** — every failure mode is mapped to an `{"error": "...", "message": "..."}` envelope.

## Constants

| Name | Value |
|---|---|
| `MAX_UPLOAD_BYTES` | `20 * 1024 * 1024` (20 MB) — enforced by `_validate_upload_path`. |

## Error helpers

| Helper | Returns |
|---|---|
| `_config_error(message)` | `{"error": "config", "message": ...}` |
| `_validation_error(message)` | `{"error": "validation", "message": ...}` |
| `_api_error(err: ClawChatApiError)` | `{"error": err.kind, "message": err.message, "meta"?: {status, path, code}}`. `meta` is omitted when no status/path/code is set. |
| `_unknown_error(exc)` | `{"error": "unknown", "message": str(exc)}` |

`err.kind` is one of `validation`, `transport`, `auth`, `api` (set by `ClawChatApiClient`). `_api_error` preserves it verbatim so callers can branch on the same vocabulary.

## Construction helpers

| Helper | Purpose |
|---|---|
| `_build_client() -> tuple[ClawChatApiClient \| None, dict \| None]` | Loads `ProfileConfig` via `profile.load_profile_config()`; returns `(client, None)` on success or `(None, _config_error(...))` if config is missing. Every handler calls this before issuing API requests. |
| `_validate_upload_path(file_path) -> tuple[Path \| None, dict \| None]` | Requires non-empty string, absolute path, existing regular file, non-empty (>0 bytes), and within `MAX_UPLOAD_BYTES`. Returns `(path, None)` or `(None, validation_error)`. |
| `_infer_mime(path)` | `mimetypes.guess_type` with `application/octet-stream` fallback. |

## Handlers

All handlers are `async`, return `dict[str, Any]`, and never raise. Each catches `ClawChatApiError` (via `_api_error`) and any other `Exception` (via `_unknown_error`).

| Handler | Args | Backing API call | Validation |
|---|---|---|---|
| `get_account_profile()` | — | `client.get_my_profile()` (`GET /v1/users/me`) | — |
| `get_user_profile(user_id)` | `user_id: str` | `client.get_user_info(user_id.strip())` (`GET /v1/users/{id}`) | `userId` required and non-blank. |
| `list_account_friends(page=None, page_size=None)` | optional ints | `client.list_friends(page, page_size)` (`GET /v1/friendships`) | `page >= 1` (default 1); `1 <= page_size <= 100` (default 20). The backend request is unpaginated; these args are retained for existing tool/CLI callers. |
| `search_users(q=None, limit=None)` | optional `q`, optional int | `client.search_users(q=q or "", limit=limit)` (`GET /v1/users/search`) | `1 <= limit <= 100` when provided. |
| `list_moments(before=None, limit=None)` | optional ints | `client.list_moments(before=before, limit=limit)` (`GET /v1/moments`) | `before >= 1` when provided; `1 <= limit <= 100` when provided. |
| `create_moment(text=None, images=None)` | optional text, optional image URL list | `client.create_moment(text=text, images=images)` (`POST /v1/moments`) | `text` must be a string when provided; `images` must be a list of strings when provided; at least one of `text` / `images` is required. |
| `delete_moment(moment_id)` | `moment_id: int` | `client.delete_moment(moment_id)` (`DELETE /v1/moments/{id}`) | `momentId >= 1`. |
| `toggle_moment_reaction(moment_id, emoji)` | `moment_id: int`, `emoji: str` | `client.toggle_moment_reaction(...)` (`POST /v1/moments/{id}/reactions`) | `momentId >= 1`; `emoji` required and non-blank. |
| `create_moment_comment(moment_id, text)` | `moment_id: int`, `text: str` | `client.create_moment_comment(...)` (`POST /v1/moments/{id}/comments`) | `momentId >= 1`; `text` required and non-blank. |
| `reply_moment_comment(moment_id, reply_to_comment_id, text)` | ints plus text | `client.reply_moment_comment(...)` (`POST /v1/moments/{id}/comments`) | `momentId >= 1`; `replyToCommentId >= 1`; `text` required and non-blank. |
| `delete_moment_comment(moment_id, comment_id)` | ints | `client.delete_moment_comment(...)` (`DELETE /v1/moments/{id}/comments/{comment_id}`) | `momentId >= 1`; `commentId >= 1`. |
| `update_account_profile(nickname=None, avatar_url=None, bio=None)` | optional strings | `client.update_my_profile(**patch)` (`PATCH /v1/users/me`) | At least one of the three must be a `str`. Non-string values are dropped silently. |
| `upload_avatar_image(file_path)` | `file_path: str` | `client.upload_avatar(...)` (`POST /v1/files/upload-url`) | `_validate_upload_path` (absolute, exists, non-empty, ≤20 MB). Returns `{url, size, mime}`. |
| `upload_media_file(file_path)` | `file_path: str` | `client.upload_media(...)` (`POST /media/upload`) | Same as `upload_avatar_image`. Returns `{url, size, mime}`. |

## Error envelope contract

Both Hermes tool results (re-serialized via `_tool_result` in `clawchat_gateway/plugin_tools.py`) and `profile` CLI output (via `profile.main`) propagate this envelope unchanged:

```json
{
  "error": "config | validation | auth | api | transport | unknown",
  "message": "<human readable>",
  "meta": { "status": 401, "path": "/v1/users/me", "code": 7 }
}
```

The `meta` block is only present for `_api_error` results that carry one of `status` / `path` / `code`.

## Adding a new handler

1. Add the `async def` here, following the `(_build_client → call → catch ClawChatApiError → catch Exception)` pattern.
2. Surface it from `clawchat_gateway/plugin_tools.py::register_tools` with a JSON schema and an emoji; wire a thin `handle_clawchat_<name>` dispatcher that wraps the result with `_tool_result(...)`.
3. Surface it from `profile.py::main` as a subparser that calls the handler with `asyncio.run` and prints the same JSON.
4. Add the tool name to `plugin.yaml::provides_tools`.
5. Cover with `tests/test_tools.py` (handler-level) and `tests/test_plugin.py` (registration / schema).
