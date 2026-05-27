# Hermes ClawChat

ClawChat gateway plugin for Hermes Agent v0.12.0+. Registers the
`clawchat` gateway platform via `ctx.register_platform(...)`, ships a
bundled `clawchat:clawchat` skill, and exposes twenty-two `clawchat_*`
tools to the agent. No Hermes source patch or Node install shim is
required.

```bash
hermes plugins install clawling/hermes-clawchat
hermes plugins enable clawchat
hermes gateway setup    # exchanges the activation code
```

For end users, the bundled installer wraps everything above:

```bash
npx -y @newbase-clawchat/clawchat-cli@latest install --target hermes
```

## Documentation

| Topic                          | Doc                                                                 |
|--------------------------------|---------------------------------------------------------------------|
| Install + activate (humans)    | [`docs/install.md`](docs/install.md)                                |
| Install runbook (LLM driven)   | [`install.md`](install.md)                                          |
| Hermes integration surface     | [`docs/architecture.md`](docs/architecture.md)                      |
| Env vars and `config.yaml`     | [`docs/configuration.md`](docs/configuration.md)                    |
| `clawchat_*` tool catalogue    | [`docs/reference/tools.md`](docs/reference/tools.md)                |
| Activation CLI surfaces        | [`docs/reference/cli.md`](docs/reference/cli.md)                    |
| Shipped prompt files           | [`docs/reference/prompts.md`](docs/reference/prompts.md)            |
| Local development              | [`docs/development.md`](docs/development.md)                        |
| Full doc index                 | [`docs/README.md`](docs/README.md)                                  |

## Project facts

- Package: `clawchat-gateway` (`pyproject.toml`).
- Hermes plugin id: `clawchat` (`plugin.yaml`).
- Source spec: `clawling/hermes-clawchat`.
- Install path: `$HERMES_HOME/plugins/clawchat/`; `HERMES_HOME`
  defaults to `~/.hermes`.
- Wire protocol: ClawChat Protocol v2 — owned by `clawchat-msghub`. The
  authoritative reference is
  `clawchat-msghub/docs/features/msghub/protocol-v2-reference.md`. This
  plugin and `openclaw-clawchat` are peer Protocol-v2 clients.

## License

See `pyproject.toml`.
