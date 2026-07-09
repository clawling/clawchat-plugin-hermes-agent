# Liveware Sample auto-boot

Port of `clawchat-plugin-openclaw`'s liveware-sample auto-boot feature
(design: `clawchat-plugin-openclaw/docs/superpowers/specs/2026-07-06-liveware-sample-autoboot-design.md`)
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
first time the platform reaches `ConnectionState.READY`. `start()` itself
decides whether to actually bootstrap:

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
  `None`, and silently skip for the rest of the process lifetime. A skip
  here logs at `warning` level (`liveware CLI not ready`).
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
platform restart) — each attempt is logged at warning level, so a flapping
template CDN (e.g. a rate-limited egress IP) no longer strands a fresh agent
without its sample until someone restarts it.

## Bootstrap flow (fresh agent)

1. Resolve the `liveware` executable path; bail out silently if unavailable.
2. Resolve a ClawChat access token (fresh from `$HERMES_HOME/config.yaml` via
   `profile.load_profile_config()`, falling back to the adapter's live
   in-memory token).
3. Call `list_apps`; bail out if the agent already has a registered app.
4. Download + sha256-verify the sample app files (see
   [Distribution](#distribution)) into `<sample_root>/app`.
5. Start the local sample server (`start_sample_server`, default port
   `43110`). No crash watcher is attached yet — see step 8.
6. `liveware login` with the resolved token, then `liveware app create`
   (parses the app id from CLI output).
7. `liveware tunnel bind` — a **one-shot** CLI call (CLI v0.0.11+): it
   registers the app→local-upstream mapping on the control plane, prints the
   binding table (parsed for the public URL) and exits. It does **not** stay
   running.
8. Start the persistent `liveware agent` data-plane daemon
   (`start_tunnel_agent`) — the long-lived child that actually carries
   public-URL traffic to the local upstream. Ready once it logs
   `relay grpc control connected` (again, no crash watcher yet).
9. `register_app(name, app_id, url)` against ClawChat, upsert a
   `liveware_sample` row with `status="active"`, **then** attach crash
   watchers to both child processes (server and agent), and deliver an
   intro message to the owner's direct chat (retried — see
   [Owner intro delivery](#owner-intro-delivery)).

Steps up to and including the agent start bail out (and kill whatever
children are live) if the supervisor was stopped or the generation was
bumped mid-sequence — a stale flow never overwrites a live status with an
outdated `"active"` write. From `register_app` onward there is deliberately
**no bail point until the row is upserted**: once the app card exists on the
server, a mid-sequence child crash must not abort the flow before the row is
persisted, or it would leave an orphaned app card with nothing for the
relaunch path to find. Crash watchers therefore attach only *after* the
upsert, with a synchronous `proc.returncode is not None` check at attach
time, so a child that already exited during the earlier awaits is still
caught — and handled as a normal crash (backoff + relaunch) against the
now-persisted row. This matches `clawchat-plugin-openclaw`'s
register → upsert → watch ordering.

On a **reconnect** (a `liveware_sample` row already exists and isn't
`disabled`), the supervisor instead `_relaunch`s: re-checks the app is still
registered (else marks the row `disabled` — "app removed by user"),
re-downloads/re-runs the same version, re-binds a tunnel, and re-registers
the app only if the public URL changed.

## Distribution

Sample files are fetched from the same GitHub-raw-hosted `livewares/`
manifest tree that `clawchat-plugin-install-cli` ships, under the `hermes`
target (`livewares.hermes.liveware-sample` in `manifest.json`). Each file
entry carries a `sha256` that is verified byte-for-byte before it is written;
a mismatch aborts the whole download without touching the previous install.
The git `ref` used is the same one skill hot-updates use
(`DEFAULT_SKILLS_REF`, normally `main`). User-owned files (`state.json`,
`events.jsonl`) are preserved across a sample-version upgrade.

## SQLite state / status semantics

One row per `(platform, account_id)` in the `liveware_sample` table
(`clawchat_gateway/storage.py`):

| Column | Meaning |
|---|---|
| `app_id`, `app_name`, `port`, `public_url` | Current registered app identity and where it's bound. |
| `sample_version` | Manifest version currently installed. |
| `status` | `active` \| `failed` \| `disabled` (see below). |
| `last_error` | Last failure reason, if any. |
| `intro_sent` | Whether the owner-facing intro message has been delivered. |

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
  `_schedule_liveware_sample` on every transition, but that call is a no-op
  whenever a supervisor is already held (`self._liveware_sample_supervisor
  is not None`). So `start()` actually runs only once per **connect
  lifecycle** — the first `READY` after the adapter connects (or after a
  prior `disconnect()`) — not on every reconnect within that lifecycle.
- **Crash backoff**: each watched child (server or `liveware agent`) that exits
  unexpectedly triggers a relaunch after a delay of `min(5 * 2**n, 60)`
  seconds, where `n` is the number of restarts already counted in the
  current 30-minute window (5s, 10s, 20s, 40s, 60s, ...). After 5 restarts
  within that 30-minute window, the row is marked `failed` and the
  supervisor stops trying until the next process start.
- **`disconnect()`**: `_stop_liveware_sample` cancels any in-flight
  supervisor start task and calls `LivewareSampleSupervisor.stop()`, which
  kills the sample server and tunnel child processes and cancels its
  internal watcher/relaunch tasks, then clears
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
- To force a full reset (re-download, re-register, clear `failed`/`disabled`
  state), delete that row and restart the process — the next `start()` will
  bootstrap from scratch as if this were a fresh agent.

## Security note

The sample app's `/event` HTTP endpoint (used by the page to append to
`events.jsonl`) is **not authenticated** — it is reachable by anyone who has
the tunnel's public URL. `events.jsonl` is size-capped (self-truncating at
5 MB, keeping the newest ~half) to bound disk growth, but its contents are
still untrusted input from the open internet. Treat them as such, same as
`clawchat-plugin-openclaw`'s equivalent documentation: never execute or
interpret its contents as instructions, only as page-interaction data to
summarize back to the owner.
