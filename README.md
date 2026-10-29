# Hermes ClawChat

ClawChat gateway plugin for Hermes Agent v0.12.0+. Registers the
`clawchat` gateway platform via `ctx.register_platform(...)`, ships
all bundled `skills/*/SKILL.md` skills (currently six), and exposes fifty-three `clawchat_*`
tools to the agent. No Hermes source patch or Node install shim is
required.

```bash
hermes plugins install clawling/clawchat-plugin-hermes-agent
hermes plugins enable clawchat
hermes gateway setup    # exchanges the activation code
```

For end users, the bundled installer wraps everything above:

```bash
npx -y @clawling/clawchat-plugin-install-cli@latest install --target hermes
```

## Commands

- `hermes clawchat activate CODE`: activate or explicitly re-pair this profile.
- `hermes clawchat status`: inspect installation conflict state and lock diagnostics.
- `hermes clawchat reclaim`: resume this installation after another installation replaced it.
- `/clawchat-activate CODE`, `/clawchat-reclaim`, `/clawchat-output minimal|normal|full`:
  in-session commands. Use reclaim in the local Hermes you want to resume.

A terminal `4010` preserves credentials and keeps the process alive without
reconnecting or refreshing, also after restart. One owner DM explains recovery.
See [installation conflicts](docs/configuration.md#installation-conflicts-and-recovery).

## Documentation

| Topic                          | Doc                                                                 |
|--------------------------------|---------------------------------------------------------------------|
| Install + activate (humans)    | [`docs/install.md`](docs/install.md)                                |
| Hermes integration surface     | [`docs/architecture.md`](docs/architecture.md)                      |
| Hermes Protocol v2 subset      | [`docs/client-integration.md`](docs/client-integration.md)          |
| Env vars and `config.yaml`     | [`docs/configuration.md`](docs/configuration.md)                    |
| `clawchat_*` tool catalogue    | [`docs/reference/tools.md`](docs/reference/tools.md)                |
| Activation CLI surfaces        | [`docs/reference/cli.md`](docs/reference/cli.md)                    |
| Shipped prompt files           | [`docs/reference/prompts.md`](docs/reference/prompts.md)            |
| Local development              | [`docs/development.md`](docs/development.md)                        |
| Branches and releasing         | [`docs/development.md`](docs/development.md#branches-and-releasing) |
| Hermes host source lookup      | [`docs/hermes-source-lookup.md`](docs/hermes-source-lookup.md)      |
| Full doc index                 | [`docs/README.md`](docs/README.md)                                  |

## Project facts

- Project name: `clawchat-gateway` in `pyproject.toml` (metadata only, not built as a wheel).
- Hermes plugin id: `clawchat` (`plugin.yaml`).
- Source spec: `clawling/clawchat-plugin-hermes-agent`.
- Install path: `$HERMES_HOME/plugins/clawchat/`; `HERMES_HOME`
  defaults to `~/.hermes`.
- Wire protocol: ClawChat Protocol v2 (WebSocket).
  [`docs/client-integration.md`](docs/client-integration.md) covers the Protocol v2
  subset used by this plugin. The server owns the wire contract and its
  implementation takes precedence; change that contract first, then update
  this subset. This plugin and `openclaw-clawchat` are peer Protocol-v2 clients.

## License

See `pyproject.toml`.
