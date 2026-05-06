# `.e2e/` — real-environment testing harness

This directory holds the scripts and assets used to exercise the
plugin against a live ClawChat backend and a real `nousresearch/hermes-agent`
Docker image. It is intended for local debugging and reproduction of
issues that the unit tests in `tests/` cannot cover (network IO, the
real Hermes runtime, the actual install/activation flow).

Everything under `.e2e/tmp/` and `.e2e/.env` is gitignored — only the
scripts, fixtures, docs, and `.env.example` are checked in.

> **Manual intervention required.** Two prerequisites cannot be created
> by the harness or by an agent — a human must provide them once before
> any test can run, and re-do them when they expire or get corrupted:
>
> - **`.e2e/.env` (JWT)** — a valid ClawChat user JWT. Has to come
>   from a logged-in human session; expires; cannot be derived from
>   anything in this repo.
> - **`.e2e/tmp/hermes_data_base/` (baseline Hermes data dir)** —
>   produced by an interactive first-run of `nousresearch/hermes-agent`
>   that walks through model selection / API-key prompts. The script
>   only *clones* this directory; it cannot create it from scratch.
>
> If either is missing, `local_start_test.sh` aborts with a clear
> message. Do **not** attempt to fabricate or auto-fill them — stop
> and ask the human to set them up.

## Layout

| Path | Purpose |
|------|---------|
| `local_start_test.sh` | Driver. Pulls a one-time connect code from the ClawChat REST API, seeds a fresh Hermes data dir, and runs `nousresearch/hermes-agent chat` in Docker so an LLM-driven turn installs + activates the plugin end-to-end. |
| `.env.example` | Template for `.env`. Holds the user JWT used to mint connect codes. Copy to `.e2e/.env` and fill in. |
| `.env` | Local secrets (JWT). Gitignored. |
| `dev_install.md` | Install instructions consumed by the agent during the test. Mounted into the container at `/opt/dev_install.md` and read with `execute_code`. |
| `tmp/hermes_data_base/` | Baseline Hermes data dir (config, model creds, etc.). Bootstrapped once, then reused. Gitignored. |
| `tmp/hermes_data/` | Per-run copy of the baseline. Recreated on every invocation. Gitignored. |

## Prerequisites

- Docker daemon running locally with the `nousresearch/hermes-agent`
  image already pulled (or reachable to pull on first run).
- `python3` on PATH (used to extract the `code` field from the JSON
  response).
- A ClawChat user JWT with permission to call
  `POST /v1/agents/connect-codes` against the configured backend
  (`http://company.newbaselab.com:19001` is hard-coded in the script).

## One-time setup

### 1. Provide credentials

```bash
cp .e2e/.env.example .e2e/.env
$EDITOR .e2e/.env   # set JWT=<your bearer JWT>; leading "Bearer " is optional
```

### 2. Bootstrap the Hermes baseline data dir

`local_start_test.sh` copies `.e2e/tmp/hermes_data_base/` into
`.e2e/tmp/hermes_data/` on every run so each test starts from a clean,
already-configured Hermes state. Build the baseline once by running
Hermes interactively against an empty data dir, completing the
first-run prompts (model selection, API keys, anything else Hermes
asks for), then exiting:

```bash
mkdir -p .e2e/tmp/hermes_data_base
docker run -it --rm -v ./.e2e/tmp/hermes_data_base:/opt/data \
    nousresearch/hermes-agent chat
```

When you exit, `.e2e/tmp/hermes_data_base/` will hold the persisted
Hermes config that subsequent test runs clone.

The script aborts with a guidance message if this directory is
missing, so you don't have to remember the command — just re-read the
error.

## Running a test

From the repo root:

```bash
bash .e2e/local_start_test.sh
```

What happens, in order:

1. Loads `JWT` from `.e2e/.env` (or the file pointed to by
   `ENV_FILE=...`). Adds the `Bearer ` prefix if missing.
2. `POST`s to `…/v1/agents/connect-codes` with that JWT and
   `x-device-id: apifox`, prints the raw envelope, and extracts
   `data.code` (falling back to top-level `code`).
3. Verifies `.e2e/tmp/hermes_data_base/` exists; bails with a
   bootstrap hint otherwise.
4. Wipes `.e2e/tmp/hermes_data/` and copies a fresh tree from the
   baseline.
5. Runs `nousresearch/hermes-agent chat -q "…"` with two volume
   mounts:
   - `./.e2e/tmp/hermes_data:/opt/data` — the writable data dir for
     this run.
   - `./.e2e/dev_install.md:/opt/dev_install.md:ro` — the install
     guide the agent reads via `execute_code`.

   The chat prompt hands the agent the one-time connect code and
   tells it to follow `/opt/dev_install.md`. From there the agent
   drives `hermes plugins install`, enables `clawchat`, runs
   `python -m clawchat_gateway.activate <CODE>`, and dispatches the
   gateway restart — all inside the container.

After the agent exits, the resulting `.e2e/tmp/hermes_data/` is the
post-install snapshot (look at `config.yaml`, `.env`,
`plugins/clawchat/`, `logs/`, etc.) which is useful for debugging.

## Post-install verification: `gateway run`

A successful install run leaves `.e2e/tmp/hermes_data/` populated with
the plugin installed, activated, and the `clawchat` platform configured
in `config.yaml` / `.env`. To verify the runtime adapter actually boots
and stays connected against that snapshot — no chat turn, no install
work — start the gateway directly:

```bash
docker run --rm -it \
    -v ./.e2e/tmp/hermes_data:/opt/data \
    nousresearch/hermes-agent gateway run
```

Expected behavior on a healthy install:

- The container starts, loads `plugins/clawchat/`, and the logs show
  the `CLAWCHAT` platform registering through `ctx.register_platform`.
- The WebSocket connects to `CLAWCHAT_WEBSOCKET_URL` and stays open
  (no reconnect loop, no auth-rejection backoff).
- Ctrl-C exits cleanly.

Failure modes and where to look:

| Symptom | Where to look |
|---------|---------------|
| Gateway exits immediately | `.e2e/tmp/hermes_data/logs/` for the boot stack trace; check `plugins/clawchat/` was actually written. |
| Reconnect loop on the WS | `.e2e/tmp/hermes_data/.env` — `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` may be missing, expired, or for a different user. |
| `CLAWCHAT` platform never registers | `config.yaml` — confirm `platforms.clawchat` exists and `enabled: true`. If absent, the install half failed; rerun `bash .e2e/local_start_test.sh`. |

If the snapshot is broken, rebuild it with another
`bash .e2e/local_start_test.sh` rather than hand-editing the data dir
— each install run reseeds from `tmp/hermes_data_base/`, so you get a
clean state instead of compounding partial fixes.

## Iterating on a failing run

- The fastest reproduction is to re-run `bash .e2e/local_start_test.sh`
  — each invocation reseeds from the baseline, so prior state from a
  broken run never bleeds in.
- If the failure is in install or activation, inspect
  `.e2e/tmp/hermes_data/logs/`, `.e2e/tmp/hermes_data/.env`, and
  `.e2e/tmp/hermes_data/config.yaml` after the script exits.
- To pin a specific Hermes image, override the `image:tag` in
  `local_start_test.sh` before running.
- To debug the runtime adapter without re-running the install half,
  use the `gateway run` invocation from
  [Post-install verification](#post-install-verification-gateway-run)
  against the existing `.e2e/tmp/hermes_data/`. The two commented
  lines at the bottom of `local_start_test.sh` are the same command,
  kept inline as a quick reference.
- Connect codes are one-time-use. If a run fails after the code is
  consumed, just re-run the script — it mints a new code on each
  invocation.

## Common errors

| Symptom | Cause / Fix |
|---------|-------------|
| `missing env file: …/.env (expected JWT=...)` | `.e2e/.env` not created. Copy `.e2e/.env.example` and fill in `JWT=`. |
| `JWT not set in …/.env` | The file exists but `JWT` is empty. |
| `failed to obtain connect code` | Backend rejected the JWT (expired / wrong audience) or returned a non-zero envelope. The raw response is printed on the previous line. |
| `missing baseline Hermes data dir: …/tmp/hermes_data_base` | First-time setup not done. Run the bootstrap docker command from the error message, complete first-run setup, then re-run. |
| Docker `image not found` for `nousresearch/hermes-agent` | `docker pull nousresearch/hermes-agent` first, or set a specific tag. |
