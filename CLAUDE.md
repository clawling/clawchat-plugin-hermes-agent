# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Keep docs in sync with code

Always read the relevant doc before changing a feature, and update it after. Whenever you add, remove, or change a Hermes tool, CLI subcommand, env var, anchor patch, config field, or wire-protocol behavior, update the matching file in the same change set:

- `README.md`, `install.md`, `.e2e/dev_install.md` — install/quick-start, env vars, user-visible flows
- `plugin.yaml` — manifest (`requires_env`, `provides_tools`, `provides_hooks`); must match what `register(ctx)` actually registers
- `docs/` per-module references — index at `docs/README.md`; one doc per `clawchat_gateway/*.py` module, plus `docs/architecture.md` (boot sequence, data flow, design choices, env vars) and `docs/clawchat-protocol.md` (wire-protocol spec; `docs/protocol.md` is the Python builder API)
- `skills/clawchat/SKILL.md` — activation/profile/avatar flows surfaced to the LLM (must stay consistent with the tool `description` fields in `__init__.py`)
- This `CLAUDE.md` — only the orientation, command quick-reference, and env-var summary below; deep-dive material lives in `docs/`

Code and docs must not drift.

## What this repo is

A **Hermes Agent plugin** that integrates the ClawChat messaging platform. It is an **installable plugin made for hermes-agent and will not be merged into the original hermes-agent project** — it is loaded at runtime by hermes-agent. On Hermes v0.12.0+ it registers the `clawchat` gateway platform via `ctx.register_platform(...)`; the legacy anchor-patch installer in `install.py` is only a fallback for older Hermes builds without the platform registry API.

The root `__init__.py` is the plugin entrypoint; `plugin.yaml` is the manifest; `clawchat_gateway/` is the gateway adapter package (also pip-installable as `clawchat-gateway`).

The source code of hermes-agent is available locally at `tmp/hermes-agent/` — refer to its code and changelog (`RELEASE_v0.*.md`) when you need to understand host APIs, the platform registry contract, or behavior changes across Hermes versions. When modifying the plugin, follow the official guidelines of hermes-agent as much as possible — prefer the documented plugin/platform APIs over ad-hoc patching, and match the host's conventions (naming, lifecycle, error surface, config shape) so the plugin behaves like a first-party platform.

For boot sequence, runtime data flow, design choices, the self-echo guard rationale, and the per-module catalogue, see `docs/architecture.md` and `docs/README.md`.

## Common commands

All runtime CLIs must use the **Hermes Python venv**, not the system Python, because the adapter imports `gateway.platforms.base` / `gateway.config` from hermes-agent at runtime. Tests stub these (see `docs/tests.md`), so the system Python is fine for tests only.

- Test runner: `pytest` (single test: `pytest path::test_name`).
- CLI references: `docs/activate.md`, `docs/profile.md`, `docs/installer.md`.
- End-user install on Hermes v0.12.0+: `hermes plugins install clawling/hermes-clawchat && hermes plugins enable clawchat`. The `clawchat_gateway.install` CLI is only the legacy fallback for older Hermes checkouts without `ctx.register_platform`.

## Testing

`tests/conftest.py` inserts the repo root onto `sys.path` and `tests/fake_hermes.py` injects stub modules for `gateway`, `gateway.config`, `gateway.platforms`, and `gateway.platforms.base` so the adapter can be imported without a real hermes-agent checkout. When adding imports from `gateway.*` in production code, extend `fake_hermes.py` or the test will fail at import time. Pytest runs in `asyncio_mode = "auto"`. Full reference: `docs/tests.md`.

For real-environment testing — exercising the plugin against a live ClawChat backend and the actual `nousresearch/hermes-agent` Docker image, or reproducing a runtime bug that the unit tests can't reach — use the harness under `.e2e/`. See `.e2e/docs/testing.md` for the full setup and run procedure (driver: `.e2e/local_start_test.sh`, baseline data dir: `.e2e/tmp/hermes_data_base/`, install spec read by the agent: `.e2e/dev_install.md`).

Note: the `.e2e/` harness has two prerequisites that **require manual human setup** and cannot be produced by an agent — a valid JWT in `.e2e/.env` (issued to a logged-in human) and the baseline `.e2e/tmp/hermes_data_base/` (built once via interactive Hermes first-run). If either is missing, stop and ask the human to provide them; do not attempt to fabricate them.

## Environment variables

Quick reference; full descriptions and resolution order in `docs/architecture.md` (Environment variables) and `docs/config.md`.

- `HERMES_HOME` — Hermes data dir (default `~/.hermes`).
- `HERMES_DIR` / `HERMES_AGENT_DIR` — hermes-agent install dir (default `$HERMES_HOME/hermes-agent`, or `/opt/hermes` if present).
- `CLAWCHAT_WEBSOCKET_URL` / `CLAWCHAT_WS_URL`, `CLAWCHAT_BASE_URL`, `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, `CLAWCHAT_USER_ID`, `CLAWCHAT_REPLY_MODE`, `CLAWCHAT_GROUP_MODE`, `CLAWCHAT_MEDIA_LOCAL_ROOTS` — override values in `platforms.clawchat.extra` at hermes-agent startup.
- `CLAWCHAT_ALLOWED_USERS`, `CLAWCHAT_ALLOW_ALL_USERS` — auth allowlist.
- `CLAWCHAT_DEVICE_ID` — override the auto-derived device id.
