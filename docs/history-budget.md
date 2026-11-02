# Conversation history budget

For operators and implementers: how ClawChat conversations are compacted into a rolling summary within a token budget, the settings that control it, and the host requirements and limits.

These keys live in `platforms.clawchat.extra`. Every key below supports
`extra.groups.<chat_id>` and `extra.chats.<chat_id>` overrides (groups also accept
`groups."*"`). Priority: exact conversation, group wildcard, account, factory.
Invalid values use the factory value; out-of-range values clamp and log the
actual value. Durations accept the ClawChat desktop agent's duration grammar
and round to minutes. Restart the gateway after changing settings.

| Key | Factory | Range | `groups.<id>` / `chats.<id>` |
| --- | --- | --- | --- |
| `budget-compaction` | `on` | on/off | both |
| `recent-window` | `3d` | 1h–30d; no off | both |
| `history-budget-dm` | `25000` | 5000–200000 tokens | both |
| `history-budget-group` | `15000` | 5000–200000 tokens | both |
| `keep-recent-turns` | `8` | 1–50 other-speaker messages | both |
| `idle-reset-dm` | `24h` | off or 1h–30d | both |
| `idle-reset-group` | `6h` | off or 1h–30d | both |

Budget maintenance is enabled by default to bound conversation history while
retaining a rolling summary and recent originals. `budget-compaction: off`
restores host compression and the legacy threshold note turn. `daily-reset`
is unconsumed; `archive-muted-groups` and `digest-every-messages` remain
unconsumed (muted messages stay in the ledger; Hermes owns its sessions).

After a real answer completes, history **strictly greater** than the DM/group
budget triggers maintenance. An idle interval also triggers maintenance **after
answering in the original session**; it does not clear history before the answer.
Idle time uses the preceding real conversational input or agent answer, captured
before this input batch. Host shutdown/startup notices, plugin notifications,
maintenance output and command/confirmation receipts do not count as activity.
Original frame `emitted_at`, then `timestamp`, take priority over ledger ingest
time; delayed offline delivery therefore keeps its original interval. The clock
is captured before the answer and does not use the processing-complete time.
History uses the first injected request's prompt-token baseline and subsequent
usage growth, or ledger estimates when usage is absent. The opening history
baseline includes the triggering input (all members of a batch) as well as the
injected window, matching the opening prompt's usage. The recent window keeps
other-speaker turns, gives budget priority, and folds replies with their questions.
Idle, confirmed `/new`, and external rotation skip sparse folds with fewer than
two other-speaker messages and no owner message.

Maintenance serializes by the host's canonical session key, including per-speaker
group keys; budget-enabled per-speaker groups also keep their input batches
separate by speaker. It fixes a durable cut, makes independent summary calls in consecutive
blocks of at most 24000 estimated tokens (`timeout=120`, `max_tokens=4000`),
validates four summary sections, publishes immutable synced files, then advances
SQLite state by generation CAS. Cuts and summary/reset boundaries retain
immutable ledger positions even when their referenced messages are recalled;
withdrawal never moves a fixed interval. Each pass handles at most three blocks; confirmed
`/new` finishes its whole interval before the next input is handed to Hermes.
After publication, optional notes run in the old session, then a completely idle
host session is renewed. The plugin rereads the **persistent route** to verify
that the session ID changed. Failures retain the original history and pending
work; retries back off by 1/2/4/8 completed real messages. Published blocks remain
available when a later block fails. No summary text or credentials enter the
maintenance diagnostic line (conversation, reason, result, blocks, duration and
prompt tokens before/after).

Arrivals are journaled in the same transaction as their ledger claim, held during
maintenance, and released in ledger order. The journal records held, dispatching
and dispatched states and a stable host event snapshot before handoff. Restart
replays held or uncertain dispatching input through ordinary authorization and
routing. Recall tombstones are checked before replay and after asynchronous
preparation; withdrawn journal entries finish without reaching the host. Saved
batches containing a withdrawn member are rebuilt from surviving frames.
Adapter-consumed replies, including skill-update consent, finish their journal
entries too; queued inputs remain pending. Only confirmed final delivery or an explicit successful no-reply terminal
can suppress uncertain replay. Pending, failed and process outputs cannot.
Host deduplication is session scoped; an uncertain retry across rotation may
answer twice. Delivery takes priority over silently losing an input.
An independent **five-minute** deadline releases held input even while an
already-started reset is still running. That reset is shielded from maintenance
timeout, checked when it eventually finishes, and never started twice; shutdown
cancels the connection's maintenance tasks. Reset-task output suppression does
not suppress real answers released at the deadline. A reset exceeding two
minutes logs a warning.

The next real message in a new session carries `## ClawChat Conversation Summary`
and `## Recent messages` in the **user** `channel_context`, keeping the system
prefix stable. With no summary, only the recent window is injected. This turn
skips the old group seed/unseen injection and excludes its own batch IDs.
Injection is committed only on the first observed `post_api_request` associated
with that dispatch's nonce and actual session ID. Usage is optional: absent
usage still confirms injection, while token estimates continue from the ledger.
Host refusal leaves the next input eligible for injection.

A user's `/new` keeps the host's confirmation gate and the existing old-session
notes. The command alone does not advance a boundary. `on_session_reset` or the
next dispatch's durable route check must confirm the change first. The confirmed
boundary excludes pre-command originals from the next window; renewed summary
work folds those originals into the summary, including a conversation never
previously summarized. The first input starts this work immediately and is
handed to the host before later held arrivals; a drain cannot skip its in-flight
journal entry. Internal maintenance resets use a separate operation
identity and do not become user `/new` events.
Reset commands and their confirmation replies are recorded as control input.
Showing the host confirmation prompt or returning from its command handler
retires their journal entries, including owner button flows that do not set the
ordinary admission receipt. A control input still held before its first handoff
can be delivered once, but uncertain `dispatching` controls are never retried;
legacy journal entries are retired instead of reopening the confirmation prompt.
If a reset's outcome is uncertain after a restart, the user must issue it again.
This policy does not bypass the host's confirmation gate or confirm a reset
before its persistent route changes.

**Host threshold priority:** ClawChat uses the plugin DM/group budgets. Hermes'
`compression.threshold_tokens` (or the missing value filled from
`session-cap-tokens`) is the **backstop only**. Budget-enabled conversations no
longer trigger legacy near-threshold notes. Activation still fills only missing
host values and never overwrites an operator's global compression setting.
Startup logs show the budgets, precedence and host backstop.

**Host compatibility and private interfaces:** budget maintenance requires
Hermes **v0.14.0 or newer**; the private reset signature is verified against
v0.14.0 through v0.22.5 canary. Later versions still require capability probes;
this is not a guarantee of future private API stability. The plugin probes
`ctx.llm.acomplete`, `channel_context`, `adapter.gateway_runner`, the async bound
`_handle_reset_command(event)` signature (exactly one positional event parameter),
canonical routing, running/queue predicates and persistent route reading at
connect. An unreadable signature or missing capability disables budget work for
the account and logs a warning; legacy notes and host compression remain.
Routing reads use `_routing_db.load_gateway_routing_entries(scope=...)` when
present; older hosts use a fresh read of `sessions.json`. A database read failure
never falls back to a possibly stale JSON mirror. Runtime summary authorization
refusal also disables budget work. The independent summary uses the host's
configured main model; it must accommodate a full block plus the previous summary.

**Unsupported combinations and differences:** host time-based resets on older
Hermes (through v0.21.0) can clear history before answering; that combination
cannot promise the plugin's answer-before-idle-maintenance behavior. Disable
host time resets or upgrade to v0.21.1+.
The requirement to compress and update notes **in the same round cannot be met
by the plugin**: independent summary calls have no tools, so notes run as a
separate silent round after summary publication. A single message exceeding a
summary block is represented by its head and tail plus its message ID; its
original remains available via `chat_history_read(around=...)`. If Hermes rotates
a session during dispatch after injection was decided, that round can miss the
injection; the next real round repairs it. A renewed summary that hits the
five-minute limit is released for delivery and logged as incomplete; it does not
meet the guarantee that the first new-session answer has the completed summary.
Reset callbacks can have host-side effects before a failed save; the plugin
cannot roll back the host's partial cleanup, so it never treats a banner or an
in-memory ID as proof of renewal.
