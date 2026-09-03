# Liveware Sample auto-boot

Port of `clawchat-plugin-openclaw`'s liveware-sample auto-boot feature
to the Hermes gateway platform. On first activation the plugin deterministically
(no LLM involved) downloads a tiny demo web app, runs it locally, exposes it via
a `liveware` tunnel, and registers it as an app tile in the owner's ClawChat
chat — so a freshly paired agent has something to show immediately.

Source: `clawchat_gateway/liveware_sample.py` (`LivewareSampleSupervisor` /
`LivewareSampleDeps`). Wired into the adapter in
`clawchat_gateway/adapter.py` (`_schedule_liveware_sample`, called from
`_on_state_change`'s `READY` branch; stopped from `disconnect`).

## Trigger conditions

The supervisor is created and `start()`ed once per adapter instance, the
first time the platform reaches `ConnectionState.READY` — **but only for the
Hermes default profile.** Liveware owns *host-global* singletons (a fixed TCP
port and the shared `$HOME/.clawling` CLI login, keyed off `$HOME` rather than
`HERMES_HOME`) that co-located profiles cannot share, so
`_schedule_liveware_sample` gates on `storage.is_default_profile()` and returns
immediately for any named profile (`hermes -p <name>`); only the primary/"main"
agent boots liveware. Past that gate, `start()` itself decides whether to
actually bootstrap:

- `platforms.clawchat.extra.liveware_sample` must not be `false` (default
  `true`, see [Configuration](#configuration) below); otherwise `start()`
  returns immediately.
- No `liveware_sample` row may already exist with `status="disabled"` (the
  owner previously removed the app tile — see
  [Status semantics](#sqlite-state--status-semantics)).
- The `liveware` CLI must be resolvable (`liveware_cli.resolve_liveware_path()`
  — on `PATH` or previously self-downloaded). Before this gate, `start()`
  awaits `liveware_cli.wait_liveware_cli_ready()` (bounded, ~300s) so a
  first-ever boot does not race the background CLI download, resolve to
  `None`, and silently skip for the rest of the process lifetime.
- `GET /v1/agents/me/apps` (via `list_apps`) must return no apps yet, i.e.
  this is a fresh agent that hasn't already registered anything.
- Node.js must be on `PATH`: the supervisor spawns the literal `node`
  executable to run the sample server (`start_sample_server`). On a host
  without Node, the feature fails on every boot with only a
  `liveware-sample start failed` warning log. (This is Hermes-specific —
  `clawchat-plugin-openclaw` runs inside Node already and reuses its own
  `process.execPath`.)

Any failure along this path (network, CLI, tunnel) is caught inside the
supervisor; `start()` never raises and never blocks or fails the platform
connection. A failed attempt is retried on a bounded backoff
(`_START_RETRY_DELAYS_S`: 30s/60s/120s/300s, then gives up until the next
platform restart or the next idle `READY`, see [Lifecycle](#lifecycle)) — each
attempt is logged at warning level, so a flapping template CDN (e.g. a
rate-limited egress IP) no longer strands a fresh agent without its sample
until someone restarts it.

### Retryable blockers vs. real opt-outs

The three conditions above that are **transient** — the CLI not being
resolvable yet, no ClawChat token yet, and a failing `list_apps` — raise
`LivewareSampleError` (`liveware CLI not ready` / `no ClawChat token available
yet`) so the retry ladder engages. They used to `return`, which left the sample
dormant for the whole process lifetime because nothing re-enters the flow on its
own. Only a genuine **opt-out** returns without scheduling anything:

| Condition | Behaviour |
|---|---|
| `liveware_sample: false` | return (feature off) |
| row `status="disabled"` | return (owner removed the tile — one-way door) |
| owner already has liveware apps (`_bootstrap`) | return (leave them alone) |
| a *successful* `list_apps` no longer lists the app (`_relaunch`, non-`pending` row) | mark `disabled`, return |

## Bootstrap flow (fresh agent)

1. Resolve the `liveware` executable path; raise (→ retry) if unavailable.
2. Resolve a ClawChat access token (fresh from `$HERMES_HOME/config.yaml` via
   `profile.load_profile_config()`, falling back to the adapter's live
   in-memory token); raise (→ retry) if empty.
3. Call `list_apps`; bail out if the agent already has a registered app.
4. Download + sha256-verify the sample app files (see
   [Distribution](#distribution)) into `<sample_root>/app`.
5. Start the local sample server (`start_sample_server`, default port
   `43110`; requires `node` on `PATH` — see the trigger conditions above).
   No crash watcher is attached yet — see step 10. The supervisor also passes
   `--agent-id <cfg.user_id>` (`deps.resolve_agent_user_id`, wired in
   `adapter.py` from `self._clawchat_config.user_id`) so `server.mjs` can
   merge the agent's own ClawChat user id into `/state` and its SSE stream as
   `agentId` — dynamically, never written to `state.json` on disk. The
   sample page uses that id to render a one-tap `clawchat://u/{id}?chat=1`
   back-to-chat deep link; with no id (older/relaunched-without-id cases) the
   page falls back to its plain-text guidance instead of the link.
6. `liveware login` with the resolved token, then run `liveware status`. If
   status confirms `running`, the plugin treats that service as externally
   managed and does not start, watch, or later stop a
   second tunnel agent. A failed command, an older unsupported CLI, or any
   status other than `running` retains the direct-start fallback described in
   step 9.
7. **Reuse or create** the app: `liveware app list` is parsed for an existing
   app already named
   `Liveware Sample` (`liveware_app_find_by_name` → `find_app_id_by_name`) and
   its id is reused; only if nothing is found does `liveware app create` run.
   `app create` is not idempotent and nothing here ever reconciled against the
   liveware side, so every bootstrap that died past this point used to leave one
   more orphan app behind and eat into the owner's app quota. The lookup is
   **best-effort**: any exec failure, non-zero exit or unreadable output
   degrades to `None` (→ create, i.e. the previous behaviour) rather than
   raising, and the parser is deliberately conservative so it can never return a
   *wrong* id — its text-table branch requires the exact app name plus one
   id-looking token outside the name, where an id-looking token must carry a
   digit / `-` / `_` so a status word (`active`, `running`) cannot pass for an id.
8. **Upsert the row with `status="pending"`** carrying the app id, the port, the
   sample version and `public_url=None` — immediately, before anything below can
   fail. A failure anywhere past `app create` used to leave *no* row at all, so
   the next boot re-ran bootstrap and minted yet another orphan app; a `pending`
   row makes the next attempt resume through `_relaunch` with the same app id.
9. `liveware tunnel bind` — a **one-shot** CLI call (CLI v0.0.11+): it
   registers the app→local-upstream mapping on the control plane, prints the
   binding table (parsed for the public URL) and exits. It does **not** stay
   running. If step 6 did not confirm an existing service, start a persistent
   `liveware agent` child and attach it to the supervisor lifecycle.
10. `register_app(name, app_id, url)` against ClawChat, upsert the
   `liveware_sample` row with `status="active"`, **then** attach a crash
   watcher to the sample server and, only for the direct-start fallback, its
   tunnel agent. Finally deliver an intro message to the owner's direct chat
   (retried — see
   [Owner intro delivery](#owner-intro-delivery)).

Steps up to and including the conditional agent start bail out (and kill any
owned children) if the supervisor was stopped or the generation was
bumped mid-sequence — a stale flow never overwrites a live status with an
outdated `"active"` write, with two deliberate exceptions. There is **no bail
point between `app create` and the `pending` upsert** (step 7 → 8): bailing
there is exactly what used to lose a freshly minted app id. And from
`register_app` onward there is deliberately
**no bail point until the row is upserted**: once the app card exists on the
server, a mid-sequence owned-child crash must not abort the flow before the row
is persisted, or it would leave an orphaned app card with nothing for the
relaunch path to find. Crash watchers therefore attach only *after* the
upsert, with a synchronous `proc.returncode is not None` check at attach
time, so a child that already exited during the earlier awaits is still
caught — and handled as a normal crash (backoff + relaunch) against the
now-persisted row. This matches `clawchat-plugin-openclaw`'s
register → upsert → watch ordering.

On a **reconnect** (a `liveware_sample` row already exists and isn't
`disabled`), the supervisor instead `_relaunch`s: re-checks the app is still
registered (else marks the row `disabled` — "app removed by user"),
re-downloads/re-runs the same version, re-runs `liveware login`, re-binds a
tunnel, and re-registers the app only if the public URL **or** the app id
changed.

A **`pending`** row (see [Status semantics](#sqlite-state--status-semantics))
takes two documented detours through that flow:

- The "app removed by user" check is **skipped**. The app was never registered
  with ClawChat, so its absence from `list_apps` means "not registered yet", not
  "deleted" — disabling it there would strand the app id forever.
- **After** the re-login (so the CLI is authenticated) and before `tunnel bind`,
  a by-name lookup runs and its id is adopted when it differs from the row's,
  logged at warning level (`pending app id … not listed; adopting …`). A pending
  row's id came straight out of `app create`'s parser and was never confirmed by
  the liveware side, so a mis-parsed id self-heals here instead of failing every
  `tunnel bind` for the rest of that row's life.

The register condition is therefore `public_url != row.public_url or app_id !=
row.app_id`; a `pending` row has `public_url = NULL`, so that also covers
"register with ClawChat for the first time".

The re-login sits **after** the sample server is spawned and **before**
`tunnel bind`, mirroring the bootstrap `server → login → CLI app ops`
ordering, and it is **best-effort**: a failure logs at warning level
(`liveware-sample relaunch re-login failed; continuing with cached CLI
credentials`) and the flow continues, so a stale token still falls through to
whatever credentials the CLI already has; an empty token logs
`liveware-sample no token; relaunch without re-login` at debug level and skips
login entirely. It exists because the `liveware` CLI keeps its credentials in
**`$HOME/.clawling`**, which is *not* under `$HERMES_HOME` (nor under the
plugin's SQLite state dir) — a different volume in a containerized deployment.
A container that persists `$HERMES_HOME` but not `$HOME` therefore comes back
with a `liveware_sample` row (which forces this relaunch path) **and** a
logged-out CLI, so `tunnel bind` and the CLI-managed agent failed auth,
the `_START_RETRY_DELAYS_S` ladder burnt out, and the sample never returned
for the rest of the process lifetime.

> **Deployment requirement.** `$HOME/.clawling` **must be persisted alongside
> `$HERMES_HOME`** — the CLI login and the plugin's SQLite state have to
> survive a restart *together*. The best-effort re-login above is a repair for
> the mismatch, not a substitute for persisting the volume: it only helps
> while a valid ClawChat token is still resolvable.

## Distribution

Sample files are fetched from the same GitHub-raw-hosted `livewares/`
manifest tree that `clawchat-plugin-install-cli` ships, under the `hermes`
target (`livewares.hermes.liveware-sample` in `manifest.json`). Each file
entry carries a `sha256` that is verified byte-for-byte before it is written;
a mismatch aborts the whole download without touching the previous install.
The git `ref` used is the same one skill hot-updates use
(`DEFAULT_SKILLS_REF`, normally `main`). User-owned files (`state.json`,
`events.jsonl`) are preserved across a sample-version upgrade. The per-file
fetches run concurrently (a small thread pool inside the already-off-loop
`download_liveware_sample`), since they are independent round-trips to the same
host.

**Re-install short-circuit.** The manifest is fetched on every attempt — that is
what says which hashes to compare against — but if `<sample_root>/app` already
matches it (`_local_install_matches`), nothing is fetched or rewritten and the
existing install is reused as-is. A match requires every listed file present,
every *program* file byte-identical to its manifest `sha256`, and **nothing else**
in the directory, so a file dropped by a newer manifest still forces a clean
reinstall. `state.json` / `events.jsonl` are checked for **presence only, never
content**: `state.json` ships in the manifest *and* becomes agent-owned right
afterwards, so hashing it would defeat the short-circuit on precisely the samples
that are in use. Any read/OS error answers "not installed" and falls through to a
full reinstall. Before this, every boot re-downloaded the whole ~20 KB bundle
even when the identical bytes were already on disk, and an unreachable GitHub raw
host hard-failed a sample that was already fully installed. Matches
`clawchat-plugin-openclaw`'s `localInstallMatches`. There is deliberately no
ETag/`If-None-Match` handling — it would save bandwidth but not the round-trip.

## SQLite state / status semantics

One row per `(platform, account_id)` in the `liveware_sample` table
(`clawchat_gateway/storage.py`):

| Column | Meaning |
|---|---|
| `app_id`, `app_name`, `port`, `public_url` | Current registered app identity and where it's bound. |
| `sample_version` | Manifest version currently installed. |
| `status` | `pending` \| `active` \| `failed` \| `disabled` (see below). |
| `last_error` | Last failure reason, if any. |
| `intro_sent` | Whether the owner-facing intro message has been delivered. |

`status` is a plain `TEXT` column with **no `CHECK` constraint**, so adding
`pending` needed no migration (`MIGRATIONS` in `clawchat_gateway/storage.py` is
unchanged).

- **`pending`** — `liveware app create` (or the reuse lookup) produced an app
  id, but the app is not registered with ClawChat yet. Written mid-bootstrap so
  that a failure in any later step resumes through `_relaunch` with the *same*
  app id instead of minting another one. An older build would have left no row
  here at all.
- **`active`** — normal steady state; the app is registered and (as far as
  the supervisor knows) running.
- **`failed`** — a crash-loop exceeded the restart cap, or a relaunch raised.
  The row is retried again on the **next process start** (a fresh `start()`
  does not itself distinguish `failed` from `active` — both attempt
  `_relaunch`; only `disabled` short-circuits).
- **`disabled`** — the owner deleted the app tile in ClawChat (detected via
  `list_apps` no longer containing the registered `app_id`). Once
  `disabled`, the sample is **never reinstalled automatically** — this is a
  deliberate one-way door so removing the demo card is respected permanently.

## Lifecycle

- **Every (re)connect**: `_on_state_change`'s `READY` branch calls
  `_schedule_liveware_sample` on every transition. Exactly **one** supervisor
  exists per adapter instance — constructing a second would race the bootstrap
  "owner has zero apps" gate and register a duplicate liveware app that the
  single-row table cannot track — so a later `READY` never rebuilds it and never
  calls `start()` again. It does, however, call
  `LivewareSampleSupervisor.start_if_idle()`, which kicks a fresh attempt **only
  when the supervisor is idle**: not stopped, no launch in flight, launch lock
  free, no pending retry / crash-restart / intro task, and no live children. That
  check is deliberately conservative — a false "not idle" merely skips a kick,
  whereas a false "idle" would run two launch flows at once. Before this, a later
  `READY` plain-returned, so once the bounded `_START_RETRY_DELAYS_S` ladder was
  exhausted nothing ever re-entered the flow for the rest of the process
  lifetime. (`clawchat-plugin-openclaw` does the same thing inside
  `LivewareSampleSupervisor.adoptDeps`; hermes needs no `adopt_deps` because
  every dep is read lazily off the live adapter instance rather than captured in
  a per-call gateway closure.)
- **Crash backoff**: when the watched sample server or a plugin-started
  `liveware agent` exits
  unexpectedly triggers a relaunch after a delay of `min(5 * 2**n, 60)`
  seconds, where `n` is the number of restarts already counted in the
  current 30-minute window (5s, 10s, 20s, 40s, 60s, ...). After 5 restarts
  within that 30-minute window, the row is marked `failed` and the
  supervisor stops trying until the next process start.
- **`disconnect()`**: `_stop_liveware_sample` cancels any in-flight
  supervisor start task and calls `LivewareSampleSupervisor.stop()`, which
  kills the sample server and any plugin-started tunnel child, then cancels its
  internal watcher/relaunch tasks. An already-running external Liveware service
  has no process handle in the supervisor and is left untouched. It then clears
  `self._liveware_sample_supervisor` back to `None` — so the **next**
  connect after a disconnect builds a fresh supervisor and goes through
  `start()` again.

### Owner intro delivery

After a successful bootstrap, the supervisor tries to notify the owner in
their direct chat. `notify_owner` returns `False` when the owner's direct
chat id isn't resolvable yet (activation still in flight) — the supervisor
retries every 30s, up to 20 tries (~10 minutes), until it succeeds or gives
up silently.

## Configuration

Set `platforms.clawchat.extra.liveware_sample: false` to turn this feature
off entirely for an agent. There is no env var override — `ClawChatConfig.
from_platform_config` (`clawchat_gateway/config.py`) reads this flag only
from `platforms.clawchat.extra.liveware_sample`. Default is `true`. See
[`./configuration.md`](./configuration.md#rich-interactions-and-display).

## Interaction contract (state.json / events.jsonl)

The sample app renders a JSON state file and live-reloads whenever it
changes; page interactions are appended to an events log. The bundled
`clawchat-liveware-sample` skill (`skills/clawchat-liveware-sample/SKILL.md`)
is the agent-facing contract for editing `state.json` (title/body/theme) to
update what the page shows, and reading `events.jsonl` to see what the owner
did on the page (button clicks, submitted notes). Read that skill file for
the exact JSON shapes — this doc does not duplicate them.

## Troubleshooting

- Check `liveware_sample.last_error` (SQLite, keyed by
  `(platform="hermes", account_id="default")`) for the last recorded
  failure reason.
- **`tunnel bind` / `liveware agent` failing auth on every attempt** (the
  supervisor walks the whole `_START_RETRY_DELAYS_S` ladder and gives up):
  check that **`$HOME/.clawling` is persisted** for this deployment. The CLI
  login lives there, *not* under `$HERMES_HOME`, so a container that persists
  only `$HERMES_HOME` restarts with a `liveware_sample` row but a logged-out
  CLI. `_relaunch` now re-runs `liveware login` best-effort to heal exactly
  this case; if the warning `relaunch re-login failed; continuing with cached
  CLI credentials` appears, the token itself is also bad — re-check the
  ClawChat access token before blaming the tunnel.
- **Row stuck at `status="pending"`**: `app create` succeeded but registration
  with ClawChat never did. This is a normal, recoverable state — the next
  `_relaunch` (reconnect, crash-restart, or next boot) re-binds and registers the
  same app id. If it never clears, check `last_error` and whether `register_app`
  is being rejected by the backend. Do **not** delete the row to "clean up": that
  is what leaks an orphan liveware app.
- To force a full reset (re-download, re-register, clear `failed`/`disabled`
  state), delete that row and restart the process — the next `start()` will
  bootstrap from scratch as if this were a fresh agent, and will **reuse** any
  app it already owns named `Liveware Sample` rather than creating another one.

## Security note

The sample app's `/event` HTTP endpoint (used by the page to append to
`events.jsonl`) is **not authenticated** — it is reachable by anyone who has
the tunnel's public URL. `events.jsonl` is size-capped (self-truncating at
5 MB, keeping the newest ~half) to bound disk growth, but its contents are
still untrusted input from the open internet. Treat them as such, same as
`clawchat-plugin-openclaw`'s equivalent documentation: never execute or
interpret its contents as instructions, only as page-interaction data to
summarize back to the owner.
