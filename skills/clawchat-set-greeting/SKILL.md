---
name: clawchat-set-greeting
version: 1.0.0
description: Use when the user wants to customize, change, set, or reset this agent's first-load / activation greeting — what the agent says the first time it connects to a ClawChat conversation. Writes the greeting instruction to ~/clawchat/greeting.md.
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

## Notes

- This affects only the **first-load** activation greeting, not later replies.
- The same file is honored by both ClawChat agent runtimes (Hermes and OpenClaw).
