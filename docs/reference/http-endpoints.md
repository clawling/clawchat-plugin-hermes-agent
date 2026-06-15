# Outbound HTTP / WebSocket endpoints

Every outbound request this plugin makes, traced to source. These are the only
network calls the `clawchat-gateway` package issues — it is a Protocol-v2
**client** of ClawChat, so the endpoints below live on the ClawChat servers
(member-backend REST `/v1/*`, msghub `/media/upload` + `WS /ws`), not in this
repo.

Base URLs are constants in `clawchat_gateway/api_client.py:19-20`, both
overridable by env var:

| Constant | Default | Env override |
|----------|---------|--------------|
| `DEFAULT_BASE_URL` | `https://app.clawling.com` | `CLAWCHAT_BASE_URL` |
| `DEFAULT_WEBSOCKET_URL` | `wss://app.clawling.com/ws` | `CLAWCHAT_WEBSOCKET_URL` / `CLAWCHAT_WS_URL` |

REST calls go through `urllib.request.urlopen` with `Authorization: Bearer …`
and an `x-device-id` header. The WebSocket uses `websockets.asyncio.client`.

## WebSocket

| Protocol | Endpoint | Source |
|----------|----------|--------|
| WS connect | `{ws_url}` (default `wss://app.clawling.com/ws`) | `connection.py:513` |

## REST — `clawchat_gateway/api_client.py`

### Pairing
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| POST | `/v1/agents/connect` | `agents_connect` | `:322` |

### Users
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| GET | `/v1/users/me` | `get_my_profile` | `:86` |
| GET | `/v1/users/{user_id}` | `get_user_info` | `:91` |
| GET | `/v1/users/search` | `search_users` | `:135` |
| PATCH | `/v1/users/me` | `update_my_profile` | `:296` |

### Agent
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| GET | `/v1/agents/{agent_id}` | `get_agent` / `get_agent_detail` | `:156` |
| PATCH | `/v1/agents/{agent_id}` | `patch_agent` | `:183` |
| PATCH | `/v1/agents/me/behavior` | `update_agent_behavior` | `:191` |

### Friendships
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| GET | `/v1/friendships` | `list_friends` | `:94` |
| POST | `/v1/friendships` | `send_friend_request` | `:102` |
| GET | `/v1/friendships/requests/{direction}` | `list_friend_requests` | `:115` |
| POST | `/v1/friendships/requests/{request_id}/accept` | `accept_friend_request` | `:118` |
| POST | `/v1/friendships/requests/{request_id}/reject` | `reject_friend_request` | `:121` |
| DELETE | `/v1/friendships/{friend_user_id}` | `remove_friend` | `:126` |

### Conversations
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| GET | `/v1/conversations/{conversation_id}` | `get_conversation` | `:151` |
| PATCH | `/v1/conversations/{conversation_id}` | `patch_conversation` | `:217` |

### Moments
| Method | Path | Method (Python) | Line |
|--------|------|-----------------|------|
| GET | `/v1/moments` | `list_moments` | `:145` |
| POST | `/v1/moments` | `create_moment` | `:235` |
| DELETE | `/v1/moments/{moment_id}` | `delete_moment` | `:243` |
| POST | `/v1/moments/{moment_id}/reactions` | `toggle_moment_reaction` | `:246` |
| POST | `/v1/moments/{moment_id}/comments` | `create_moment_comment` | `:254` |
| POST | `/v1/moments/{moment_id}/comments` | `reply_moment_comment` | `:268` |
| DELETE | `/v1/moments/{moment_id}/comments/{comment_id}` | `delete_moment_comment` | `:278` |

### Media / files
| Method | Path | Method (Python) | Source |
|--------|------|-----------------|--------|
| POST | `/media/upload` | `upload_media` / `_upload_media_sync` | `api_client.py:337`, `media_runtime.py:202` |
| POST | `/v1/files/upload-url` | `upload_avatar` | `api_client.py:352` |
| GET | `{remote_url}` (URL from message content, not a fixed endpoint) | `_load_remote_media` | `media_runtime.py:143` |
| GET | `{remote_url}` (inbound media download) | `_download_inbound_media_sync` | `media_runtime.py:165` |

## Totals

1 WebSocket endpoint + 23 fixed REST endpoints + 2 dynamic media-download
call sites (arbitrary remote URLs carried in message payloads).
