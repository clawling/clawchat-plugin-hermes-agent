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
├── skills/                      # All bundled */SKILL.md skills (currently six) + manifest.json
├── tests/                       # Tracked by named .gitignore exceptions
├── uv.lock                      # Locked development and test environment
└── docs/                        # This documentation tree
```

## Setting up a local environment

The package metadata supports plain `pip` and `uv`. The tracked `uv.lock`
locks the development and test environment. Hermes installs the source tree
without using this lockfile; runtime requirements are in `pyproject.toml`.

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

The `tests/` directory is ignored by default (`.gitignore: tests/*`),
with named exceptions for tracked tests and their shared `conftest.py`.
To include a new test in the repository, add a `!tests/<file>` entry to
`.gitignore` and a comment above the list describing the invariant it pins.
Without that exception, the test stays local. Run the tracked tests from
a clean checkout with:

```bash
uv run --extra test pytest
uv run --extra test pytest tests/test_x.py
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

`clawchat_gateway.inbound_trace` logs one line per inbound frame and warns
`inbound rate spike ... (possible self-echo / interrupt loop)` when one chat
delivers 5 new messages within 30 s. Only messages that passed the self-echo
drop and the message-id dedupe count, so replays and redeliveries in a busy
group do not trip it; a warning means that many distinct messages really
reached the agent.

## Consulting Hermes host source

When a question crosses into Hermes host behavior — plugin SDK contract,
`ctx.register_platform`/`ctx.register_skill`, gateway dispatch, the
`send_message` tool, or activation lifecycle — point `tmp/hermes/` at a
matching Hermes Agent v0.12.0+ checkout and follow
[`./hermes-source-lookup.md`](./hermes-source-lookup.md). The path is
already in `.gitignore`; nothing committed depends on it.

## Modifying the wire protocol

[`./client-integration.md`](./client-integration.md) is a plugin-side derivative
of the server's Protocol v2 contract; the server implementation takes precedence
if they disagree. Update the server contract first, then this copy and
update `clawchat_gateway/protocol.py` (outbound builders) and
`clawchat_gateway/inbound.py` (parsing). Mirror the same change in
`clawchat-plugin-openclaw/src/` — the two plugins are peers.

## Hermes install scanner

`hermes plugins install` clones this repository and runs Hermes' own
security scanner (`tools/plugin_guard.py`, enabled by
`plugins.scan_on_install`, on by default) over **every file in the clone**
— docs, skills and tracked tests included. An external plugin is
`community` trust, so a `dangerous` verdict blocks the install and
`--force` does not override it; a single CRITICAL finding is enough.
`caution` (HIGH/MEDIUM findings) only asks the user to confirm.

So never put a literal that the scanner rates critical into any tracked
file, even as test data — a destructive shell command (recursive delete of
a root path), reading a secrets file, a reverse shell, and similar. Use a
harmless placeholder such as `run-cleanup --everything` instead.
`tests/test_install_scan.py` runs the real scanner over a copy of the
tracked files and fails on any critical finding; it needs a Hermes source
checkout (`$HERMES_SOURCE_DIR`, `tmp/hermes` or `tmp/hermes-agent`) and
skips without one, so run it with one present before a release.

## Excluded from this checkout

`.gitignore` excludes runtime-only Python artefacts, development
directories (`.e2e/`, `deploy/`, `scripts/`) and local agent instructions
(`CLAUDE.md`) from the install payload. `AGENTS.md` is tracked — PR review
reads its review rules — so it is installed too; it holds only code review
rules and no instructions for a running agent. In `tests/`, only the
named exceptions, including `conftest.py`, are tracked; other tests stay
local. Every tracked file, including those tests, is copied into
`$HERMES_HOME/plugins/clawchat/` on every install and must pass the Hermes
install scanner described above.

## Packaging caveats

`clawchat_gateway/_yaml.py` reads through `hermes_yaml`, then PyYAML, then
ruamel.yaml, matching the parser used by new and old Hermes hosts; the ruamel
fallback follows the host's YAML 1.1 policy. YAML libraries are test extras,
not plugin runtime dependencies. The plugin does not write YAML itself:
config writes go through `hermes_cli.config.save_config`, so Hermes owns
formatting and comments.

`MANIFEST.in` only ships `prompts/*.md`, and `[tool.setuptools.packages.find]`
in `pyproject.toml` only includes the `clawchat_gateway*` package. The
top-level `skills/` tree — all bundled `skills/*/SKILL.md` directories
(currently six; see [skill updates](./skill-updates.md#bundled-ids-are-auto-discovered-not-hardcoded)) **and**
`skills/manifest.json` — lives outside that package and is not referenced by
`MANIFEST.in` either, so none of it is included in a built wheel. A
wheel-only install therefore has an empty `bundled_skill_ids()` result (the
glob over `skills/*/SKILL.md` matches nothing) and a no-op load-time
tombstone sweep (`apply_bundled_tombstones()` finds no `skills/manifest.json`
and returns `[]`) — no bundled skills register, and no stale managed ids get
purged. This is acceptable today because Hermes installs from the source
tree, not from a wheel — every file in the tracked tree, `skills/` included,
is copied into `$HERMES_HOME/plugins/clawchat/` on install — but if you ever
publish to PyPI, packaging must be reworked first (extend `MANIFEST.in` and
the packaging config to carry `skills/` alongside the package, and adjust
`bundled_skills_dir()`'s path resolution if the layout changes) or a
wheel-based install will silently ship with zero bundled skills.
