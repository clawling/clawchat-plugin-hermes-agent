# ClawChat Agent Plugins — Token Refresh & Auto-Logout (canonical parity spec)

> **Byte-identical duplicate.** The same file lives in both adapter repos, at
> `clawchat-plugin-openclaw/docs/token-refresh.md` and
> `clawchat-plugin-hermes-agent/docs/token-refresh.md`. Mirror every edit into
> the other repo in the same change (`sha256sum` both copies to check), and
> **never renumber a section** — source comments in both repos cite them by
> number (`token_refresh.py` §A/§B/§C, `api_client.py` §0, `adapter.py` §C.1,
> `openclaw-vs-hermes.md` §C.2).

This spec is implemented identically by both adapters:

- **OpenClaw** (TypeScript) — `clawchat-plugin-openclaw/`
- **Hermes** (Python) — `clawchat-plugin-hermes-agent/`

The two plugins are parallel adapters of one Protocol v2. Behavior described here MUST be
identical across both. When changing the wire shape, update both plugins (and the msghub
Protocol v2 contract doc (owner)) together.

## Problem

Both plugins receive and store a `refresh_token` from `POST /v1/agents/connect` but never use
it. The `access_token` expires after **24h**; when it does the WebSocket handshake fails with
`hello-fail` and the plugin is unusable until a human re-pairs it. This spec adds automatic
refresh, correct auto-logout on permanent expiry, and protocol-correct WebSocket continuation.

## Confirmed product decisions

| # | Decision |
|---|----------|
| Scope | **Proactive + reactive** refresh (not reactive-only). |
| Hermes device id | Persist a `device_id` column **and** require deployments to pin `CLAWCHAT_DEVICE_ID`; warn on boot if unpinned. |
| Hermes recovery | On successful refresh, write the rotated token to **both** `.env` and SQLite and switch the process onto the SQLite-credentials path, so a future permanent expiry self-recovers via wait-for-activation without a gateway restart. |
| Auto-logout UX | On permanent failure, surface a user-visible status/chat message (in addition to logs). |

---

## 0. The refresh endpoint (shared contract)

`POST /v1/auth/refresh`  (NOT `/clawnext/auth/refresh`)

- **Unauthenticated** — send **no** `Authorization` header; the refresh token in the body is the credential.
- **Required header:** `X-Device-Id` — must equal the device id baked into the session at connect time (the backend rejects any mismatch against the session's device id). ≤128 bytes.
- **Body:** `{ "refresh_token": "<64-char hex>" }`.
- **Always returns HTTP 200.** Branch on the envelope `code`, never on HTTP status:

| `code` | meaning | class |
|--------|---------|-------|
| `0` | success; `data = { access_token, refresh_token }` — **rotated**, old refresh token dies immediately | success |
| `10003` (`CodeInvalidRefresh`) | refresh token not found / revoked / expired / **device mismatch** | **PERMANENT** → auto-logout |
| `400` | bad body / missing or oversized device id | **PERMANENT (client bug)** → auto-logout |
| `1` (`CodeInternal`) | server internal error (no rotation committed) | **TRANSIENT** → retry |
| any non-200 (500/LB/transport) / network error | — | **TRANSIENT** → retry |

- The refreshed access token preserves the agent identity (`aid`/`oid`); scopes are read live server-side.
- TTLs: access **24h**, refresh **60d** (sliding).

### ⚠️ Rotation hazard (drives persistence ordering)

Rotation is strict single-use with **no overlap / no grace window**. On `code:0` the old refresh
token is dead the instant the response arrives. Therefore:

> **Persist the new `{access_token, refresh_token}` durably BEFORE treating the refresh as
> complete; swap the in-memory token only AFTER persistence succeeds.**

A crash after the server rotates but before the client persists permanently bricks the agent
(must re-pair). Persist first, swap second.

### Per-plugin persistence semantics

The abstract rule above ("persist durably before swap") is implemented differently per plugin
and, for OpenClaw, per transport:

- **OpenClaw, store-backed (SQLite store available):** only the SQLite `rotateActivationTokens`
  write must succeed before the in-memory swap. If it reports a 0-row update (no matching
  activation row), the plugin self-heals by re-seeding the row from the in-memory account
  identity with the rotated pair (`login_method: "config-seed"`) and then proceeds as on
  success. Only a genuine SQLite write failure/exception keeps the refresh **transient** (no
  swap, back off, keep the current tokens). The channel-config file write is a **best-effort,
  logged mirror** that runs *after* the SQLite write succeeds or self-heals — its failure does
  **not** block the swap.
- **OpenClaw, store-less (no SQLite store, e.g. test transports):** unchanged — the
  channel-config write is still required before the swap; its failure keeps the refresh
  transient.
- **Hermes:** unchanged — both `.env` and SQLite must be written before the swap (§C.2).

---

## A. Refresh timing

### A.0 Knowing expiry — decode the JWT `exp` locally

Decode the access token's `exp` claim (base64url-decode the payload segment, read `exp` as
epoch seconds). **Fallback** if unparseable / no `exp`: `activated_at + 24h` from the
`activations` row. Do **not** persist a separate expiry column — derive it from the token on
each load.

- OpenClaw: add `decodeJwtExp(token): number | null`.
- Hermes: add `_jwt_exp(token) -> int | None` next to the existing `_jwt_claim`.

### A.1 Proactive refresh

Compute `refresh_at = exp - max(30min, min(2h, 0.25 * (exp - iat)))`, plus `±5min` jitter
(avoids a fleet-wide synchronized refresh storm). For a 24h token this fires ~2h before expiry.

- OpenClaw: a `setTimeout`-driven `refreshTimer`, armed when a connection becomes ready
  (from the live token's `exp`), re-armed after every successful refresh, cleared on
  disconnect/stop.
- Hermes: fold the `exp` check into the existing per-connection `_watch_activation_credentials`
  2s READY loop (or a sibling asyncio task on the supervisor's event loop).

Flow: reach `refresh_at` → call refresh → persist (§0 ordering) → swap in-memory → **close WS
and reconnect with the new token** (§D) → re-arm timer from the new token's `exp`.

### A.2 Reactive refresh

1. **REST 401/403** — both clients classify these as an `auth` error today. On such an error
   from any authenticated REST call, run the single-flight refresh; on success rebuild the
   api-client with the new token and **retry the original call once**.
2. **WS `hello-fail`** — refresh **only** on genuine token rejection:
   - reason is **exactly** `"authentication failed"` (msghub §3.5 — equality, not a regex or
     substring; this is the *only* terminal reason) → refresh.
   - reason matches auth-backend-unavailable (5xx: "auth service unavailable" / "temporarily
     unavailable") → **backoff-reconnect with the same token, do NOT refresh**.
   - **any other reason — `nonce mismatch`, `invalid connect …`, and any string msghub adds
     later** → transient: backoff-reconnect with the same token, **no refresh triggered by the
     reason**. The reason string never decides a refresh here; that is what keeps a new
     server-side reason from becoming a fleet-wide refresh storm (refresh tokens are single-use).
   - A client **MAY**, before that transient reconnect, run the §A.1 proactive refresh early
     when the **local `exp`** is already within the proactive margin — reconnecting with a token
     about to expire would only produce `"authentication failed"` and a refresh anyway. The
     trigger is the local clock, not the reason; on a transient/skipped refresh outcome the
     client still backoff-reconnects with the current token and never tears down.

   Implementation status (2026-08-27): both adapters match `"authentication failed"` by exact
   equality (trim + case-fold) — hermes `_is_token_rejected` (`connection.py`, since 2026-08-23),
   openclaw `isTerminalHelloFailReason` (`src/ws-client.ts`) + `classifyHelloFailReason`
   (`src/runtime.ts`) — and route every other reason as transient with the outbound queue
   intact. Hermes implements the optional early-refresh; openclaw does not (its ws-client
   backoff-reconnects without consulting the runtime), which is within the MAY.

   On the refresh-eligible branch: attempt one single-flight refresh. Success → reconnect with
   new token. Permanent → auto-logout (§C).

### A.3 Single-flight / no-hot-loop guard (mandatory)

"Do not hot-loop the same token." One in-flight refresh per process:

- **In-flight dedupe:** concurrent callers (proactive timer + reactive 401 + reactive hello-fail)
  await the same in-flight promise/future rather than firing parallel refreshes.
- **Rejected-token latch:** record the access token a refresh was attempted for; do not
  re-attempt for the *same* access token until it actually changes. Reuses the existing
  `rejectedActivationToken` / `_rejected_activation_token` seam.
- **Minimum interval:** floor (≥30s) between refresh attempts of the same token, so a
  reconnect storm cannot become a refresh storm.

### A.4 Startup — refresh-if-near-expiry before first connect

On boot, before the first WS connect: load the stored access token, decode `exp`. If past or
within the proactive margin and a `refresh_token` exists, run a synchronous single-flight
refresh, persist, then connect with the fresh token (this recovers a long-stopped pod with no
manual re-pair). If refresh returns PERMANENT → auto-logout immediately (skip the doomed connect).

---

## B. Refresh failure handling

| Outcome | Detected as | Class | Action |
|---------|-------------|-------|--------|
| Success | `code == 0` | — | persist → swap → reconnect |
| Invalid refresh (not found/revoked/expired/device mismatch) | `code == 10003` | **PERMANENT** | **auto-logout + re-pair prompt** |
| Bad request | `code == 400` | **PERMANENT (bug)** | log; auto-logout |
| Internal error | `code == 1` | **TRANSIENT** | retry w/ backoff |
| HTTP non-200 (500/LB) | transport | **TRANSIENT** | retry w/ backoff |
| Network error / timeout / DNS | exception | **TRANSIENT** | retry w/ backoff |

- **Backoff for transient:** `min(30s, 1s * 2^(n-1)) ± jitter`, cap 30s. Retry effectively
  unbounded but rate-limited (mirrors the WS supervisors that retry forever). A transient
  refresh failure **never** auto-logs-out — no rotation was committed, so the old refresh token
  is still valid; keep the WS in backoff with the current access token and keep retrying refresh.
- **Transient→permanent escalation:** a network failure *after* the server committed the
  rotation but before the response is indistinguishable from transient; the next retry then
  returns `code:10003`. Rule: escalate to PERMANENT (auto-logout) **only when a subsequent
  attempt returns `code:10003`** — never on transient alone.

---

## C. Auto-logout on permanent expiry

"Logout" = the **refresh token is permanently invalid** (`code:10003`, or persistent `400`
device-mismatch), so the agent can no longer mint tokens and a human must re-pair with a fresh
single-use connect code.

### C.0 The fork (be explicit)

- **Access expired BUT refresh valid → NOT logout.** Refresh, persist, reconnect. Silent and
  automatic. This is the common 24h case.
- **Refresh permanently invalid → DO logout.**

### C.1 What logout does (both plugins)

1. **Clear credentials atomically:** blank `token` / `refreshToken` in the config (OpenClaw) or
   remove `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` from `.env` (Hermes), AND blank the
   `access_token` / `refresh_token` columns of the `activations` row. **Keep** `user_id` /
   `owner_user_id` / `agent_id` so re-pair reuses the same identity (re-pair mode).
2. **Status:** set the account to not-connected / not-configured / not-running with
   `lastError` = "token expired — re-pair required", via the existing auth-failure path.
3. **Recovery entrypoint:** human runs `/clawchat-activate <CODE>` (or `openclaw channels
   login` / `hermes clawchat activate <CODE>`), which re-feeds credentials the
   wait-for-activation loop picks up — no process restart needed.
4. **User-visible notification** (decision): emit a status/chat message in addition to logs:
   *"ClawChat token expired and could not be refreshed. Re-pair with `/clawchat-activate <code>`."*
   Keep wording identical across both plugins.

### C.2 Hermes recovery unification (decision)

On every **successful** refresh, write the rotated token to **both** `.env` and SQLite and set
`_using_activation_db_credentials = True`. This moves an env-booted process onto the
SQLite-credentials path, so a future permanent expiry self-recovers via
`_wait_for_activation_credentials` instead of `_stopping = True` + gateway restart.

---

## D. WebSocket continuation

A token only enters via the `connect` envelope of a **new** socket; a refreshed token cannot be
hot-swapped onto a live WS. Always **close then reconnect** with the new token, reusing the same
`device_id` to preserve the replay cursor.

| Scenario | WS behavior |
|----------|-------------|
| Proactive refresh succeeds while WS healthy | persist → **close WS** gracefully → reconnect with new token in a fresh `connect`. |
| Reactive refresh after `hello-fail(auth)` | socket already closed by the server; after refresh the supervisor reconnects with the new token. |
| Refresh in-flight | supervisor must **not** open a socket with the dead token — gate reconnect on the in-flight refresh. On success reconnect immediately (reset backoff); on transient-fail keep backoff with the current token. |
| Permanent failure → WS stops | OpenClaw: auth-failed latch stops the reconnect loop, runtime flips not-configured. Hermes: SQLite-creds path → WS stops, supervisor falls into wait-for-activation; (with §C.2 the env path is converted to the SQLite path on first refresh). |

**Never** refresh/reconnect on non-auth closes (duplicate-session takeover, missed pongs,
backpressure, handshake-timeout) — backoff-reconnect with the same token + device id. Two of
those closes carry timing the client must honour: **`4001`** (takeover) → raise the reconnect
floor to ≥ 5 s (msghub §3.6); **`4002`** (`duplicate_session_throttled`, opt-in) → wait the JSON
close reason's `retry_after_ms` (60 s, doubling to 300 s) — longer than openclaw's `maxDelay`
of 15 s. Neither client reads the close code today.

---

## E. Device-id (refresh precondition)

The refresh endpoint requires `X-Device-Id` equal to the **connect-time** device id. Neither
plugin stores it today.

- **OpenClaw:** sends the constant `CHANNEL_ID = "clawchat-plugin-openclaw"` as `x-device-id` on
  every REST call including `/v1/agents/connect`, so that same constant is the correct refresh
  device id. (Do NOT use the hostname-derived WS id or any `resolved_device_id` — those are not
  the session device id.)
- **Hermes:** uses `get_device_id()` (env `CLAWCHAT_DEVICE_ID` verbatim if `hermes-`-prefixed,
  else host fingerprint). Risk: an unpinned fingerprint changes on pod reschedule → refresh
  fails `10003` device-mismatch → spurious logout.

**Fix (both):** add a `device_id` column to `activations`; at connect, write the **exact**
`x-device-id` used (OpenClaw: `CHANNEL_ID`; Hermes: `get_device_id()` evaluated at connect). At
refresh, read that column and send it verbatim. Backfill for legacy rows (no column value): fall
back to the deterministic connect-time value. Hermes additionally requires `CLAWCHAT_DEVICE_ID`
to be pinned (document + boot warning if unpinned).

---

## F. Test obligations (per plugin, kept in parity)

- JWT `exp` decode: valid / no-exp / malformed → fallback.
- Refresh client: `code` matrix (0 rotated / 10003 permanent / 400 permanent / 1 transient /
  non-200 transient); asserts `X-Device-Id` = stored device id and **no** Authorization header.
- Single-flight (N concurrent callers → 1 HTTP call), rejected-token latch, min-interval.
- Proactive timer fires at `refresh_at`; startup refresh-if-near.
- `hello-fail` gating: exact `"authentication failed"` → refresh; every other reason → backoff
  with the same token, no reason-triggered refresh (optional early §A.1 refresh on local `exp`).
- Persistence ordering — OpenClaw, store-backed: SQLite write (or self-heal config-seed
  re-write on a 0-row update) succeeds **before** in-memory swap; the channel-config mirror
  write happens after and its failure does not block the swap.
- Persistence ordering — OpenClaw, store-less, and Hermes (unchanged): rotated token written to
  both stores **before** in-memory swap.
- Permanent failure clears creds in both stores, flips not-configured, emits the user message.
- WS: proactive success closes old socket + reconnects with new token; no reconnect with dead
  token while refresh in-flight.
- Transient→`10003` escalation auto-logs-out.
