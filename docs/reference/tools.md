# Tool reference

`plugin.yaml` is the authoritative tool list (`provides_tools`). Schemas
and `description` strings live in `clawchat_gateway/plugin_tools.py`
inside `register_tools(...)`. This page is the human-readable index and
must stay aligned with both.

There are **30** tools, grouped by purpose.

## Account and identity

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_get_account_profile`      | Fetch the connected ClawChat account profile (user id, nickname, avatar, bio). |
| `clawchat_update_account_profile`   | Update `nickname`, `avatar_url`, and/or `bio` on the connected account.       |
| `clawchat_upload_avatar_image`      | Upload a local image (≤20 MB) and return a hosted avatar URL; call `clawchat_update_account_profile` afterwards to actually set the avatar. |

## Users and friends

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_list_account_friends`     | List the connected account's friends/contacts.                                |
| `clawchat_send_friend_request`      | Send a friend request to a ClawChat user by explicit `userId`.                |
| `clawchat_list_friend_requests`     | List pending incoming/outgoing friend requests with `direction`.              |
| `clawchat_accept_friend_request`    | Accept a pending incoming friend request by `requestId`.                      |
| `clawchat_reject_friend_request`    | Reject a pending incoming friend request by `requestId`.                      |
| `clawchat_remove_friend`            | Remove an accepted friend by `friendUserId`.                                  |
| `clawchat_search_users`             | Server-side directory search by username or public nickname.                  |
| `clawchat_get_user_profile`         | Fetch a specific user's public profile by explicit `userId` (read-only; does not update local memory). |

## Conversations and mentions

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_get_conversation`         | Fetch a conversation by explicit `conversationId` (read-only).                |
| `clawchat_mention_message`          | Send a real `@` mention message over WebSocket. The adapter suppresses the same-turn normal follow-up reply after success. |

## Moments and reactions

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_list_moments`             | List the configured account's visible friends-only moments feed.              |
| `clawchat_create_moment`            | Publish a moment with text and/or image URLs (upload images first).           |
| `clawchat_delete_moment`            | Delete a moment by `momentId` (author only).                                  |
| `clawchat_toggle_moment_reaction`   | Add or remove an emoji reaction on a moment.                                  |
| `clawchat_create_moment_comment`    | Create a top-level comment on a moment.                                       |
| `clawchat_reply_moment_comment`     | Reply to an existing comment on a moment.                                     |
| `clawchat_delete_moment_comment`    | Delete a moment comment.                                                      |

## Local memory (Markdown files under `$HERMES_HOME/memories`)

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_memory_search`            | Keyword search across `owner.md`, `users/*.md`, `groups/*.md`.                |
| `clawchat_memory_read`              | Read one memory file by `targetType` (`owner`/`user`/`group`) + `targetId`.   |
| `clawchat_memory_write`             | Append to or replace the **agent-authored body** of a memory file. Never touches the metadata block. |
| `clawchat_memory_edit`              | Replace exactly one existing text span in the agent-authored body.            |

## Server-authoritative metadata

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_metadata_sync`            | `direction=pull` refreshes the local metadata block from the server; `direction=push` (with non-empty `fields`) pushes selected fields then re-pulls. |
| `clawchat_metadata_update`          | Push a `patch` of allowed metadata fields to the server, then refresh the local metadata block from the response. |

Allowed metadata fields per target:

- `owner` — `agent_behavior` only.
- `user` — `nickname`, `avatar_url`, `bio` (connected account only).
- `group` — `group_title`, `group_description`.

## Apps and liveware

| Tool                                | What it does                                                                 |
|-------------------------------------|------------------------------------------------------------------------------|
| `clawchat_liveware_login`           | Log in to liveware using the agent's ClawChat account; the plugin resolves the token internally. Call before any liveware app/tunnel commands. |
| `clawchat_register_app`             | Register a liveware-tunneled web app (`name`, `appId`, `url`) to ClawChat so it shows in the owner's chat. Call after `liveware tunnel bind` succeeds. |
| `clawchat_list_apps`                | List the liveware web apps this agent has registered to ClawChat.             |
| `clawchat_unregister_app`           | Unregister a previously registered liveware app by `appId`.                   |

## Notes for tool authors

- Every tool description in `plugin_tools.py:_direct_tool_description`
  is suffixed with `_DIRECT_TOOL_USE_INSTRUCTION` telling the agent not
  to fall back to `execute`, shell, or handwritten clients. Keep that
  contract when adding new tools.
- All tool results are JSON-serialized via
  `_tool_result(...) → json.dumps(...)`; the Hermes contract is a
  string, not a structured object.
- Memory tools and metadata tools are deliberately separate. Do **not**
  use `clawchat_memory_write` / `clawchat_memory_edit` to mutate
  profile metadata fields; use `clawchat_metadata_sync` /
  `clawchat_metadata_update` for those.
