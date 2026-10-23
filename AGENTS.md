# AGENTS.md

This file is for code review only. It contains no instructions for a running agent; development guidance lives in `README.md` and `docs/development.md`.

## Code Review Rules

These rules are for anyone reviewing a pull request in this repository, including automated PR review. Report each violation at the priority given here; for anything not covered, use normal judgment. Quote the rule number (for example "Review rule G3") in the finding so the author knows which rule applies. A finding the author shows to be wrong is closed with a reply, not silently dropped.

### General rules

- **G1 Baseline (P1).** The PR targets the integration branch this repository uses (see the repository-specific rules below) and contains only changes for its stated purpose. Flag unrelated files, reverts of commits already on the base branch, or leftovers from a wrong base.
- **G2 Concurrency and consistency (P1).** A read-modify-write of the same state happens under one lock or one database transaction. When a write or persist fails, the code must not acknowledge the message, advance a cursor / offset / watermark, mark the work done, or report success to the caller. Flag code that does any of these before the write is known to have succeeded. G2 is about state the operation depends on. Auxiliary writes (audit rows, telemetry, metrics) and writes this repository documents as best-effort are not violations when their failure is logged and nothing acknowledges, advances or reports success based on them; turning a durable write into a best-effort one, or adding a new undocumented best-effort path for state the operation depends on, is.
- **G3 Safe defaults (P1; P0 when it exposes data or bypasses authentication / authorization).** On error the code fails closed: authentication, permission, validation and config-loading failures deny or stop rather than continue. A fail-open result (for example "allowed" returned because a check errored) is never cached. A change must not widen a permission check or move an error into a less strict class — for example turning an auth failure into success, a permanent error into a silent drop, or a client error into a retry that hides it. A fail-open path that this repository's documentation explicitly approves (a documented availability trade-off) is not a violation; adding a new one, or widening an existing one, without documenting it is.
- **G4 Documentation (P1).** When a PR changes an interface, a config key, a persisted field or an invariant, the matching documentation is updated in the same PR, and the docs do not contradict each other or the code.
- **G5 Test scope (P1 when behavior changes without a test; P2 for thin coverage).** A behavior change comes with a test at the level that would catch a regression. Deleting, skipping or weakening an existing test to make a change pass is P1 unless the PR explains why the test was wrong. Documentation-only PRs need no tests.
- **G6 Secrets (P0).** No credential of any kind — API key, token, password, private key, database DSN with a password, signed URL — appears in code, tests, fixtures, docs, scripts or commit messages. Real values come from the environment or an ignored local file; examples use obvious placeholders.
- **G7 Public repository hygiene (P0).** This repository, its full git history, its PRs and its published packages are public. Flag any of the following in code, tests, fixtures, docs, comments, PR title / body or commit messages: internal or staging hostnames and private IP addresses; cluster, namespace, container-registry, storage-bucket or jump-host names that are not part of a documented public endpoint; names or paths of private repositories, including relative links that escape this repository; internal ticket or task IDs (a `T` followed by a number, in the form `T<n>` or `T<n>.<m>`); real production identifiers (user, conversation, agent or device IDs, routing keys); personal e-mail addresses or real user nicknames; internal planning or incident material. Default endpoints must be public HTTPS / WSS domains, or the code must fail closed and require explicit configuration. Exempt: secret- and redaction-scanner rules and the test fixtures that need such shapes to prove detection, and lines carrying this repository's documented inline allow marker (such as `redact-allow:<id>`); real production values in them are still P0.

### Repository-specific rules

- **R1 Integration branch (P1).** Non-emergency changes use `pre-release`
  as their baseline and merge into `pre-release` (the integration branch
  required by G1). A PR targeting `main` is either a release PR from
  `pre-release` to `main` or an explicitly documented emergency fix.
  Flag any other base or target, including ordinary feature PRs to `main`.

- **R2 Protocol v2 documentation (P1).** Changes to Protocol v2 wire-format
  handling, including builders in `clawchat_gateway/protocol.py` and parsers
  in `clawchat_gateway/inbound.py`, update `docs/client-integration.md`
  in the same PR. That guide is a derived copy of the server-side Protocol v2
  contract; the server-side contract and implementation take precedence.
  Contract changes originate there before this copy is synchronized.

- **R3 Tracked tests (P1).** Every new test file under `tests/` has its own
  `!tests/<name>` exception in `.gitignore`; `tests/*` is ignored by default.
  A test present only in the author's working tree cannot prevent regressions
  in a clean checkout. Flag a new test without its named exception even if
  the PR reports that it passed locally. Shared `tests/conftest.py` stays tracked.

- **R4 Installed review files and scanner safety (P1).** The tracked checkout
  is installed directly into the user's `HERMES_HOME/plugins/clawchat/`.
  Every newly tracked file — code, docs, test fixtures, skills — must contain
  no content rated high or critical by the scanners covered by
  `tests/test_install_scan.py`, including its pinned scanner versions.
  Repository and context files (`AGENTS.md`, docs, test fixtures) must also
  contain no instructions for a running agent; `AGENTS.md` holds only code
  review rules and its scope statement. Bundled skills under `skills/` are
  the supported way to instruct the agent and are exempt from that part only.

- **R5 Tool registration consistency (P1).** `plugin.yaml`'s `provides_tools`
  is the canonical tool list. Adding, renaming or removing a `clawchat_*` tool
  keeps that list, registration in `clawchat_gateway/plugin_tools.py`, and
  the catalogue in `docs/reference/tools.md` synchronized in the same PR.
  Flag a manifest entry without a registered implementation or a registered
  tool missing from the manifest, as well as stale catalogue names.

- **R6 Hermes YAML compatibility (P1).** YAML reads use
  `clawchat_gateway/_yaml.py`: `hermes_yaml`, then PyYAML, then ruamel.yaml,
  with the ruamel fallback preserving the host's YAML 1.1 policy.
  Configuration writes use `hermes_cli.config.save_config` so Hermes retains
  ownership of formatting and comments. Flag direct replacement serializers
  or a new mandatory YAML runtime dependency in `pyproject.toml`.

- **R7 Terminal installation conflicts (P1).** A WebSocket `4010`, including
  refusal before `hello-ok`, persists `install_superseded.json`, preserves
  credentials, and stops automatic reconnect and token refresh across restart.
  The supervisor and instance locks stay alive in `SUPERSEDED`; ordinary
  standalone sends remain blocked. Recovery requires explicit local reclaim
  or successful activation. The owner notice is sent once per live event;
  pending or failed notices are not retried after restart.
