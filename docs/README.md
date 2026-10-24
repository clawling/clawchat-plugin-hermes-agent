# hermes-clawchat — docs

Documentation for the `clawchat` Hermes plugin (Python package
`clawchat-gateway`, source `clawling/clawchat-plugin-hermes-agent`). The plugin
registers a `clawchat` gateway platform inside a running Hermes Agent
v0.12.0+ process, ships all bundled `skills/*/SKILL.md` skills (currently six),
and exposes all `clawchat_*` tools listed in `plugin.yaml`'s `provides_tools`.

## I want to…

| Goal                                                         | Start here                                              |
|--------------------------------------------------------------|---------------------------------------------------------|
| Install the plugin in a Hermes Agent                         | [`./install.md`](./install.md)                          |
| Activate the plugin in a Hermes Agent                        | [`./activation.md`](./activation.md)                    |
| Run several agents on one host / avoid pairing the wrong Hermes profile | [`./install.md#confirm-the-target-profile-before-every-install--activate`](./install.md#confirm-the-target-profile-before-every-install--activate) |
| Run several profiles under one multiplexed gateway | [`./multiplex-profile-isolation.md`](./multiplex-profile-isolation.md) |
| Inspect credential deletion, suspension and persistence races | [Hermes appendix](./token-refresh.md#h-hermes-appendix--credential-lifecycle-and-races) |
| Understand automatic token refresh & auto-logout             | [`./token-refresh.md`](./token-refresh.md)              |
| Understand how the plugin plugs into the Hermes runtime      | [`./architecture.md`](./architecture.md)                |
| Configure ClawChat output visibility                         | [`./output-visibility.md`](./output-visibility.md)      |
| Understand the Liveware Sample auto-boot demo app             | [`./liveware-sample.md`](./liveware-sample.md)          |
| Look up an env var or `platforms.clawchat.extra` field       | [`./configuration.md`](./configuration.md)              |
| Set per-person direct / ask / deny rules, capabilities or all tools | [Tool grants](./configuration.md#per-person-tool-grants) |
| Check cross-session execution limits and cross-chat messaging | [Routing limits](./configuration.md#cross-session-routing-and-known-limits) |
| Upgrade the grant database safely | [Storage upgrade](./configuration.md#storage-upgrade) |
| Configure tool grants and handle guest access requests | [Owner and operator guide](./tool-grants.md) |
| Audit non-owner tools, origins and attachment safety | [Non-owner boundaries](./non-owner-boundaries.md) |
| Find a specific `clawchat_*` tool                            | [`./reference/tools.md`](./reference/tools.md)          |
| See every outbound HTTP / WS endpoint the plugin calls       | [`./reference/http-endpoints.md`](./reference/http-endpoints.md) |
| Use one of the activation CLIs                               | [`./reference/cli.md`](./reference/cli.md)              |
| Look up the Hermes Protocol v2 subset                       | [`./client-integration.md`](./client-integration.md)    |
| Edit a prompt that ships with the plugin                     | [`./reference/prompts.md`](./reference/prompts.md)      |
| Understand Hermes prompt injection surfaces                  | [`./reference/prompt-injection.md`](./reference/prompt-injection.md) |
| Understand dynamic skill updates & skill registration        | [`./skill-updates.md`](./skill-updates.md)              |
| Hack on the Python source locally                            | [`./development.md`](./development.md)                  |
| Inspect the Hermes Agent host source                         | [`./hermes-source-lookup.md`](./hermes-source-lookup.md) |

## Conventions

1. **Code is the source of truth.** Every claim in this tree should be
   traceable to a path under `clawchat_gateway/` or to `plugin.yaml`.
2. **`plugin.yaml` is canonical for the tool list** (`provides_tools`).
   `docs/reference/tools.md` is the human catalogue; it must stay in sync
   when tools are added or renamed.
3. **`skills/clawchat-core/SKILL.md`** is a *runtime artefact* loaded by
   Hermes via `ctx.register_skill(...)`. Treat it as agent-facing
   documentation, not as a developer or operator guide.
4. Installation steps live in [`./install.md`](./install.md); activation
   entrypoints, flags and persisted state live in [`./activation.md`](./activation.md).
   Credential lifecycle implementation details live in the
   [Hermes appendix](./token-refresh.md#h-hermes-appendix--credential-lifecycle-and-races).
   Elsewhere, summarize and link to these references.
5. [`./client-integration.md`](./client-integration.md) covers the Protocol v2
   subset used by this plugin. The server owns the wire contract and its
   implementation takes precedence. Change that contract first, then update
   this subset and the peer Protocol-v2 clients.
