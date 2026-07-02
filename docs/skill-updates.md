# Dynamic skill updates

How the plugin's `SKILL.md` files are updated at runtime — without reinstalling
the plugin or restarting the Hermes process — and how a skill the plugin never
bundled becomes visible to the host. Implementation lives in
`clawchat_gateway/skill_update.py` (mechanism) plus `clawchat_gateway/adapter.py`
(trigger + consent gate) and `__init__.py::_register_skill` (registration).

## End-to-end flow

| Step | Actor | Code |
|------|-------|------|
| 1. Publish: bump the frontmatter `version` of a `SKILL.md` under the install-cli repo's `skills/` tree, regenerate `skills/manifest.json` (`schema: 1`, entries carry `version`/`path`/`sha256`/`bytes`), push to `main` or tag `skills-vX.Y.Z` | Publisher (install-cli repo) | consumed contract: `skill_update.py` `parse_skills_manifest`, `OFFICIAL_SKILLS_BASE` |
| 2. Trigger: server sends a content-free `notify.signal` with `payload.type == "clawchat.skill.update.check"` (msghub `POST /internal/v1/notify-signals`) | ClawChat server | `adapter.py::_on_notify_signal` → `_spawn_skill_update_check` |
| 3. Check: fetch the official manifest (hardcoded base — the signal never carries a URL), compare against the local managed manifest; missing-locally counts as an update | Plugin (background task) | `skill_update.check_skill_update` |
| 4. Consent: message the owner's direct chat (`_owner_direct_chat_id`, the activation conversation) and persist a pending record (30 min TTL) | Plugin | `adapter.py` prompt send; `skill_update.write_pending` |
| 5. Apply: on an unambiguous owner "更新"-style reply, download each `SKILL.md`, verify size + sha256 + frontmatter (`name == skill id`, `version == manifest version`), then atomically overwrite the managed copy and update the local manifest — all-or-nothing | Plugin (consent gate runs before the LLM) | `adapter.py::_maybe_consume_skill_update_consent`; `skill_update.apply_skill_update` |
| 6. Take effect: registered paths are the managed copies, and writes are in-place atomic replaces (never delete-then-write), so the host reads the new content on the next `skill_view` — no restart, no re-registration | Hermes host | `skill_update.atomic_write_text`; registration below |

The managed (writable) skills root is `$HERMES_HOME/clawchat-skills/`
(`skill_update.managed_skills_dir`), seeded from the plugin's read-only bundled
`skills/` snapshot at load (`seed_managed_skill`).

## Registration: bundled, managed extras, and hot registration

`__init__.py::_register_skill(ctx)` does three things at plugin load:

1. **Bundled skills** (`clawchat`, `liveware-app`): seed a managed copy and
   `ctx.register_skill(id, managed_path, description=...)`.
2. **Managed extras**: any id present in the managed manifest but not bundled —
   i.e. delivered earlier by a dynamic update — is registered too, with its
   description read from the SKILL.md frontmatter. Without this, a dynamically
   delivered skill would vanish from the host on every restart.
3. **Registrar capture**: `skill_update.set_skill_registrar(ctx.register_skill)`
   stores the host registrar so step 5 above can *hot-register* a brand-new
   skill immediately after apply (`skill_update.hot_register_new_skills`) —
   the skill becomes resolvable via `skill_view("clawchat:<id>")` in the same
   process, no restart. Only ids the local manifest had never seen
   (`current is None`) are hot-registered; updates to existing ids need no
   re-registration because the registered path is unchanged.

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
- Apply is all-or-nothing across the batch; any sha256/size/frontmatter
  mismatch aborts before the first write.
- Hot-registration failures are per-skill logged-and-skipped so a bad skill
  cannot break the consent flow; seed failures fall back to the read-only
  bundled path so the skill mechanism can never break plugin load.

## Parity with the OpenClaw plugin

`clawchat-plugin-openclaw` implements the same publish/check/consent/apply
contract, but needs **no registration step at all**: the OpenClaw host scans
and watches its managed skills dir (`~/.openclaw/skills`) directly, so writing
the file is the delivery — including brand-new ids. When changing the manifest
contract or consent semantics, update both plugins and the install-cli
manifest generator together.
