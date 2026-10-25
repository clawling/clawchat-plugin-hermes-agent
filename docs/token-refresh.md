# ClawChat Agent Plugins — Token Refresh & Auto-Logout

> **Independently maintained guides.** Each adapter keeps its own version at
> `clawchat-plugin-openclaw/docs/token-refresh.md` and
> `clawchat-plugin-hermes-agent/docs/token-refresh.md`. Behavior differences
> are noted here; the files are not required to match. Keep section numbers
> stable — source comments in both repos cite them by
> number (`token_refresh.py` §A/§B/§C, `api_client.py` §0, `adapter.py` §C.1).

For implementers: this guide describes Hermes refresh behavior and compares
the peer adapter where noted. Credential races are specified once in the Hermes
appendix (§H).

Peer adapters:

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
| Hermes device id | Activation persists the connect-time `device_id`; refresh reuses it. Pin `CLAWCHAT_DEVICE_ID` when pairing without durable SQLite; boot warns when only a volatile fallback is available. |
| Hermes recovery | Successful refresh persists to **both** `.env` and SQLite before adoption. Confirmed current permanent failures log out and wait for activation; uncertain or stale results follow §H. Adoption passes the access-token latch and fingerprint guard. |
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

OpenClaw applies the same classification, including the non-200 `10003`
row. The failure matrix in §B and logout rule in §C describe Hermes.

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
window only when `auth.refresh_grace_enabled` is enabled (default off); its
duration, minimum replay age, and replay budget are server-controlled.
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
- **Hermes:** conditional SQLite write, then `.env` mirror; both must succeed
  before the swap (§H.4). Partial-write recovery and windows are in §H.7.
- **OpenClaw, named accounts:** the same rules apply per account; the config mirror for a
  named account writes `channels.<id>.accounts.<accountId>.token` / `.refreshToken`, while the
  `default` account keeps writing the root `channels.<id>.token` / `.refreshToken`.

---

## A. Refresh timing

### A.0 Knowing expiry — decode the JWT `exp` locally

Decode the access token's `exp` claim (base64url-decode the payload segment, read `exp` as
epoch seconds). **Client fallback assumption** if unparseable / no `exp`:
`activated_at + 24h` from the
`activations` row (`access_token_exp` in `token_refresh.py`). This is a client
fallback, **not a server TTL**. Without either a usable `exp` or activation
timestamp, Hermes cannot schedule proactive refresh. Do **not** persist a
separate expiry column — derive it on each load.

- OpenClaw: `decodeJwtExp(token): number | null`.
- Hermes: `_jwt_exp(token) -> int | None` next to `_jwt_claim` in `config.py`.

### A.1 Proactive refresh

Compute `refresh_at = exp - max(30min, min(2h, 0.25 * (exp - iat)))`.
Hermes runtime checks `now >= refresh_at` without jitter through
`is_token_near_expiry`; the jittered `refresh_at_epoch` helper is not used by the
connection. If `iat` is absent or not earlier than `exp`, the quarter-life term
uses the client's 24h fallback constant, not a server TTL.
For example, a hypothetical 24h token reaches the margin about 2h before expiry.
OpenClaw adds `±5min` scheduling jitter.

- OpenClaw: a `setTimeout`-driven `refreshTimer`, armed when a connection becomes ready
  (from the live token's `exp`), re-armed after every successful refresh, cleared on
  disconnect/stop.
- Hermes: the per-connection `_watch_activation_credentials` READY loop calls
  `_maybe_proactive_refresh` after checking for changed or revoked credentials.

Proactive flow: reach `refresh_at` → refresh → persist (§0) → swap → close WS
and reconnect (§D). Hermes checks the new token on subsequent watcher polls;
OpenClaw re-arms its timer.

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

   Both adapters match `"authentication failed"` by exact
   equality (trim + case-fold) — Hermes `_is_token_rejected` (`connection.py`),
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
- **Rejected credentials:** Hermes uses the access-token latch and durable refresh
  fingerprint guard in §H.1, plus the manager's full-pair latch in §H.6.
- **Minimum interval:** Hermes `RefreshManager.refresh` floors new refresh calls
  for the same access token at 30s. Internal transient retries use §B instead:
  known-not-rotated failures may retry sooner. A changed access token bypasses
  that manager floor.

### A.4 Startup — refresh-if-near-expiry before first connect

On boot, before the first WS connect: load the stored access token, decode `exp`. If past or
within the proactive margin and a `refresh_token` exists, run a synchronous single-flight
refresh, persist, then connect with the fresh token (this recovers a long-stopped pod with no
manual re-pair). If refresh returns PERMANENT → auto-logout immediately (skip the doomed connect).

---

## B. Refresh failure handling

| Outcome | Detected as | Class | Action |
|---------|-------------|-------|--------|
| Success | HTTP 200 + integer `code == 0` | — | persist → swap → continue per §D |
| Invalid refresh (not found/revoked/expired/device mismatch) | integer `code == 10003` | **PERMANENT** | **auto-logout + re-pair prompt** |
| Bad request | HTTP 200 + integer `code == 400` | **PERMANENT (bug)** | log; auto-logout |
| Internal error | HTTP 200 + integer `code == 1` | **TRANSIENT** | retry w/ backoff |
| HTTP non-200 + invalid refresh | parseable JSON integer `code == 10003` | **PERMANENT** | auto-logout + re-pair prompt |
| Other HTTP non-200 (including `code == 400`) | transport | **TRANSIENT** | retry w/ backoff |
| Network error / timeout / DNS | exception | **TRANSIENT** | retry w/ backoff |

This matrix classifies the HTTP result; adopting a permanent result also requires
the request-pair ownership checks in §H.3 / §H.6.

- **Backoff for transient:** `min(30s, 1s * 2^(n-1)) ± jitter`, cap 30s. Retry effectively
  unbounded but rate-limited (mirrors the WS supervisors that retry forever). A transient
  refresh failure **never** auto-logs-out. Keep the current credentials and retry;
  a retry may recover a lost rotation only if the server's grace rules permit it.
  Source deletion or suspension stops retries (§H.5).
- **Attempt deadline:** Hermes stops awaiting a refresh attempt (request **and**
  response body) after **20s**, reporting **TRANSIENT** with unknown outcome.
  The urllib worker cannot be cancelled: it may continue and rotate server-side;
  its late result is discarded. Each socket operation has a 15s timeout, which
  does not bound DNS or the complete request. Worker-held ownership locks remain
  held until the underlying request finishes (§A.3).
- **Retry within the grace window:** after a transient whose outcome is unknown (deadline hit,
  connection reset, non-200 — anything except a failure that provably never reached the server
  or an explicit `code:1`), Hermes schedules the next attempt with the same
  refresh token **31–35s after the previous attempt began**, subject to worker
  and event-loop delays (30s spacing plus 1–5s jitter). Whether a retry remains eligible for grace depends
  on the server's configured window and replay budget. This applies to the
  proactive path too: Hermes retries within the same refresh call instead of
  waiting for another watcher poll.
- **Transient→permanent escalation:** a network failure *after* the server committed the
  rotation but before the response is indistinguishable from transient. The next retry inside
  the grace window may get `code:0` if grace is enabled and the token age,
  active rotation chain and replay budget qualify; timing alone does not guarantee it.
  If a subsequent attempt still returns `code:10003` — the window has passed, the token was
  explicitly revoked, or the replays are used up — escalate to PERMANENT (auto-logout) as
  before. Rule: escalate **only when a subsequent attempt returns `code:10003`** — never on
  transient alone. A client cannot and must not try to tell an out-of-window `10003` apart.

---

## C. Auto-logout on permanent expiry

"Logout" follows integer `code:10003` (including a non-200 JSON response), or
HTTP 200 + integer `code:400` (a bad request/client bug). A non-200 `400` is transient
and never logs out. A current, confirmed permanent result requires the owner's
identity-bound reconnect prompt; stale or unconfirmed results follow §H.3 / §H.6.

### C.0 The fork (be explicit)

- **Access expired BUT refresh valid → NOT logout.** Refresh, persist, reconnect. Silent and
  automatic.
- **Refresh permanently invalid → DO logout.**

### C.1 What logout does (both plugins)

1. **Clear credentials in separate store writes:** blank `token` / `refreshToken` in the config (OpenClaw) —
   in the slot of the affected account (root for `default`, `accounts.<accountId>` otherwise) —
   or write empty `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` tombstones to `.env`
   (Hermes), AND blank the `access_token` / `refresh_token` activation columns.
   There is no cross-store transaction. Hermes ownership checks, marker order,
   partial clear results and its complete four-step sequence are in §H.1 / §H.6. **Keep** `user_id` /
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

Successful refresh persists SQLite then `.env` before adopting the pair and
setting `_using_activation_db_credentials = True` (§H.4). SQLite takes startup
precedence over env/extra bootstrap credentials. Current, confirmed permanent
failure waits for activation across startup, proactive, REST and hello-fail
paths, including env bootstrap; no prior refresh or restart is needed.
The four-step logout, adoption guards and exceptions are specified in §H.

---

## D. WebSocket continuation

A token only enters via the `connect` envelope of a **new** socket; a refreshed token cannot be
hot-swapped onto a live WS. Proactive success closes and reconnects with the new
token, reusing the same `device_id` to preserve the replay cursor. Hermes REST
refresh instead keeps a healthy socket; its next connection uses the new pair.

| Scenario | WS behavior |
|----------|-------------|
| Proactive refresh succeeds while WS healthy | persist → **close WS** gracefully → reconnect with new token in a fresh `connect`. |
| Hermes REST 401/403 refresh succeeds | persist → swap → retry the original REST call once; `reactive_refresh()` calls `_attempt_refresh(close_ws_on_success=False)`, leaving the healthy WS open. |
| Reactive refresh after `hello-fail(auth)` | socket already closed by the server; after refresh the supervisor reconnects with the new token. |
| Refresh in-flight | supervisor must **not** open a socket with the dead token — gate reconnect on the in-flight refresh. On success reconnect immediately (reset backoff); on transient-fail keep backoff with the current token. |
| Permanent refresh failure → WS stops | OpenClaw: auth-failed latch stops reconnects, runtime flips not-configured. Hermes: a current, confirmed result takes §H.1; stale or uncertain results take §H.3 / §H.6. |

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
When pairing without durable SQLite, pin `CLAWCHAT_DEVICE_ID`, using a different
value per profile. Boot warns when the resolved id is volatile. See
[device-id durability](./configuration.md#device-id-durability-deployment-requirement).

---

## F. Test obligations (Hermes, with peer differences noted)

- JWT `exp` decode: valid / no-exp / malformed → fallback.
- Refresh client: HTTP 200 `code` matrix (0 rotated / 10003 permanent / 400 permanent /
  1 transient); non-200 + parseable JSON integer 10003 permanent, every other non-200
  response (including 400) transient (OpenClaw asserts the same matrix). Assert `X-Device-Id` = stored device id and
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
- WS: proactive success reconnects with the new token; REST success keeps a healthy WS; no reconnect with dead
  token while refresh in-flight.
- (server returns 10003 after the grace window) Transient→`10003` escalation auto-logs-out.
- Hermes stops waiting at the 20s deadline and classifies TRANSIENT; a continuing
  urllib worker's late result is ignored and its ownership leases remain held.
- After a transient whose outcome is unknown (including on the proactive path),
  Hermes schedules retry with the same refresh token 31–35s after the previous
  attempt began. Success persists the rotated pair; a current `10003` takes the
  permanent path. The manager coalesces concurrent callers, although a timed-out
  urllib worker may still be running.

---

## H. Hermes appendix — credential lifecycle and races

This appendix is the implementation reference for Hermes credential deletion,
revocation and suspension; the operator summary is in
[activation](./activation.md#credential-deletion-and-revocation).

### H.0 Credential sources and revocation

Deletion stops a SQLite-backed session; server revocation requires authentication evidence.

A never-activated process (no SQLite row and no complete env/extra bundle) polls
for activation every 2 seconds without connect or refresh; logs `activation_wait`
and DEBUG `activation_poll`.

For a SQLite-sourced session, a source check returning `None` (deleted row or
cleared credentials) logs WARNING `activation_credentials_revoked action=wait_activation
reason="activation credentials deleted"`, clears the
in-memory access/refresh pair and identity, latches the old access token, enters
`AUTH_FAILED` and closes the WS. Checks run in the watcher, before every connect
(including after backoff), before refresh and during transient retry. The
supervisor waits for activation without reconnect or refresh using the old pair;
this local deletion sends no refresh-permanent auto-logout message. Read errors
follow §H.2 rather than being treated as deletion.

A server-side session revocation may kick the socket with no status / `1005`.
That close alone proves nothing: reconnect uses normal backoff. A subsequent
`hello-fail` `authentication failed` triggers refresh; a permanent result such as
HTTP 401 with integer `code:10003` logs `hello_fail_refresh` and
`auth_logout action=re_pair_required`, then takes §H.1. The rejected token is
not reconnected.

When the server rejects the token and there is no refresh token, the existing
`auth_failed` branch applies: SQLite-sourced credentials log
`action=wait_activation` and wait for activation; env/extra credentials log
`action=stop_reconnect` and stop that supervisor. Transient errors
(`remote auth service unavailable`, 5xx, timeouts) still retry or
backoff-reconnect per §A / §B and never revoke credentials. `4001` / `4002`
keep their takeover / duplicate-session meaning. Terminal `4010` instead preserves
credentials and waits for explicit local `hermes clawchat reclaim` or
`/clawchat-reclaim`, without reconnect or refresh, including after restart.
Successful activation (also `--repair` / `--new-account`) clears this state.

After the owner re-activates, the poll adopts the new row (its access token
differs from the latched one and passes the refresh fingerprint check), logs
`activation_loaded` and reconnects without a process restart. If the row still
exists but its token changed, the watcher keeps logging
`activation_credentials_changed`, closes the old WS and reconnects with the new
token (§D); that is not the revocation path.

### H.1 Confirmed permanent logout and adoption guards

A current, confirmed permanent refresh failure records a fingerprint before clearing credentials and waiting for activation.

1. Writes the rejected refresh token's SHA-256 hex fingerprint and rejection
   time to SQLite (`revoked_refresh_tokens`, migration 12), before clearing
   credentials. The marker never contains the plaintext token.
2. Writes empty `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` tombstones in `.env`
   and blanks the SQLite `activations` token columns, preserving identity.
   Each clear result is logged; a failed clear leaves the durable guard active.
3. Clears in-memory credentials, sets `AUTH_FAILED`, closes the WebSocket and
   waits for new activation for startup, proactive, REST and hello-fail failures.
   On restart and during activation polling, a SQLite marker or in-memory
   fingerprint blocks empty refresh tokens and tokens matching that fingerprint.
   A different refresh token can be adopted only if its access token differs from the
   rejected access-token latch; only then are the latch and marker cleared.
   Without a marker, access-token-only setups still connect unless that access
   token has already been rejected by hello. Fresh installs keep their original
   waiting status. A marker read failure preserves credentials; the in-memory
   fingerprint guard and access-token latch still apply. Observing a changed row
   alone never clears the latch; only adopting credentials that pass both guards does.
4. Surfaces a user-visible message (in addition to logs):

   > ClawChat token expired and could not be refreshed. Ask your owner to send you the reconnect prompt from the ClawChat app, then follow https://agent-connection.clawling.com/reconnect.md.

Ownership uncertainty and late-result exceptions are specified in §H.3 and §H.6.

### H.2 Unreadable SQLite and SUSPENDED

Three consecutive credential-read failures suspend a SQLite-backed session without revoking its credentials.

For SQLite-sourced connections, credential reads share one consecutive-failure
counter; any successful credential read resets it. The first two failures keep
the existing connection/retry behavior, while a failed persistence read always
refuses its write. At `CREDENTIAL_READ_FAILURE_LIMIT` (currently 3), log WARNING
`activation_credentials_unreadable action=suspend`, enter `SUSPENDED`, close the
WS and stop reconnecting and refreshing. Suspension retains the in-memory
credentials, writes no credential stores or revoked marker, latches no rejected
token and sends no auto-logout message. Env/extra bootstrap connections and
never-activated polling are unaffected by this counter.

The disconnected connection record is finalized as `suspended`, including its
disconnect timestamp, before recovery starts a new connection record.

While suspended, the supervisor checks SQLite every 2 seconds. A readable row
matching the retained credentials resumes connecting; a changed row is adopted
and connected with its new token. Recovery logs
`activation_credentials_readable action=reconnect`. A readable `None` instead
takes the deletion/revocation path above. The same source check runs immediately
before every `CONNECTING` transition and every refresh request: deletion stops
both, and rotation adopts the new pair for connect or skips the old refresh.

Each connection attempt retains its credential generation and token. If
suspension, revocation, rotation or stopping invalidates an in-flight connect or
handshake, the returned socket is closed with a timeout; it cannot send a stale
connect frame or enter `READY`. Recovery does not revive that old attempt.

### H.3 Bootstrap suspension and pending permanent failures

An unconfirmed permanent result waits for credential ownership to become readable before logout.

An env/extra bootstrap session also suspends immediately if a permanent refresh
cannot clear a matching SQLite row and strict `.env` reading is unavailable
(`env_matched is None`). It logs `activation_credentials_unreadable action=suspend`,
closes the WS and stops refresh/reconnect while retaining its in-memory pair.
The unconfirmed failure writes no new rejection marker, latches no rejected pair
and sends no auto-logout message. Bootstrap rejection markers are always deferred
until logout confirms ownership, even when `.env` was readable at the rejection
callback. Normal bootstrap logout with a matching pair or absent
`.env` keys still writes tombstones and stops the rejected session.

Bootstrap suspension polls every 2 seconds too, retaining the pending permanent
failure's credential pair and reason. SQLite sessions also retain pending permanent
failures when callback source checks reach the read-failure suspension limit,
using the same recovery rules. Once SQLite or strict `.env` reads confirm
that same pair (absent `.env` keys also confirm ownership), the gateway completes
conditional logout, writes the revoked marker and latches the rejected pair,
then enters `AUTH_FAILED` with auto-logout notification. It sends no connect frame
and makes no additional HTTP refresh request. A new SQLite pair, including a
refresh-only change, instead discards the pending result and adopts the new
activation using the usual token/fingerprint checks before reconnecting.

Adoption, revocation and stopping clear the pending result. Generic source
suspension without a pending permanent failure can still resume the same pair.

If `.env` holds a different pair, the gateway retains suspension and logs
`activation_credentials_changed action=wait_activation`; tokens alone do not
establish the replacement identity, so write a new SQLite activation to recover.
Observing a different `.env` pair does not discard the pending failure: only
actually adopting new activation credentials does. If `.env` returns to the
original pair before adoption, the pending permanent failure completes logout.
SQLite observation without adoption also retains the pending result.
If SQLite cannot be read, or there is no usable row and `.env` remains
unreadable, suspension continues without connecting or refreshing.

Outside suspension, a confirmed different `.env` pair (`env_matched is False`)
keeps the existing late-result discard behavior and does not trigger suspension.

### H.4 Conditional refresh persistence

Only the complete request pair may rotate, with SQLite committed before the `.env` mirror.

If the row is deleted while a refresh is in flight, the persist callback
re-checks the credential source and revocation state. For a connection using
SQLite credentials, persistence conditionally updates the row only if its
access and refresh token still match the request's original pair. Even a
refresh-only change rejects an old successful response. A cleared, rotated or
deleted row rejects the write without seeding or touching `.env`; a successful
SQLite update happens **before** the `.env` mirror. A late successful result
cannot restore discarded in-memory credentials or overwrite a newly adopted
activation.

For env/extra bootstrap without a row yet, a missing row is normal
and the watcher keeps the socket. Bootstrap refresh persistence also rechecks
SQLite and binds the write to the full request pair. It first conditionally
updates a matching row; when no row exists it may atomically seed one with
`INSERT ... ON CONFLICT DO NOTHING`. A different or concurrently inserted row
rejects the write. Strict `.env` reads must confirm the request pair (or absent
token keys) before seeding and again before mirroring: unreadable or changed
`.env` is never overwritten. SQLite succeeds before the `.env` write.

A refused
persist result remains transient and does not swap the in-memory pair. SQLite
and `.env` are separate writes: a failure or change after the SQLite commit can
leave that rotated row persisted while the `.env` mirror and memory stay old.

### H.5 Source checks during transient retries

Transient retries stop when their credential source is deleted or becomes suspended.

An in-flight transient refresh also checks the credential source after each
transient failure and before and after its backoff sleep. Even with unbounded
retries, a deletion observed at those checks closes the WS and stops the next
HTTP attempt, then the supervisor waits for activation. The last HTTP error
remains transient, so this does not send a permanent-failure auto-logout
message. Repeated store read failures reach the same suspension limit, stopping
the in-flight retry loop before another HTTP attempt without clearing the
retained credentials.

### H.6 Late permanent results and observer races

A permanent result may clear only the pair that started its request.

Permanent-failure rejection and logout callbacks are bound to the pair that
started the request, just like successful token persistence. If that pair no
longer matches memory, the connection is already auth-failed/suspended, or the
SQLite row's access/refresh pair has changed, the callbacks discard the late
result and log INFO `refresh_result_discarded action=ignore`. They do not clear
the newer row or memory, write a revoked marker or send auto-logout.

Logout's SQLite clear also
requires both the expected access and refresh token, preserving a row whose
pair changes before that write. A credential read failure below the suspension
limit still permits permanent-failure handling when the request's pair matches
memory; the SQLite clear remains conditional on the complete request pair, including env/extra
bootstrap requests. The `.env` clear also requires the stored values to match
that pair: an unreadable `.env` or a different stored value is preserved, while
absent keys (credentials supplied via the process environment or extra config)
still receive the empty tombstone so a restart does not re-read the dead token.
Read failures still count toward the same suspension limit. When a bootstrap request clears
no SQLite row and `.env` contains a different pair, its permanent result is
discarded, allowing a refresh-only replacement to load.

The refresh manager latches the
`(access_token, refresh_token)` pair only after both permanent-failure callbacks
accept the result. A discarded result leaves no new rejection latch or logout
flag, and a replacement refresh token is not rejected merely because its
access token is unchanged. The existing refresh interval floor still applies.

Terminal `hello-fail` credential changes finish before awaiting state observers,
so an observer adopting a new activation cannot have it cleared by the old
failure's continuation.

### H.7 Synchronous writes and cross-process windows

Event-loop ordering protects the local task, but SQLite and `.env` have no shared transaction.

Persistence still runs synchronously on the event loop,
intentionally keeping the credential guard and write in the same task without
an intervening `await`; slow storage can delay other tasks.
The `.env` comparison and helper write are separate steps; another process can
still change the file between them.
The SQLite-then-`.env` order narrows but does not close the cross-process
window: if another process clears both stores after the SQLite commit and
before the `.env` mirror is written, the mirror can still receive the rotated
pair (the next watcher poll still revokes the live connection, but a restart
could bootstrap from `.env`). If the SQLite update succeeds and the `.env`
write fails, the in-memory pair is not swapped; the watcher or a restart adopts
the SQLite pair, and `.env` is not retried on its own.

### H.8 Removing only SQLite is insufficient

Deleting the activation row alone does not keep the agent offline after restart.

Deleting only the SQLite row does not remove env/extra bootstrap tokens (for
example `CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` in the Hermes `.env`). If
they are still present, the next process start bootstraps from them again. To
take the agent offline for good, stop the gateway or remove the plugin and
clear those values as well.
