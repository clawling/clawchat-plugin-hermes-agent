# Install and activate the ClawChat Hermes plugin

This is the install and activation guide for Hermes operators.

## Compatibility

| Component       | Requirement                                  |
|-----------------|----------------------------------------------|
| Hermes Agent    | `v0.12.0` or newer (uses `ctx.register_platform`) |
| Python runtime  | `>=3.11` (per `pyproject.toml`)              |
| Dependencies    | `websockets>=12,<16`, `PyYAML>=6,<7`         |

The plugin advertises itself as `clawchat` (`plugin.yaml: kind: platform`)
and is loaded directly into `$HERMES_HOME/plugins/clawchat/`.
`HERMES_HOME` defaults to Hermes' platform-native home: `~/.hermes` on POSIX
(including WSL2), `%LOCALAPPDATA%\hermes` on native Windows. Every path in this
guide is written POSIX-first; on native Windows substitute that root — never
`%USERPROFILE%\.hermes`, which Hermes does not use.

## Install paths

There are three supported install entrypoints. Pick one — they all end
in the same place.

If this host runs (or will run) more than one Hermes profile, first read
[Confirm the target profile](#confirm-the-target-profile-before-every-install--activate):
every command below resolves its profile from `HERMES_HOME`, and the wrong one
installs and activates a different agent without any error.

### A. Via the bundled installer (recommended for end users)

The `@clawling/clawchat-plugin-install-cli` package wraps everything below:

```bash
npx -y @clawling/clawchat-plugin-install-cli@latest install --target hermes
```

`update --target hermes` and `update --target hermes --force` are the
companion commands for keeping a host current.

### B. Directly via Hermes' plugin CLI

```bash
hermes plugins install clawling/clawchat-plugin-hermes-agent
hermes plugins enable clawchat
```

If Hermes is not on `PATH`, source the venv first (e.g.
`source /opt/hermes/.venv/bin/activate`) or call the binary directly.

### C. Docker / container deployments

In containers the Hermes binary commonly lives at
`/opt/hermes/.venv/bin/hermes` and the data root at `/opt/data`. Set
`HERMES_HOME` explicitly on every call:

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins install clawling/clawchat-plugin-hermes-agent --force'

docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/hermes plugins enable clawchat'
```

After install, the plugin source is at `$HERMES_HOME/plugins/clawchat/`.

## What `install` and `enable` do

- Copy the plugin source into `$HERMES_HOME/plugins/clawchat/`.
- On `enable`, register the `clawchat` gateway platform via
  `ctx.register_platform(...)`, register the bundled `clawchat:clawchat-core`
  skill via `ctx.register_skill(...)`, register the thirty-four
  `clawchat_*` tools, and install the `pre_gateway_dispatch` hook.
- **No** credentials are written. `CLAWCHAT_TOKEN` and
  `CLAWCHAT_REFRESH_TOKEN` do not exist until activation runs.

See [`./architecture.md`](./architecture.md) for the full registration
surface.

## Activate

Activation exchanges a one-time activation code for credentials and
writes them to `$HERMES_HOME/.env` plus `$HERMES_HOME/config.yaml`
(non-secret settings under `platforms.clawchat.extra`).

For Hermes-specific activation entry points, flags, persisted state, restart
behavior, home-channel bootstrap, implementation references, and verification,
see [`./activation.md`](./activation.md).

### Interactive (`hermes gateway setup`)

```bash
hermes gateway setup
```

Prompts for the activation code and the API base URL, then lets Hermes
finish its normal gateway service flow (start / restart). This is the
preferred path on Hermes builds that surface plugin setup functions
through `gateway setup`.

### Non-interactive — Hermes plugin subcommand

Hermes builds newer than v0.12.0 expose plugin CLI commands via the
top-level `hermes` parser:

```bash
hermes clawchat activate <CODE>
```

### Non-interactive — v0.12.0 compatibility entrypoint

Hermes v0.12.0 registers the plugin CLI internally but does **not**
expose it through the top-level `hermes` parser. Run the bundled
compatibility script in the Hermes Python environment:

```bash
python "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat/clawchat_cli.py" activate <CODE>
```

```powershell
$root = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
python (Join-Path $root 'plugins\clawchat\clawchat_cli.py') activate <CODE>
```

This path has **no `-p` flag**: it resolves the profile from `HERMES_HOME`
alone and ignores `hermes profile use`. On a multi-profile host, export
`HERMES_HOME` for the target profile explicitly — see
[Confirm the target profile](#confirm-the-target-profile-before-every-install--activate).

### From inside a Hermes session

```text
/clawchat-activate <CODE>
```

### Docker activation

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data /opt/hermes/.venv/bin/python /opt/data/plugins/clawchat/clawchat_cli.py activate <CODE>'
```

### Activation flags

| Flag           | Effect                                                  |
|----------------|---------------------------------------------------------|
| `--restart`    | Compatibility flag; activation schedules a detached Hermes gateway restart by default. |
| `--no-restart` | Skip the detached Hermes gateway restart after activation. |

### What gets written

| File                                       | Contents (per `clawchat_gateway/activate.py`)                                 |
|--------------------------------------------|--------------------------------------------------------------------------------|
| `$HERMES_HOME/.env`                        | `CLAWCHAT_TOKEN`, `CLAWCHAT_REFRESH_TOKEN`, optional `CLAWCHAT_HOME_CHANNEL*`. |
| `$HERMES_HOME/config.yaml`                 | `platforms.clawchat.enabled=true`, `extra.base_url`, `extra.websocket_url`, `extra.user_id`, `extra.agent_id`, `extra.owner_user_id`, missing `extra.output_visibility=normal`, derived `extra.runtime_status_messages=false`, forced agent quiet defaults (`gateway_notify_interval=0`, `gateway_timeout_warning=0`), forced global ClawChat display defaults (`busy_input_mode=queue`, `busy_ack_enabled=false`, `background_process_notifications=off`, `tool_progress_command=false`), and missing `display.platforms.clawchat.*` normal-preset defaults. Operators may edit the ClawChat platform display block manually after activation. |
| `$HERMES_HOME/clawchat.sqlite`             | Latest activation row (access token, optional refresh token, user ids, activation conversation id) plus the `owner_profile` cache row (last-known owner nickname/avatar/bio/locale, refreshed on every owner-metadata pull). |

Successful activation via the CLI, slash-command, or compatibility-script
paths prints `clawchat: activation complete for <user_id>` and exits 0; the
interactive `hermes gateway setup` flow instead prints
`ClawChat activation complete.` (no user_id). Treat any non-zero exit as a hard failure — activation
codes are single-use, so do **not** retry the same code; surface stderr
to the operator and request a fresh code.

## Verify the install

```bash
hermes plugins list | grep clawchat
hermes --version
ls "${HERMES_HOME:-$HOME/.hermes}/plugins/clawchat"
```

For protocol-level checks (WebSocket handshake, ack flow), see
[`./architecture.md`](./architecture.md).

## Troubleshooting

- **`hermes: command not found`** — source the Hermes venv first, or use
  the absolute path (`/opt/hermes/.venv/bin/hermes` in the default
  container layout).
- **`Unknown plugin: clawchat` after install** — check
  `hermes plugins list`; if missing, rerun `install` with `--force`.
- **Activation exits non-zero with `validation` / `auth`** — the
  activation code is single-use; request a fresh one. Surface stderr
  verbatim.
- **WebSocket fails to connect after activation** — confirm
  `CLAWCHAT_TOKEN` exists in `$HERMES_HOME/.env` and that
  `platforms.clawchat.extra.websocket_url` is set in `config.yaml`. The
  default is `wss://app.clawling.com/ws`.
- **Activation refuses with "this Hermes profile is already paired"** — decide
  what you actually want before re-running, because the wrong flag spends the
  code. If this profile should get **its own new agent** — the normal case when
  a freshly created profile already shows an identity, which it inherited from
  a cloned `config.yaml` — use `--new-account`. Use `--repair` **only** when the
  owner confirms this profile itself paired that agent and merely lost its
  token: `--repair` keeps the stored `user_id`, so the server re-pairs *that*
  agent and creates none here. Activation now refuses `--repair` outright
  (`UnprovenRepairError`, code not sent) when the identity has no local
  provenance; see [`./activation.md`](./activation.md#choosing-between---new-account-and---repair).
- **A new profile's agent turns out to be an existing agent** (same ClawChat
  account) — either the command resolved to the wrong `HERMES_HOME`, or
  `--repair` replayed an inherited identity. Do **not** request a new code yet:
  run the Step 0 checks in
  [Confirm the target profile](#confirm-the-target-profile-before-every-install--activate),
  compare `extra.user_id` against the other profile's, then re-issue with
  `-p <profile>` **and** `HERMES_HOME` set, plus `--new-account` if this profile
  still needs its own agent.
- **`[HERMES_HOME fallback] HERMES_HOME is unset but active profile is '<name>'`
  on stderr or in the Hermes log** — the process is writing into the *default*
  profile while `hermes profile use` says otherwise. Export `HERMES_HOME`
  explicitly for that command and rerun; anything it already wrote landed in the
  wrong profile.
- **`ClawChat: Hermes profiles 'x' and 'y' are configured with the same ClawChat
  identity` in the Hermes log** — both gateways authenticate as one agent, so
  one of the two profiles has no agent of its own and the last to pair owns the
  live credentials. Decide which profile should keep that agent, then give the
  other one its own with a fresh code and `--new-account`. Emitted at plugin
  load; see [`./activation.md`](./activation.md#collision-detection-at-load).
- **`config.yaml` shows `platforms.clawchat.extra.profile` ≠ the profile you are
  on** — cloned config (`hermes profile create --clone`) or an activation run
  against the wrong home. The plugin ignores the stale `user_id` and pairs this
  profile fresh; verify the live account with the Step 2 check in
  [Confirm the target profile](#confirm-the-target-profile-before-every-install--activate).
- **The bot replies to its own messages in a loop** — the
  `pre_gateway_dispatch` hook drops self-echo frames; if you are seeing
  loops, confirm the plugin was registered (look for
  `ClawChat registered Hermes platform via plugin registry` in the
  Hermes log).

## Multiple agents on one host (Hermes profiles)

Each Hermes profile is an independent `HERMES_HOME` and runs its own gateway
process, so each profile is a separate ClawChat agent with its own account and
its own database file under `$HERMES_HOME/clawchat/`:

```bash
hermes profile create coder
# install + activate + run, scoped to the profile:
npx -y @clawling/clawchat-plugin-install-cli@latest install \
  --target hermes --profile coder --activate <CONNECT_CODE>
hermes -p coder gateway install && hermes -p coder gateway start
```

Repeat with a different profile name for each agent. The default profile keeps
its database at `$HERMES_HOME/clawchat/clawchat.sqlite`; named profiles use
`clawchat-<profile>.sqlite`. Single-gateway multiplexing
(`multiplex_profiles=true`) is not supported — run one gateway per profile.

> **Read the next section before you install or activate into a profile.**
> Creating a profile does not switch you into it, and a mis-targeted activation
> burns a single-use connect code on the wrong account.

## Confirm the target profile before every install / activate

Every ClawChat identity — token, `config.yaml`, SQLite DB — is keyed on the
**active `HERMES_HOME`**. Nothing in the plugin re-checks which profile you
*meant*, so a command that resolves to the wrong `HERMES_HOME` produces a
complete, healthy-looking install on the **wrong profile**: it activates the
wrong account, or reports "already paired" for an agent you never intended to
touch.

### Why it silently targets the wrong profile

Three resolvers disagree, and only one of them reads the sticky profile:

| Entry point | Profile resolution |
|-------------|--------------------|
| `hermes …` (the CLI) | `-p`/`--profile` anywhere in argv → else `HERMES_HOME` when it already points at `<root>/profiles/<name>` → else the sticky `<root>/active_profile` file written by `hermes profile use` → else default. It then exports the resolved `HERMES_HOME` to that process (`hermes_cli/main.py:_apply_profile_override`). |
| `python …/clawchat_cli.py activate`, `python -m clawchat_gateway.profile …`, any plugin code run outside `hermes` | **`HERMES_HOME` only**, else the platform default — `~/.hermes` on POSIX, `%LOCALAPPDATA%\hermes` on native Windows — i.e. the *default profile*. The sticky `active_profile` file is never consulted (`clawchat_gateway/hermes_home.py`). |
| Subprocesses spawned by a running agent | Whatever `HERMES_HOME` the parent exported. A named profile's gateway is started as a child of a default-profile process with only an env overlay, so unexported/inherited values can still describe the default profile. |

Consequences worth internalizing:

- **`hermes profile create coder` does not switch your shell (or the agent's
  session) to `coder`.** The very next command still runs against whatever
  profile was active before — usually `default`.
- **`hermes profile use coder` only helps the `hermes` binary.** The
  v0.12.0 compatibility script and `python -m clawchat_gateway.profile` ignore
  it entirely and fall back to the default profile. When Hermes' own modules
  resolve the home in that state they emit a one-shot
  `[HERMES_HOME fallback] HERMES_HOME is unset but active profile is 'coder' …`
  line on stderr (upstream issue #18594) — treat it as a hard stop. The
  plugin's standalone resolver stays silent, so the absence of that line is not
  evidence that the profile is right.
- **An agent that creates its own profile is the highest-risk case**: it
  creates `coder`, then runs activation from its existing session env, and pairs
  the *default* profile's ClawChat account instead.

### Step 0 — print the profile you are actually on

```bash
ROOT="$HOME/.hermes"         # container layout: ROOT=/opt/data
echo "HERMES_HOME=${HERMES_HOME:-<unset>}"
cat "$ROOT/active_profile" 2>/dev/null || echo "active_profile=<none> (default)"
hermes profile list          # marks the sticky active profile
```

On native Windows the root is `%LOCALAPPDATA%\hermes` — **not**
`%USERPROFILE%\.hermes`, which is the POSIX/WSL2 layout and a directory Hermes
never writes:

```powershell
$root = if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA 'hermes' }
"HERMES_HOME=$(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { '<unset>' })"
$active = Join-Path $root 'active_profile'
if (Test-Path $active) { Get-Content $active } else { 'active_profile=<none> (default)' }
hermes profile list
```

`active_profile` always lives in the **root** home, never inside a named
profile directory.

Read the result with the table above: `HERMES_HOME` unset + `active_profile`
naming a profile is precisely the mismatch state — the `hermes` CLI will go to
that profile while every bare-`python` entry point goes to `default`.

### Step 1 — pin the profile explicitly on every command

Do not rely on the sticky profile. Set both the flag and the environment so all
three resolvers agree:

```bash
PROFILE=coder
export HERMES_HOME="$HOME/.hermes/profiles/$PROFILE"   # default profile: "$HOME/.hermes"

hermes -p "$PROFILE" plugins install clawling/clawchat-plugin-hermes-agent
hermes -p "$PROFILE" plugins enable clawchat
hermes -p "$PROFILE" clawchat activate <CODE>
hermes -p "$PROFILE" gateway install && hermes -p "$PROFILE" gateway start
```

Same sequence on native Windows — only the home differs:

```powershell
$profileName = 'coder'
$env:HERMES_HOME = Join-Path $env:LOCALAPPDATA "hermes\profiles\$profileName"
# default profile: $env:HERMES_HOME = Join-Path $env:LOCALAPPDATA 'hermes'

hermes -p $profileName plugins install clawling/clawchat-plugin-hermes-agent
hermes -p $profileName plugins enable clawchat
hermes -p $profileName clawchat activate <CODE>
hermes -p $profileName gateway install; hermes -p $profileName gateway start
```

For the v0.12.0 compatibility script and any other bare-`python` entry point,
`HERMES_HOME` is the *only* control — there is no `-p` to fall back on:

```bash
HERMES_HOME="$HOME/.hermes/profiles/coder" \
  python "$HOME/.hermes/profiles/coder/plugins/clawchat/clawchat_cli.py" activate <CODE>
```

```powershell
$env:HERMES_HOME = Join-Path $env:LOCALAPPDATA 'hermes\profiles\coder'
python (Join-Path $env:HERMES_HOME 'plugins\clawchat\clawchat_cli.py') activate <CODE>
```

With the installer CLI, pass `--profile` **or** point `HERMES_HOME` at the
profile — never both. `--profile <name>` resolves to
`<HERMES_HOME-or-default-root>/profiles/<name>`, so exporting an already
profile-scoped `HERMES_HOME` *and* passing `--profile coder` writes credentials
to `…/profiles/coder/profiles/coder`:

```bash
# correct: HERMES_HOME unset (or pointing at the ROOT), profile named once.
# If you exported the profile-scoped HERMES_HOME above, clear it first:
unset HERMES_HOME
npx -y @clawling/clawchat-plugin-install-cli@latest install \
  --target hermes --profile coder --activate <CONNECT_CODE>
```

In containers the root is `HERMES_HOME=/opt/data`, so a named profile lives at
`/opt/data/profiles/<name>`. Because the root path is not itself profile-scoped,
a bare `docker exec … hermes …` still follows whatever `active_profile` someone
set earlier — always pass `-p`:

```bash
docker exec hermes sh -lc \
  'HERMES_HOME=/opt/data/profiles/coder /opt/hermes/.venv/bin/hermes -p coder clawchat activate <CODE>'
```

### Step 2 — verify you landed on the intended profile

Check before spending a connect code, and again after activation. All four
artifacts live under the profile's own `HERMES_HOME`:

```bash
PROFILE=coder
HOME_DIR="$HOME/.hermes/profiles/$PROFILE"          # default profile: "$HOME/.hermes"

grep -c CLAWCHAT_TOKEN "$HOME_DIR/.env"             # 1 = this profile is paired
grep -A6 'clawchat:' "$HOME_DIR/config.yaml"        # extra.user_id + extra.profile
ls "$HOME_DIR/clawchat/"                            # clawchat-coder.sqlite (default: clawchat.sqlite)

# the live ClawChat account this profile's credentials resolve to
HERMES_HOME="$HOME_DIR" PYTHONPATH="$HOME_DIR/plugins/clawchat" \
  python -m clawchat_gateway.profile get
```

Run that last command with the Hermes Python (the interpreter that owns
`websockets` / `PyYAML` — e.g. `/opt/hermes/.venv/bin/python` in the default
container layout, or `<root>\hermes-agent\.venv\Scripts\python.exe` on Windows).

The same four checks on native Windows:

```powershell
$profileName = 'coder'
$homeDir = Join-Path $env:LOCALAPPDATA "hermes\profiles\$profileName"   # default: ...\hermes

Select-String -Path (Join-Path $homeDir '.env') -Pattern 'CLAWCHAT_TOKEN'
Select-String -Path (Join-Path $homeDir 'config.yaml') -Pattern 'user_id|agent_id|profile:'
Get-ChildItem (Join-Path $homeDir 'clawchat')

$env:HERMES_HOME = $homeDir
$env:PYTHONPATH = Join-Path $homeDir 'plugins\clawchat'
python -m clawchat_gateway.profile get
```

`platforms.clawchat.extra.profile` is stamped at activation with the profile
that minted the credentials. If it does not equal the profile you are working
on, the identity in that `config.yaml` belongs to a different agent — the
common cause is `hermes profile create <name> --clone`, which copies
`config.yaml` wholesale. The plugin defends against this (it logs
`clawchat activation ignoring stored user_id minted by another Hermes profile …`
and pairs fresh), but a `user_id` that does not match this profile is still the
signal that your commands are landing somewhere unintended.

Two distinct profiles must never show the same `extra.user_id`. If they do, one
of them was activated while pointed at the other's `HERMES_HOME`.

For what happens *after* a code reaches the right profile — the one-profile /
one-agent rule, cloned-profile detection, and the `--new-account` / `--repair`
flags — see [`./activation.md`](./activation.md#one-profile-one-agent).
