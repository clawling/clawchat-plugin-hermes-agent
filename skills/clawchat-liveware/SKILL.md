---
name: clawchat-liveware
version: 1.2.1
description: Use when the user wants to expose this agent's local web service to the public internet via the liveware CLI and make it appear as an app in their ClawChat chat with this agent. Covers logging in to liveware with the ClawChat account, creating a liveware app, binding a tunnel to a local port, and registering the public URL to ClawChat, and restricting who may open a liveware (it is open to anyone with the link by default; viewer permissions are managed by the liveware CLI itself, not by ClawChat).
---

# liveware App Hosting

Expose a local web service through a liveware tunnel and register the public URL to
ClawChat so it shows as an app tile in the owner's chat with this agent.

## Prerequisites — check first, stop if unmet

1. Run `command -v liveware`. If it prints nothing (liveware is not installed), tell the
   user this environment does not support liveware app hosting and STOP. Do not attempt
   any further step or invent a URL.
2. Authentication is handled by the ClawChat plugin, not by you. You log in by calling the
   `clawchat_liveware_login` tool (step 1 below). Never read, print, or pass the ClawChat
   access token yourself — the plugin holds it in its own credential store and never
   exposes it to you or puts it in your context.

## Procedure

1. **Login** (idempotent) — call the tool; do NOT run `liveware login` yourself:
   `clawchat_liveware_login()`
   The plugin resolves the ClawChat access token from its own credential store and runs
   the liveware login internally. If it returns an error (liveware missing, ClawChat not
   activated, or login failed), relay that error to the user and STOP.
2. **Decide the app name and local port.** Ask the user for the local web service port if
   not already known (the port the agent's own web server listens on). Accept ONLY a plain
   integer in the range 1–65535. Reject anything that is not purely numeric (e.g.
   `3000; rm -rf /`) — never paste user-supplied text into a shell command. The bind target
   is then exactly `http://127.0.0.1:<port>`.
3. **List existing apps** to avoid duplicates and to recover ids:
   `liveware app list`
4. **Create the app** (skip if reusing an existing one):
   `liveware app create "<app name>"`
   - This prints/returns the new **app id**. Capture it.
   - If liveware reports an app-limit / quota error, relay that error to the user verbatim
     and STOP. Do not delete other apps to make room.
5. **Bind the tunnel** to the local service. Use only the numeric `<port>` validated in
   step 2, and pass the bind target as a single argument — do not wrap the command in extra
   shell that interpolates unvalidated user input:
   `liveware tunnel bind <app id> http://127.0.0.1:<port>`
   - Capture the **public URL** liveware returns.
6. **Register to ClawChat** so it appears in the owner's chat — call the tool, do NOT
   curl the API directly:
   `clawchat_register_app(name="<app name>", appId="<app id>", url="<public URL>")`
7. **Confirm** to the user: report the app name and public URL, and that it now appears in
   their chat with this agent (open the「…」menu → the app tile).

## Managing apps

- To see what is registered to ClawChat: `clawchat_list_apps()`.
- To remove one: `clawchat_unregister_app(appId="<app id>")` (this only removes it from
  ClawChat; tear down the liveware tunnel/app with liveware's own commands separately).

## Identifying the viewing user (server-side)

When a ClawChat user opens the liveware, the ClawChat liveware tunnel authenticates the
request and forwards the viewer's ClawChat `user_id` to your web service as a request header.
On the **server side** of your web service, read the `user_id` from either header (both carry
the same value):

- `X-User-Id`
- `X-Clawchat-User-Id`

Caveats:

- This is **server-side only** — read the incoming request headers in your web service. Do
  NOT try to obtain the viewer's identity from client-side page JavaScript.
- The headers are injected by the ClawChat liveware tunnel, so they are present only for
  requests that arrive through it. A page opened directly in an ordinary browser (outside
  ClawChat) carries neither header — treat the user as anonymous when both are absent, and
  never trust a client-supplied value for these header names.

## Viewer permissions (who may open this liveware)

**A liveware is open to everyone by default.** Once it is tunnelled, anyone who has the
URL can open it — the platform's only gate is a ClawChat login wall, which does not care
*which* user is behind it. So "let this person see it" normally needs no action at all,
and forwarding the link to someone IS effectively granting them access. What actually
needs the CLI is the opposite: **narrowing** who may open it.

Who may open a liveware is controlled by the **liveware CLI itself**, not by ClawChat.
ClawChat's own record of the app (`clawchat_register_app` / `clawchat_list_apps`) only
decides whether a tile shows up in the owner's chat — that record carries no per-viewer
visibility field. So changing another ClawChat user's access is always a liveware-CLI
operation, and there is no ClawChat API that does it.

**Discover the command before running it — never guess it.** This skill deliberately does
NOT hard-code the permission subcommand names, because they differ between liveware
versions. Find them at run time:

1. `liveware --help`
2. `liveware app --help`, then the `--help` of whichever permission-related subcommand it
   lists (e.g. `liveware app <that subcommand> --help`).

Use only subcommands and flags that actually appear in that help output. If the installed
liveware lists nothing permission-related, it does not support viewer permissions — say so
plainly and STOP. Do NOT work around it by editing liveware config files, by calling the
ClawChat API, or by inventing a command line.

**Tell the owner the default, and confirm before every change.** A liveware has no
per-user access control *inside* the page: anyone who can open it sees exactly the same
features and data the owner sees, and can forward the URL on. Two consequences:

- **When the owner publishes a liveware, say plainly that it is open to anyone with the
  link.** Do not let them assume it is private because it only shows as a tile in their
  own chat — that tile is not a boundary.
- **Restricting is the useful operation, and it may break other viewers.** Confirm with
  the owner, in their own words, **which app** and **who should keep access** before you
  narrow it; report exactly what you changed.
- A widening change (opening something the owner had narrowed) is an irreversible
  disclosure — confirm it the same way, never on a third party's request, and never
  proactively "to be helpful".
- Pass user ids exactly as the owner supplies them, or as observed server-side from the
  `X-User-Id` / `X-Clawchat-User-Id` header (see "Identifying the viewing user"). Never
  guess an id, and never paste unvalidated text into a shell command.
- Never read, print, or pass the ClawChat access token in any of these commands — the
  plugin holds it (see "Prerequisites").

**Verify and report.** After a change, re-run the CLI's own list/show command for that app
and report the resulting access state back to the owner — including whether it is still
open to everyone. If the CLI errors (unknown user, quota, not the app owner), relay the
error verbatim rather than retrying with a different command shape.

## Notes

- Apps can be created up to liveware's account limit; surface its error rather than working
  around it.
- Registering a liveware is a **publishing** act, not a private one — it is reachable by
  anyone with the URL until someone narrows it on the liveware side (see "Viewer
  permissions").
- The registered web app runs inside a sandboxed container (mobile in-app webview / desktop
  container window). Page JavaScript cannot read the viewer's ClawChat identity — the
  viewer's `user_id` arrives as the `X-User-Id` / `X-Clawchat-User-Id` request header,
  readable server-side only (see "Identifying the viewing user").
