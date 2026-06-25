# Hermes Source Lookup Guide

This repository is ClawChat's Hermes Agent plugin. Most questions should be answered from this repository first: `pyproject.toml`, `plugin.yaml`, `clawchat_gateway/`, `__init__.py`, and the matching tests. Inspect local Hermes Agent source only when the question crosses into Hermes host behavior.

## When To Inspect Hermes Source

Inspect the Hermes Agent source under `tmp/hermes/` when one of these applies:

- The Hermes plugin SDK contract is unclear, such as `ctx.register_platform`, `ctx.register_skill`, plugin context wiring, plugin lifecycle hooks, or how the gateway dispatches incoming events to a registered platform.
- The issue is in the host-side lifecycle, such as `hermes plugins install/enable/disable`, plugin metadata scanning, `hermes gateway setup`, gateway start/restart, activation-code exchange, or config reload.
- A behavior depends on the built-in `send_message` tool, the `pre_gateway_dispatch` hook chain, or how the host resolves a target reference like `clawchat:cnv_…`.
- You need to confirm whether the minimum host version supports a capability — Hermes v0.12.0+ is the floor, and ABI details (signature of `register_platform`, available context attributes, skill loader format) shift between releases. Cross-check the matching `RELEASE_v0.X.0.md` in the upstream tree.
- This plugin's code disagrees with Hermes docs, tests, or runtime behavior, and you need the host's actual behavior.

## When Not To Inspect Hermes Source

Resolve these within this repository first:

- ClawChat Protocol v2 envelopes, event taxonomy, routing, replay, or streaming semantics. Start with `docs/client-integration.md` (the protocol contract) and `docs/architecture.md`. Verify against `clawchat_gateway/`.
- ClawChat REST, media upload, account profile, and friends behavior. Start with `docs/architecture.md`, `docs/configuration.md`, and the matching modules under `clawchat_gateway/`.
- The `clawchat:clawchat` skill and the `clawchat_*` tool catalogue. Start with `docs/reference/tools.md`, `docs/reference/prompts.md`, `plugin.yaml`, and `skills/clawchat/SKILL.md`.
- README, install prose, and ordinary project-description wording.
- Unit-test or import-time failures whose stack traces only involve this repository's source.

## How To Inspect

`tmp/hermes/` is an ignored local checkout. It is not a submodule and is not part of this repository's tests or published plugin. Populate it with a symlink to a local Hermes Agent source tree, or a fresh clone:

```bash
# Option A — symlink to a local checkout
ln -s /path/to/hermes-agent tmp/hermes

# Option B — fresh clone of the upstream source
git clone <hermes-agent-repo-url> tmp/hermes
```

Match the checkout to the Hermes version that hosts this plugin. The plugin currently requires Hermes Agent v0.12.0+; cross-check by reading `RELEASE_v0.X.0.md` under `tmp/hermes/` for the version actually in use.

Use focused searches so unrelated source files do not dominate the results. Examples:

```bash
rg -n "register_platform|register_skill|register_tool|register_prompt" tmp/hermes
rg -n "pre_gateway_dispatch|gateway dispatch|gateway setup|activation" tmp/hermes
rg -n "send_message|_parse_target_ref|target_ref" tmp/hermes
rg -n "hermes plugins (install|enable|disable|update)" tmp/hermes
```

Prefer Hermes source and tests over Hermes prose docs. When reporting a conclusion, cite concrete local paths and line numbers, and state that the conclusion comes from host source rather than this plugin's docs.

Do not edit `tmp/hermes/` to fix this plugin. It is only for checking host behavior. If a Hermes Agent change appears necessary, collect the evidence and impact first, then handle that as a separate task upstream.
