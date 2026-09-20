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

Device identity keeps the current paired-device compatibility rules and named
profile suffix. Only the host fingerprint is globally cached; explicit device
IDs and profile suffixes are resolved per call.

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

The tests use temporary profiles, synthetic credentials and an offline supervisor.
They cover connection coexistence and replacement, A/B/A database identity,
scoped sends, missing secrets, cross-context token rotation/logout, and device
identity compatibility. They do not contact ClawChat or change existing profiles.

After deploying and restarting the gateway, verify each profile has its own
`ready` connection and test actual messages with both accounts. A systemd
`active` state or a startup notification alone is not end-to-end chat validation.
