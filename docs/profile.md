# Profile — `clawchat_gateway/profile.py`

`profile.py` provides the local CLI for the ClawChat account/profile tool surface. It no longer owns profile-update logic directly; each subcommand delegates to the matching async handler in `clawchat_gateway.tools`.

## Configuration Loader

`load_profile_config()` remains public and is used by `tools.py`.

| Function | Purpose |
|---|---|
| `_hermes_home()` | Resolve `$HERMES_HOME` or default to `~/.hermes`. |
| `_load_env(path)` | Parse simple `KEY=value` / `export KEY=value` lines from `$HERMES_HOME/.env`; missing file returns `{}`. |
| `_load_yaml(path)` | Load `config.yaml`, raising `ProfileConfigError` when it is missing, unreadable, or not an object. |
| `load_profile_config()` | Read process env first, then `$HERMES_HOME/.env`, then `platforms.clawchat.extra`; require non-empty `token` and `user_id`, default `base_url` to `DEFAULT_BASE_URL`, and return `ProfileConfig`. |

The token normally comes from `CLAWCHAT_TOKEN` in `.env` after activation. Reading `platforms.clawchat.extra.token` remains as a backward-compatible fallback for older installs.

`ProfileConfigError` is raised only by the loader helpers. The CLI handlers catch no exceptions from tools directly because tool handlers return error dictionaries instead of raising.

## CLI

All commands use:

```bash
python -m clawchat_gateway.profile <subcommand>
```

On success, the CLI prints pretty JSON to stdout and exits 0. If the handler result contains an `"error"` key, the same JSON is printed to stderr and the process exits 1.

### `get`

Fetch the configured ClawChat account profile.

```bash
python -m clawchat_gateway.profile get
```

Backing handler: `tools.get_account_profile()`.

### `get-user <userId>`

Fetch another ClawChat user's public profile by explicit user id.

```bash
python -m clawchat_gateway.profile get-user user_123
```

Backing handler: `tools.get_user_profile(user_id)`.

### `friends`

List the configured account's friends. The current backend request is unpaginated; `--page` and `--page-size` remain accepted for existing scripts but are not sent to ClawChat.

```bash
python -m clawchat_gateway.profile friends
python -m clawchat_gateway.profile friends --page 2 --page-size 50
```

Backing handler: `tools.list_account_friends(page=..., page_size=...)`.

### `update`

Update one or more account profile fields. At least one option is required.

```bash
python -m clawchat_gateway.profile update --nickname "Hermes Bot"
python -m clawchat_gateway.profile update --avatar-url https://cdn.example/avatar.png --bio "Available on ClawChat"
```

Backing handler: `tools.update_account_profile(nickname=..., avatar_url=..., bio=...)`.

### `upload-avatar <path>`

Upload a local image file to avatar storage and return the hosted URL. This command does not set the account avatar by itself; pass the returned URL to `update --avatar-url`.

```bash
python -m clawchat_gateway.profile upload-avatar /absolute/path/to/avatar.png
```

Backing handler: `tools.upload_avatar_image(path)`.

### `upload-media <path>`

Upload a local file or media attachment and return the public URL.

```bash
python -m clawchat_gateway.profile upload-media /absolute/path/to/file.pdf
```

Backing handler: `tools.upload_media_file(path)`.

## Error Envelope

Tool and CLI errors share the same shape:

```json
{
  "error": "config|validation|auth|api|transport|unknown",
  "message": "...",
  "meta": {
    "status": 401,
    "path": "/v1/users/me",
    "code": 7
  }
}
```

`meta` is omitted when there is no status/path/code context. See [`tools.md`](./tools.md) for the handler-side implementation that produces these envelopes.
