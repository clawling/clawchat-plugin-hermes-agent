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
├── pyproject.toml               # Project metadata only (name: clawchat-gateway)
├── MANIFEST.in                  # Setuptools prompt inclusion rules
├── clawchat_gateway/            # All runtime modules — adapter, connection, protocol, tools, …
├── prompts/                     # Required platform.md + optional defaults
├── skills/                      # All bundled */SKILL.md skills (currently six) + manifest.json
├── tests/                       # Tracked by named .gitignore exceptions
├── uv.lock                      # Locked development and test environment
└── docs/                        # This documentation tree
```

## Setting up a local environment

The project is metadata-only and supports plain `pip` and `uv`. The tracked `uv.lock`
locks the development and test environment. Hermes installs the source tree
without using this lockfile; runtime requirements are in `pyproject.toml`.

### With `uv`

```bash
uv sync --extra test
```

### With `pip`

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[test]"
```

Without a `[build-system]` table, pip uses its setuptools PEP 517 fallback;
`-e` mirrors how Hermes imports the source tree. `uv` keeps the project
non-package via `tool.uv.package = false` and installs its dependencies.

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

## State files and the write gate

State files use `clawchat_gateway/atomic_file.py`, the repository's single
implementation of synced, atomic file publication. It preserves existing
permission bits by default and creates new files with private permissions.
Create-only seed publication and directory installation retain their specialized
publish steps and use the shared directory-sync helper.

Memory, managed skill manifest/publication, and plugin `.env` read-modify-write
operations hold `state_lock.state_file_lock` for the complete mutation.
Locks are reentrant within a thread and serialize cooperating threads and processes;
timeouts or unavailable locking fail closed by raising `StateLockError` before mutation.
Credential compare-and-write steps (logout clears, bootstrap token rotation) check the
persisted `.env` pair and write it inside one lock, so a concurrent re-pair is never
overwritten. Managed skill downloads happen before taking the manifest lock; state is
re-read under the lock before publishing.
Hermes' own `.env` writers do not acquire this plugin lock and remain outside its protection.

`tests/check_state_writes.py` scans production filesystem write calls.
`tests/test_state_write_gate.py` runs this gate. New writes either use the atomic
helper or have a counted `ALLOWLIST` entry with a B/C classification and a
specific reason. Import aliases are outside the static scanner's coverage.

Credential exit tests use `assert_no_secrets` from `tests/conftest.py` to check
caller results, exceptions, logs and report payloads against obvious fake values.
Redact credentials where values first become display text. Debug snapshots and
tool audits pass through `redact_display_data` before being written to disk;
message bodies are omitted from routing logs (only their lengths are recorded).
Parse errors that carry a source position (YAML) are shown as error class and
line/column only, never the quoted source.

## Live debugging against a Hermes process

The supported development loop is:

1. Activate a fresh Hermes Agent instance in the [supported range](install.md#compatibility).
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
matching Hermes Agent checkout in the [supported range](install.md#compatibility) and follow
[`./hermes-source-lookup.md`](./hermes-source-lookup.md). The path is
already in `.gitignore`; nothing committed depends on it.

## Writing docs

Keep each `.md` under 40 KB (hard cap 60 KB); a change that leaves a file above 40 KB splits it in the same PR or says why in the PR. Docs describe current behavior only; version history goes to git history and the GitHub release notes, or to a one-line-per-item `Upgrade notes` list at the end. Write each rule in full in one place and link to it elsewhere. When splitting or trimming, keep every heading, anchor and `§` number that something links to (leave a same-named short section that links on). `tests/test_tool_grants_tools.py` reads `docs/configuration.md`, `clawchat_gateway/device_id.py` points at it (warning text and its Device id durability section), and source comments and tests cite `docs/client-integration.md` by `§` number — update them together.

## Modifying the wire protocol

[`./client-integration.md`](./client-integration.md) is a plugin-side derivative
of the server's Protocol v2 contract; the server implementation takes precedence
if they disagree. Update the server contract first, then this copy and
update `clawchat_gateway/protocol.py` (outbound builders) and
`clawchat_gateway/inbound.py` (parsing). Mirror the same change in
`clawchat-plugin-openclaw/src/` — the two plugins are peers.

## Re-syncing client-integration.md

`client-integration.md` covers this plugin's Protocol v2 subset. The server owns
the wire contract and its implementation takes precedence; change the server
contract first, then this subset and the peer client implementations.

1. Read the provenance sync commit and pending-upstream list at the top of
   `client-integration.md`; retain that record rather than declaring a full sync.
2. Use the upstream guide at that commit as the merge base, its desired revision
   as upstream, and this subset as local. Three-way merge the relevant changes;
   never overwrite the subset with the full guide. Check server code on conflicts.
3. Preserve every existing heading and section number. Keep Hermes behavior in
   place under **Hermes:** and record which pending items were actually applied.
4. Subset whitelist: retain connection/envelope/routing, materialized messages,
   opt-in streaming, typing, metadata/notification/permission/result handling,
   reactions/recall/system messages, fragments, legacy replay, heartbeat/errors
   and media. Keep §9.2, §9.7, §11.4, §11.6 and §14.4 as stubs; §11.7 keeps only
   legacy dedup plus defensive truncation, §16 has no examples, §17 is Hermes-only.
   Exception: retain the Hermes: optional reliable_delivery_v2 subsection in §11.7.
   Do not restore full unsupported-frame semantics elsewhere in the guide.
5. Compare `connection.py:build_connect_capabilities` with the OpenClaw peer,
   honoring its “keep in sync with OpenClaw” comment; an upstream feature does
   not authorize adding its capability to either plugin.
   `reliable_delivery_v2` is an approved Hermes-only, opt-in exception;
   OpenClaw does not declare it.
6. Diff heading text, resolve all § references and verify constants/error names
   against both implementations. Update provenance without exposing private paths.

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
tracked files and fails on any critical finding. The shared
`tests/hermes_source.py` helper checks `HERMES_SOURCE_DIR` first, then
`tmp/hermes` or `tmp/hermes-agent` here and in the main checkout from a linked
worktree. Without the required source marker, the skip reason lists every
searched path; `HERMES_SOURCE_REQUIRED=1` turns these skips into failures.
The pytest header shows the source found (or searched paths) and whether the
session uses real Hermes or the double; the session stays on the double unless
`HERMES_REAL_HOST=1` (most tests are written against it). Exception: when Hermes
already imports from `PYTHONPATH` / `sys.path`, `tests/conftest.py` uses the real
host even without `HERMES_REAL_HOST=1`.
`tests/test_multiplex_isolation.py` is the exception: it needs a Hermes
checkout on `PYTHONPATH` and runs on its own
([multiplex-profile-isolation.md](multiplex-profile-isolation.md)).
Run with a source present before a release. The scan also runs each pinned older Hermes scanner, so the checkout
needs those tags (`git fetch --tags`). Hermes v0.20.3–v0.20.6 are an
[unsupported range](install.md#compatibility), so their scanners are not pinned
by these tests. Prose counts too: older scanners rate `host` followed
later on the same line by a `$` as DNS exfiltration, so keep "host" and a
`$VARIABLE` off the same line.

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

No wheel is built or published; Hermes copies every tracked file, including
`skills/`, into the plugin directory and imports from there. The project has
no build backend because Hermes 0.22+ merges every profile's plugins into one
uv workspace and only gives metadata-only members unique names; a backend
would make multiple copies collide on `clawchat-gateway` (see
[workspace-name troubleshooting](./install.md#duplicate-workspace-member)).

The retained setuptools configuration only includes `clawchat_gateway*`, and
`MANIFEST.in` only includes `prompts/*.md`. The top-level `skills/` tree — all
bundled `skills/*/SKILL.md` directories (currently six; see
[skill updates](./skill-updates.md#bundled-ids-are-auto-discovered-not-hardcoded))
and `skills/manifest.json` — lives outside the package. A future wheel would
need packaging rework to include that tree and, if its layout changes, adjust
`bundled_skills_dir()` so skill discovery and the tombstone sweep still work.

## Branches and releasing

`hermes plugins install clawling/clawchat-plugin-hermes-agent` installs from
this repository's `main` branch, not from a tag or a package index. **Merging
into `main` is therefore the publication itself**: from that moment new
installs and reinstalls get the new code. Hermes is the documented exception
to **tag before publish**. Create and push the annotated tag immediately
after the release PR merge, in the same sitting, before doing anything else
or fast-forwarding `pre-release`; the tag records what was served. Any later
build or publish must use a clean checkout of that tag.

Everything reaches `main` through a GitHub PR; nothing is pushed to `main`
directly.

- Day-to-day, non-urgent changes are merged into the `pre-release` branch, not
  `main`, also through a GitHub PR (`--base pre-release`) that is
  squash-merged. `main` only moves when a release is cut, so it always equals
  the newest released version.
- An urgent fix (security, widespread connection failure) may target `main`
  directly with a squash-merged PR that includes the version bump; tag it like
  any release and afterwards merge `main` back into `pre-release`.

A release runs in this fixed order. Run the commands from the repository root
in Bash, using a version-bump topic branch based on the latest `origin/pre-release`.
Dev-only images and deployments are not releases and are exempt from this order.

1. **Prepare the version bump for `pre-release`.** The plugin reports it to the server
   at pairing, so every release bumps both `clawchat_gateway/__init__.py`
   `__version__` and `plugin.yaml` `version` in lockstep; the `-N` suffix is
   the release counter, e.g. `0.14.0-31` →
   `0.14.0-32`. Recent release commits are titled
   `chore(release): <old> -> <new>`. After editing both fields, set the intended
   version and display the values:

   ```bash
   read -r -p 'New version (without v): ' release_version &&
     test -n "$release_version" || exit 1
   rg -n '^(__version__ =|version:)' clawchat_gateway/__init__.py plugin.yaml || exit 1
   ```

   **Manually check that both displayed values equal `$release_version` and
   differ from the previous release; stop if any value is wrong.** The tracked
   tests do not compare these two fields, so this check is required. Then run
   the suite with its optional test dependencies (including the install scanner
   with a Hermes source checkout, as described above):

   ```bash
   uv run --extra test pytest || exit 1
   ```

   Commit and push the bump before opening its PR, then squash-merge it into
   `pre-release`. These commands assume the topic branch contains only the
   intended release changes; review the diff before committing:

   ```bash
   git diff -- clawchat_gateway/__init__.py plugin.yaml || exit 1
   git add clawchat_gateway/__init__.py plugin.yaml &&
     git commit -m "chore(release): $release_version" &&
     git push -u origin HEAD || exit 1
   bump_pr=$(gh pr create --base pre-release --title "chore(release): $release_version" \
     --body "Bump runtime and manifest versions to $release_version.") || exit 1
   gh pr merge "$bump_pr" --squash || exit 1
   ```

2. **Merge `pre-release` into `main` through a PR, with a merge commit** —
   this is the moment the release goes live. First fetch and repeat the manual
   version check on the remote branch. Stop unless both values equal
   `$release_version`; do not open the release PR until the bump PR is merged:

   ```bash
   git fetch origin || exit 1
   git show origin/pre-release:clawchat_gateway/__init__.py || exit 1
   git show origin/pre-release:plugin.yaml || exit 1
   ```

   ```bash
   release_pr=$(gh pr create --base main --head pre-release --title "release: $release_version" \
     --body "Release $release_version from pre-release.") || exit 1
   gh pr merge "$release_pr" --merge || exit 1
   ```

3. **Tag the merge commit on `main` right away and push the tag.** The tag
   name equals the version, without a `v`:

   ```bash
   release_commit=$(gh pr view "$release_pr" --json mergeCommit --jq .mergeCommit.oid) || exit 1
   git fetch origin --tags || exit 1
   git merge-base --is-ancestor "$release_commit" origin/main || exit 1
   ```

   Stop if any command fails. Use the PR's merge commit because the floating
   `origin/main` tip may already include a later merge. Tag that exact commit
   and push the tag before fast-forwarding `pre-release`:

   ```bash
   git tag -a "$release_version" "$release_commit" -m "Release $release_version" &&
     git push origin "refs/tags/$release_version" || exit 1
   git push origin "$release_commit:refs/heads/pre-release" || exit 1
   ```

   Installers never read the tag. It records exactly which `main` commit was
   served as that version and is the reference for rollback and for anything
   built from this release later (always build from a clean checkout of the
   tag, never from a branch). A merge into `main` without a version bump and
   a tag ships code that no version number names.

4. **Record the release.** Create a GitHub Release for the pushed tag, with
   the version as its title and the exact commit in its notes:

   ```bash
   gh release create "$release_version" --verify-tag --title "$release_version" \
     --notes "commit: $release_commit" || exit 1
   ```

   Add later facts with `gh release edit`, never by moving the tag.

Never tag a commit that is not on `main`. Never move or delete a pushed release
tag; if a release needs a correction, use a new version and a new release PR.
Tag messages, PR titles and PR bodies are public like commit messages.

**Open item: pin installs to a tag.** Installs currently follow `main`, so
the release moment cannot be separated from the merge, and a bad merge
reaches new installs at once. Investigate whether `hermes plugins install`
can install a tag/ref (for example, an `@<tag>` ref), so installs could pin
to the release tag and `main` merges would stop being publications. Until
then, the exception above applies.
