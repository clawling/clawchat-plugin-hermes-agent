# ClawChat Agent Plugins — Token Refresh & Auto-Logout

> **Independently maintained guides.** Each adapter keeps its own version at
> `clawchat-plugin-openclaw/docs/token-refresh.md` and
> `clawchat-plugin-hermes-agent/docs/token-refresh.md`. Behavior differences
> are noted here; the files are not required to match. Keep section numbers
> stable — source comments in both repos cite them by
> number (`token_refresh.py` §A/§B/§C, `api_client.py` §0, `adapter.py` §C.1).

This guide describes Hermes behavior and compares the peer adapter where noted:

- **OpenClaw** (TypeScript) — `clawchat-plugin-openclaw/`
- **Hermes** (Python) — `clawchat-plugin-hermes-agent/`

The two plugins are parallel adapters of one Protocol v2, maintained separately.
When changing the wire shape, update the server's Protocol v2 contract first,
then the plugin guides and implementations.

## Problem

Both plugins store a `refresh_token` from `POST /v1/agents/connect` and use it
for automatic refresh. Hermes implements proactive and reactive refresh,
auto-logout on permanent refresh failure, and WebSocket reconnection with the
new token. Access-token expiry comes from JWT `exp`; token lifetimes are
server-controlled.

## Confirmed product decisions

| # | Decision |
|---|----------|
| Scope | **Proactive + reactive** refresh (not reactive-only). |
| Hermes device id | Activation persists the connect-time `device_id`; refresh reuses it. Deployments must pin `CLAWCHAT_DEVICE_ID` for new pairings; boot warns when only a volatile fallback is available. |
| Hermes recovery | Successful refresh persists to **both** `.env` and SQLite before adoption. Every permanent refresh failure persists the revoked fingerprint, closes WS and waits for activation without a restart, including env bootstrap failures. Adoption must pass the access-token latch and fingerprint guard. |
| Auto-logout UX | On permanent failure, surface a user-visible status/chat message (in addition to logs). |

---

## 0. The refresh endpoint (shared contract)

`POST /v1/auth/refresh`  (NOT `/clawnext/auth/refresh`)

- **Unauthenticated** — send **no** `Authorization` header; the refresh token in the body is the credential.
- **Required header:** `X-Device-Id` — must equal the device id baked into the session at connect time (the backend rejects any mismatch against the session's device id). ≤128 bytes.
- **Body:** `{ "refresh_token": "<64-char hex>" }`.
- **Normally returns HTTP 200.** Hermes classifies both the HTTP status and the integer envelope `code`:

| HTTP status / integer `code` | meaning | class |
|--------|---------|-------|
| HTTP 200 + `0` | success; `data = { access_token, refresh_token }` — **rotated**; the old refresh token is dead for normal use (the backend only tolerates a replay of it inside the short grace window — see Rotation hazard) | success |
| HTTP 200 + `10003` (`CodeInvalidRefresh`) | refresh token not found / revoked / expired / **device mismatch** | **PERMANENT** → auto-logout |
| HTTP 200 + `400` | bad body / missing or oversized device id | **PERMANENT (client bug)** → auto-logout |
| HTTP 200 + `1` (`CodeInternal`) | server internal error (no rotation committed) | **TRANSIENT** → retry |
| non-200 + parseable JSON integer `10003` | invalid refresh, including an urllib HTTPError body | **PERMANENT** → auto-logout |
| all other non-200 responses (including `400`) / network errors | HTML, malformed JSON and string codes do not prove revocation | **TRANSIENT** → retry |

**Peer difference:** OpenClaw treats every non-200 refresh response as
**TRANSIENT**, regardless of body; it does not apply Hermes' non-200 `10003`
classification. The failure matrix in §B and logout rule in §C describe Hermes.

- The refreshed access token preserves the agent identity (`aid`/`oid`); scopes are read live server-side.
- Token lifetimes are determined by the server; use JWT `exp` for access expiry.

### ⚠️ Rotation hazard (drives persistence ordering)

Rotation is still single-use: on `code:0` the old refresh token is dead for normal use. The backend
adds a **replay grace window that exists only to cover attempts whose outcome the client could not
see** (the server rotated, the response was lost): the old token may be redeemed again
within a server-controlled window and replay budget, only with the same device id,
and only while the token that rotation issued has not itself been refreshed. Each
redemption revokes the token issued by the previous one. Outside the window — or after a logout /
re-pair / explicit revocation — reuse returns `code:10003` exactly as before. **Clients must not
rely on the window for anything else.** The server supports this refresh grace
window; its duration, minimum replay age, and replay budget are server-controlled.
Therefore:

> **Persist the new `{access_token, refresh_token}` durably BEFORE treating the refresh as
> complete; swap the in-memory token only AFTER persistence succeeds.**

A crash after the server rotates but before the client persists bricks the agent (must re-pair)
unless the old token is retried within the grace window. Persist first, swap second.

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
- **OpenClaw, named accounts:** the same rules apply per account; the config mirror for a
  named account writes `channels.<id>.accounts.<accountId>.token` / `.refreshToken`, while the
  `default` account keeps writing the root `channels.<id>.token` / `.refreshToken`.

---

## A. Refresh timing

### A.0 Knowing expiry — decode the JWT `exp` locally

Decode the access token's `exp` claim (base64url-decode the payload segment, read `exp` as
epoch seconds). **Client fallback assumption** if unparseable / no `exp`:
`activated_at + 24h` from the
`activations` row. Do **not** persist a separate expiry column — derive it from the token on
each load.

- OpenClaw: `decodeJwtExp(token): number | null`.
- Hermes: `_jwt_exp(token) -> int | None` next to `_jwt_claim` in `config.py`.

### A.1 Proactive refresh

Compute `refresh_at = exp - max(30min, min(2h, 0.25 * (exp - iat)))`, plus `±5min` jitter
(avoids a fleet-wide synchronized refresh storm). For a 24h token this fires ~2h before expiry.

- OpenClaw: a `setTimeout`-driven `refreshTimer`, armed when a connection becomes ready
  (from the live token's `exp`), re-armed after every successful refresh, cleared on
  disconnect/stop.
- Hermes: the per-connection `_watch_activation_credentials` READY loop calls
  `_maybe_proactive_refresh` after checking for changed or revoked credentials.

Flow: reach `refresh_at` → call refresh → persist (§0 ordering) → swap in-memory → **close WS
and reconnect with the new token** (§D) → re-arm timer from the new token's `exp`.

### A.2 Reactive refresh

1. **REST 401/403** — both clients classify these as an `auth` error. On such an error
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

Hermes canonical gateways additionally require host-local profile and identity
ownership before any refresh, including startup, activation polling, proactive
timers, REST recovery and hello-fail recovery. A contending gateway enters
`STANDBY` and skips refresh until it acquires the locks; activation polling takes
the identity lock before its refresh. Retry callbacks recheck ownership, and
the HTTP worker retains lock references until the request finishes even if its
async caller is cancelled. Unsupported locks warn and proceed unlocked;
standalone sibling-device senders are exempt. See
[configuration](configuration.md#host-local-single-instance-locks).

- **In-flight dedupe:** concurrent callers (proactive timer + reactive 401 + reactive hello-fail)
  await the same in-flight promise/future rather than firing parallel refreshes.
- **Rejected-token latch:** latch an access token rejected by hello or permanent refresh
  failure. Hermes startup and activation polling must not reload that access token,
  including access-token-only credentials with no refresh token or revoked marker.
  Adoption requires a different access token and a refresh token that passes the
  revoked-fingerprint guard. Clear the access latch only after adopting a different
  access token; observing a changed activation row does not clear it.
- **Durable refresh guard (Hermes):** persist the rejected refresh token's SHA-256 hex
  fingerprint and rejection time to SQLite before clearing either credential store.
  A present SQLite marker or in-memory fingerprint rejects an empty refresh token
  or one matching the fingerprint, regardless of its credential source. With no
  marker, preserve existing credentials, including access-token-only setups. Marker
  read failures fail open for this guard; the in-memory fingerprint and access-token
  latch still apply.
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
| Success | HTTP 200 + integer `code == 0` | — | persist → swap → reconnect |
| Invalid refresh (not found/revoked/expired/device mismatch) | integer `code == 10003` | **PERMANENT** | **auto-logout + re-pair prompt** |
| Bad request | HTTP 200 + integer `code == 400` | **PERMANENT (bug)** | log; auto-logout |
| Internal error | HTTP 200 + integer `code == 1` | **TRANSIENT** | retry w/ backoff |
| HTTP non-200 + invalid refresh | parseable JSON integer `code == 10003` | **PERMANENT** | auto-logout + re-pair prompt |
| Other HTTP non-200 (including `code == 400`) | transport | **TRANSIENT** | retry w/ backoff |
| Network error / timeout / DNS | exception | **TRANSIENT** | retry w/ backoff |

- **Backoff for transient:** `min(30s, 1s * 2^(n-1)) ± jitter`, cap 30s. Retry effectively
  unbounded but rate-limited (mirrors the WS supervisors that retry forever). A transient
  refresh failure **never** auto-logs-out — either no rotation was committed, so the old refresh
  token is still valid, or the retry below redeems it inside the grace window; keep the WS in
  backoff with the current access token and keep retrying refresh.
- **Attempt deadline:** every refresh attempt (request **and** response body) has a **20s total
  deadline**; hitting it aborts the attempt and counts as **TRANSIENT**. 20s stays below the 30s
  §A.3 minimum interval, so the floor, not the deadline, sets the retry cadence.
- **Retry within the grace window:** after a transient whose outcome is unknown (deadline hit,
  connection reset, non-200 — anything except a failure that provably never reached the server
  or an explicit `code:1`), the next attempt with the same refresh token starts **30–45s after
  the previous attempt began** (implementations: the 30s floor plus 1–5s jitter, or the floor
  plus WS reconnect backoff). Whether a retry remains eligible for grace depends
  on the server's configured window and replay budget. This applies to the
  proactive path too: a transient proactive refresh arms one retry instead of waiting for the
  next proactive arm.
- **Transient→permanent escalation:** a network failure *after* the server committed the
  rotation but before the response is indistinguishable from transient. The next retry inside
  the grace window gets `code:0` (the server's grace replay) and proceeds as a normal success.
  If a subsequent attempt still returns `code:10003` — the window has passed, the token was
  explicitly revoked, or the replays are used up — escalate to PERMANENT (auto-logout) as
  before. Rule: escalate **only when a subsequent attempt returns `code:10003`** — never on
  transient alone. A client cannot and must not try to tell an out-of-window `10003` apart.

---

## C. Auto-logout on permanent expiry

"Logout" follows integer `code:10003` (including a non-200 JSON response), or
HTTP 200 + integer `code:400` (a bad request/client bug). A non-200 `400` is transient
and never logs out. A permanent failure requires re-pairing with a fresh single-use
connect code.

### C.0 The fork (be explicit)

- **Access expired BUT refresh valid → NOT logout.** Refresh, persist, reconnect. Silent and
  automatic.
- **Refresh permanently invalid → DO logout.**

### C.1 What logout does (both plugins)

1. **Clear credentials atomically:** blank `token` / `refreshToken` in the config (OpenClaw) —
   in the slot of the affected account (root for `default`, `accounts.<accountId>` otherwise) —
   or write empty `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` tombstones to `.env`
   (Hermes), AND blank the `access_token` / `refresh_token` activation columns.
   Hermes first writes the fingerprint/time marker independently of either clear,
   then reports and logs each store's clear result. **Keep** `user_id` /
   `owner_user_id` / `agent_id` so re-pair reuses the same identity (re-pair mode).
2. **Status:** set the account to not-connected / not-configured / not-running with
   `lastError` = "token expired — re-pair required", via the existing auth-failure path.
3. **Recovery entrypoint:** the owner sends the agent the **reconnect prompt** from the
   ClawChat app (a connect code bound to this identity; the agent follows the public
   reconnect page). `/clawchat-activate <CODE>` / `openclaw channels login` /
   `hermes clawchat activate <CODE>` remain the mechanics underneath; the bound code
   usually restores the incumbent identity on its own, and if activation still reports
   it as already paired, run it again with `--repair`. Credentials are re-fed to the
   wait-for-activation loop — no process restart needed.
4. **User-visible notification**: emit a status/chat message in addition to logs:
   *"ClawChat token expired and could not be refreshed. Ask your owner to send you the
   reconnect prompt from the ClawChat app, then follow
   https://agent-connection.clawling.com/reconnect.md."*
   Keep wording identical across both plugins.

### C.2 Hermes recovery unification

On every **successful** refresh, write the rotated token to **both** `.env` and SQLite
before adopting it, and set `_using_activation_db_credentials = True`.

On permanent refresh failure, persist the revoked fingerprint/time marker before
clearing credentials, clear the in-memory pair, set `AUTH_FAILED`, close the WS and
wait for new activation. This applies equally to startup, proactive, REST-reactive
and hello-fail paths, including an env-booted process's first failure; no successful
refresh or gateway restart is required first.

SQLite takes precedence over the `.env`/legacy-config bootstrap copy. Startup load
and activation polling require both a different access token from the rejected
access-token latch and a usable refresh token under the fingerprint guard. Only
adopting a pair that passes both checks clears the access latch and revoked marker.
Without a marker, an empty refresh token alone does not block credentials. A failed
marker read preserves credentials while the in-memory guards still apply. Fresh
installs waiting for activation retain their original state/status.

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
| Permanent refresh failure → WS stops | OpenClaw: auth-failed latch stops reconnects, runtime flips not-configured. Hermes: persist revoked marker → clear creds → `AUTH_FAILED` → close WS → wait for activation, for SQLite and env bootstraps alike. Adoption must pass both the access-token latch and refresh-fingerprint guard (§C.2). |

Non-auth closes (duplicate-session takeover, missed pongs, backpressure,
handshake-timeout) never trigger token refresh. Ordinary non-auth closes reconnect
with backoff using the same token and device ID. Terminal **`4010`** is the exception
to reconnect: it persists superseded state and stops **all** refresh paths (startup,
proactive, handshake rejection, and REST 401), including sibling senders. Refresh
returns `skipped`, never permanent failure or auto-logout. The single owner notice
uses the existing token without refresh; if it fails it is not retried. Restart
keeps waiting until local reclaim or successful activation clears the state.
Two retryable closes carry timing the client must honour: **`4001`** (takeover) → raise the reconnect
floor per §3.6 of [the protocol guide](./client-integration.md#36-duplicate-session-policy);
**`4002`** (`duplicate_session_throttled`, takeover refusal or handshake limiting)
→ wait the JSON close reason's server-determined `retry_after_ms`.
Both clients read the close code: openclaw in `ws-client.ts`'s
`handleClose`, hermes in its supervisor loop in `connection.py`; each keeps its own
takeover streak and applies these floors.

---

## E. Device-id (refresh precondition)

The refresh endpoint requires `X-Device-Id` equal to the **connect-time** device id.
Both plugins persist that value in `activations.device_id` when pairing.

- **OpenClaw:** sends the constant `CHANNEL_ID = "clawchat-plugin-openclaw"` as `x-device-id` on
  every REST call including `/v1/agents/connect`, so that same constant is the correct refresh
  device id. (Do NOT use the hostname-derived WS id or any `resolved_device_id` — those are not
  the session device id.)
- **Hermes:** activation writes the exact `x-device-id` used for pairing.
  `_resolve_device_id()` prefers that stored value, then JWT `did`, then the
  legacy host id for already-paired credentials. Only a fresh pairing uses the
  current agent-scoped `get_device_id()` derivation. This preserves the paired id
  even if the host fingerprint changes.

At refresh, both plugins reuse the stored pairing id, with compatibility
fallbacks for legacy rows (OpenClaw: `CHANNEL_ID`; Hermes: the resolution above).
Hermes deployments must pin `CLAWCHAT_DEVICE_ID` for durable new pairings,
using a different value per profile. Boot warns when the resolved id is volatile.

---

## F. Test obligations (Hermes, with peer differences noted)

- JWT `exp` decode: valid / no-exp / malformed → fallback.
- Refresh client: HTTP 200 `code` matrix (0 rotated / 10003 permanent / 400 permanent /
  1 transient); non-200 + parseable JSON integer 10003 permanent, every other non-200
  response (including 400) transient. OpenClaw instead treats all non-200 responses
  as transient. Assert `X-Device-Id` = stored device id and
  **no** Authorization header; transient failures keep credentials and write no marker.
- Single-flight (N concurrent callers → 1 HTTP call), rejected-token latch, min-interval.
- Hermes access-token-only hello rejection: several activation polls never reconnect
  with the rejected access token; a different access token is adopted and reconnects.
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
- (server returns 10003 after the grace window) Transient→`10003` escalation auto-logs-out.
- A refresh attempt that reaches the 20s total deadline is aborted and classified TRANSIENT
  (verified with a fake fetch / `urlopen` that never returns).
- After a transient whose outcome is unknown (including on the proactive path), the next
  attempt with the same refresh token starts 30–45s after the previous attempt began; a
  success on that retry persists the rotated pair, a `10003` takes the permanent path, and no
  second concurrent refresh is started.
