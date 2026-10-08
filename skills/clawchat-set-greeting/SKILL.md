---
name: clawchat-set-greeting
version: 1.3.0
description: >-
  Use when the user wants to customize, change, set, or reset this agent's greetings — the
  first-load / activation greeting to the owner (greeting.md) or the first message sent to a
  newly added non-owner friend (friend-greeting.md), both in this Hermes profile's
  $HERMES_HOME/clawchat/ folder.
---

# Set the ClawChat first-load greeting

The **first-load greeting** is what this agent does the first time it connects to a
ClawChat direct conversation with its owner. By default the ClawChat plugin injects a
built-in instruction telling you to send a short, friendly self-introduction.

You can override that instruction with a file named **`greeting.md`** in this Hermes
profile's ClawChat folder, **`$HERMES_HOME/clawchat/`**. When that file exists and is
non-empty, the plugin uses its content as the **body** of the instruction on the next
activation / first connect, in place of the built-in wording.

It is a **partial** override, not a full replacement of everything the plugin sends: the
plugin may still append a short trailing line of its own after your text. Today that is a
`Reply in <Language>.` line, added when the owner's ClawChat app language is known (and
omitted entirely when it is not). That line says which language to answer in, not what to
say, so it is not something your override replaces — write your instruction as the *what*,
and do not try to cancel or contradict the trailing line from inside the file.

## Where the files live — one folder per profile

Each Hermes profile is a separate ClawChat agent with its own greetings, so the folder is
the profile's own, never the OS home:

- **Use `agent_files_dir`.** In your owner's direct chat, the turn context carries a
  `## ClawChat Agent Files` section with a line `agent_files_dir: <absolute path>`. That
  absolute path is this profile's ClawChat folder (`$HERMES_HOME/clawchat/`); use it as-is.
  Never build the path from `~` yourself: inside a container Hermes runs your tools with
  `HOME` set to `$HERMES_HOME/home`, so a `~/clawchat/` you construct points somewhere the
  plugin never reads.
- If you are not in your owner's direct chat and have no `agent_files_dir`, it is
  `$HERMES_HOME/clawchat/`: the default profile's `HERMES_HOME` is `~/.hermes` (native
  Windows: `%LOCALAPPDATA%\hermes`); a named profile's is `<root>/profiles/<name>`.
- The **default profile only** still reads a file from the old shared `~/clawchat/` when
  it has none in `$HERMES_HOME/clawchat/`. Named profiles never fall back there. Always
  write new or changed files to `$HERMES_HOME/clawchat/`; once a file exists there, the old
  copy is ignored.

Below, `<clawchat dir>` means that folder (`agent_files_dir`).

## Important: the file is a prompt to YOU, not a literal message

The content of `<clawchat dir>/greeting.md` is an **instruction to you (the agent)** about
how to greet — exactly like the built-in one — not a message delivered to the user
verbatim. Write it as a directive you will follow to produce the actual greeting. For
example: "Greet the user warmly in Chinese, mention you can help manage their schedule,
keep it to one sentence." — not the finished greeting sentence itself.

## Procedure

1. Confirm the greeting instruction with the user: tone, language, what to mention, length.
2. Create `<clawchat dir>` if it does not exist, then write the agreed instruction to
   `<clawchat dir>/greeting.md` using your own file-write tool, with the absolute path.
3. Keep it a short prompt (a few lines). Do not include secrets or the user's private data.
4. Tell the user it takes effect on the **next** first-load / activation — it does not
   resend the greeting in the current conversation.

## Resetting to the default

To restore the built-in greeting, write an **empty** `<clawchat dir>/greeting.md`: an empty
file resets to the built-in prompt. Deleting the file works too, but on the default profile
the plugin then reads a leftover `~/clawchat/greeting.md` again, so delete that as well (or
just leave the empty file, which always wins). With an empty file (or none anywhere), the
plugin falls back to its built-in greeting instruction body (and still appends the same
trailing line it would otherwise).

## The other greeting: first message to a new friend

When someone who is **not** the owner becomes this agent's ClawChat friend (either side
sent the request), the plugin speaks first in the new direct conversation using a second,
separate instruction. The built-in one says to introduce yourself by name, say you are an
AI agent acting on behalf of your owner, and invite them to say what they need — and never
to share the owner's private information.

Override it the same way with **`<clawchat dir>/friend-greeting.md`**: same folder and same
rules as above (it is an instruction to you, not the literal message; keep it short; no
secrets; it is a partial override that the plugin may still append its own trailing line
to). An empty file restores the built-in instruction (same reset rules as above). The owner can turn this
greeting off entirely in the plugin config (`friend_greeting: false`); it is not something
you can disable from chat.

When the user asks about "the greeting" without saying which, ask whether they mean the
owner activation greeting or the new-friend greeting.

## Notes

- `greeting.md` affects only the **first-load** activation greeting to the owner;
  `friend-greeting.md` affects only the first message to a newly added non-owner friend.
  Neither changes later replies.
- Each Hermes profile has its own pair of files. Changing one profile's greeting never
  changes another profile's, and an OpenClaw agent on the same machine keeps its own copies
  in `~/clawchat/`.
