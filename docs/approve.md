# Command Approval

Hermes asks ClawChat for command approval when a tool or shell operation needs
an explicit user decision before it can continue.

## Current Message Shape

The adapter currently sends command approval as one `text` fragment with a
Markdown fallback that includes the command, reason, and slash-command choices.
It does not send an `approval_request` fragment on the display path yet.

The text fragment is intentionally complete enough to approve by replying with a
slash command:

````text
Command approval required:
```shell
rm -rf /tmp/example
```

Reason: delete in root path

Choose:
- Approve Once - reply /approve
- Approve Session - reply /approve session
- Always Approve - reply /approve always
- Deny - reply /deny
````

There is no extra `Text fallback:` line. The text fragment itself is the
fallback.

## Rich Approval Fragment

`approval_request` is the structured rich fragment intended for clients that
support approval buttons and action payloads. The current ClawChat clients may
not render that fragment and can display it as unsupported content, for example
`[unsupported content]`.

Until the client renders `approval_request` correctly, the adapter suppresses
that rich fragment in approval display messages and sends text only. The
`[unsupported content]` marker is the client rendering of an unsupported rich fragment,
not text added by the Hermes adapter.

## Group Approval Routing

When an approval request originates from a group chat, the adapter does not send
the approval prompt back into the group. It forwards the prompt to the owner's
direct chat and prefixes the text with:

```text
ClawChat group <group_id> requires owner attention.
```

The adapter records the owner direct chat as a temporary route for the original
group approval session. The canonical Hermes command approval replies are
`/approve`, `/approve session`, `/approve always`, and `/deny`.

For forwarded group approvals only, the adapter also accepts `/always` as an
alias for `/approve always` and `/cancel` as an alias for `/deny`. These aliases
exist for owner-forwarding compatibility, but the displayed prompt uses the
canonical Hermes commands.

If the owner direct chat is unavailable, the adapter returns
`clawchat owner direct chat unavailable` and does not send the group approval
prompt.

Group approval forwarding follows the same display rule: the owner direct chat
receives text only, without an appended `approval_request` fragment.
