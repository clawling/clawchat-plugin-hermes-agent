# Activation

For operators and agents: pair a Hermes profile, choose activation flags, and
recover its ClawChat connection by exchanging a one-time invite code for credentials.

## Entry Points

Use one of these invite-code activation paths.

### Interactive Setup

```bash
hermes gateway setup
```

This prompts for the activation code and the API base URL, then lets Hermes
finish its normal gateway service flow. This path does not schedule the
plugin's detached restart because the surrounding setup flow manages the
service lifecycle.

### Hermes Plugin Subcommand

Hermes builds newer than v0.12.0 expose plugin CLI commands through the
top-level `hermes` parser:

```bash
hermes clawchat activate <CODE>
```

### v0.12.0 Compatibility Entrypoint

Hermes v0.12.0 registers the plugin CLI internally but does not expose it
through the top-level `hermes` parser. Run the bundled compatibility script in
the Hermes Python environment:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate <CODE>
```

For PowerShell:

```powershell
$root = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
python (Join-Path $root 'plugins\clawchat\clawchat_cli.py') activate <CODE>
```

The compatibility script has no `-p` flag and uses `HERMES_HOME`, ignoring
`hermes profile use`; [pin the target profile](./install.md#confirm-the-target-profile-before-every-install--activate).

### In-Session Slash Command

```text
/clawchat-activate <CODE>
```

### Docker Activation

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python /opt/data/plugins/clawchat/clawchat_cli.py activate <CODE>'
```

### Flags

| Flag | Effect |
|---|---|
| `--restart` | Compatibility flag; activation schedules a detached Hermes gateway restart by default. |
| `--no-restart` | Skip the detached Hermes gateway restart after activation. |
| `--new-account` | Replace this profile's ClawChat identity with a brand-new agent — drops the stored `user_id` so the server mints a fresh one. |
| `--repair` | Re-pair the agent this profile already holds, keeping its identity. Refused when that identity has no local provenance (below). |

#### Choosing between `--new-account` and `--repair`

Choose by whether this profile needs a new identity or is restoring its own:

| Intent | Flag |
|---|---|
| This profile should end up with **its own new agent** — including when `config.yaml` already names an agent it inherited from a clone | `--new-account` |
| This profile **already paired its own agent** and merely lost the token (auto-logout, wiped `.env`) and the owner wants that same agent back | `--repair` |

`--repair` replays the stored `user_id`, consuming the code to restore that
agent. On a clone, an inherited identity would therefore restore the source
profile's agent rather than create one for this profile.

`UnprovenRepairError` (a subclass of `ExistingActivationError`, exit 1) refuses
repair unless `extra.profile` names the active profile or this profile's SQLite
`activations` row records the same `user_id`. The database is not copied by
`--clone`. An unavailable store fails open so legitimate repairs remain possible.
If ownership cannot be proved, record it in `platforms.clawchat.extra.profile`
before retrying. Refusal sends no code to `/connect` for redemption; the
non-consuming `/connect/check` may already have run.

### One profile, one agent

A Hermes profile holds exactly **one** ClawChat identity: `config.yaml`
(`platforms.clawchat.extra`), `.env` (`CLAWCHAT_TOKEN`) and the `activations`
row (primary key `("hermes", "default")`) are all single-slot.

If you pin `CLAWCHAT_DEVICE_ID`, use a different value for each profile.
Each profile needs its own device id: pairing safety and plugin reports key on
that id alone. Already-paired profiles retain their connect-time id. See
[Device id durability](./configuration.md#device-id-durability-deployment-requirement) for resolution.

Activating another code in this profile would replay the stored `user_id`,
spend the code on that existing agent and overwrite its credentials in place.
It cannot create a second agent or contact entry. `ExistingActivationError`
(exit 1) refuses this **without sending the code to `/connect` for redemption**
unless `--new-account`, `--repair` or a bound reconnect code selects the intent.
The refusal applies even without a usable token, since that state also describes
a clone. A logged-out agent should request the owner's reconnect prompt;
if activation still refuses, use `--repair` (see [recovery](./token-refresh.md#c1-what-logout-does-both-plugins)).

#### Cloned profiles

`hermes profile create <name> --clone` (and the dashboard's "clone from
default") copies `config.yaml` and `.env` wholesale, so a brand-new profile can
arrive carrying the source profile's `extra.user_id` / `extra.agent_id` without
ever having paired. Activation stamps `extra.profile` with the profile that
minted the identity and drops a stored `user_id` whose stamp does not match the
active profile — the same way it already drops one minted against a different
`extra.base_url`. Such a profile pairs fresh, with no flag needed.

A config written before `extra.profile` existed carries no stamp and is
indistinguishable from a clone, which is why the refusal above is unconditional
rather than token-gated.

#### Collision detection at load

Guards cannot identify an existing collision when another agent's `user_id`
has already been stamped with this profile's `extra.profile`: it appears locally
legitimate, including to the repair-provenance check.

At load, `profile_collision.py` (via `_register_platform`) compares this
profile's `extra.user_id` with sibling `config.yaml` files under the Hermes root
(`<root>/config.yaml` and `<root>/profiles/*/config.yaml`). The same identity on
the same `base_url` produces this warning:

```text
ClawChat: Hermes profiles 'g' and 'default' are configured with the same
ClawChat identity (usr_…). Both gateways authenticate as that one agent …
To give this profile its own agent, activate a fresh connect code with
--new-account …
```

Read-only and best-effort: only `extra.user_id` / `extra.base_url` are read
(never a token), unreadable siblings are skipped, and a failure never blocks
registration. Identical ids under different `base_url`s are not reported —
separate deployments mint ids independently.

To run a **second agent on the same host**, give it its own Hermes profile and
serve it from the default profile's gateway (`gateway.multiplex_profiles: true`
in the default profile's `config.yaml` — the owner's call):

```bash
hermes profile create <name>
hermes -p <name> plugins install clawling/clawchat-plugin-hermes-agent --enable
hermes -p <name> clawchat activate <CODE>
hermes -p default gateway restart   # picks up the new profile; restarts all served profiles
```

Without multiplexing, give the profile a gateway of its own instead:
`hermes -p <name> gateway install && hermes -p <name> gateway start`.

Prefer a bare `hermes profile create` over `--clone` for a second ClawChat
agent: cloning copies credentials that the new profile must not reuse.

CLI activation prints `clawchat: activation complete for <user_id>` and exits 0;
Interactive setup prints `ClawChat activation complete.` Treat non-zero exits as
failures and surface stderr. Codes are single-use: follow server guidance below
before retrying; otherwise request a fresh code. When the server sends connect-code guidance, stderr also
carries `clawchat: next_action=<value>` and `clawchat: hint: <sentence>` lines
— follow them (see [Connect-code failure guidance](#connect-code-failure-guidance)).

## REST Contract For This Host

Activation calls the shared endpoint:

```text
POST {base_url}/v1/agents/connect
Content-Type: application/json
```

The Hermes request body is:

```json
{ "code": "<invite>", "platform": "hermes", "type": "clawbot", "plugin_version": "<clawchat_gateway.__version__>", "user_id": "<optional-existing-user_id>" }
```

`base_url` comes from the interactive setup answer for `hermes gateway setup`.
The direct activation commands — both `clawchat activate` and the in-chat
`/clawchat-activate` — resolve it via `config.resolve_activation_base_url()`
(env → Hermes env store → `$HERMES_HOME/.env`), falling back to
`https://app.clawling.com`. The installer writes the deployment's
`CLAWCHAT_BASE_URL` into that `.env`, so a code minted on a custom backend is
sent there rather than to the public default. When
`platforms.clawchat.extra.user_id` already contains a non-empty value,
activation sends it as optional `user_id`; otherwise the field is omitted.

### Pre-check and bound reconnect codes

`plugin_version` carries the package `__version__` for backend build tracking.
`activate()` first calls `POST /v1/agents/connect/check`, including the stored
`user_id` only when `/connect` will replay it. Under `--new-account`, the
pre-check still runs but omits the id so it cannot refuse that discarded id.
Pre-check unavailability does not block redemption.

A reconnect code reports `bound_agent: true` and can only restore its bound
agent. Activation treats it as `--repair` when a local identity exists, without
requiring local provenance because the server enforces the binding.
`--new-account` is refused on a bound code before redemption: it cannot mint a
new identity. Both calls attach `agent_kind` / `os` / `lane` / `wiki_version` /
`matched_install` from `clawchat_gateway/onboarding.py` (`os` and `wiki_version`
are omitted when unavailable).

The response must include `access_token`, `agent.user_id`, `agent.owner_id`,
and `conversation.id`; optional `agent.id` is persisted when returned.

### Stale `user_id` recovery

The stored `user_id` is a re-pair hint, not a credential: the code determines
the account. A deleted agent or copied config can leave a dead id that returns
`16001 agent not found` even when `/connect/check` reports the code pairable.
Two defences apply:

- **Environment guard.** The stored id is only replayed when `extra.base_url`
  matches the `base_url` being activated against (trailing slashes ignored).
  Agent ids are per-deployment, so an id from another backend is dropped
  without ever being sent.
- **16001 retry.** If the server still answers `16001` for a replayed id, the
  client logs a warning, drops the id, and retries the same code once as a
  fresh pairing. A `16001` failure leaves the invite code `pending`
  server-side, so the retry cannot double-spend it. Apart from the
  `wait_retry` re-redeem below, no other envelope code is retried — an owner
  mismatch (`16014`) is a genuine rejection, and a code that was never replayed
  has no stale state to shed.

The retry handles servers that return `16001` instead of pairing fresh for an
unresolvable `user_id`.

### Connect-code failure guidance

Newer backends attach machine-readable guidance to the error envelope's `data`
on a failed `POST /v1/agents/connect` (and on `/connect/check` business
errors):

| `data` field | Meaning |
|---|---|
| `hint` | English sentence for the agent, ending with a link to the public onboarding guide. |
| `next_action` | Closed enum: `ask_owner_new_code` (unknown / expired code — it is dead, never retry), `wait_retry` (rate limited — the code was **not** consumed), `reconnect` (redeemed from another device — the owner must send a reconnect prompt), `stop` (owner mismatch, bad `user_id`, malformed request — an unchanged retry fails the same way). |
| `retry_after_seconds` | Only with `wait_retry`: how long to wait before retrying. |

`ClawChatApiError` exposes them as `hint`, `next_action` (verbatim, including
values this release does not know) and `retry_after_seconds`; all are `None`
against older backends.

- **`wait_retry` re-redeem.** `agents_connect_with_retry` waits
  `retry_after_seconds` (capped at 120 s; 60 s if missing or invalid) and
  redeems the same code **once** more. This budget is separate from the
  transport retries (which only cover requests that provably never reached the
  server) and never refills, so the call stays bounded. If the server throttles
  again, the error carries `wait_retried=True` and the CLI adds a "do not loop"
  line.
- Every other `next_action` — including unknown values — fails immediately.
- `hermes clawchat activate` keeps its `clawchat: activation failed (...)`
  first line unchanged and appends `clawchat: next_action=<value>
  [retry_after_seconds=N]` and `clawchat: hint: <sentence>` lines when present.
  `/clawchat-activate` returns the same lines in-chat, and `hermes gateway
  setup` prints them before re-raising. Without guidance fields, every surface
  behaves exactly as before.

## Persisted State

Activation writes:

| File | Contents |
|---|---|
| `$HERMES_HOME/.env` | `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, optional `CLAWCHAT_HOME_CHANNEL*`. |
| `$HERMES_HOME/config.yaml` | `platforms.clawchat.enabled=true`, `extra.base_url`, `extra.websocket_url`, `extra.user_id`, `extra.agent_id`, `extra.owner_user_id`, missing `extra.output_visibility=normal`, derived `extra.runtime_status_messages=false`, forced agent quiet defaults (`gateway_notify_interval=0`, `gateway_timeout_warning=0`), forced global ClawChat display defaults (`busy_input_mode=queue`, `busy_ack_enabled=false`, `background_process_notifications=off`, `tool_progress_command=false`), missing `display.platforms.clawchat.*` normal-preset defaults, and forced memory routing defaults (`memory.user_profile_enabled=false`, `memory.nudge_interval=0`; see [`./configuration.md`](./configuration.md#hermes-built-in-memory)), and a missing `compression.threshold_tokens` filled from `session-cap-tokens` (see [`./configuration.md`](./configuration.md#sediment-turns)). Operators may edit the ClawChat platform display block manually after activation. |
| `$HERMES_HOME/clawchat/clawchat.sqlite` (named profiles: `clawchat-<profile>.sqlite`; see [database location](./configuration.md#database-location)) | Latest activation row, including access token, optional refresh token, user ids, activation conversation id, and the connect-time `device_id` (`activations.device_id`); plus the `owner_profile` cache row (last-known owner nickname/avatar/bio/locale, refreshed on every owner-metadata pull, advisory only). |

Returned `.env` tokens and `config.yaml` user ids overwrite any previously
configured ClawChat activation credentials — which is why activation refuses to
run against a live activation unless `--new-account` or `--repair` says which
overwrite was intended (see [One profile, one agent](#one-profile-one-agent)).

SQLite is the authoritative runtime credential record; `.env` is the bootstrap
copy. Startup adopts a complete SQLite row, falling back to complete env/extra
credentials; the first refresh seeds SQLite. Tokens never enter `config.yaml`.
A registered platform with no complete credentials waits for activation without
blocking startup, then adopts the new row without a restart. An unregistered
platform still needs plugin reload or gateway restart. See [Hermes recovery](./token-refresh.md#c2-hermes-recovery-unification).

Activation writes the WebSocket URL to `platforms.clawchat.extra.websocket_url`:
the explicit `CLAWCHAT_WEBSOCKET_URL` (or `CLAWCHAT_WS_URL`) when set — the
installer writes it next to `CLAWCHAT_BASE_URL`, and the runtime prefers it too —
otherwise one derived from `base_url` (`activate._derive_websocket_url`).

## Automatic Token Refresh & Auto-Logout

Token lifetime is server-controlled. The plugin refreshes before JWT `exp` using
a lifetime-based margin, and reactively on REST `401`/`403` or a WebSocket
`hello-fail` that rejects authentication. Timing, retries and persistence are
specified in [token refresh](./token-refresh.md#a-refresh-timing).

### How refresh works

Successful refresh normally needs no operator action. Proactive refresh
reconnects the WebSocket; REST recovery retries the failed request once and
keeps a healthy socket. Transient failures retry without logging out; see
[failure handling](./token-refresh.md#b-refresh-failure-handling).

### Auto-logout (permanent refresh failure)

A confirmed permanent refresh failure clears credentials while preserving
identity, closes the socket and waits for activation. The user sees:

> ClawChat token expired and could not be refreshed. Ask your owner to send you the reconnect prompt from the ClawChat app, then follow https://agent-connection.clawling.com/reconnect.md.

The [Hermes appendix](./token-refresh.md#h1-confirmed-permanent-logout-and-adoption-guards)
specifies the four logout steps, durable fingerprint guard and adoption checks.

**Operator recovery:** do not spend a fresh connect code on it. The owner sends
the agent the reconnect prompt from the ClawChat app (its code is bound to this
agent). Follow the reconnect page above and use any activation entrypoint.
Activation usually restores the identity on its own; if it still reports the
identity as already paired, re-run with `--repair`. The waiting supervisor adopts
the new credentials without a Hermes restart, including after an env-booted
process's first permanent refresh failure.

### Device-id column (SQLite migration)

Activation stores the connect-time id in `activations.device_id` (migration 6); refresh reuses it.
See [device-id durability](./configuration.md#device-id-durability-deployment-requirement); pin the id when SQLite is not durable.

## Credential Deletion And Revocation

| Case | Behavior |
|---|---|
| Never activated: no SQLite row and no complete env/extra credentials | Poll for activation every 2 seconds; no connect or refresh. |
| SQLite activation row deleted or credentials cleared | Clear memory, close WS and wait for activation; no refresh-permanent auto-logout message. Read errors suspend after a bounded count. |
| Server-side session revocation | Reconnect normally, then refresh on authentication rejection; a confirmed permanent failure takes auto-logout. |

See the [Hermes appendix](./token-refresh.md#h-hermes-appendix--credential-lifecycle-and-races)
for source checks, logs, suspension, conditional writes and cross-process races.

## Restart Or Reload

CLI activation and in-session slash activation request a detached Hermes
gateway restart by default. Use `--no-restart` to skip that restart.

Reloading plugins only reconnects handlers for existing platform adapters; it
does not create a ClawChat adapter or start its connection. After activation,
ClawChat connects only after the Hermes gateway starts or restarts.

Successful CLI and slash activation payloads include `gateway_restart_required`, even
with `--no-restart`. It is `true` only for the `schedule` plan. `owner_restart_command`
is `hermes gateway restart` for the default profile, `hermes -p <name> gateway restart`
for a separate named profile, or `hermes -p default gateway restart` for a
multiplexed named profile; it is absent for `no_gateway` (install and start first).

A multiplexed named profile needs no restart on Hermes 0.21.6 and newer: the running
multiplexer rescans served profiles every 30 seconds and builds adapters for one
whose `config.yaml` or `.env` changed, which activation does. Its command is only
the fallback for older hosts or a multiplexer that was just turned on.

When a restart is required, both activation entry points explain the connection
requirement and print English and Chinese messages for the agent to relay.
If no new `clawchat state -> ready` line appears within a few minutes, ask the
owner to run the printed command after the current reply ends, in a terminal
outside Hermes. Hermes refuses a restart command executed inside the gateway
process and waits for ongoing sessions, including the agent's current turn.

For the default profile, the printed owner messages are:

> ClawChat is activated. Once my reply ends, please run `hermes gateway restart` in a terminal (not in this chat). After the restart I will greet you in ClawChat.

> ClawChat 已激活。请在我这条回复结束后，在终端里(不是在这个对话里)执行 `hermes gateway restart`,重启后我会在 ClawChat 里跟你打招呼。

Before requesting it, activation checks the active profile
(`clawchat_gateway/restart.py`, `plan_gateway_restart`) and does **not** spawn a
restart that cannot work; it prints the next step instead:

| Detected | What activation does |
|---|---|
| Default profile: neither `gateway.pid` nor `gateway_state.json` in `$HERMES_HOME` | Prints `hermes gateway install` then `hermes gateway start`; a bare restart without an installed service would start an unmanaged foreground gateway. |
| Named profile: no gateway runtime files and no serving multiplexer | Recommends enabling `gateway.multiplex_profiles` in the default config, adding this profile to `gateway.multiplex_profile_allowlist` if set, then `hermes -p default gateway restart`. Fallback: `hermes -p <name> gateway install` then `hermes -p <name> gateway start`. |
| Named profile served by the default multiplexer (`gateway.multiplex_profiles` or `GATEWAY_MULTIPLEX_PROFILES`, narrowed by the allowlist) | Says Hermes 0.21.6+ picks the profile up within about 30 s; otherwise prints `hermes -p default gateway restart`, which restarts all served profiles. Hermes refuses per-profile restart here. |
| Otherwise | Requests restart; warns when the default gateway serves multiple profiles. |

A requested restart is not a confirmed one. The detached process runs from
`$HERMES_HOME` (never from the plugin folder, which a lingering process would
keep locked on Windows) and writes the output of `hermes gateway restart` plus a
final `exit_code=<n>` line to `$HERMES_HOME/clawchat/gateway-restart.log`,
overwritten on each request. To verify:

1. The restart log ends with `exit_code=0`.
2. The Hermes log (under `$HERMES_HOME/logs/`) has a `clawchat state -> ready`
   line from after the activation.

No `exit_code` after a few minutes means the restart may still be waiting for
sessions or the launcher stopped with the old gateway; confirm with the ready
line. This check cannot detect a separately installed gateway holding the
profile, service-manager errors or drain waits; inspect the restart log.

On macOS, launchd may contain two gateway definitions with labels such as
`ai.hermes.gateway` and `ai.hermes.gateway-<profile>`; older and newer
installations can resolve different labels. Directly running
`launchctl kickstart` on a label may start an additional gateway.

First use `hermes gateway status` to find the running PID, then
`launchctl list | grep ai.hermes.gateway` to find the label matching that PID.
Use `hermes gateway restart` to restart, with the profile selector printed by
activation when applicable. Leave the decision to remove extra launchd
definitions to the owner.

`hermes gateway setup` leaves service lifecycle to Hermes. `--restart` remains
a compatibility flag; CLI and slash activation restart by default.

## Activation Bootstrap

Activation sets `CLAWCHAT_HOME_CHANNEL` to the `conversation.id` returned by
`/v1/agents/connect` and sets `CLAWCHAT_HOME_CHANNEL_NAME` to `ClawChat`.

Hermes uses that activation direct conversation for home-channel/default
delivery. The latest activation row in plugin SQLite also stores the activation
conversation id.

## Implementation References

- `__init__.py`: platform, CLI, slash and home-channel registration.
- `clawchat_gateway/commands.py`, `cli.py`, `setup.py`: activation entrypoints.
- `clawchat_gateway/activate.py`: exchange and persistence; `restart.py`: restart plans.
- `clawchat_gateway/connection.py`: activation polling, WS lifecycle and auto-logout.
- `clawchat_gateway/token_refresh.py`: timing, single-flight, latches and persist-before-swap.
- `clawchat_gateway/api_client.py`: connect/refresh HTTP; `device_id.py`: pairing id.

## Verification

The regression suite for activation writes lives in `tests/`. Named tests
and shared fixtures are tracked through `.gitignore` exceptions; other tests
stay local (see [`./development.md`](./development.md#running-tests)). Use
the smallest command that covers the activation change being touched:

```bash
uv run --extra test pytest tests/test_activate_device_id.py
uv run --extra test pytest
```

For a live Hermes setup, see
[`./development.md`](./development.md#live-debugging-against-a-hermes-process).
