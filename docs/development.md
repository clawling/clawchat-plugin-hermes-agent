# Development

This repo ships as a runtime Hermes plugin: Hermes copies the tracked
tree directly into `$HERMES_HOME/plugins/clawchat/` and imports the
top-level `__init__.py`. There is no Hermes-internal build step. Local
development means setting up a Python environment that can import the
same modules Hermes loads.

## Repository layout

```
.
├── __init__.py                  # Hermes entrypoint; only defines register(ctx) and helpers
├── clawchat_cli.py              # v0.12.0 compatibility CLI
├── plugin.yaml                  # Plugin manifest — canonical for provides_tools
├── pyproject.toml               # Python distribution (name: clawchat-gateway)
├── MANIFEST.in                  # Bundles prompts/*.md when building a wheel
├── clawchat_gateway/            # All runtime modules — adapter, connection, protocol, tools, …
├── prompts/                     # Required platform.md + optional defaults
├── skills/clawchat/SKILL.md     # Bundled Hermes Plugin Bundle skill
└── docs/                        # This documentation tree
```

## Setting up a local environment

The package metadata supports plain `pip` and `uv`. The repo does not
ship a lockfile for runtime dependencies (only the wheel constraints in
`pyproject.toml`).

### With `uv`

```bash
uv venv
uv pip install -e ".[test]"
```

### With `pip`

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

`-e` mirrors how Hermes imports the source tree.

## Running tests

`pyproject.toml` declares pytest configuration:

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-q"
pythonpath = ["."]
```

The `tests/` directory is **not** tracked in this repo
(`.gitignore: tests/`) — the published plugin checkout intentionally
excludes development-only material. To work on tests:

```bash
uv run pytest                  # if a local tests/ directory exists
uv run pytest tests/test_x.py
```

Most of the adapter, protocol, and tool wiring is testable without a
live Hermes process; the WebSocket transport
(`clawchat_gateway/connection.py`) is the main piece that needs a fake
or recorded server.

## Live debugging against a Hermes process

The supported development loop is:

1. Activate a fresh Hermes Agent v0.12.0+ instance.
2. `hermes plugins install` this repo (or symlink the source tree into
   `$HERMES_HOME/plugins/clawchat/`).
3. Run `hermes gateway setup` or `hermes clawchat activate <CODE>` to
   exchange credentials.
4. Tail the Hermes log — the plugin logs through
   `logging.getLogger("clawchat_gateway.*")` (notably
   `clawchat_gateway.connection`, `clawchat_gateway.adapter`,
   `clawchat_gateway.inbound_trace`).

The runtime hook
`__init__._clawchat_pre_gateway_dispatch` will log
`clawchat pre_gateway_dispatch skip: self-echo chat_id=... user_id=...`
when it drops a self-echo frame — useful for confirming the plugin is
loaded.

## Consulting Hermes host source

When a question crosses into Hermes host behavior — plugin SDK contract,
`ctx.register_platform`/`ctx.register_skill`, gateway dispatch, the
`send_message` tool, or activation lifecycle — point `tmp/hermes/` at a
matching Hermes Agent v0.12.0+ checkout and follow
[`./hermes-source-lookup.md`](./hermes-source-lookup.md). The path is
already in `.gitignore`; nothing committed depends on it.

## Modifying the wire protocol

The Protocol v2 contract is documented in
[`./client-integration.md`](./client-integration.md). Update it first, then
update `clawchat_gateway/protocol.py` (outbound builders) and
`clawchat_gateway/inbound.py` (parsing). Mirror the same change in
`clawchat-plugin-openclaw/src/` — the two plugins are peers.

## Excluded from this checkout

`.gitignore` excludes runtime-only Python artefacts plus several
development directories (`tests/`, `.e2e/`, `deploy/`, `scripts/`,
`AGENTS.md`, `CLAUDE.md`). Those exist in private working trees and are
intentionally kept out of the install payload — every file in the
tracked tree is copied into `$HERMES_HOME/plugins/clawchat/` on every
install.

## Packaging caveats

`MANIFEST.in` only ships `prompts/*.md`. The bundled skill at
`skills/clawchat/SKILL.md` is **not** in `MANIFEST.in`, so a built wheel
would omit it. This is acceptable today because Hermes installs from
the source tree, not from a wheel — but if you ever publish to PyPI,
extend `MANIFEST.in` first.
