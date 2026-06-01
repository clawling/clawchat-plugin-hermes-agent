# Install and activate the ClawChat Hermes plugin

This is the human-facing guide. For the deterministic single-shot
runbook that LLM installers (e.g. `openclaw-clawchat-cli`) follow,
see [`../install.md`](../install.md) at the repo root.

## Compatibility

| Component       | Requirement                                  |
|-----------------|----------------------------------------------|
| Hermes Agent    | `v0.12.0` or newer (uses `ctx.register_platform`) |
| Python runtime  | `>=3.11` (per `pyproject.toml`)              |
| Dependencies    | `websockets>=12,<16`, `PyYAML>=6,<7`         |

The plugin advertises itself as `clawchat` (`plugin.yaml: kind: platform`)
and is loaded directly into `$HERMES_HOME/plugins/clawchat/`.
`HERMES_HOME` defaults to `~/.hermes`.

## Install paths

There are three supported install entrypoints. Pick one — they all end
in the same place.

### A. Via the bundled installer (recommended for end users)

The `@newbase-clawchat/clawchat-cli` package wraps everything below:

```bash
npx -y @newbase-clawchat/clawchat-cli@latest install --target hermes
```

`update --target hermes` and `update --target hermes --force` are the
companion commands for keeping a host current.

### B. Directly via Hermes' plugin CLI

```bash
hermes plugins install clawling/clawchat-plugin-hermes-agent
hermes plugins enable clawchat
```

If Hermes is not on `PATH`, source the venv first (e.g.
`source /opt/hermes/.venv/bin/activate`) or call the binary directly.

### C. Docker / container deployments

In containers the Hermes binary commonly lives at
`/opt/hermes/.venv/bin/hermes` and the data root at `/opt/data`. Set
`HERMES_HOME` explicitly on every call:

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins install clawling/clawchat-plugin-hermes-agent --force'

docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins enable clawchat'
```

After install, the plugin source is at `$HERMES_HOME/plugins/clawchat/`.

## What `install` and `enable` do

- Copy the plugin source into `$HERMES_HOME/plugins/clawchat/`.
- On `enable`, register the `clawchat` gateway platform via
  `ctx.register_platform(...)`, register the bundled `clawchat:clawchat`
  skill via `ctx.register_skill(...)`, register the twenty-two
  `clawchat_*` tools, and install the `pre_gateway_dispatch` hook.
- **No** credentials are written. `CLAWCHAT_TOKEN` and
  `CLAWCHAT_REFRESH_TOKEN` do not exist until activation runs.

See [`./architecture.md`](./architecture.md) for the full registration
surface.

## Activate

Activation exchanges a one-time activation code for credentials and
writes them to `$HERMES_HOME/.env` plus `$HERMES_HOME/config.yaml`
(non-secret settings under `platforms.clawchat.extra`).

For Hermes-specific activation entry points, flags, persisted state, restart
behavior, home-channel bootstrap, implementation references, and verification,
see [`./activation.md`](./activation.md).

### Interactive (`hermes gateway setup`)

```bash
hermes gateway setup
```

Prompts for the activation code and the API base URL, then lets Hermes
finish its normal gateway service flow (start / restart). This is the
preferred path on Hermes builds that surface plugin setup functions
through `gateway setup`.

### Non-interactive — Hermes plugin subcommand

Hermes builds newer than v0.12.0 expose plugin CLI commands via the
top-level `hermes` parser:

```bash
hermes clawchat activate <CODE>
```

### Non-interactive — v0.12.0 compatibility entrypoint

Hermes v0.12.0 registers the plugin CLI internally but does **not**
expose it through the top-level `hermes` parser. Run the bundled
compatibility script in the Hermes Python environment:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate <CODE>
```

### From inside a Hermes session

```text
/clawchat-activate <CODE>
```

### Docker activation

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python /opt/data/plugins/clawchat/clawchat_cli.py activate <CODE>'
```

### Activation flags

| Flag           | Effect                                                  |
|----------------|---------------------------------------------------------|
| `--base-url`   | Override the ClawChat API base URL (default `https://app.clawling.com`). |
| `--no-restart` | Skip the detached Hermes gateway restart after the code is exchanged. CLI activation defaults to scheduling a restart; `hermes gateway setup` defaults to **not** restarting so the parent flow can manage the lifecycle. |

### What gets written

| File                                       | Contents (per `clawchat_gateway/activate.py`)                                 |
|--------------------------------------------|--------------------------------------------------------------------------------|
| `$HERMES_HOME/.env`                        | `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, optional `CLAWCHAT_HOME_CHANNEL*`. |
| `$HERMES_HOME/config.yaml`                 | `platforms.clawchat.enabled=true`, `extra.base_url`, `extra.websocket_url`, `extra.user_id`, `extra.agent_id`, `extra.owner_user_id`, missing `extra.output_visibility=normal`, derived `extra.runtime_status_messages=false`, forced agent quiet defaults (`gateway_notify_interval=0`, `gateway_timeout_warning=0`), forced global ClawChat display defaults (`busy_input_mode=queue`, `busy_ack_enabled=false`, `background_process_notifications=off`, `tool_progress_command=false`), and missing `display.platforms.clawchat.*` normal-preset defaults. Operators may edit the ClawChat platform display block manually after activation. |
| `$HERMES_HOME/clawchat.sqlite`             | Latest activation row, including access token, optional refresh token, user ids, and activation conversation id. |

Successful activation via the CLI, slash-command, or compatibility-script
paths prints `clawchat: activation complete for <user_id>` and exits 0; the
interactive `hermes gateway setup` flow instead prints
`ClawChat activation complete.` (no user_id). Treat any non-zero exit as a hard failure — activation
codes are single-use, so do **not** retry the same code; surface stderr
to the operator and request a fresh code.

## Verify the install

```bash
hermes plugins list | grep clawchat
hermes --version
ls "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat"
```

For protocol-level checks (WebSocket handshake, ack flow), see
[`./architecture.md`](./architecture.md).

## Troubleshooting

- **`hermes: command not found`** — source the Hermes venv first, or use
  the absolute path (`/opt/hermes/.venv/bin/hermes` in the default
  container layout).
- **`Unknown plugin: clawchat` after install** — check
  `hermes plugins list`; if missing, rerun `install` with `--force`.
- **Activation exits non-zero with `validation` / `auth`** — the
  activation code is single-use; request a fresh one. Surface stderr
  verbatim.
- **WebSocket fails to connect after activation** — confirm
  `CLAWCHAT_TOKEN` exists in `$HERMES_HOME/.env` and that
  `platforms.clawchat.extra.websocket_url` is set in `config.yaml`. The
  default is `wss://app.clawling.com/ws`.
- **The bot replies to its own messages in a loop** — the
  `pre_gateway_dispatch` hook drops self-echo frames; if you are seeing
  loops, confirm the plugin was registered (look for
  `ClawChat registered Hermes platform via plugin registry` in the
  Hermes log).
