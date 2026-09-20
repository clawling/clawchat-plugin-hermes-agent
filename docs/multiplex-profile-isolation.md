# ClawChat under a multiplexed Hermes gateway

With `gateway.multiplex_profiles: true`, Hermes creates each profile's adapter
inside a context-local home and secret scope. ClawChat must preserve that scope
without changing the process-wide environment.

Each profile keeps its existing `.env` and SQLite database. Store instances are
cached by absolute database path; supervisor slots use `(profile_home, account_id)`.
The database account key remains `default`, so existing activation rows need no
migration. Starting a replacement connection still stops an older connection for
the same profile, but never another profile's connection.

Connection lifecycle and credential callbacks inherit the context captured at
construction. Token rotation and logout therefore write the owning profile's
files even when invoked from another context. Mention, reaction and media sends
resolve the active profile's adapter. Missing scoped credentials do not fall back
to another profile's ambient environment.

Credential reads stay delegated to Hermes rather than re-deciding the fallback
here. `_get_env` takes its scoped branch whenever a secret scope is installed
*or* multiplexing is on, and lets `get_scoped_secret` separate the three shapes:
the default profile (multiplex on, unscoped) and a single-profile deployment
(scoped, multiplex off) both still reach `os.environ`, because there it is their
own value and env-injected deployments keep nothing else; only a scoped profile
under multiplexing fails closed on a miss. Narrowing that condition, or dropping
to a bare `get_secret`, silently 401s cron deliveries and env-only containers.

Auto-logout leaves a tombstone. `clear_persisted_credentials` writes
`CLAWCHAT_TOKEN` / `CLAWCHAT_REFRESH_TOKEN` back EMPTY instead of deleting the
lines, because the secret scope is a snapshot taken at gateway start: a deleted
line leaves that snapshot (and any ambient `os.environ` value) holding the token
we just revoked, and the next adapter reads it straight back and retries auth
forever. An empty managed value resolves to `""` ahead of every fallback.

The platform seed Hermes builds per profile (`_clawchat_env_enablement`) reads
through the same scoped resolver. Reading `CLAWCHAT_HOME_CHANNEL` and the
endpoint overrides from process-wide `os.environ` handed a named profile the
default profile's home conversation, so home delivery went to the wrong chat.

Device identity keeps the current paired-device compatibility rules and named
profile suffix. Only the host fingerprint is globally cached; explicit device
IDs and profile suffixes are resolved per call. The `functools.lru_cache` moved
off `get_device_id` onto `_host_device_id`, so any test fixture that used to
call `get_device_id.cache_clear()` must now clear `_host_device_id` instead.

## Regression checks

Run from this repository using a Hermes installation with multiplex support
(validated with Hermes 0.21.3). These integration checks import the real Hermes
scope/config helpers; installing the plugin's test extra alone is insufficient.

```bash
PYTHONDONTWRITEBYTECODE=1 \
PYTHONPATH="$HOME/.hermes/hermes-agent" \
"$HOME/.hermes/hermes-agent/venv/bin/python" \
tests/test_multiplex_isolation.py
```

Any Hermes 0.21+ checkout on `PYTHONPATH` works; the plugin's own dependencies
are not needed for this file.

The tests use temporary profiles, synthetic credentials and an offline supervisor.
They cover connection coexistence and replacement, A/B/A database identity,
scoped sends, missing secrets and the two env-fallback shapes that must survive,
cross-context token rotation/logout, and device identity compatibility. They do
not contact ClawChat or change existing profiles.

This file is the one exception to the repository's `tests/` ignore rule — see
`.gitignore` — so the multiplex invariants ship with the code instead of relying
on `git add -f`. The rest of the suite stays untracked, which also means a
change like the device-id cache move above cannot be caught by CI: run the local
suite before merging anything that touches these modules.

After deploying and restarting the gateway, verify each profile has its own
`ready` connection and test actual messages with both accounts. A systemd
`active` state or a startup notification alone is not end-to-end chat validation.
