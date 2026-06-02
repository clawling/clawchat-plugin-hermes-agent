# CLI reference

The plugin contributes three command surfaces to a Hermes install. They
all wrap the same `clawchat_gateway.activate.activate_and_maybe_restart`
coroutine; pick whichever matches the host.

## In-session slash command

```text
/clawchat-activate CODE [--restart] [--no-restart]
```

- Registered through `ctx.register_command("clawchat-activate", ...)`
  (`__init__._register_commands`).
- Handler: `clawchat_gateway.commands.handle_clawchat_activate_command`.
- Schedules a detached gateway restart only when `--restart` is present.

## Top-level `hermes clawchat …`

```bash
hermes clawchat activate CODE [--restart] [--no-restart]
```

- Registered through `ctx.register_cli_command("clawchat", ...)`
  (`__init__._register_cli_commands`).
- Available on Hermes builds that surface plugin CLI commands through
  the top-level parser. **Not** available on plain v0.12.0.
- Handler: `clawchat_gateway.cli.handle_clawchat_cli`.

## v0.12.0 compatibility script

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate CODE [--restart] [--no-restart]
```

- Standalone Python entrypoint at `clawchat_cli.py`.
- Adds the plugin root to `sys.path` and re-uses the same
  `setup_clawchat_cli` parser as path B.
- Use this when `hermes clawchat …` is not exposed.

## Interactive setup

```bash
hermes gateway setup
```

- Backed by `clawchat_gateway.setup.setup_clawchat_platform` (passed
  into `register_platform` as `setup_fn`).
- Prompts for the activation code and the API base URL, then exchanges
  the code **without** scheduling a restart so the surrounding
  `hermes gateway setup` flow can manage the lifecycle.

## Flag summary

| Flag           | Default                              | Behavior                                                                                  |
|----------------|--------------------------------------|-------------------------------------------------------------------------------------------|
| `CODE`         | required                             | Single-use activation code. Use exactly as provided; do not normalize, lowercase, or retry. |
| `--restart`    | absent                               | Schedule a detached Hermes gateway restart after activation. |
| `--no-restart` | absent                               | Compatibility flag that prevents restart scheduling when `--restart` is also present. |

## Exit codes

| Code | Meaning                                                                                              |
|------|------------------------------------------------------------------------------------------------------|
| `0`  | Activation succeeded. Prints `clawchat: activation complete for <user_id>` to stdout.                |
| `1`  | `ClawChatApiError` (validation, auth, network, etc.). The CLI prints `clawchat: activation failed (<kind> [<path>] [status=N] [code=N]): <message>` to stderr. |
| `2`  | `clawchat_cli.py` / `cli.handle_clawchat_cli` got no subcommand — prints help.                       |

## What activation writes

See [`../install.md`](../install.md) for the file-level summary. The
authoritative implementation is in
`clawchat_gateway/activate.py:persist_activation`.
