# Native CLI - `clawchat_gateway/cli.py` and `clawchat_cli.py`

Registers and handles the Hermes-native `hermes clawchat ...` command group through `ctx.register_cli_command(...)`.

## `setup_clawchat_cli`

```python
setup_clawchat_cli(parser: argparse.ArgumentParser) -> None
```

Adds the `activate` subcommand:

```text
usage: hermes clawchat activate [--base-url URL] [--no-restart] CODE
```

- `CODE` - required ClawChat activation code.
- `--base-url` - optional ClawChat HTTP API base URL; defaults to `DEFAULT_BASE_URL`.
- `--no-restart` - persist activation but skip the detached Hermes gateway restart.

The parser stores `handle_clawchat_cli` as the command handler.

## `handle_clawchat_cli`

```python
handle_clawchat_cli(args: argparse.Namespace) -> int
```

For `activate`, calls:

```python
activate_and_maybe_restart(
    args.code,
    base_url=args.base_url,
    restart=not args.no_restart,
)
```

On success, prints concise status lines:

```text
clawchat: activation complete for <user_id>
clawchat: Hermes restart scheduled in 2s
```

The restart line is omitted when `--no-restart` is used. Unknown or missing subcommands print help and exit `2`.

## v0.12 compatibility entrypoint

Hermes Agent v0.12.0 has `ctx.register_cli_command(...)`, but its top-level `hermes` parser only discovers memory-provider CLI commands. General plugin commands are stored on `PluginManager._cli_commands` but are not added to argparse, so `hermes clawchat activate ...` is not available there.

The repo-root `clawchat_cli.py` is the command-line fallback for that host version:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate CODE
```

The script prepends its plugin directory to `sys.path`, builds the same parser with `setup_clawchat_cli(...)`, and dispatches to `handle_clawchat_cli(...)`. It therefore writes Hermes config through the same `activate_and_maybe_restart(...)` path as the native plugin CLI command.
