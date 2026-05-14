# Native CLI - `clawchat_gateway/cli.py`

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
