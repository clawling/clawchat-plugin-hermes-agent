# Prompt reference

Prompts live at the repo root under `prompts/` and are loaded by
`clawchat_gateway.plugin_prompts.load_clawchat_prompts_from_root` at
plugin import time. `MANIFEST.in` ships them when the package is built
as a wheel; for the Hermes install path they are copied along with the
rest of the plugin tree into `$HERMES_HOME/plugins/clawchat/prompts/`.

## Files

| File                                   | Status      | Where it is injected                                                       |
|----------------------------------------|-------------|----------------------------------------------------------------------------|
| `prompts/platform.md`                  | **Required** — missing or empty raises at plugin import. | Passed to `register_platform(..., platform_hint=platform_prompt())`. Used by Hermes as the persistent ClawChat platform hint. |
| `prompts/default-owner-behavior.md`    | Optional, present in this repo.                           | Returned by `default_owner_behavior_prompt()`; used as the default `agent_behavior` field when the owner-metadata block is empty. |
| `prompts/default-group-bio.md`         | Optional, present in this repo.                           | Returned by `default_group_bio_prompt()`; used as the default `group_description` placeholder for groups that have not set one. |

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
