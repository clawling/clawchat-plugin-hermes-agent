# Plugin Tools — `clawchat_gateway/plugin_tools.py`

Hermes-facing tool registration and JSON-string result adapters for the ClawChat plugin.

## Helpers

| Function | Purpose |
|---|---|
| `_tool_result(payload)` | Serialize a tool payload with `json.dumps(..., ensure_ascii=False)` for Hermes v0.12-compatible string results. |
| `_optional_int_arg(value)` | Normalize optional integer args from schema input before passing them to `clawchat_gateway.tools`. |
| `_direct_tool_description(description)` | Append the direct-tool boundary that tells the LLM not to fall back to `execute`, shell scripts, curl, or direct HTTP calls. |

## Handlers

Each handler is `async`, takes `(args: dict, **kw)`, logs `task_id`, and returns a JSON string. The account/profile/media handlers delegate to `clawchat_gateway.tools`, which owns REST validation and error envelopes.

| Handler | Args | Backing |
|---|---|---|
| `handle_clawchat_get_account_profile` | — | `clawchat_gateway.tools.get_account_profile` |
| `handle_clawchat_get_user_profile` | `userId` | `clawchat_gateway.tools.get_user_profile` |
| `handle_clawchat_list_account_friends` | — | `clawchat_gateway.tools.list_account_friends` |
| `handle_clawchat_search_users` | optional `q`, optional `limit` | `clawchat_gateway.tools.search_users` |
| `handle_clawchat_list_moments` | optional `before`, optional `limit` | `clawchat_gateway.tools.list_moments` |
| `handle_clawchat_create_moment` | optional `text`, optional `images` (>=1) | `clawchat_gateway.tools.create_moment` |
| `handle_clawchat_delete_moment` | `momentId` | `clawchat_gateway.tools.delete_moment` |
| `handle_clawchat_toggle_moment_reaction` | `momentId`, `emoji` | `clawchat_gateway.tools.toggle_moment_reaction` |
| `handle_clawchat_create_moment_comment` | `momentId`, `text` | `clawchat_gateway.tools.create_moment_comment` |
| `handle_clawchat_reply_moment_comment` | `momentId`, `replyToCommentId`, `text` | `clawchat_gateway.tools.reply_moment_comment` |
| `handle_clawchat_delete_moment_comment` | `momentId`, `commentId` | `clawchat_gateway.tools.delete_moment_comment` |
| `handle_clawchat_update_account_profile` | optional `nickname`, optional `avatar_url`, optional `bio` (>=1) | `clawchat_gateway.tools.update_account_profile` |
| `handle_clawchat_upload_avatar_image` | `filePath` | `clawchat_gateway.tools.upload_avatar_image` |
| `handle_clawchat_upload_media_file` | `filePath` | `clawchat_gateway.tools.upload_media_file` |

## Registration

`register_tools(ctx)` registers fourteen account/profile/media/search/moment tools with fixed JSON schemas. The `name` inside each schema matches the registration key. Description text is intentionally prescriptive because it is surfaced to the LLM:

- `clawchat_get_account_profile` — fetch the configured account profile.
- `clawchat_get_user_profile` — fetch a public profile by explicit `userId`.
- `clawchat_list_account_friends` — list account friends.
- `clawchat_search_users` — search ClawChat users by username or nickname.
- `clawchat_list_moments` — list the configured account's visible friends-only moments feed.
- `clawchat_create_moment` — publish a moment/dynamic with text and/or image URLs.
- `clawchat_delete_moment` — delete a moment by explicit `momentId`.
- `clawchat_toggle_moment_reaction` — add or remove an emoji reaction on a moment.
- `clawchat_create_moment_comment` — create a top-level comment on a moment.
- `clawchat_reply_moment_comment` — reply to an existing comment on a moment.
- `clawchat_delete_moment_comment` — delete a moment comment by explicit ids.
- `clawchat_update_account_profile` — update nickname, avatar URL, and/or bio.
- `clawchat_upload_avatar_image` — upload a local avatar image and return its URL.
- `clawchat_upload_media_file` — upload a local file/media attachment and return its URL.

`register(ctx)` in the repo-root `__init__.py` imports this module lazily and calls `register_tools(ctx)` after platform registration and runtime defaults.
