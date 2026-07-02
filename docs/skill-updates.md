# Dynamic skill updates

How the plugin's `SKILL.md` files are updated at runtime — without reinstalling
the plugin or restarting the Hermes process — and how a skill the plugin never
bundled becomes visible to the host, or a retired skill is removed from it.
Implementation lives in `clawchat_gateway/skill_update.py` (mechanism) plus
`clawchat_gateway/adapter.py` (trigger + consent gate) and
`__init__.py::_register_skill` (registration).

## End-to-end flow

| Step | Actor | Code |
|------|-------|------|
| 1. Publish: bump the frontmatter `version` of a `SKILL.md` under the install-cli repo's `skills/` tree (or retire one — move its id from `LAYOUT` to the per-target `REMOVED` list), regenerate `skills/manifest.json` (`schema: 1`, entries carry `version`/`path`/`sha256`/`bytes`; top-level `removed: {target: [skillId, ...]}` is optional and lists tombstoned ids), push to `main` or tag `skills-vX.Y.Z` | Publisher (install-cli repo) | consumed contract: `skill_update.py` `parse_skills_manifest`, `OFFICIAL_SKILLS_BASE` |
| 2. Trigger: server sends a content-free `notify.signal` with `payload.type == "clawchat.skill.update.check"` (msghub `POST /internal/v1/notify-signals`) | ClawChat server | `adapter.py::_on_notify_signal` → `_spawn_skill_update_check` |
| 3. Check: fetch the official manifest (hardcoded base — the signal never carries a URL); for every manifest entry, hash the *local* managed `SKILL.md` bytes and compare to the entry's `sha256` — any mismatch (including a missing local file) is reported as an update to converge; separately, walk `removed[hermes]` and report a removal for every tombstoned id that is locally installed and not bundled | Plugin (background task) | `skill_update.check_skill_update` |
| 4. Consent: message the owner's direct chat (`_owner_direct_chat_id`, the activation conversation) with one combined prompt covering both updates and removals, and persist one pending record (30 min TTL) | Plugin | `adapter.py::build_skill_update_prompt`; `skill_update.write_pending` |
| 5. Apply: on an unambiguous owner "更新"-style reply, apply updates and removals in the same try — updates: download each `SKILL.md`, verify size + sha256 + frontmatter (`name == skill id`, `version == manifest version`), then atomically overwrite the managed copy and update the local manifest (skill already converged on disk → skipped); removals: delete the managed `SKILL.md` and drop its local manifest entry — then send one combined ack | Plugin (consent gate runs before the LLM) | `adapter.py::_maybe_consume_skill_update_consent`; `skill_update.apply_skill_update`, `skill_update.apply_skill_removal` |
| 6. Take effect: registered paths are the managed copies, writes are in-place atomic replaces (never delete-then-write) so an update is visible on the next `skill_view` with no restart; a removal instead deletes the registered path, so the host lazily treats the registration as stale — no client-side unregister call exists or is needed | Hermes host | `skill_update.atomic_write_text`; registration below |

The managed (writable) skills root is `$HERMES_HOME/clawchat-skills/`
(`skill_update.managed_skills_dir`), seeded from the plugin's read-only bundled
`skills/` snapshot at load (`seed_managed_skill`).

## Convergence: sha-based check, not version comparison

`check_skill_update` never trusts a version number to decide whether to
converge — it compares raw bytes. For every skill in `skills.hermes` of the
official manifest, it hashes the managed `SKILL.md` on disk
(`local_skill_sha(skill_id)`, sha256 of the raw bytes; `None` when the file is
missing) and compares it to the manifest entry's `sha256`. Any mismatch —
including "no local file at all" — is reported as an update; an exact match is
a no-op. This single rule uniformly covers three cases the old strict
version-comparison logic had to special-case:

- **Upgrade** — remote version is newer than local.
- **Rollback** — remote version is older than local (e.g. a bad release gets
  reverted upstream); the sha differs, so it still converges.
- **Same-version content fix** — the manifest re-publishes the same `version`
  string with corrected content (and thus a different `sha256`); this still
  converges even though a version-only comparison would have called it a
  no-op.

`current` in the consent prompt/log (`PendingSkillUpdate.current`) is read
from the *local managed manifest* version (not the manifest target), purely
for the owner-facing "vX → vY" wording — the decision to converge never
depends on it. A manifest `skills.<target>` entry with an *empty* dict value
(`{}`) is legal and simply yields no per-skill entries for that target;
only a missing `skills.<target>` key raises `SkillUpdateError`.

Self-heal semantics: because convergence is sha-based, a manually-edited
managed `SKILL.md` (e.g. an operator hand-patches a file on disk) is
indistinguishable from drift — the next check reports it as an update, and
once the owner consents it is overwritten back to the official content. This
is expected, not a bug: the managed tree is meant to always converge to
whatever the official manifest says, never to preserve local edits.

Publish-side constraint: the official manifest's version for a bundled skill
(`clawchat`, `liveware-app`) must never be **lower** than the version already
shipped in the plugin's own bundled snapshot. `seed_managed_skill` reseeds the
managed copy from the bundled snapshot whenever the managed version is older
than the bundled one — a manifest that regresses a bundled skill's version
would fight the sha-convergence apply on every subsequent plugin load (seed
reverts it forward, then the next check tries to converge it back down).
Non-bundled ids have no such constraint since nothing reseeds them.

## Deletion propagation (tombstone removal)

A skill can be retired from the official source without the plugin ever
uninstalling itself. `skills/manifest.json` carries an optional top-level
`removed` field: `{"<target>": ["<skillId>", ...]}`. The generator
(install-cli repo, `scripts/build-skills-manifest.mjs`) hardcodes the
per-target retirement list as `REMOVED` and fails the build if the same id
appears in both `LAYOUT` (live) and `REMOVED` (tombstoned) for a target; both
consumer-side parsers (`skill_update.parse_skills_manifest` here, and the
OpenClaw plugin's equivalent) re-validate the same invariant against
whatever manifest they actually fetch, independent of the generator.

**Consumer-side removal requires all three conditions** (`check_skill_update`):

1. the id is tombstoned for `hermes` in `removed[hermes]`,
2. the id is present in the *local* managed manifest (i.e. actually installed
   here — a tombstone for a skill this plugin never had is a no-op), and
3. the id is **not** one of the bundled ids (`HERMES_SKILL_IDS = ("clawchat",
   "liveware-app")`) — a tombstone naming a bundled id is ignored with a
   `logger.warning`, never acted on, since deleting a bundled skill would only
   trigger `seed_managed_skill` to reseed it right back.

**Consent** is merged with updates into a single flow: one `PendingConsent`
carries both `updates` and `removals` lists (`PendingConsent.removals`;
`pending.json` is backward-compatible — `read_pending` accepts a record with
either list populated), and one owner message combines both
(`build_skill_update_prompt`): an update segment ("我的技能有更新 …") and a
removal segment ("以下技能将下线移除:…"), joined with `;` when both are
present, ending in the same "回复「更新」确认,「取消」忽略。" call to action.
A single "更新" reply affirms both parts together; there is no way to accept
one and reject the other.

**Apply** (`skill_update.apply_skill_removal`) is the one legitimate delete of
a registered skill path in this codebase:

- Delete the managed `SKILL.md` (`managed_skill_path(skill_id)`); a
  `FileNotFoundError` is swallowed (idempotent — already gone counts as done).
- Best-effort `rmdir` of the now-empty parent skill directory; any `OSError`
  (non-empty, already gone, permissions) is ignored.
- Drop the id from the local managed manifest.
- Unlike `apply_skill_update`'s all-or-nothing validate-then-write, each
  removal is handled independently: an `OSError` other than
  "not found" during unlink is logged and that one id is skipped (`continue`),
  it does not abort the rest of the batch — and since the skipped id's
  `local.pop(skill_id, None)` never runs, its local manifest entry is left in
  place, so the next `check_skill_update` still sees it as tombstoned +
  locally installed and retries the removal.
- All manifest-entry drops for the batch are written to disk in one
  `write_local_manifest` call at the end, not per-id.
- Bundled ids reaching this function are refused defensively (same guard as
  the check step) and logged, never deleted.

**Host-side effect**: there is no explicit unregister call. The Hermes host
lazily treats a registration whose backing file is missing as stale; on the
next plugin load, the managed-extras registration pass (`__init__._register_skill`)
simply finds no local-manifest entry for the removed id and registers nothing
for it, so it never reappears.

**Idempotency**: re-running the apply for an already-removed id is a no-op
(`FileNotFoundError` path) that still clears any stray manifest entry.

**Recovery**: tombstones are kept **permanently** in `REMOVED` (they are tiny;
this is what lets an agent that reconnects after arbitrary downtime still
converge correctly instead of re-discovering old history). To bring a retired
skill back, the generator moves the id from `REMOVED` back into `LAYOUT` for
that target — at that point it is a normal live manifest entry again, and
since the local file was deleted, the next check reports it as a fresh
install (`current is None`) rather than a removal.

## Registration: bundled, managed extras, and hot registration

`__init__.py::_register_skill(ctx)` does three things at plugin load:

1. **Bundled skills** (`clawchat`, `liveware-app`): seed a managed copy and
   `ctx.register_skill(id, managed_path, description=...)`.
2. **Managed extras**: any id present in the managed manifest but not bundled —
   i.e. delivered earlier by a dynamic update — is registered too, with its
   description read from the SKILL.md frontmatter. Without this, a dynamically
   delivered skill would vanish from the host on every restart. A tombstoned
   id has no manifest entry (removed by `apply_skill_removal`), so this pass
   naturally stops registering it — no separate unregister path is needed.
3. **Registrar capture**: `skill_update.set_skill_registrar(ctx.register_skill)`
   stores the host registrar so step 5 above can *hot-register* a brand-new
   skill immediately after apply (`skill_update.hot_register_new_skills`) —
   the skill becomes resolvable via `skill_view("clawchat:<id>")` in the same
   process, no restart. Only ids the local manifest had never seen
   (`current is None`) are hot-registered; updates to existing ids need no
   re-registration because the registered path is unchanged. Removals are
   never hot-registered (there is nothing to register).

Historical note: before 2026-07, only bundled ids were ever registered, so a
brand-new skill delivered by the update flow was written to disk (and confirmed
to the owner as "已更新") but never became visible to the host — not even after
a restart. The managed-extras pass and hot registration close that gap.

Host-side contract (verify against `tmp/hermes/hermes_cli/plugins.py`
`register_skill`): plain registry write, callable at any time; requires the
path to exist and the bare name to match `[a-zA-Z0-9_-]+`; plugin skills are
explicit-load only (`skill_view`), they do not appear in the system prompt's
`<available_skills>` index.

## Failure behaviour

- Check/network errors are logged and swallowed — never surface to the owner,
  never affect the WS connection (`adapter.py::_handle_skill_update_check`).
- Update apply is all-or-nothing across the batch: any sha256/size/frontmatter
  mismatch aborts before the first write. Removal apply is per-id best-effort
  (see "Deletion propagation" above) and does not share that all-or-nothing
  gate.
- Hot-registration failures are per-skill logged-and-skipped so a bad skill
  cannot break the consent flow; seed failures fall back to the read-only
  bundled path so the skill mechanism can never break plugin load.

## Parity with the OpenClaw plugin

`clawchat-plugin-openclaw` implements the same publish/check/consent contract
through the point where the owner is asked for consent, including sha-based
convergence and tombstone removal (same three conditions: tombstoned, locally
installed, not bundled — `OPENCLAW_SKILL_IDS` there vs. `HERMES_SKILL_IDS`
here), but needs **no registration step at all**: the OpenClaw host scans and
watches its managed skills dir (`~/.openclaw/skills`) directly, so writing or
deleting the file *is* the delivery/removal — including brand-new ids, with
no lazy-cleanup step needed on the removal side either. The two plugins'
owner-facing message wording differs slightly (style only, not contract):
OpenClaw's update segment uses a colon ("我的技能有更新:…") where Hermes does
not, and OpenClaw shows a missing local version as "v无" where Hermes shows
"v—" — both express the same three states (upgrade, rollback, content-only
revision) and the same combined updates+removals prompt/consent/ack flow.
There is also a separator difference in the **consent prompt's** update
listing: OpenClaw's `runSkillUpdateCheck` joins multiple pending-update
descriptions with `;` (`skill-update.ts`, `updates.map(describeUpdate).join(";")`),
while Hermes's `PendingConsent.summary()` (`skill_update.py`) joins the same
kind of per-update descriptions with `、` (the Chinese enumeration comma). The
post-apply **ack** summary itself doesn't diverge this way — both
`build_skill_update_ack` (Hermes, `adapter.py`) and OpenClaw's applied-summary
line join with `、`.

The **apply** phase genuinely diverges, though, not just in wording:

- **Failure retry.** Hermes clears the pending record *before* attempting
  apply (`adapter.py`'s affirm branch calls `_clear_pending_skill_update()`
  ahead of the `try`), so a failed apply leaves nothing for the owner to
  retry — they have to wait for the next `check` signal. OpenClaw's
  `handleOwnerConsentReply` keeps the pending record on a caught apply error,
  so the owner can just reply "更新" again to retry the same batch.
- **Removal batching.** Hermes's `apply_skill_removal` is per-id
  best-effort — an `OSError` unlinking one id is logged and skipped via
  `continue`, and the rest of the batch still applies (see "Deletion
  propagation" above). OpenClaw's removals loop in `handleOwnerConsentReply`
  has no per-id try/catch; one failed `removeManagedSkill` throws out of the
  loop and aborts whatever removals (and any trailing updates) hadn't run yet.

When changing the manifest contract or consent semantics, update both
plugins and the install-cli manifest generator together.
