---
name: clawchat-set-greeting
version: 1.1.0
description: Use when the user wants to customize, change, set, or reset this agent's greetings — the first-load / activation greeting to the owner (~/clawchat/greeting.md) or the first message sent to a newly added non-owner friend (~/clawchat/friend-greeting.md).
---

# Set the ClawChat first-load greeting

The **first-load greeting** is what this agent does the first time it connects to a
ClawChat direct conversation with its owner. By default the ClawChat plugin injects a
built-in instruction telling you to send a short, friendly self-introduction.

You can override that instruction with a file at **`~/clawchat/greeting.md`** (the
`clawchat` folder in the current user's home directory). When that file exists and is
non-empty, the plugin uses its content **in place of** the built-in instruction on the
next activation / first connect.

## Important: the file is a prompt to YOU, not a literal message

The content of `~/clawchat/greeting.md` is an **instruction to you (the agent)** about how
to greet — exactly like the built-in one — not a message delivered to the user verbatim.
Write it as a directive you will follow to produce the actual greeting. For example:
"Greet the user warmly in Chinese, mention you can help manage their schedule, keep it to
one sentence." — not the finished greeting sentence itself.

## Procedure

1. Confirm the greeting instruction with the user: tone, language, what to mention, length.
2. Create the `~/clawchat/` directory if it does not exist, then write the agreed
   instruction to `~/clawchat/greeting.md` using your own file-write tool. The path is
   literally the `clawchat` folder in the home directory.
3. Keep it a short prompt (a few lines). Do not include secrets or the user's private data.
4. Tell the user it takes effect on the **next** first-load / activation — it does not
   resend the greeting in the current conversation.

## Resetting to the default

To restore the built-in greeting, delete `~/clawchat/greeting.md` (or empty it). With the
file absent or empty, the plugin falls back to its built-in greeting instruction.

## The other greeting: first message to a new friend

When someone who is **not** the owner becomes this agent's ClawChat friend (either side
sent the request), the plugin speaks first in the new direct conversation using a second,
separate instruction. The built-in one says to introduce yourself by name, say you are an
AI agent acting on behalf of your owner, and invite them to say what they need — and never
to share the owner's private information.

Override it the same way with **`~/clawchat/friend-greeting.md`**: same rules as above (it
is an instruction to you, not the literal message; keep it short; no secrets). Delete or
empty the file to restore the built-in instruction. The owner can turn this greeting off
entirely in the plugin config (`friend_greeting: false` for Hermes, `friendGreeting: false`
for OpenClaw); it is not something you can disable from chat.

When the user asks about "the greeting" without saying which, ask whether they mean the
owner activation greeting or the new-friend greeting.

## Notes

- `greeting.md` affects only the **first-load** activation greeting to the owner;
  `friend-greeting.md` affects only the first message to a newly added non-owner friend.
  Neither changes later replies.
- Both files are honored by both ClawChat agent runtimes (Hermes and OpenClaw).
