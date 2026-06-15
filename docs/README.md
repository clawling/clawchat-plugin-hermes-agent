# hermes-clawchat — docs

Documentation for the `clawchat` Hermes plugin (Python package
`clawchat-gateway`, source `clawling/clawchat-plugin-hermes-agent`). The plugin
registers a `clawchat` gateway platform inside a running Hermes Agent
v0.12.0+ process, ships a bundled `clawchat:clawchat` skill, and exposes
twenty-six `clawchat_*` tools to the agent.

## I want to…

| Goal                                                         | Start here                                              |
|--------------------------------------------------------------|---------------------------------------------------------|
| Install the plugin in a Hermes Agent                         | [`./install.md`](./install.md)                          |
| Activate the plugin in a Hermes Agent                        | [`./activation.md`](./activation.md)                    |
| Understand how the plugin plugs into the Hermes runtime      | [`./architecture.md`](./architecture.md)                |
| Understand command approval prompts and group owner routing  | [`./approve.md`](./approve.md)                          |
| Configure ClawChat output visibility                         | [`./output-visibility.md`](./output-visibility.md)      |
| Look up an env var or `platforms.clawchat.extra` field       | [`./configuration.md`](./configuration.md)              |
| Find a specific `clawchat_*` tool                            | [`./reference/tools.md`](./reference/tools.md)          |
| See every outbound HTTP / WS endpoint the plugin calls       | [`./reference/http-endpoints.md`](./reference/http-endpoints.md) |
| Use one of the activation CLIs                               | [`./reference/cli.md`](./reference/cli.md)              |
| Look up a wire-protocol shape (envelope, events, streaming)  | [`./client-integration.md`](./client-integration.md)    |
| Edit a prompt that ships with the plugin                     | [`./reference/prompts.md`](./reference/prompts.md)      |
| Understand Hermes prompt injection surfaces                  | [`./reference/prompt-injection.md`](./reference/prompt-injection.md) |
| Hack on the Python source locally                            | [`./development.md`](./development.md)                  |
| Inspect the Hermes Agent host source                         | [`./hermes-source-lookup.md`](./hermes-source-lookup.md) |

## Conventions

1. **Code is the source of truth.** Every claim in this tree should be
   traceable to a path under `clawchat_gateway/` or to `plugin.yaml`.
2. **`plugin.yaml` is canonical for the tool list** (`provides_tools`).
   `docs/reference/tools.md` is the human catalogue; it must stay in sync
   when tools are added or renamed.
3. **`skills/clawchat/SKILL.md`** is a *runtime artefact* loaded by
   Hermes via `ctx.register_skill(...)`. Treat it as agent-facing
   documentation, not as a developer or operator guide.
4. The install guide is [`./install.md`](./install.md). Keep install and
   activation instructions there so published docs have one operator-facing
   source of truth.
5. The wire protocol is documented in
   [`./client-integration.md`](./client-integration.md) — the authoritative
   Protocol v2 contract for this plugin. This plugin and `openclaw-clawchat`
   are peer Protocol-v2 clients; update the protocol doc when the WebSocket
   protocol changes.
