# Activation

Hermes activation connects the `clawchat` gateway platform to ClawChat by
exchanging a one-time invite code for ClawChat credentials.

For the shared cross-agent activation contract, see
[`../../docs/activation.md`](../../docs/activation.md).

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

The Hermes request body is fixed:

```json
{ "code": "<invite>", "platform": "hermes", "type": "clawbot" }
```

`base_url` comes from the interactive setup answer for `hermes gateway setup`;
the direct activation commands use `https://app.clawling.com`.

The Hermes activation path currently requires the response to include
`access_token`, `agent.user_id`, `agent.owner_id`, and `conversation.id`.
`agent.id` is optional and is persisted when returned.

## Persisted State

Activation writes:

| File | Contents |
|---|---|
| `$HERMES_HOME/.env` | `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, optional `CLAWCHAT_HOME_CHANNEL*`. |
| `$HERMES_HOME/config.yaml` | `platforms.clawchat.enabled=true`, `extra.base_url`, `extra.websocket_url`, `extra.user_id`, `extra.agent_id`, `extra.owner_user_id`, missing `extra.output_visibility=normal`, derived `extra.runtime_status_messages=false`, forced agent quiet defaults (`gateway_notify_interval=0`, `gateway_timeout_warning=0`), forced global ClawChat display defaults (`busy_input_mode=queue`, `busy_ack_enabled=false`, `background_process_notifications=off`, `tool_progress_command=false`), and missing `display.platforms.clawchat.*` normal-preset defaults. Operators may edit the ClawChat platform display block manually after activation. |
| `$HERMES_HOME/clawchat.sqlite` | Latest activation row, including access token, optional refresh token, user ids, and activation conversation id. |

Credential tokens are stored in `.env` for runtime resolution and in plugin
SQLite for the latest activation record. Runtime resolution uses a complete
env-backed credential bundle first. If env-backed credentials are missing, the
running adapter waits for and then reads the latest SQLite activation row. The
plugin never copies `CLAWCHAT_TOKEN` or `CLAWCHAT_REFRESH_TOKEN` into
`config.yaml`.

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
- `clawchat_gateway/connection.py`: waiting-for-activation credential polling
  and WebSocket connection lifecycle.
- `clawchat_gateway/api_client.py`: `agents_connect` HTTP request.
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
