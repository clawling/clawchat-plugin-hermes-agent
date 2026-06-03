# Hermes ClawChat

ClawChat gateway plugin for Hermes Agent v0.12.0+. Registers the
`clawchat` gateway platform via `ctx.register_platform(...)`, ships a
bundled `clawchat:clawchat` skill, and exposes twenty-two `clawchat_*`
tools to the agent. No Hermes source patch or Node install shim is
required.

```bash
hermes plugins install clawling/clawchat-plugin-hermes-agent
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
| Hermes integration surface     | [`docs/architecture.md`](docs/architecture.md)                      |
| Wire protocol contract         | [`docs/client-integration.md`](docs/client-integration.md)          |
| Env vars and `config.yaml`     | [`docs/configuration.md`](docs/configuration.md)                    |
| `clawchat_*` tool catalogue    | [`docs/reference/tools.md`](docs/reference/tools.md)                |
| Activation CLI surfaces        | [`docs/reference/cli.md`](docs/reference/cli.md)                    |
| Shipped prompt files           | [`docs/reference/prompts.md`](docs/reference/prompts.md)            |
| Local development              | [`docs/development.md`](docs/development.md)                        |
| Hermes host source lookup      | [`docs/hermes-source-lookup.md`](docs/hermes-source-lookup.md)      |
| Full doc index                 | [`docs/README.md`](docs/README.md)                                  |

## Project facts

- Package: `clawchat-gateway` (`pyproject.toml`).
- Hermes plugin id: `clawchat` (`plugin.yaml`).
- Source spec: `clawling/clawchat-plugin-hermes-agent`.
- Install path: `$HERMES_HOME/plugins/clawchat/`; `HERMES_HOME`
  defaults to `~/.hermes`.
- Wire protocol: ClawChat Protocol v2 (WebSocket). The authoritative contract
  is [`docs/client-integration.md`](docs/client-integration.md). This plugin
  and `openclaw-clawchat` are peer Protocol-v2 clients.

## License

See `pyproject.toml`.
