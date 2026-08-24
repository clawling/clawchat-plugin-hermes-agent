# CLI reference

The plugin contributes four command surfaces to a Hermes install. Three are
activation surfaces wrapping the same
`clawchat_gateway.activate.activate_and_maybe_restart` coroutine — pick
whichever matches the host. The fourth, `/clawchat-output`, has nothing to do
with activation and sets the output visibility preset. The plugin additionally
backs the host's own `hermes gateway setup` flow (Interactive setup, below) via
`setup_fn`; that command belongs to Hermes, not to this plugin.

## In-session slash command

```text
/clawchat-activate CODE [--restart] [--no-restart] [--new-account] [--repair]
```

- Registered through `ctx.register_command("clawchat-activate", ...)`
  (`__init__._register_commands`).
- Handler: `clawchat_gateway.commands.handle_clawchat_activate_command`.
- Schedules a detached gateway restart by default. Use `--no-restart` to skip it.

## Top-level `hermes clawchat …`

```bash
hermes clawchat activate CODE [--restart] [--no-restart] [--new-account] [--repair]
```

- Registered through `ctx.register_cli_command("clawchat", ...)`
  (`__init__._register_cli_commands`).
- Available on Hermes builds that surface plugin CLI commands through
  the top-level parser. **Not** available on plain v0.12.0.
- Handler: `clawchat_gateway.cli.handle_clawchat_cli`.

## v0.12.0 compatibility script

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate CODE [--restart] [--no-restart] [--new-account] [--repair]
```

- Standalone Python entrypoint at `clawchat_cli.py`.
- Adds the plugin root to `sys.path` and re-uses the same
  `setup_clawchat_cli` parser as `hermes clawchat …` above, so it accepts the
  same flags.
- Use this when `hermes clawchat …` is not exposed.

## Output-visibility slash command

```text
/clawchat-output minimal|normal|full
```

- Registered through `ctx.register_command("clawchat-output", ...)`
  (`__init__._register_commands`).
- Handler: `clawchat_gateway.commands.handle_clawchat_output_command`.
- Takes exactly one argument; anything else returns
  `usage: /clawchat-output minimal|normal|full`. An unsupported mode returns the
  same usage line plus the validation message.
- Applies the preset by rewriting `$HERMES_HOME/config.yaml`
  (`output_visibility.apply_output_visibility`): `platforms.clawchat.extra`
  (`output_visibility`, `runtime_status_messages`),
  `display.platforms.clawchat`, and `agent.gateway_notify_interval` /
  `agent.gateway_timeout_warning`. It is a host-wide platform setting, not a
  per-chat or per-session preference, and it does **not** restart the gateway.
- On success it replies with the resolved visibility, runtime-status state, and
  detail level. The change applies to new ClawChat messages.
- Full preset-to-config mapping: [`../output-visibility.md`](../output-visibility.md).

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
| `--restart`    | absent                               | Compatibility flag; activation schedules a detached Hermes gateway restart by default. |
| `--no-restart` | absent                               | Skip the detached Hermes gateway restart after activation. |
| `--new-account` | absent                              | Replace this profile's ClawChat identity with a brand-new agent. Drops the stored `extra.user_id` from the replay so the code pairs a fresh agent instead of re-binding to the incumbent one. Required when the profile is already paired and you want a *second* agent under the same profile — or when the identity was inherited from a cloned config. |
| `--repair`     | absent                               | Re-pair the agent this profile already holds, keeping its identity. Replays the stored `extra.user_id` and spends the code on it; it never creates a new agent. Only for "this profile paired its own agent and lost its token". Refused with `UnprovenRepairError` when the profile cannot prove it owns that identity (no matching `extra.profile` stamp and no local pairing record) — the shape of a cloned or copied `config.yaml`. |

Without `--new-account` or `--repair`, activation on an already-paired profile
is refused up front with `ExistingActivationError` (exit code `1`), and the
single-use connect code is **not** spent — the message names both flags and the
`hermes profile create` path for a second agent.

## Exit codes

| Code | Meaning                                                                                              |
|------|------------------------------------------------------------------------------------------------------|
| `0`  | Activation succeeded. Prints `clawchat: activation complete for <user_id>` to stdout.                |
| `1`  | `ClawChatApiError` (validation, auth, network, etc.). The CLI prints `clawchat: activation failed (<kind> [<path>] [status=N] [code=N]): <message>` to stderr. |
| `1`  | `ExistingActivationError` / `UnprovenRepairError` — a local precondition failure, refused before the code is spent. The CLI prints `clawchat: activation refused — <message>` to stderr. |
| `2`  | `clawchat_cli.py` / `cli.handle_clawchat_cli` got no subcommand — prints help.                       |

## What activation writes

See [`../install.md`](../install.md) for the install-level summary. The
authoritative implementation is in
`clawchat_gateway/activate.py:persist_activation`.
