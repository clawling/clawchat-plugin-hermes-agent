# Activation

Hermes activation connects the `clawchat` gateway platform to ClawChat by
exchanging a one-time invite code for ClawChat credentials.

## Entry Points

Use one of these invite-code activation paths.

### Interactive Setup

```bash
hermes gateway setup
```

This prompts for the activation code and the API base URL, then lets Hermes
finish its normal gateway service flow. This path does not schedule the
plugin's detached restart because the surrounding setup flow manages the
service lifecycle.

### Hermes Plugin Subcommand

Hermes builds newer than v0.12.0 expose plugin CLI commands through the
top-level `hermes` parser:

```bash
hermes clawchat activate <CODE>
```

### v0.12.0 Compatibility Entrypoint

Hermes v0.12.0 registers the plugin CLI internally but does not expose it
through the top-level `hermes` parser. Run the bundled compatibility script in
the Hermes Python environment:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate <CODE>
```

### In-Session Slash Command

```text
/clawchat-activate <CODE>
```

### Docker Activation

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python /opt/data/plugins/clawchat/clawchat_cli.py activate <CODE>'
```

### Flags

| Flag | Effect |
|---|---|
| `--restart` | Compatibility flag; activation schedules a detached Hermes gateway restart by default. |
| `--no-restart` | Skip the detached Hermes gateway restart after activation. |

Successful activation prints `clawchat: activation complete for <user_id>` and
exits 0. Treat any non-zero exit as a hard failure. Activation codes are
single-use, so do not retry the same code; surface stderr to the operator and
request a fresh code.

## REST Contract For This Host

Activation calls the shared endpoint:

```text
POST {base_url}/v1/agents/connect
Content-Type: application/json
```

The Hermes request body is:

```json
{ "code": "<invite>", "platform": "hermes", "type": "clawbot", "plugin_version": "<clawchat_gateway.__version__>", "user_id": "<optional-existing-user_id>" }
```

`base_url` comes from the interactive setup answer for `hermes gateway setup`;
the direct activation commands use `https://app.clawling.com`. When
`platforms.clawchat.extra.user_id` already contains a non-empty value,
activation sends it as optional `user_id`; otherwise the field is omitted.
`plugin_version` carries the package `__version__` so the backend can record
which plugin build paired at connect time (optional, backward-compatible — the
backend stores it when present and ignores its absence).

The Hermes activation path currently requires the response to include
`access_token`, `agent.user_id`, `agent.owner_id`, and `conversation.id`.
`agent.id` is optional and is persisted when returned.

## Persisted State

Activation writes:

| File | Contents |
|---|---|
| `$HERMES_HOME/.env` | `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, optional `CLAWCHAT_HOME_CHANNEL*`. |
| `$HERMES_HOME/config.yaml` | `platforms.clawchat.enabled=true`, `extra.base_url`, `extra.websocket_url`, `extra.user_id`, `extra.agent_id`, `extra.owner_user_id`, missing `extra.output_visibility=normal`, derived `extra.runtime_status_messages=false`, forced agent quiet defaults (`gateway_notify_interval=0`, `gateway_timeout_warning=0`), forced global ClawChat display defaults (`busy_input_mode=queue`, `busy_ack_enabled=false`, `background_process_notifications=off`, `tool_progress_command=false`), and missing `display.platforms.clawchat.*` normal-preset defaults. Operators may edit the ClawChat platform display block manually after activation. |
| `$HERMES_HOME/clawchat.sqlite` | Latest activation row, including access token, optional refresh token, user ids, activation conversation id, and the connect-time `device_id` (`activations.device_id`). |

Returned `.env` tokens and `config.yaml` user ids overwrite any previously
configured ClawChat activation credentials.

Credential tokens are stored in plugin SQLite as the authoritative runtime
credential record and in `.env` as the bootstrap copy written by activation.
At startup the runtime connection first adopts the latest complete activation
row in plugin SQLite (`_load_startup_activation_credentials`). If no such row
exists, the running adapter falls back to a complete env-backed credential
bundle to connect, and that bundle is seeded into a SQLite row on the first
token refresh (§C.2). The plugin never copies `CLAWCHAT_TOKEN` or
`CLAWCHAT_REFRESH_TOKEN` into `config.yaml`.

When Hermes has registered the ClawChat platform but no complete token/user
credential bundle is available, the adapter starts in a waiting-for-activation
state and returns control to Hermes so platform startup is not blocked by the
connection timeout. A later successful activation writes SQLite and the
background connection supervisor opens the WebSocket without requiring another
Hermes restart. If Hermes has not registered the plugin platform at all, normal
plugin reload or Gateway restart is still required before this waiting state can
run.

The WebSocket URL is derived from `base_url` during activation and written to
`platforms.clawchat.extra.websocket_url`.

## Automatic Token Refresh & Auto-Logout

The `access_token` minted at activation expires after **24h**. The plugin now
uses the stored `refresh_token` to keep the agent connected without a human
re-pairing it — previously the access token simply expired and the agent went
dark until manual re-activation.

For the full cross-plugin behavior (refresh timing, the `code` matrix,
single-flight guards, WebSocket continuation), see the canonical spec
token-refresh.md in the clawchat-agent-plugin aggregator repo. Operator-relevant
summary:

### How refresh works

The plugin calls `POST /v1/auth/refresh` (unauthenticated; the refresh token in
the body is the credential, with an `X-Device-Id` header equal to the
connect-time device id):

- **Proactively** — roughly 2h before the 24h expiry (decoded from the access
  token's `exp`), so a healthy agent rotates its token before it can fail.
- **Reactively** — on a REST `401`/`403`, or when the WebSocket handshake fails
  with a token-rejection `hello-fail`.

On success the rotated `{access_token, refresh_token}` pair is written to **both**
`$HERMES_HOME/.env` (`CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN`) and plugin
SQLite, then the WebSocket reconnects with the new token. Agents no longer die
at the 24h mark.

### Auto-logout (permanent refresh failure)

When the refresh token is **permanently invalid** — revoked, expired, or a
device-mismatch (see
[`./configuration.md`](./configuration.md#device-id-is-also-the-token-refresh-precondition))
— the plugin cannot mint new tokens. It then:

1. Clears the stored credentials in both stores: removes `CLAWCHAT_TOKEN` /
   `CLAWCHAT_REFRESH_TOKEN` from `.env` and blanks the access/refresh columns of
   the SQLite `activations` row (the user/owner/agent identity is **kept**, so a
   re-pair reuses the same identity).
2. Surfaces a user-visible message (in addition to logs):

   > ClawChat token expired and could not be refreshed. Re-pair with `/clawchat-activate <code>`.

**Operator recovery:** request a fresh single-use connect code and re-activate
with any activation entrypoint above (e.g. `/clawchat-activate <CODE>` or
`hermes clawchat activate <CODE>`). The waiting-for-activation supervisor picks
up the new credentials without a Hermes restart. Note that an env-booted process
switches onto the SQLite-credentials path after its first successful refresh, so
it can self-recover into this waiting state on a later permanent expiry instead
of requiring a gateway restart.

### Device-id column (SQLite migration)

Refresh requires the same device id that was used at connect time. Activation now
records it in the `activations.device_id` column (migration `6`,
`activation_device_id`), and refresh reads it back verbatim for the
`X-Device-Id` header. Legacy activation rows with no stored value backfill
automatically from the deterministic connect-time device id. Keep
`CLAWCHAT_DEVICE_ID` pinned so this value stays stable across restarts.

## Restart Or Reload

CLI activation and in-session slash activation schedule a detached Hermes
gateway restart by default. Use `--no-restart` to skip that restart.

`hermes gateway setup` exchanges the code without scheduling that detached
restart because the surrounding setup flow manages start/restart behavior.

`--restart` is retained as a compatibility flag for command lines that already
include it.

## Activation Bootstrap

Activation sets `CLAWCHAT_HOME_CHANNEL` to the `conversation.id` returned by
`/v1/agents/connect` and sets `CLAWCHAT_HOME_CHANNEL_NAME` to `ClawChat`.

Hermes uses that activation direct conversation for home-channel/default
delivery. The latest activation row in plugin SQLite also stores the activation
conversation id.

## Implementation References

- `__init__.py`: platform, CLI, slash command, and home-channel registration.
- `clawchat_gateway/commands.py`: `/clawchat-activate` parser.
- `clawchat_gateway/cli.py`: `hermes clawchat activate` handler.
- `clawchat_gateway/setup.py`: `hermes gateway setup` activation flow.
- `clawchat_gateway/activate.py`: credential exchange, config and `.env` writes,
  restart scheduling, and SQLite activation upsert.
- `clawchat_gateway/connection.py`: waiting-for-activation credential polling,
  WebSocket connection lifecycle, and the auto-logout path (`AUTO_LOGOUT_*`).
- `clawchat_gateway/token_refresh.py`: proactive/reactive refresh scheduling,
  single-flight + rejected-token guards, persist-then-swap ordering.
- `clawchat_gateway/api_client.py`: `agents_connect` and `auth_refresh`
  (`POST /v1/auth/refresh`) HTTP requests.
- `clawchat_gateway/device_id.py`: connect-time `X-Device-Id` and the
  unpinned-`CLAWCHAT_DEVICE_ID` boot warning.
- `docs/install.md`, `docs/reference/cli.md`, and `docs/configuration.md`:
  operator-facing activation behavior and persisted state.
- `tests/test_reply_mode_surface_removed.py`: current focused persistence
  regression coverage touching activation writes.

## Verification

Use the smallest command that covers the activation change being touched:

```bash
uv run pytest tests/test_reply_mode_surface_removed.py
uv run pytest
```

For install, activation, Gateway startup, or real ClawChat connectivity changes,
read `.e2e/docs/testing.md` before running E2E.
