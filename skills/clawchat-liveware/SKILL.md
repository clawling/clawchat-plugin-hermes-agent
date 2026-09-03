---
name: clawchat-liveware
version: 1.2.6
description: Use when the user wants to expose this agent's local web service to the public internet via the liveware CLI and make it appear as an app in their ClawChat chat with this agent. Covers logging in to liveware with the ClawChat account, creating a liveware app, binding a tunnel to a local port, registering the public URL to ClawChat, restricting who may open each app, and fully unregistering and deleting an app.
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
4. **Create the app with its access policy** (skip if reusing an existing one):
   `liveware app create "<app name>" --policy <public|private|allowlist>`
   - For `allowlist`, include the complete initial viewer list with repeatable
     `--allow-user <user id>` flags or one `--allow-users <user1,user2>` flag.
   - Access policy belongs to this exact app; it does not change any other app. If the
     user did not choose a policy, explain that the default is `public` before creating it.
   - This prints/returns the new **app id**. Capture it.
   - If liveware reports an app-limit / quota error, relay that error to the user verbatim
     and STOP. Do not delete other apps to make room.
5. **Bind the tunnel** to the local service. Use only the numeric `<port>` validated in
   step 2, and pass the bind target as a single argument — do not wrap the command in extra
   shell that interpolates unvalidated user input:
   `liveware tunnel bind <app id> http://127.0.0.1:<port>`
   - Capture the **public URL** liveware returns.
6. **Verify the bind and relay connection** through the public URL:
   `curl --fail --silent --show-error '<public URL>/liveware-status'`
   - Relay registration is asynchronous. Retry this read-only GET every 2 seconds for up
     to 60 seconds.
   - HTTP 200 alone is not success. Require JSON `code == 0`, `data.appId` equal to the
     exact app id from step 4, and `data.bound`, `data.relayConnected`, and `data.live` all
     equal to `true`.
   - `bound` means the control plane resolves the app to a tunnel instance;
     `relayConnected` means that instance has connected to the relay; `live` means both
     conditions are ready.
   - If the deadline expires, report the last response and run the read-only
     `liveware status` and `liveware app list` commands to distinguish an incomplete bind
     from a disconnected agent. STOP without creating another app or repeating the bind.
     Treat an app-id mismatch as the wrong URL or app, not as a transient state.
   - This endpoint checks control-plane and relay state, not the local web service. Also
     confirm `http://127.0.0.1:<port>` responds successfully. An unauthenticated request to
     the normal public application path may be rejected even when `/liveware-status` is
     healthy.
7. **Register to ClawChat** so it appears in the owner's chat — call the tool, do NOT
   curl the API directly:
   `clawchat_register_app(name="<app name>", appId="<app id>", url="<public URL>")`
8. **Confirm** to the user: report the app name, public URL, final `bound`,
   `relayConnected`, and `live` values, and that it now appears in their chat with this
   agent (open the「…」menu → the app tile).

## Managing and fully removing apps

- To see what is registered to ClawChat: `clawchat_list_apps()`.
- Removing only the ClawChat registration leaves the Liveware URL accessible. Treat app
  removal as complete only after both the ClawChat registration and the Liveware app are
  gone.

For a full removal:

1. Treat the user's initial removal request only as permission to inspect. Run
   `clawchat_list_apps()` and `liveware app list`, resolve one exact app id, then run
   `liveware app inspect <exact app id>`. Perform no mutation in this step.
2. Show the user the exact app id, app name, current access policy, and that confirmation
   will remove both its ClawChat tile and Liveware public route. Keep the public URL from
   the inspection for later verification, but omit it from the confirmation prompt. Ask
   for an explicit second confirmation after showing the other details. The initial
   removal request is not this confirmation; end the turn without unregistering or
   deleting anything.
3. After the user confirms, re-run `clawchat_list_apps()`, `liveware app list`, and
   `liveware app inspect <exact app id>`. Continue only if the id, name, URL, and access
   policy still match the inspected snapshot, and the displayed fields match what the
   user confirmed. If any field changed or the target is ambiguous, show the updated id,
   name, and access policy and request confirmation again without displaying the URL.
4. Remove the tile from ClawChat:
   `clawchat_unregister_app(appId="<exact app id>")`
   Stop if this fails; do not delete the Liveware app while its ClawChat registration is
   unresolved.
5. Remove the public route, tunnel binding, and access policy:
   `liveware app delete <exact app id>`
   `app delete` performs the Liveware-side unbind, so a separate `tunnel unbind` is not
   required.
6. Verify the exact app id is absent from both `clawchat_list_apps()` and
   `liveware app list`. Poll the former public URL's `/liveware-status` for up to 60
   seconds; completion requires it to stop reporting that app as `live: true` (a not-found
   response or `bound: false`, `relayConnected: false`, and `live: false` is expected).
7. Report full removal only when both inventories and the public status check pass. If the
   Liveware deletion fails after ClawChat unregisters, report a partial removal and the
   still-accessible app id; do not hide the failure or retry with a different command.

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

Access is configured independently for each exact app id through the liveware CLI:

- `public`: anyone who passes the platform's entry authentication can open the app.
- `private`: only the app owner can open it.
- `allowlist`: the owner plus the listed ClawChat user ids can open it.

ClawChat app registration only controls whether the tile appears in chat; it does not
store or change viewer permissions.

To change one app:

1. Run `liveware app inspect <exact app id>` and confirm its name, owner, and current
   access policy match the user's intended target.
2. Confirm the complete desired policy with the owner. Widening access can disclose the
   app; narrowing access can remove existing viewers.
3. Run exactly one of:
   - `liveware app access <exact app id> --policy public`
   - `liveware app access <exact app id> --policy private`
   - `liveware app access <exact app id> --policy allowlist --allow-user <user id>`
   - `liveware app access <exact app id> --policy allowlist --allow-users <user1,user2>`
4. For `allowlist`, provide every non-owner user who should retain access. The command
   replaces that app's complete allowlist; it is not an incremental add. The owner remains
   allowed automatically.
5. Re-run `liveware app inspect <exact app id>` and require the reported policy and full
   allowlist to match the requested state. Then probe `<public URL>/liveware-status` and
   require the same healthy result defined in procedure step 6. Report both access and
   tunnel state.

Pass user ids exactly as the owner supplies them or as observed server-side from
`X-User-Id` / `X-Clawchat-User-Id`. Never guess an id or apply one app's permission request
to another app. If the CLI rejects the change, relay the error and stop; do not edit
liveware config files or call ClawChat APIs as a workaround.

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
