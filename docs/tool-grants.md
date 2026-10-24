# Tool grants

For owners and operators: configure per-person tool access, confirm grants, and manage guest requests.

## Per-person tool grants

`tool-grants: on` (default) lets the owner set per-person rules for exact host
or connected-app tools, capabilities, or all tools. The three modes are
`direct` (no per-call approval), `ask` (ask the owner on each call), and `deny`
(refuse). Sensitive tools can be `direct`; sensitivity changes the confirmation
warning, never the requested mode. The owner requests grants in their ClawChat
direct chat; code resolves names from explicit user ids.

## Default host access

Hermes gives a ClawChat turn the platform's full host toolset, and its
factory `approvals.mode: smart` lets an auxiliary model run a "dangerous"
command it rates low-risk without asking anyone — while commands such as
`cat`, `ls` or `curl` never count as dangerous at all. The
[slash approval handling](tool-grants.md#slash-commands-in-direct-chats) only sees
calls that reach Hermes' gateway approval step, so on its own
it could not stop a friend, or anyone in a group, from getting the agent to
run commands on the owner's machine or read `$HERMES_HOME/.env`,
`config.yaml` or `owner.md`.

`session_search` goes because it reads every conversation in Hermes' session
store — the owner's direct chat, other friends' chats, other groups, other
platforms, and with its `profile` argument other profiles' stores — and has no
way to stay inside the current chat. Recalling what a person said before still
works through their ClawChat note (`users/<usr_id>.md`, the group's note).
`connections` (Hermes 0.21.3+) goes because on a messaging platform
`manage_connections` installs, enables or authorizes MCP servers in the owner's
config without a confirmation card, and while it is in a turn the owner's
connected hosted accounts (mail, calendars, issue trackers, through
`tool_search` / `tool_call`) are callable there.

`memory` goes because what it writes to `MEMORY.md` / `USER.md` is in the
system prompt of every later conversation, the owner's direct chat included
(from the next new session, or the next compression of a running one), where
it reads like the agent's own note. The tool only writes (`add` / `replace` /
`remove`); reading is that system-prompt injection, which is unchanged.
`owner.md` is different: it is the plugin's own note, injected into every
ClawChat turn and writable from every one (see
[`./reference/tools.md`](./reference/tools.md#what-a-conversation-may-read));
what of it to say where is left to the model.

Home Assistant (`homeassistant`, on by itself once `HASS_TOKEN` is set) and
Spotify (`spotify`) stay available in these turns: a friend or a group can
switch the owner's devices or play music on the owner's account, unless an
effective ask or deny rule requires approval or refuses the call. Owners who do
not want that should disable those toolsets for ClawChat
(`platform_toolsets.clawchat`).

Without grants or `public-tools`, `no_mcp` drops **every** MCP server from such
a turn, including the "portable" MCP servers other Hermes plugins bring: they
are configured by the owner too. The owner's direct chat keeps all of them.

See [non-owner boundaries](non-owner-boundaries.md#host-tools-in-turns-the-owner-did-not-start)
for the restricted toolsets, version matrix and enforcement.

## Public tools

`platforms.clawchat.extra.public-tools` adds exact tool names to the default
tool row for every non-owner turn. Dispatch, execution checks, short channel
hints, turns without a snapshot and `tool-grants: off` all use the same row.
Only installed, available tools can run; the setting does not install or enable
host tools. For example:

```yaml
platforms:
  clawchat:
    extra:
      tool-grants: on
      public-tools:
        - feishu_doc_read
```

The value must be an array of strings. Names are case-sensitive, trimmed, and
must match `[A-Za-z0-9_][A-Za-z0-9_.:/-]*`. Any invalid item disables the **whole
list**: a non-string, an empty or malformed name, `all`, a `cap:`, `tool:` or
`group:` selector, any member of the six sensitive capabilities (Terminal,
Files, Code execution, Sub-agents, Scheduled tasks, Browser and desktop control),
their verified host aliases, any `browser_` name, or any of the four
authorization tools, or any owner-only `clawchat_orchestrate_*` tool. One warning identifies only the first problem item and
its reason, never the whole configuration; non-string values use a placeholder.
A non-array value is also rejected. Repeated reads of the same invalid value
do not repeat the warning. An omitted value or empty array adds nothing and
logs nothing.

These additions do not appear in the grant list and cannot be changed in chat.
With grants enabled, a person's explicit matching deny (exact tool, capability
or `all`, using normal precedence) still closes that tool for them. Listing a
tool does not lift the plugin's per-call restrictions on `skill_manage` writes,
`skill_view` with inline shell, local-file media parameters, or
`text_to_speech.output_path`. Public access covers only the current chat;
cross-chat delivery still needs an explicit matching grant.

See [owner-only tools](non-owner-boundaries.md#owner-only-tools) for widget and
orchestration exclusions and their independent authority checks.

## Selectors and capabilities

The `tools` array accepts these selectors:

| Input | Meaning | Stored `what_kind` / `what` |
|---|---|---|
| `tool:<exact-name>` | One exact tool; preserve name and case after the first `tool:` | `tool` / exact name |
| `cap:<id>` | One capability from the canonical mapping below | `capability` / ID |
| Bare capability ID, e.g. `terminal` | Same as `cap:terminal` | `capability` / ID |
| Bare tool name, e.g. `read_file` | Same as `tool:read_file` unless reserved | `tool` / exact name |
| `all` | Runtime all-tools rule | `all` / `all` |

Capability IDs and bare `all` are reserved. Colliding tools must use
`tool:terminal` or `tool:all`. Names are case-sensitive; whitespace, control
characters, `*`, `?` and host `group:*` selectors are not accepted. MCP server
and toolset names do not select groups of tools.

Hermes validates exact names against its trusted registry and availability
against its effective host catalog. Unknown tools or capability IDs return
`invalid-what`; known but unavailable tools, or capabilities with no available
mapped member, return `not-available`. Neither error writes pending/deny rows
or sends a card. The entire selection is validated, normalized and deduplicated
before execution; an invalid entry rejects it all. Revoke can still name stored
tools that have been uninstalled.

Capability IDs, display names, sensitivity and exact Hermes tool mappings are
defined by [`tool_capabilities.TOOL_CAPABILITIES`](../clawchat_gateway/tool_capabilities.py).
Use that mapping as the capability reference.

Only tools actually registered and available participate. Legacy `process`,
`cronjob` and `send_message` are compatibility mappings, not new tools; current
Hermes does not expose `send_message` to the model. All listed `kanban_*` tools
belong to `subagent`. Exact `browser_*` tools also receive sensitive warning
copy, but a prefix does not grant access or add arbitrary third-party tools to
the capability. MCP, dynamic connector and other unclassified tools need an
exact rule or `all`. `memory` means host memory, not conversation-scoped
`clawchat_memory_*`; their resource and identity checks remain in force.

## Rule resolution and all tools

Effective per-person rules take precedence over `non-owner-host-tools`:
deny refuses; direct executes without this gate, while ask routes each call to
the owner's direct chat, including normally open tools such as `web_search`.
A host that cannot escalate a hook's `approve` blocks calls requiring approval.
Any other setting value means `off`.

For each person and concrete call, ignore expired rows, then choose matching
**exact tool > capability > all**. Only within the highest matching level,
choose **deny > ask > direct**, including overlapping capabilities. A more
specific direct rule can override a broader deny. For example, `all` direct
plus `cap:terminal` deny blocks Terminal; adding `tool:process_manage` direct
opens only that exact tool.

Exact-name matching mirrors the verified host aliases `process` →
`process_manage`, `cronjob` → `cronjob_manage`, `todo` → `todo_list`,
`tour` → `gui_tour`, and `tip` → `show_tip`. Stored rows and old pending cards
keep their original names; alias conflicts use deny > ask > direct at execution.

With no matching rule, use the host's ordinary default for that call. Explicit
ask or deny also applies to normally open tools such as `web_search`. In a
group with `non-owner-host-tools: approve`, a restricted tool with no matching
row defaults to ask for that person, then participates in the intersection.
This fallback is **per tool**, not per person: a grant for Feishu does not
remove approval fallback for an unrelated tool. Friend direct chats do not get
that fallback. Neither cross-chat delivery nor unattributed spontaneous turns
get it. Explicit deny and failed resolution
cannot be bypassed by approve.

Resolve each non-owner speaker first, then intersect: any deny refuses; any
missing permission refuses; otherwise any ask requires approval; only all-direct
runs directly. The owner's own messages are neutral. A nonempty trusted batch
whose speakers are all the owner has full ordinary host tools, including in a
group, but cannot manage grants there. Only the owner's own user id is recognized;
other agents belonging to the owner remain ordinary users. Missing/invalid
speakers and grant-store failures use defaults, never an owner-only empty-set
shortcut or an approve fallback that could miss a deny. Spontaneous turns use
the [origin rules](non-owner-boundaries.md#spontaneous-turns-and-origin-inheritance).
An allowed person may ask again alone after a mixed-turn block.

`all` is stored as one rule and evaluated at runtime against the host's
**effective tool surface**, including sensitive tools, connected tools and
**tools installed in the future**. The surface applies host configuration,
platform, global disabled toolsets, dependencies and credentials before this
plugin's per-turn narrowing. Execution rechecks current availability. It excludes
`clawchat_tool_grant`, `clawchat_tool_revoke`, `clawchat_tool_grants_list` and
`clawchat_tool_request`. It cannot bypass host global disablement, host policy,
platform availability or tool identity/resource checks. Exact and capability
exceptions still override `all`.

On this machine, turns started by others cannot dispatch sub-agents, create or run scheduled tasks, or create Kanban tasks; they can only view and stop existing ones.
Cards and activation receipts repeat this restriction for `all`, sub-agent and
schedule capabilities, and related exact tools.

## Confirmation and per-call approval

Direct and ask resolve every person's name in code, freeze selectors, mode
and expiry, and send a confirmation card only to the activation-recorded
owner direct chat. Nothing is active until a real owner message there sends
`/clawchat-allow <6-digit code>` within ten minutes. The command confirms the
frozen mode, including ask; it does not change it to direct. Wrong, expired
and reused codes never activate a grant. Codes are account-scoped, random
ASCII digits (leading zeros preserved), reserved in SQLite with at most five
collision retries, and never returned to the model. This is one initial attempt
plus at most five retries. Confirmation atomically
writes rows and consumes the code. `deny` writes immediately without a card.

Cards use the owner's language (zh, zh_Hant or en; others use en).
`richInteractions` does not change this text-and-command card. They show names
and ids, capability display names/exact tools, mode and an explicit
`Expires: No limit` / `Expires: {expiry}` line (`到期：不限` / `到期：{expiry}`),
plus a hint to say “until Friday” to set a deadline. Dates use the known owner
time zone, otherwise UTC marked `(UTC)`. Ordinary consequence copy says:
“This means: {names} can have me call these tools for them. Whatever you can
see or do with these tools, they can too.” It also says access is by tool and
does not guarantee restriction to one document or resource.

For direct/ask grants containing `cap:schedule`, `cap:subagent`, `all`, or an
exact member of either capability (including host aliases), cards also add the
code-generated self-running consequence line (R):

> Scheduled tasks, sub-agents, and kanban tasks that {names} starts run with the owner's configuration and all tools.

It is absent for deny. This line describes the host's execution model; it does
not open the cross-session routes Hermes blocks. The Hermes restriction copy
remains on the card as well. Guest request cards for `schedule` or `subagent`
show both lines, since they offer direct/ask choices.

A sensitive direct card, including `all`, adds this consequence copy (using
code-resolved names):

> {names}能在这台机器上跑任何命令、读写任何文件，包括这个 Agent 的登录凭据、你的模型 key 和这张授权列表。拿到凭据的人可以冒充这个 Agent 跟别人说话。只给你完全信任的人。

Every `all` card explicitly includes future tools and sensitive tools, and
excludes the four authorization tools. Ask cards say “These operations will
ask you first every time.” When per-call approval is unavailable, cards say
“This machine cannot ask you each time. You can only choose Use directly or
Deny.” Ask is not offered: an ask grant returns `error:"validation"`,
`message:"ask-unavailable"` and that explanation in a `note` in the owner's
language (English fallback), without a card or grant write; direct cards include
that explanation. The current proactive card
confirms a preselected mode; guest request cards instead select direct/ask/deny.
Profile names and labels neutralize Markdown markers, backticks and slashes
with fullwidth characters; only the code-generated confirmation command is clickable.

Ask needs Hermes 0.21.0+ and a working owner approval route. Card creation,
confirmation and execution all check this. Yolo, `approvals.mode: off`,
session/permanent allowlist bypass, unsupported hooks, unreadable policy or a
broken route refuse ask; runtime never downgrades it to direct. Each call has
a fresh rule key and shows speakers' names (ids when unavailable), conversation
type, tool and raw arguments to the owner. Only “Approve Once” and “Deny” are
offered. Session/permanent choices and aliases are treated as deny and persist
no permission. Rejection/timeout is `ask-declined`; unavailable routing or
failed delivery is `ask-unavailable`. Files, Terminal and Code execution direct
calls accept local-path arguments; other restricted direct calls retain the
local-path check. Attachment delivery retains its separate resource checks.

## Management and turn snapshots

[Tool parameters and return shapes](./reference/tools.md#per-person-tool-grants)
cover grant, revoke, list and request. The three management tools require `tool-grants: on`, ClawChat,
the activation direct chat, a real owner sender and a live nonce bound to the
host's exact `(session_id, turn_id)`. Hook and handler both verify them.
Groups (even owner-only batches), friends, other platforms and synthetic
turns cannot manage grants. Group grants freeze current membership, excluding
the owner and this Agent; other people's agents may be included. Every name
must resolve. Missing names or incomplete membership fail the entire operation.
New members do not inherit access and leaving does not revoke it.

Revoke changes storage immediately without a confirmation card, including
removing deny (`plan_deny_removal` returns `immediate`). Removing deny
returns to remaining rules or defaults. Changes affect subsequent turn
snapshots; they do not rewrite an already running snapshot. List includes
unexpired deny rows. A deny result must not be described as permission to use tools.

With `tool-grants: off`, stored rules are retained but ignored, management
and request tools are not registered and the grants hint is omitted. If registration cannot
read configuration, tools remain discoverable by the owner, but execution still
enforces off. The independent `non-owner-host-tools` policy keeps its behavior,
including approve when an old host cannot bind grants to an execution turn,
as long as the plugin turn names its speakers. A capable host with a missing/bad binding cannot use approve to bypass
potentially unseen deny rows.
Unattributed spontaneous turns have no group approve fallback on any host,
also when `tool-grants: off`. With grants off, non-owner cross-chat delivery is refused
as `not-granted`; `public-tools` still supplies default tools for the current chat.

## Non-owner hints and refusal reasons

The dynamic short prompt lists host capabilities with at least one currently
available member and no available member authorized as direct or ask, as
English display names plus IDs, e.g.
`Not available to the caller yet: Terminal (terminal), Files (files).`
It omits uninstalled/globally disabled capabilities and omits the list sentence
when empty. One authorized available member, including an exact-tool exception
to a capability denial, removes the capability from this hint; schemas and
execution still check each tool separately. Catalog lookup failure emits only
generic guidance. Mixed batches
describe shared availability without identifying who lacks access. The rest
says to call `clawchat_tool_request` with the ability ID and one short reason
for missing access, never request after denial or a mixed-turn block, and
explain only the requested ability in the chat's language. It forbids inventing reasons or revealing
others' access, settings, paths or versions.

Refusals carry `reasonCode` and fixed English guidance in `blockReason` /
`message` (legacy ordinary-policy refusals retain their existing messages):

| Reason code | Meaning and guidance |
|---|---|
| `not-granted` | Missing access; call `clawchat_tool_request` with the ability ID and one short reason. Do not try another route. |
| `denied` | The owner has not opened this ability. Do not retry or request it again. |
| `batch-intersection` | Multiple non-owner speakers lack a common permission. An allowed person can ask again alone; do not reveal who lacks it. |
| `ask-declined` | The owner rejected or has not answered this call. Do not retry this request; try later or contact the owner. |
| `ask-unavailable` | Per-call approval cannot be performed. Do not treat it as direct access; contact the owner. |
| `not-available` | The tool is unavailable here. Do not suggest requesting access or another route. |
| `cross-session` | The call would start or route work in another session. Do not retry or use another route; ask the owner directly. |

Current host unavailability takes precedence over grant refusal. Permission
refusals with multiple non-owner speakers use `batch-intersection`; the owner
plus one guest still uses that guest's `denied` / `not-granted` reason.

## Guest access requests and resumption

`clawchat_tool_request({capability, reason})` is independently visible to every
non-owner turn, even under `all:deny`. It does not inherit from `all` and cannot
specify a user or destination. In a group batch containing the owner and exactly
one trusted non-owner, that non-owner is the requester regardless of message
order. Two or more non-owners create no request context. Deny, mixed-turn blocks, unavailable capabilities
and existing access send no card. Unclassified tools require an owner-created
exact-tool grant. Model arguments never determine requester identity or routing.
For `cross-chat` requests, only unexpired explicit tool, capability or `all`
rules with mode `direct` or `ask` count as existing access; public current-chat
defaults do not.

One transaction checks the person × capability six-hour cooldown and the
Agent-wide rolling 24-hour limit of 20 request cards, reserves a code in
`tool_grant_pending` and creates `sending`. Code collisions retry at most five
times after the initial attempt.
A successful send changes it to `pending`; failure changes it to `failed` and
never claims the owner was asked. Only successful sending permits “I have asked the owner.”
An existing pending card or any non-failed
request within the six-hour cooldown returns `waiting`, `sent:false`, with the
same waiting guidance, even after a decision. This creates no new card or code
and writes no grant. Daily quota exhaustion returns `blocked` with
`reason="rate-limited"` and `sent:false`; the fixed guidance states that no
request was sent and the caller can ask the owner directly. Failed sends retain
the daily quota but do not impose the person cooldown. `REQUEST_DAILY_WINDOW_POLICY`
is `rolling-24h`: `resolve_request_daily_window(now)` defines `(now − 24h, now]`,
independent of the owner's timezone. Failed sends also count toward the maximum
of 20; a card exactly 24 hours old no longer occupies quota.

Only the owner's activation direct chat receives a code-generated card.
Profile names, group names and the relayed reason are cleaned, with links,
controls and hidden characters removed and Markdown/slash commands neutralized;
reasons are limited to 60 Unicode code points and marked “Their words, relayed”.
Subagent and schedule cards include the same three-language Hermes limitations
and self-running consequence line (R) as proactive direct/ask cards; other
capabilities do not include those lines.
A request card lasts 24 hours. `/clawchat-allow`, `/clawchat-ask` and
`/clawchat-deny` select direct/ask/deny without another confirmation card.
Ask is absent and refused when the actual approval route cannot ask reliably.
A proactive 10-minute card still accepts only `/clawchat-allow` for its frozen
mode. Every matching command is consumed before model dispatch; only a real,
platform-identified owner in this account's owner direct chat may confirm.
A card left in `sending` is also decidable: receiving the code proves delivery.

See [wake and notice delivery](non-owner-boundaries.md#guest-access-requests-and-resumption)
for the atomic decision, continuation and fallback notice lifecycle.

## Storage upgrade

Migration **14** (`tool_grants_v2`) transactionally rebuilds `tool_grants`
using `what_kind + what` instead of `tool`, verifies copied row counts, and
uses primary key `(account_id, grantee_user_id, what_kind, what)`.
`what_kind` is `tool`, `capability` or `all`; mode is `direct`, `ask` or `deny`.
Existing exact names migrate as `what_kind='tool'`, even if named `terminal`;
names, labels, modes and expiry are preserved, and old ask rows stay ask.
`tool_grant_pending` adds `purpose` (`grant` / `request`) and `payload_version`
(1 / 2). Old pending payloads remain version 1 exact-tool confirmations;
new grant cards use version 2 normalized selectors. Expiry timestamps are UTC
epoch milliseconds; `NULL` means no limit and `expires_at <= now` is inactive.

**After this upgrade, do not downgrade to an old plugin. Old and new plugin
processes must not share one database.** Old queries are incompatible with
the rebuilt table.

Migration **15** (`tool_grant_requests`) adds durable requests and wake notices.
It records the trusted requester and original chat/session/message, one opaque
request ID, one shared six-digit pending code, capability, sanitized reason and
summary, timestamps, decision/decider, unique wake key, wake state and notice
consumption time. Indexes cover person/capability/time, account/time and
status/expiry. Migration **16** (`tool_request_notice_claims`) adds notice
claim tokens and timestamps, preserving existing requests and decisions.
**Migrations 14–16 cannot be downgraded. New and old processes must not share a DB.**

## Slash commands in direct chats

Because every ClawChat user is allowed by default, the plugin itself decides
who may run a Hermes slash command in a direct chat. The agent's owner may run
any. Anyone else may only run commands that touch nothing but their own direct
session: `/new` (alias `/reset`), `/compress` (alias `/compact`), `/help`,
`/whoami`. Every other command — `/restart`, `/update`, `/config`, `/yolo`,
`/approve`, plugin commands such as `/clawchat-activate`, and any command a
future Hermes adds — is dropped before it reaches Hermes. This is an
allow-list (`NON_OWNER_DIRECT_SLASH_COMMANDS` in `clawchat_gateway/adapter.py`)
so a new Hermes command is owner-only until it is reviewed. If the owner id is
not known yet, only the allow-listed commands pass. Group commands are governed
by `group_command_mode` instead.

Hermes asks before `/new` / `/reset` (`approvals.destructive_slash_confirm`,
on by default) and takes text replies: approve, always or cancel, with
aliases. A non-owner answers that prompt through the plugin, not through
Hermes: for a direct chat with anyone but the owner the adapter implements
Hermes' `send_slash_confirm` hook, shows a prompt with only `/approve` and
`/cancel`, and resolves the reply itself via `tools.slash_confirm` for the
exact session and confirm id Hermes registered for that chat. Accepted
replies are Hermes' own spellings: `/approve` `/yes` `/ok` `/confirm`,
`approve` `approve once` `once`; `/cancel` `/no` `/deny` `/nevermind`,
`cancel` `no` `nevermind` (`!` works in place of `/`). Two host behaviours
stay out of reach this way:

- "always" (`/always`, `/remember`, `always`, `always approve`) persists
  `destructive_slash_confirm: false` in the owner's config.yaml. A non-owner's
  "always" is refused and the prompt stays open.
- Hermes sends `/approve` to a blocking dangerous-command approval in the
  same session ahead of the confirm. The plugin never forwards a non-owner's
  confirm reply to Hermes, so it can only ever resolve the confirm.

The owner, and groups, keep Hermes' own prompt and reply handling.

Hermes also reads some *plain text* as gateway control, which the slash-command
allow-list never sees: `always` / `approve` / `cancel` answer a pending
confirm, `yes` / `always` approve a blocking dangerous-command approval while
the agent is busy, and `restart gateway` in a direct chat becomes `/restart`.
On Hermes 0.20.1+ (whose `MessageEvent` has `allow_gateway_control`) the
plugin sets that field to `false` on every non-owner event except a command it
already let through (the allow-list above, or a group command allowed by
`group_command_mode`); a group batch counts as the owner's only when every
message in it is. Hermes then treats the text as conversation only, so it
still reaches the agent but controls nothing. One side effect: a non-owner's
answer to a Hermes `clarify` prompt arrives as a normal message instead of
resolving the prompt. On older hosts the field is not passed (they reject
it); instead the plugin drops a non-owner's `always`-type confirm reply, the
plain-text restart phrases, and an approval word while a dangerous-command
approval blocks that chat's session. In groups a non-owner's `/always` or
`/remember` is dropped even when `group_command_mode` is `all`.

For an ordinary dangerous-command approval inside a non-owner's direct chat,
Hermes' approval prompt (the command plus `/approve` / `/deny` choices) is
not shown to them, since they cannot approve it. The plugin denies that
approval immediately through Hermes' own `/deny` path
(`tools.approval.resolve_gateway_approval`), so the agent's tool call returns
BLOCKED at once instead of holding the chat's session for the approval
timeout, and tells the friend the step was declined because only the owner
can approve it. On Hermes hosts whose resolve call accepts a reason, the
agent also sees that it was declined by policy and must not retry; older
hosts relay a plain denial. The owner is not notified. On a host without
that resolve path the plugin only sends the note, and Hermes waits and
denies on timeout as before. Per-person ask grants are the exception: their
approval goes to the owner's direct chat instead (see
[per-call approval](#confirmation-and-per-call-approval)). Other owner-chat
and group approvals keep their existing behavior.

