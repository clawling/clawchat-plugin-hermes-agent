---
name: liveware-app
description: Use when the user wants to expose this agent's local web service to the public internet via the liveware CLI and make it appear as an app in their ClawChat chat with this agent. Covers creating a liveware app, binding a tunnel to a local port, and registering the public URL to ClawChat.
---

# liveware App Hosting

Expose a local web service through a liveware tunnel and register the public URL to
ClawChat so it shows as an app tile in the owner's chat with this agent.

## Prerequisites — check first, stop if unmet

1. Run `command -v liveware`. If it prints nothing (liveware is not installed), tell the
   user this environment does not support liveware app hosting and STOP. Do not attempt
   any further step or invent a URL.
2. The ClawChat access token must be available as the `CLAWCHAT_TOKEN` environment
   variable (the ClawChat plugin sets it). Never print the token.

## Procedure

1. **Login** (idempotent):
   `liveware login --access-token "$CLAWCHAT_TOKEN"`
2. **Decide the app name and local port.** Ask the user for the local web service port if
   not already known (e.g. the port the agent's own web server listens on). The bind target
   is `http://127.0.0.1:<port>` (or the host:port the user specifies).
3. **List existing apps** to avoid duplicates and to recover ids:
   `liveware app list`
4. **Create the app** (skip if reusing an existing one):
   `liveware app create "<app name>"`
   - This prints/returns the new **app id**. Capture it.
   - If liveware reports an app-limit / quota error, relay that error to the user verbatim
     and STOP. Do not delete other apps to make room.
5. **Bind the tunnel** to the local service:
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

## Notes

- Apps can be created up to liveware's account limit; surface its error rather than working
  around it.
- The registered web app opens in a sandboxed in-app browser on mobile with no ClawChat
  login injected — do not assume the page can read the user's ClawChat identity.
