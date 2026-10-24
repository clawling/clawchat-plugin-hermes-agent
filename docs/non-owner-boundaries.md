# Non-owner boundaries

For implementers and auditors: trace non-owner tool enforcement, execution origins, and attachment safety.

## Host tools in turns the owner did not start

Non-owner turns — a friend's direct chat or a group batch with non-owner
speakers — lose these host toolsets by default (rules can restore authorized
tools; see [tool grants](tool-grants.md#per-person-tool-grants)). A trusted owner-only batch keeps ordinary host tools even
in a group; grant management remains owner-direct-chat-only. The default
restricted toolsets are:
`terminal` (`terminal`, `process_manage`), `file` (`read_file`, `write_file`,
`patch`, `search_files`), `code_execution` (`execute_code`), `delegation`
(`delegate_task`), `cronjob` (`cronjob_manage`), `kanban` (every `kanban_*`
tool), `computer_use` and
`browser` (every `browser_*` tool: it opens `file://` URLs and runs page
scripts and raw CDP, so it reads local files too), Hermes' `memory` tool,
`session_search`, `connections` (`manage_connections` and every hosted
connector tool `connectors__<app>__<tool>`), and every **MCP tool** — the MCP
servers the owner configured for ClawChat (a filesystem or database server
reads the same files the `file` tools do).
Kanban tasks run in a separate process with the assignee profile's full tools
and without the initiating turn's session identity. The plugin's hook cannot
identify the caller there, so it blocks worker/subscriber-routing mutations
even with a grant (see the [audited route list](#cross-session-execution-routes)).
For the same reason the hook also blocks Bot Mode's `message_agent` (the
receiving agent runs with its own full tools).
Authority is resolved for the trusted speakers in each batch. Restoring
toolsets can rebuild Hermes' cached agent; the hook still checks concrete
calls. See [Local attachments](#local-attachments) for file delivery authority;
other plugin local-path restrictions remain. In non-owner turns, HTTP(S) attachments are fetched only from globally
routable unicast addresses. Connections use the checked IP directly, without
proxies, and each redirect is resolved and checked again. In non-owner ClawChat
turns, `send_message` to other platforms cannot carry attachments; ClawChat
targets retain the local-file and remote-address policy. Without a matching
explicit rule, web search/extract and skills keep their existing access.
Owner-side turns keep ordinary host access; plugin resource checks remain.

The plugin's own synthetic-turn rules are unchanged: its one-time activation
greeting keeps ordinary owner tools based on the stored pairing;
contact/comment notifications, permission receipts, memory migration hints and
sediment turns use defaults. Synthetic turns cannot manage grants. Management
requires the verified real owner in that direct chat. See
[Spontaneous turns and origin inheritance](#spontaneous-turns-and-origin-inheritance).
On old hosts without plugin hooks, plugin-generated turns that use defaults
are refused entirely; all supported host versions provide hooks.

Hermes before 0.19.1 does not pass the turn's chat type to plugins. There the
plugin falls back to the chat id: a turn is the owner's only in the owner's
activation conversation (ids compared case-insensitively) when the plugin
does not know that chat as a group; anything it cannot place is not the
owner's. The memory tools' read scope uses the same fallback, so the owner's
direct chat reads every note on those hosts too.

Two layers (`clawchat_gateway/host_tools_guard.py`):

- the adapter's per-source toolset override (`toolsets_for_source`, Hermes
  0.20.1+): without grants, `public-tools` or the `approve` opt-in, these
  restricted tools are not in the turn's schema at all. Grant management tools use a separate
  `clawchat-grants` toolset, removed from non-owner turns under both policies.
  MCP needs Hermes'
  `no_mcp` sentinel in that list: a list that names no MCP server makes
  Hermes add back every enabled one. On an older host the plugin logs a
  warning at load and relies on the second layer;
- a `pre_tool_call` hook that applies per-person grants first, then blocks
  ungranted restricted calls still reaching dispatch (or escalates them under
  `approve`). MCP tools are recognised by their host toolset
  (`mcp-<server>`), else by the `mcp_` name prefix (`mcp_<server>_<tool>`
  before Hermes 0.18.1, `mcp__<server>__<tool>` since); hosted connector tools
  by their `connectors__` prefix (the `tool_call` bridge hands the hook the
  underlying tool's name).

Without an applicable per-person grant, the hook also limits a few tools
those turns keep, by what a call asks for. Legacy toolset narrowing can leave
these tools in the schema, while the current catalog filter can omit them;
the channel prompt carries a line listing the limits:

- `skill_manage` is blocked: a skill it creates or edits in place (bundled and
  hub skills included, `scripts/` too) is in the owner's direct chat from then
  on. `skills_list` and `skill_view` stay — the plugin's own `clawchat-*`
  skills are read through them — except that when the owner turned on
  `skills.inline_shell` (Hermes then runs a skill's ``!`cmd``` snippets when it
  is viewed), `skill_view` opens only ClawChat's own skills (`clawchat:<name>`,
  or the bare name of a bundled or updated one). An unreadable host config
  counts as inline shell on;
- `vision_analyze`, `video_analyze` and `image_generate` (`image_url`,
  `reference_image_urls`) read any image on the machine on Hermes' default
  local backend. They take only `http(s)://` and `data:` sources and files
  inside the host's media caches (`$HERMES_HOME/cache`, `image_cache`, …) or
  the plugin's inbound attachment directory (`media_download_dir`, default
  `/tmp/clawchat-media`); paths are resolved (symlinks, `..`) first;
- `text_to_speech` with an `output_path` is blocked: it writes anywhere outside
  Hermes' protected paths. Without one the audio goes to Hermes' cache as usual.

An external memory provider (`memory.provider`: honcho, mem0, supermemory, …)
brings its own tools. Hermes gates them with the `memory` toolset only from
0.18.1, and only layer 1 drops that toolset, so the hook restricts ungranted calls by the
names the provider itself lists (loaded once per process). If the provider
cannot be loaded or lists no tools, a turn layer 1 does not narrow (no override,
or `approve`) blocks ungranted calls to tools the host's tool registry does not know, and the
plugin logs it.

What each Hermes version gets (block columns describe calls without an
applicable per-person grant). Per-person entries also require actual
`pre_llm_call` support and matching execution ids; a version alone is not proof:

| Hermes | Layer 1: tools left out of the schema | Layer 2: `pre_tool_call` block | `approve` | Memory provider tools | Per-person grants |
|---|---|---|---|---|---|
| 0.21.0 and later | yes | yes | yes — ungranted restricted calls go to the human gate | left out with `memory` (layer 1); under `approve` ungranted calls go to the gate | direct in both layers; ask with a reliable owner approval route and no bypass |
| 0.20.1 – 0.20.x | yes | yes | no — blocked instead (the tools stay visible) | left out with `memory` (layer 1); under `approve` blocked by name | direct in both layers; ask refused |
| 0.19.1 – 0.20.0 | no — the tools stay visible | yes | no — blocked instead | visible, blocked by name | direct through the hook only; ask refused |
| 0.12.0 – 0.19.0 | no — the tools stay visible | yes (no chat type from the host: the owner's activation chat id decides, see above) | no — blocked instead | visible, blocked by name | unsupported; management tools require upgrade to 0.19.1+ |

`connections` exists from Hermes 0.21.3. The argument limits above and the
memory provider restrictions are layer 2 only, on every version; applicable
grants take precedence over these restrictions.

Where ungranted tools stay visible but their calls are blocked, each such turn's
ClawChat channel prompt says so, so the model does not spend rounds trying
them. Every Hermes that can load the plugin (0.12.0+, `register_platform`)
can take the hook; should one ever load it without `register_hook`, no layer
could keep these tools out, so the plugin does not answer friends' direct
chats or groups at all there (it logs `reason=host_without_plugin_hooks`)
and only the owner's direct chat works.

`non-owner-host-tools: approve` is the owner's opt-in: restricted host tools stay
visible, and calls without an applicable per-person grant go to Hermes'
human-approval gate, which smart never answers. A friend's direct chat declines
these ungranted calls on the spot; a group's approval goes to the owner.
See [rule resolution](tool-grants.md#rule-resolution-and-all-tools) and the
[audited routing boundary](#cross-session-routing-and-known-limits) for grant precedence and limits.

Hermes ignores an exception from either layer and then *opens up* (all
platform tools; the hook skipped), so both fail closed: an override that
cannot work out the platform toolset narrows the turn to the plugin's own
`clawchat` toolset plus `no_mcp` (without the sentinel Hermes would add
every MCP server back), and a hook that cannot place the turn blocks the call.

## Owner-only tools

`clawchat_widget_send` is also owner-only and excluded from public overrides,
`all` and exact grants. Its catalog, hook and handler use the same matching
ClawChat snapshot checks, but require `owner_side` or `legacy_owner_tools`;
orchestration `approved_retry` authority alone is insufficient. Existing host
internal turns in the owner DM qualify. Current scheduled heartbeats are
non-internal and have no owner snapshot; they and native cron workers without
ClawChat context remain blocked. It sends only to the effective owner's resolved DM, needs no
`purpose`, and leaves normal follow-up replies enabled. See
[widget tools](reference/tools.md#owner-home-screen-widgets).

The twelve `clawchat_orchestrate_*` tools are owner-only: catalog filtering,
`pre_tool_call` and a handler check each enforce a matching ClawChat turn
snapshot. Owner-only speaker batches (including owner batches in groups),
trusted owner actions and orchestration `approved_retry` receipts qualify;
review forks, mixed batches, grant receipts and process completions do not.
An owner-DM host internal event qualifies through its existing owner snapshot.
Native cron workers that clear ClawChat session context remain blocked.
Retry receipts open only orchestration. Overlapping owner batches retain that
authority; non-owner speakers or overlapping turns without orchestration authority
revoke it, except that a fresh trusted retry receipt keeps its own authority.
A retry receipt that lands while its session is busy waits for idle before
dispatch: Hermes runs mid-turn input as a follow-up under the running turn's
nonce, which the receipt itself would revoke. Neither `all` nor exact per-person
grants/requests include these tools; stored rules naming them are ignored.
Listing one in `public-tools` rejects the entire list with an `owner-only tool` warning.

Every call requires `purpose`, a nonblank string, trimmed and truncated to 60
Unicode code points for the approval card. The pre-hook stores trusted conversation/owner attribution by tool-call
id and snapshot nonce in a locked, bounded ten-minute cache. The handler consumes
it and rechecks the live snapshot and initiator; revoked or released snapshots
fail closed. Without an entry it checks the current session snapshot before
constructing a client. Group-info guidance appears only when the conversation
tool is both in the host catalog and visible under the current snapshot.
Requests carry `X-Clawchat-Origin-Conversation`, `X-Clawchat-Origin-Initiator`
and `X-Clawchat-Purpose` encoded with UTF-8 percent escapes (including `+` and `%`);
writes retain JSON content-type.
Backend code `21007` becomes `owner_turn_only`; other orchestration business
envelopes remain intact. Missing trusted session attribution fails closed.

## Management and turn snapshots

Speaker authority comes from parsed sender IDs and the coalescer's internal
speaker tuple; envelope `messages` fields never establish authority.
Exact-name resolution canonicalizes verified host aliases without rewriting storage.

The adapter freezes grants before dispatch and carries a nonce in the source
message id. `pre_llm_call` binds it once to the actual session and turn; calls
must match nonce, chat, session and turn, with no latest-session fallback.
Overlapping snapshots in one session intersect. Messages that could mix
permissions with an executing granted non-owner turn wait for the next batch;
the owner's direct chat admits approvals and control messages immediately.
Plugin synthetic or unidentified input in that direct chat removes owner
verification and intersects live grants with defaults until the session is
idle; finishing one nonce alone cannot restore it. Trusted host-internal turns
use the origin rules below and never gain grant-management verification.
Read failures, expired grants and missing snapshots add no authority.
Inner calls without execution IDs, such as `execute_code` RPC calls, use the
snapshot already bound to `HERMES_SESSION_MESSAGE_ID`; an explicit deny on a
default tool still applies. On a host supporting execution binding, failure to
recover it means defaults without approve. Hosts lacking `pre_llm_call` keep
approve for calls without execution IDs only when the plugin turn nonce in the
host's trusted `HERMES_SESSION_MESSAGE_ID` context names a turn with speakers;
host-started and plugin synthetic turns get defaults without approve. Hosts
that do not expose `HERMES_SESSION_MESSAGE_ID` to tools cannot attribute any
turn, so group approve is unavailable there.

With grants enabled, a bound non-owner turn can use `tool_search` and
`tool_describe` only when the grants catalog filter is installed and the
snapshot read by that filter matches the bound grants and non-owner speakers,
including the chat ID. The host reads this authorized catalog with
`skip_tool_search_assembly=True`; it includes `clawchat_tool_request` even under
`all:deny` and excludes ungranted, denied and expired tools. In guest turns the
plugin neutralizes the host's remote connector search/describe, registry
fallback and list of declared but unavailable sources, so the bridges only see the filtered catalog, including after an
authorization expires between checks and dispatch. Without a complete guard, guest schemas skip search assembly, so no catalog
listing or source summary reaches the model. Bridges stay `not-available`
when that guard cannot be installed or verified, filtering is missing, or
snapshots mismatch. `tool_describe` returns the same refusal as a direct call
for registered names hidden in this turn or unavailable on the host. Owner turns
and other platforms keep the host's original search behavior. `tool_call` is
unpacked by the host and the real inner tool still passes through the execution hook.

## Local attachments

Replies and `clawchat_send_file` share the same local-file policy. A non-owner
turn can send a local file into its current conversation when **every caller
has direct access to every Files tool** (`read_file`, `write_file`, `patch`,
`search_files`). The ordinary selector priority and group intersection apply:
`cap:files` with mode `ask` does not open attachments, and an exact-tool ask/deny exception
to Files or `all:direct` closes this route. Cross-chat access alone does not
open local attachments.

Alternatively, a successful tool call permitted by `pre_tool_call` can declare
its own generated files in the structured completion result. These artifacts
are available only for that live turn, account, session, caller batch and
conversation. The permit binds the call id (or tool name plus a canonical
argument fingerprint), also verifies tool name and arguments when an id exists,
and is consumed once. An approved call under `non-owner-host-tools: approve`
also qualifies with `tool-grants: off`; that approval never permits another
call or a later completion. Failed/blocked completions consume without adding
artifacts. Ending the turn immediately clears pending permits and artifacts.

Guest delivery rejects symlinks (including parent directories), hard links,
non-regular files, credential/system paths, and files changed since declaration.
On POSIX (Linux and macOS), registration and guest reads open the root directory,
then each requested component with `openat` (`os.open` with `dir_fd`) and
`O_NOFOLLOW`. Directory opens use `O_DIRECTORY | O_CLOEXEC` and Linux's `O_PATH`
when available, otherwise `O_RDONLY`; the final file uses
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC`. Missing required flags or
directory-fd support refuses delivery. Because macOS opens directories with
`O_RDONLY`, a file under an ancestor directory that grants search (execute)
but not read permission, such as mode `0711`, is refused there even though
it could be opened by path. After user-home expansion, paths must
be absolute with a single leading separator and no NUL, empty, `.` or `..`
components (including repeated or trailing separators and the root alone).
The complete delivery policy must return exactly the requested traversal path.
macOS system symlink aliases are refused too; use the actual `/private/...`
path where applicable, still subject to the same denylist. macOS additionally
uses `F_GETPATH` after opening and after every verification walk: the kernel's
actual path spelling must match the traversal path exactly and pass the full
policy without normalization. This rejects case aliases that could bypass
string policy on case-insensitive volumes; failed queries refuse delivery.
Linux uses no kernel query returning canonical case. Denied paths on
case-insensitive directories or mounts (including ext4/f2fs casefold and vfat)
are matched by string policy; case aliases there are outside the guarantees.
macOS verifies actual spelling with `F_GETPATH`; Windows uses `normcase`.

The recorded identity combines this verified traversal path with the original
fd's device/inode/size/mtime/ctime and link count from `fstat`. Only a regular
file with one link is accepted. Before reading, and twice after reading, the
reader reapplies policy, checks the original fd, reopens the entire path from
the root using an extra fd, compares its complete identity and closes it.
It then takes a strict pathname sample with `os.stat(..., follow_symlinks=False)`,
reapplies policy, and performs the final path sample with `lstat` from the root:
every ancestor must exist as a real directory, and the leaf's complete identity
must match. The original fd is then checked again. Every identity comparison
includes all recorded fields. Extra fds only
verify identity; bytes come only from the original fd, up to its checked size
(100 MiB limit), with short reads refused. A final turn authorization check
precedes delivery.
Windows retains `GetFinalPathNameByHandleW`, checking the opened handle's path
against the complete policy and `fstat` identity, with prefix, case and native
short-name normalization. The expected path is normalized before opening;
rechecks bracket the normalized source-path policy check with two handle
identity checks. Failed handle queries refuse delivery.

Persistent parent moves or links left at the original location are refused by
the final `lstat` chain, including moves after the preceding stat or policy
check. File replacement and deletion are also refused. Changes during the final
chain can escape observation: user-space path queries are not atomic and cannot
prove the namespace stayed unchanged throughout reading. For example, if a
parent directory is moved into a denied location while the final chain is
running and a link to its new location is left at the original place, the
chain can still pass; the bytes delivered are those already read from the
file that the no-follow walk opened at the allowed path. Paths do not establish
a file's historical origin, and mount
aliases (including bind mounts) are not symlinks; an alias of the same object
cannot always be distinguished. Mount switches that change identity are refused.
Owner and local-operator delivery keep their original path checks and byte reads.
Avatar, moment image, app icon and media upload tools also retain their original
read paths. Files direct does not relax public-address checks for remote media.

## Narrow-tool artifact contract

A narrow tool must **declare only files it generated itself** and **construct
the output path itself**. It must not echo an arbitrary caller-supplied path
into the artifact result or declare pre-existing owner files. The plugin trusts
installed tool code to honor this contract; granting a tool that violates it
can expose local files.

Return a structured object (or its JSON serialization) through the host's
`post_tool_call` completion hook, for example:

```json
{"details":{"ok":true,"media":{"path":"/tmp/generated-speech.mp3"}}}
```

Only `details.media` is recognized: local `media`, `path`, `url`, `mediaUrl`,
`filePath`, `fileUrl`, strings in `mediaUrls`, or those keys in `attachments`
objects. `outbound:false`, error/failure/pending results and unknown shapes
grant nothing. Text in `content`, legacy `details.path`, and Hermes `MEDIA:`
lines are not artifact declarations. A model can reference an already allowed
file for delivery, but cannot create its permission by writing a path.

The artifact route requires a registered completion hook, a live nonce snapshot,
matching session/turn ids and explicit host `status:"ok"`, with no completion
error. These fields are not available on every supported Hermes release:
missing hooks/fields close the artifact route. No new host helper import or
`transform_tool_result` fallback is used; Files direct still works wherever
the existing execution binding is supported.

## Cross-session routing and known limits

### Cross-session execution routes

**No grant opens another execution session from a non-owner turn.** This
boundary also applies to `all` and group approve. The audited routes are:

- `delegate_task` spawn/steer (omitted action means spawn); only list/stop pass
  the routing check.
- `message_agent`, which starts work in another agent's session.
- `process` / `process_manage` calls with `session_id` unless the trusted process
  registry records the current host session as `parent_session_id` and its
  `session_key` still matches the trusted current approval-session key. Transfers
  update the latter. Unavailable ownership fails closed. Target-free list calls
  use ordinary authorization.
- `cronjob` / `cronjob_manage` create/run/enable and any action except
  list/pause/remove, even if delivery is origin/local.
- Worker/subscriber Kanban mutations: `kanban_create`, `kanban_complete`,
  `kanban_block`, `kanban_schedule`, `kanban_request_review`,
  `kanban_request_changes`, `kanban_unblock`, `kanban_link`, `kanban_comment`.

These routes cannot preserve the initiating turn's bound authority. Hermes
`subagent` / `schedule` grants therefore currently open only read or management
actions and remaining non-routing Kanban actions, subject to other checks.
New host tools/actions that start workers or wake subscribers must be added to
the audited inventory in `host_tool_targets_current_session`; arbitrary argument
scanning cannot prove the destination is safe. Parent grants are not inherited
into child sessions or sessions created after compression.

Cross-chat **messages** are separate: `cap:cross-chat` applies to
`clawchat_mention_message`, `clawchat_react_message`, `clawchat_send_file` and
`send_message` on older hosts that actually register it (current Hermes does
not expose it to the model). Non-owner delivery to another conversation needs
an **explicit matching grant**, resolved by exact tool > capability > `all`
and the batch intersection: direct sends; ask asks the owner for each call
(`ask-unavailable` if that cannot be done); deny or no grant refuses with
`denied`, `not-granted` or `batch-intersection`. Missing or unresolvable targets
count as cross-chat, never as the current chat. `CROSS_CHAT_GATE_POLICY` is
`grant`; `resolve_cross_chat_gate` returns `allow`, `ask` or `block`.

Default tools, `public-tools` and group approve cover **only the current chat**.
With grants off, or on old hosts without execution binding, non-owner cross-chat
delivery is `not-granted`. Owner-side turns are exempt. A cross-chat deny alone
does not hide schemas when a current-chat action is permitted; execution still
checks the actual target. Target, attachment and resource checks remain:
a cross-chat grant alone does not open local attachments. Tools that start or
continue work in another execution session still return `cross-session`, even
with a cross-chat grant.

Other host limits:

- Per-person grants need execution ids and `pre_llm_call` binding support;
  unknown/missing capabilities never imply authorization. On hosts unable to
  bind, legacy approve remains where the host supports it.
- Before Hermes 0.20.1, management tools can remain visible to non-owner turns,
  but execution refuses them. Toolset restoration can expose several members
  on older hosts; the hook still checks each concrete tool. Where supported,
  the catalog filter narrows schemas and search results after host caching
  and before search assembly.
- Granted group batches can rebuild the host agent/toolset cache. Other
  messages cannot interject into a held granted turn.

## Guest access requests and resumption

Confirmation atomically consumes the code, writes the capability rule, records
allowed/asked/denied with the decider and sets `wake_state=pending`. Write errors
roll back all of it. Receipt rendering failure after commit still reports success.
A unique `wake_key` serializes `pending → dispatching` before asynchronous work.
Wake dispatch resolves the original conversation and builds
`InboundMessage.internal_synthetic(grant_origin=…)`.
The process-local origin contains requester ID, request ID and card expiry;
the parser ignores all wire origin/synthetic/requester fields. Grant speakers
contain only the requester, never an owner identity, and current grants are
loaded again. Wakeups wait for the original session to be idle before `_handle_inbound`;
later real input retains the existing busy admission queues;
expiry is checked again immediately before `handle_message`. This avoids
newer host-only imports such as `admit_internal_event`, absent from the full
v0.12.0+ compatibility contract.
Deny wakes only to explain the refusal. No caller becomes owner-side or gains
ordinary owner tools.

Startup dispatches unexpired pending wakes. A dispatching claim older than ten
minutes becomes failed, with a pending notice; taking notices also recovers
stale claims. Failed wakes retain the effective grant and inject one short
English notice only on that requester's next real turn in the original
chat/session, expiring with the card. Notices are atomically claimed with a
ten-minute lease and marked consumed only after the host accepts the event.
Rejection or exceptions release the claim; expired claims are reclaimed on
startup or notice retrieval. Claim tokens prevent a late acknowledgement from
consuming a newer claim. Ambiguous dispatches are not retried;
the host has no guaranteed idempotent delivery, so this is one claimed dispatch,
not a promise of strict end-to-end exactly-once delivery.

`tool_requests.py` owns fixed card, continuation and notice copy and sanitation;
`clawchat_tool_request` has its own `clawchat-requests` toolset and uses the exact
nonce/session/turn binding and immutable `RequestOrigin`. Quota checks, code
insertion and request creation use one `BEGIN IMMEDIATE`; see
[guest access requests](tool-grants.md#guest-access-requests-and-resumption) for quota and card policy.

## Spontaneous turns and origin inheritance

`ORIGIN_INHERITANCE_POLICY` is `receipt-and-process-completion`.
`resolve_origin_grant_context` recognizes only two sources for inheriting a
requester's grants:

- Authorization-card wakeups (`grant-receipt`), with trusted origin attached
  inside the plugin, resume as the requester with current grants as described in [wake and notice delivery](#guest-access-requests-and-resumption).
- Background process completions (`process-completion`) from a successful
  `terminal` call with both `background: true` and `notify: true` (older hosts:
  `notify_on_complete: true`). Explicit non-`None` `notify` takes precedence over
  the old flag; missing or `None` `notify` falls back to the old flag.
  With grants enabled, `turn_attachments.post_tool_call` calls
  `process_origins.record_process_origin` to record the process ID, starting turn's
  plugin nonce, non-owner speaker set, account, chat and session route. Only a
  host `internal=True` event can match: metadata `notification_origin` must be
  `process_registry_synthetic`, `original_trigger_message_id` must equal that
  starting turn's plugin nonce, and the source chat and recorded
  `gateway_session_key` must match. Both ends need the host session key; a host
  that does not expose `HERMES_SESSION_KEY` to tool hooks gets no inheritance. The completion's first-line process ID
  narrows the match to one registered process; message text never identifies
  the requester. The registration is consumed once. Current grants are loaded
  again into a new turn snapshot, intersecting multiple starters' permissions.
  Revoked or expired access no longer applies. The turn is never owner-side,
  and unmatched tools get defaults without group approve.

The process registry is in memory, limited to 512 origin entries with a
seven-day lifetime. Restart, expiry or eviction loses inheritance. Watch and
heartbeat notifications, batched completions and mismatched or missing records
use only defaults. Process-completion inheritance is disabled with
`tool-grants: off`. Scheduled tasks, sub-agent reports and Kanban work do not
inherit the requester's grants. An unattributed spontaneous turn is treated as
a person without grants and has no group approve fallback on any host, also
with grants off.

There is one separate owner-chat rule: a host `internal=True` turn in the
activation owner's direct chat, without an existing plugin snapshot, uses
ordinary owner tools for the owner's own scheduled tasks, heartbeats, exec
completions and sub-agent reports. It does not gain verified-owner status and
cannot use the four authorization tools. This depends on guest turns being
confined to their own execution session by `cross-session` checks, unable to
start a turn in the owner's chat. If the host adds a tool that can start turns
in another session before the plugin's audited route inventory is updated,
that premise breaks. The plugin's own synthetic-turn rules described above
remain unchanged.

Host background review forks with a nonempty thread tool whitelist also use
ordinary owner tools in the owner's direct-chat session, subject to the host's
whitelist, when no snapshot is bound to the fork and its nonce proves that the
parent turn was owner-side in the same chat. Its snapshot must still be live or
have been recently released as owner-side. Released attribution keeps only
nonce, chat, owner-side status and release time in memory, limited to 512 entries
with a one-hour lifetime; restart, expiry or oldest-entry eviction loses this
fallback. Empty or unknown nonces and non-owner parents never qualify. Forks
gain no verified-owner status and cannot use the four authorization tools.
Forks in guest or group sessions gain no additional access.
Hermes can run `pre_tool_call` in a separate thread, where its
`_thread_tool_whitelist` is invisible. At registration the plugin mirrors the
host's set/clear whitelist API into a ContextVar carried by the host's copied
hook context, and also requires the called tool to be in that nonempty
whitelist. Clearing the host whitelist resets the mirror only in the current
context. Contexts copied before clear, including contexts propagated to threads
spawned inside a fork, retain its whitelist; they still require the owner's
direct chat, a same-chat owner-side parent and membership of the called tool
in that whitelist, and always refuse the four authorization tools. If an older
host or an unavailable mirror supplies no whitelist in the hook context, the fork
remains denied; same-thread hooks can still read the host's thread-local value.
When the host review fork builds its whitelist without a live snapshot, the
catalog retains ordinary owner tools, including `skill_manage`, only on a thread
named exactly `bg-review`, with `enabled_toolsets` supplied as a nonempty list
or tuple containing only `memory` and/or `skills`, and with the same
owner-direct-chat and owner-side parent checks. All four authorization tools
remain excluded. A renamed thread or missing, empty or different toolsets keep
the ordinary filtering without this exception.

Known limitation: this fork rule relies on Hermes internals that are not a
public plugin API — `hermes_cli.plugins.set_thread_tool_whitelist` /
`clear_thread_tool_whitelist`, the thread-local `_thread_tool_whitelist`, and
the review thread name `bg-review` (all as of Hermes 0.21.6). If a host release
renames or removes any of them, owner review forks lose `skill_manage` and
similar owner tools again (they are refused); no session gains access.

## Inbound media downloads

Inbound downloads allow HTTPS only; `media_allow_http` explicitly enables HTTP.
Relative URLs use the media base derived from the [Media settings](configuration.md#media) and undergo the
same checks. Non-HTTP(S) schemes are rejected before joining a URL to its base.
For a direct connection, each hop resolves the hostname once, rejects the
request if **any** resolved address is not public unicast, and connects to a validated IP while retaining
the hostname for Host and TLS verification. Localhost names are also rejected.
Connection failures try the remaining distinct validated IPs in order without
resolving again. TCP connection and TLS handshake each use a timeout capped by
the remaining attachment deadline. At most four inbound DNS lookups may remain
pending; further lookups are rejected until a slot becomes available.
Every redirect chooses its route again and repeats the applicable checks, with
at most five redirects; HTTPS-to-HTTP redirects require the HTTP opt-in too.
Backslashes in redirect destinations are
normalized before resolving and validating their target host.

`media_proxy_mode` controls environment proxies on every hop:

- `auto` (default): use the target scheme's proxy from Python's
  `urllib.request.getproxies()` (including `HTTP_PROXY` / `HTTPS_PROXY`). Without
  a proxy for that scheme, connect directly with full address validation.
  Unsupported proxy URLs, including `socks5://`, fall back to direct connections
  with a warning once per process; the warning omits the proxy URL.
- `on`: require a supported proxy for that scheme. A missing or unsupported
  proxy rejects the download.
- `off`: always connect directly with full DNS / public-address checks and IP
  pinning, ignoring environment proxies.

In `auto` and `on`, a hostname matched by `urllib.request.proxy_bypass(host)`
(`no_proxy` / `NO_PROXY`) takes the direct path with full address validation.
This also applies after redirects. Only `http://` proxy URLs are supported:
HTTPS targets use CONNECT followed by TLS with target hostname verification;
HTTP targets use an absolute-URI GET and still require `media_allow_http`.
The proxy itself may have a private address.

**Residual risk:** when `auto` (default) or `on` uses a proxy, target hostname
DNS resolution and private-network checks are delegated to that proxy. The
plugin rejects private, loopback and link-local IP literals before contacting
the proxy, and rejects localhost names, unless `media_allow_private_network`
is enabled. It does not resolve other target hostnames locally. If the proxy
can access private networks, the plugin cannot prevent a request whose
hostname resolves to an internal address. Set `media_proxy_mode: "off"` for
strict local destination validation (and leave `media_allow_private_network`
disabled).

Downloads **never attach Authorization** to the target, even for URLs on the
configured base origin: media URLs must be public or presigned GET URLs.
Proxy URL userinfo is sent only as Basic `Proxy-Authorization` to the proxy
(on CONNECT for HTTPS), and never logged or sent through the tunnel.

The default limit is 100 MiB per attachment. An oversized Content-Length is
rejected immediately; bodies stream in 64 KiB chunks with the same byte limit.
The socket timeout defaults to 30 seconds and the total attachment deadline to
300 seconds. Temporary `.part` files are removed on failure and renamed only
after a complete download. A rejected or failed attachment is logged without
URL userinfo, queries or fragments and skipped; remaining attachments and the
text message continue normally. Environment values take precedence over the
matching extra keys for all six inbound download settings.

## `media_local_roots` no longer scopes outbound delivery

`media_local_roots` used to be an allowlist of directories a local file could be
uploaded from. It is still parsed and still threaded through to
`media_runtime.ensure_allowed_local_path`, but that function accepts the value
for backward compatibility only and **does not restrict delivery** any more —
eligible local files may be sent from any directory.

What is actually enforced is a fixed **credential / system-path denylist** in
`clawchat_gateway/media_runtime.py`, applied after symlinks are resolved so it
cannot be bypassed via a symlink:

- absolute prefixes `/etc`, `/proc`, `/sys`, `/dev`, `/root`;
- paths under the user's home: `.ssh`, `.aws`, `.gnupg`, `.kube`,
  `.config/gcloud`, `.hermes/.env`;
- each resolved Hermes home and its named profiles: the `clawchat/` plugin data
  directory, `.env`, `config.yaml`, `auth.json`, and plugin database files
  (including SQLite sidecars and existing stores at custom paths);
- the Windows equivalents resolved from the environment (`%SystemRoot%` /
  `%WINDIR%`, and the Credential Manager / DPAPI / Vault directories under
  `%APPDATA%` and `%LOCALAPPDATA%`).

A path that matches the denylist is rejected with a neutral `ValueError` in all
turns; ordinary local files remain available in the owner's direct chat or on a
local operator surface, regardless of `media_local_roots`.

## Known limits of the attachment policy

The plugin judges attachments by the ClawChat turn they come from. Where no
ClawChat turn is in play, it does not restrict further:

- A delivery made with no session context and no trusted live turn nonce (Hermes sends a turn's reply after
  clearing the turn's context; cron delivery) may carry attachments into the
  owner's direct chat, local files and http(s) URLs alike, fetched the ordinary
  way. The only recipient there is the owner. Into any other chat it carries
  no local file. A trusted live reply nonce can retain only that turn's Files
  or artifact permission; a released nonce cannot recover it.
- A `send_message` call from another platform's own session, or with no session
  context, to a non-ClawChat target is left to Hermes and that platform: the
  plugin does not decide what another platform's turns may send.
- Hermes itself checks that a `MEDIA:` path exists before the plugin sees the
  call. The plugin applies its attachment policy afterwards; for a refused
  attachment nothing is read or sent, but that existence check has already happened.

