# Prompt reference

Prompts live at the repo root under `prompts/` and are loaded by
`clawchat_gateway.plugin_prompts.load_clawchat_prompts_from_root` at
plugin import time. `MANIFEST.in` ships them when the package is built
as a wheel; for the Hermes install path they are copied along with the
rest of the plugin tree into `$HERMES_HOME/plugins/clawchat/prompts/`.

## Files

| File                                   | Status      | Where it is injected                                                       |
|----------------------------------------|-------------|----------------------------------------------------------------------------|
| `prompts/platform.md`                  | **Required** — missing or empty raises at plugin import. | Passed to `register_platform(..., platform_hint=platform_prompt())`. Used by Hermes as the persistent ClawChat platform hint. Also carries the privacy floor (do not carry private detail across contexts), kept here rather than in `agent_behavior` so the owner cannot delete it. Also carries the memory routing rule ("Where a remembered fact goes"): facts about one person go to `users/<usr_id>.md`, about a group to `groups/<cnv_id>.md`, about the owner to `owner.md`, read before write, and Hermes' own `memory` tool / MEMORY.md only for facts independent of who the agent is talking to. It is here, not only in the skill, because Hermes' `memory` tool description is in every turn too. Also teaches the sticker syntax in one sentence, verbatim as the app and the OpenClaw plugin word it: a reply whose whole body is `[claw:spark.<id>]` is rendered as one of the app's Spark stickers. It is here because Hermes has no plugin-owned plain-text send tool to describe it on (`clawchat_mention_message` always carries a mention, so its message is never a lone token). |
| `prompts/default-owner-behavior.md`    | Optional, present in this repo.                           | Returned by `default_owner_behavior_prompt()`; used as the default `agent_behavior` field when the owner-metadata block is empty. |
| `prompts/default-group-bio.md`         | Optional, present in this repo.                           | Returned by `default_group_bio_prompt()`; used as the default `group_description` placeholder for groups that have not set one. |

`platform.md` also tells the agent to keep each memory note a compact profile rather than an append-only log (rewrite it once it passes about 1,000 characters).

The `PromptName` literal in
`clawchat_gateway/plugin_prompts.py:15` is the canonical list of
recognized prompt names. The loader treats `platform` as required and
the default metadata prompts as optional.

## Overriding prompts

To replace a shipped prompt, edit the file in `prompts/` and rebuild /
reinstall the plugin. Hermes does not currently expose a runtime
override surface for these files; the plugin loads them once at import.

## Where they are read at runtime

| Function                              | Caller                                                       |
|---------------------------------------|--------------------------------------------------------------|
| `platform_prompt()`                   | `__init__._register_platform → register_platform(platform_hint=...)` |
| `default_owner_behavior_prompt()`     | `clawchat_gateway.clawchat_metadata` (owner-metadata defaults) |
| `default_group_bio_prompt()`          | `clawchat_gateway.clawchat_metadata` (group-metadata defaults) |

## Greeting flows and language

Owner activation uses `greeting.md`; new-friend turns use `friend-greeting.md`. These
per-agent files are independent of the shipped `prompts/` files above.

`friend_greeting` controls whether the agent **speaks first** to a newly added friend
who is not its owner. On a `friend.added` signal whose `entity_id` is not the owner, the
adapter resolves the new direct conversation through `POST /v1/conversations/direct`
(the signal only carries the counterparty `usr_…` id; the server created the
conversation inside the accept transaction) and runs one synthetic turn in it with the
prompt from `$HERMES_HOME/clawchat/friend-greeting.md`, falling back to the built-in
`greeting.FRIEND_GREETING_PROMPT`.

Before that turn the adapter refreshes the friend's profile (`_refresh_user_profile`,
bounded by `FRIEND_GREETING_PROFILE_TIMEOUT_SECONDS` = 5 s) and puts their nickname in
the turn's `sender_name`: a brand-new friend has never written, so nothing has cached
their profile yet and the turn would otherwise name their `usr_…` id. A slow or failed
lookup greets without the name. Both directions count — someone adding the agent
(auto-accepted by the `friend.accept` policy) and the agent's own request being
accepted.

The owner's `friend.added` is skipped (the activation bootstrap greets the owner).
Dedupe is persisted in the message ledger keyed on the signal's `event_id` (fallback
`message_id`), so a reconnect replay never greets twice; a failed conversation lookup is
logged and dropped. The default is `true`; set `false` to keep `friend.added` a pure
awareness event (`adapter._schedule_friend_greeting` /
`adapter._dispatch_friend_greeting`). This flag is independent of `awareness_note`.

Both `greeting.md` and `friend-greeting.md` (see [the agent's own
files](../configuration.md#the-agents-own-files) for where they live) are **partial**
overrides, not full replacements: whichever body they supply (or the built-in fallback,
if the file is absent/empty/unreadable) gets a trailing `Reply in <Language>.`
instruction appended — the override says what to say, not which language to say it in.

That line is **conditional**. The language is resolved from the owner's reported app
locale, read off disk via `adapter._owner_locale()` through
`owner_language.resolve_owner_language_if_known` — which returns `None`, not `en`, when
no locale has been reported or the owner metadata has not landed yet. A `None` language
appends **no line at all**.

Neither greeting dispatch path may grow an await to chase the locale, and hard-asserting
"Reply in English." at an owner who may not speak English is worse than asserting
nothing: with no line the model infers the language from context. (The Liveware Sample
intro lookup is the opposite case and keeps its English fallback — there a copy has to
be chosen and `en` is the documented default.)

The friend-greeting turn uses the **owner's** resolved language even though its
recipient is the new friend, not the owner: friends are usually in the same language
circle, and the backend only exposes `locale` on the owner profile endpoint, so no
third-party locale is obtainable.
