# Instance locks and installation conflicts

For operators recovering a stopped installation, and implementers checking lock and
reclaim guarantees; use the runbook first, then the implementation specification.

## Operator runbook

```bash
hermes clawchat status
hermes clawchat reclaim
```

Run both in the intended Hermes profile on the installation you want to resume.

| Log / state | Meaning and action |
|---|---|
| `instance_lock_standby` / `STANDBY` | Another local gateway holds the profile or identity lock. Inspect the holder record; stop the unwanted holder. The waiting gateway retries within 30 s. |
| `instance_lock_acquired` | Local contention ended; INFO includes `waited_seconds`. |
| `install_superseded` / `SUPERSEDED` / close `4010` | Another installation displaced this one. Credentials remain valid; reclaim here to resume, which can displace the other installation. Restart alone preserves durable supersession. |
| `install_reclaimed` | Superseded wait ended and the connection resumes. |
| `cannot persist superseded installation` | Memory-only blocking state; follow the generation/restart recovery below. |

**Never delete a lock file while a gateway is running.** Kernel ownership, not file
existence, controls the lock; deleting it can let two processes lock different inodes at
the same path. Leftover files need no cleanup.

Run `hermes clawchat status` to inspect the short install ID, superseded time, winner
version, notice result, and instance-lock status when available in this process. Status
filters superseded state using the configured user ID, falling back to a read-only
SQLite activation identity lookup. It reports `identity` as the first eight characters
or `unknown`; an unknown identity keeps the fail-closed view. A different identity's
residual state reports `is_superseded: false` and `stale_state_for_other_identity:
true`.

A separate CLI process reports lock state as `unknown` and includes the last holder
record; that persisted record alone does not prove a running holder. Run `hermes
clawchat reclaim` **on the installation you want to use** to clear the state. A running
gateway checks within 30 seconds; an offline gateway connects on its next start. This
can displace the other installation. `/clawchat-reclaim` in local Hermes also wakes this
process's waiting connections immediately. Successful activation, including `--repair`
and `--new-account`, clears the state too.

If that cleanup fails, activation still succeeds and logs a warning to retry `hermes
clawchat reclaim`. The compatibility `clawchat_cli.py` accepts both `status` and
`reclaim`. Status is read-only: an absent or invalid seed produces no install ID and
creates no seed, state or lock file, and the plugin database is opened read-only without
initialization or migration (SQLite may still maintain its own `-wal` / `-shm` side
files for an existing WAL database).

If logs show `cannot persist superseded installation`, run `hermes clawchat reclaim` at
any time on this installation. Even a response saying there is no disk record advances
the generation; a waiting gateway with a readable initial and current generation
releases its memory-only state and reconnects within 30 seconds.

If the initial generation was unreadable, or the current generation remains unreadable,
restart the gateway (memory-only state does not survive a restart); `/clawchat-reclaim`
in that Hermes process also works once the generation file is writable again.
Process-local reclaim clears its cache directly, and a restart loses memory-only state;
without a disk state, startup connects normally.

## Implementation specification

Source: `clawchat_gateway/instance_lock.py`, `install_identity.py`, and `connection.py`.
The profile home follows Hermes' per-profile override; these safeguards apply to
single-profile and multiplexed gateways alike.

### Host-local single-instance locks

Before loading activation credentials or refreshing/connecting, a canonical gateway
acquires `<profile_home>/clawchat/gateway.lock`. Once `user_id` is known, it also
acquires `<hermes_root>/clawchat-locks/<sha256(user_id)[:16]>.lock`. `profile_home`
follows Hermes' per-profile home override under multiplexing; `hermes_root` is the root
owning `profiles/`, resolved by `profile_collision.hermes_root()`. The identity lock is
shared across profiles under that root, including two processes bound to the same agent.

Changing activation identity releases the old identity lock before acquiring the new
one. Neither device IDs nor tokens, cursors, or stored activation data change.

Contention sets `ConnectionState.STANDBY` (value `"standby"`): no WebSocket connection
or token refresh occurs. Hermes counts the adapter as started, so its watcher does not
repeatedly rebuild it. `instance_lock_standby` logs one ERROR with lock kind/path,
holder pid/hostname/started_at, and retry interval, followed by WARNING reminders at
most every ten minutes. On takeover, `instance_lock_acquired` logs INFO with
`waited_seconds`. For diagnostics, `connection.instance_lock_status()` returns the
waiting lock and holder details.

Read the lock file as JSON to identify the holder. Records contain only pid, hostname,
UTC start time, kind, profile home and plugin version; the identity filename hashes the
user ID and the record contains no tokens or clear user ID. New directories use mode
`0700`, lock files `0600`. The OS releases ownership when the holder exits, including
after `SIGKILL` (`kill -9`); a standby gateway retries within 30 seconds, then resumes
normal activation/startup refresh/connect. Shutdown cancels standby promptly.

In-process replacement retains ownership across the old supervisor's stop; an in-flight
HTTP worker retains its own reference until it finishes.

Leftover lock files are harmless: ownership comes from the kernel lock, not file
existence or JSON contents. **Do not delete lock files while a gateway runs**: another
process could then lock a different inode at the same path. POSIX uses nonblocking
`flock`; Windows locks one byte at a far offset so holder JSON remains readable.
Unsupported locking (for example a network filesystem without lock support, or an
unwritable path) logs one WARNING per path and proceeds without that lock.

This safeguard is host-local: different OS users with separate `HERMES_HOME` roots do
not share the identity lock, and separate hosts use installation-conflict handling when
supported, with protocol takeover backoff as the legacy fallback. Standalone senders use
a sibling connect device ID and are exempt from both locks.

### Installation conflicts and recovery

Canonical connections report an `install_id` and `capabilities.install_conflict: true`.
The ID is the first 24 hex characters of SHA-256 over a random seed, the existing host
device fingerprint, hostname, and resolved absolute profile home. Only the hash is sent.
`<profile_home>/clawchat/install_seed` is published once using an atomic hard link from
a fully written, fsynced temporary file with mode `0600`; concurrent creation never
replaces an existing seed. It survives restarts and may travel with a copied profile.

If hard links are unavailable, installation identity is omitted rather than publishing a
partial seed. Changing the machine, hostname, or profile directory produces a different
ID. If the seed cannot be read or written, or is malformed, a WARNING is logged once and
both fields are omitted, preserving legacy `4001` handling. Standalone sibling
connections omit both fields.

When two different installations use the same agent/device, a supporting hub sends
terminal close **`4010`** to the displaced installation, before or after `hello-ok`. The
plugin enters `ConnectionState.SUPERSEDED`: the process stays alive, retains its
instance locks, and stops reconnecting and starting new token refreshes. The HTTP worker
rechecks supersession before sending.

A successful rotation already in flight is still saved to both credential stores (if WS
teardown cancels the credential watcher meanwhile, the save completes before the
cancellation is honoured and a later reclaim re-reads the stored pair); it never
schedules a reconnect while superseded. The token remains valid and credentials are
preserved. State is atomically written with mode `0600` to
`<profile_home>/clawchat/install_superseded.json`, including agent/device/installation
IDs, UTC time, optional winner version, and owner-notice outcome.

Creation, owner notice updates, and reclaim are serialized under a process-local lock
plus the persistent `<profile_home>/clawchat/install_superseded.lock` (mode `0600`).
POSIX uses `flock`; Windows uses a byte-range lock. The lock file is never removed.
Unsupported locking or an inaccessible lock file reports one WARNING per path and
refuses state changes; no mutation proceeds without the cross-process lock. Contention
retries for up to 120 seconds and then fails without performing an unlocked update.

A failed event creation blocks locally without sending a notice; a failed reclaim
reports an error and the CLI returns a nonzero exit code.

Failed event creation is cached only in this process's memory to block connection and
refresh. Cached events are never written back to disk, even after storage recovers or
another identity's stale state remains on disk. Owner-notice results only update the
matching disk event; they cannot persist a memory-only event. A failed result update
logs a WARNING and is never retried.

When a connection first observes `4010` it reads a snapshot of
`<profile_home>/clawchat/install_reclaim_generation`. Every reclaim first atomically
replaces this generation file (mode `0600`) under the state lock, even when no
superseded state exists on disk or in the reclaiming process; if that fails, the reclaim
fails with disk and memory state unchanged (restarting the gateway drops memory-only
state).

Event creation compares the generation again under the state lock: if a reclaim
completed after the snapshot, the event is not recorded, no notice is sent, and the
connection proceeds as reclaimed. A waiting process checks every 30 seconds: only a
readable, present generation different from its readable snapshot releases its cached
event; this check never writes state or requires the state file lock.

An absent generation file represents the initial generation, a file that disappears
later proves nothing, and an unreadable file is unknown and cannot establish that a
reclaim occurred (the event is then recorded and the installation stays stopped:
fail-closed). On restart, a matching (or unreadable) state file blocks connection before
startup refresh, holding only the profile lock; a different agent identity ignores it.

If configuration has no user ID yet, a read-only SQLite identity lookup supplies that
filter without loading credentials or changing startup order. A failed lookup keeps the
fail-closed behavior. A live `4010` retains any identity lock already held. Without
superseded state, an unpaired gateway keeps its existing startup path and takes no
identity lock until connection credentials are available. An ERROR log includes reclaim
instructions; reminders are at most every ten minutes.

For each live `4010` event the plugin attempts one owner DM through a standalone sibling
device, with a total timeout and no token refresh. The durable `owner_notice` records
`pending`, `sent` (message ID), `failed` (error type), or `skipped` (no owner
conversation). When available, the notice includes the other installation's plugin
version, limited to 64 printable characters. Failure and restart never retry the notice.

Reclaim cancels pending notices on the connection's event loop; external reclaim cancels
them when the standby loop detects the cleared state. Because the standby poll runs
every 30 seconds while a notice may take up to its total timeout, the notice also
re-checks right before connecting and again right before sending that its event is still
on disk; after any reclaim it is not sent. Cancelled notices do not write a result.
Other standalone sends return an error while superseded.
