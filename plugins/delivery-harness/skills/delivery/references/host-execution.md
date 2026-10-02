# Host execution and honest evidence

## Capability preflight

`delivery.py doctor` checks local executable/module availability, not credentials,
network reachability, test devices or agent liveness. Inspect the current host's
available tools before starting. Git is required; Python 3.10+ on macOS/Linux is
the supported deterministic runtime. `gh` and working authentication are needed
only for GitHub publication. Windows locking/process support is not implemented.

Use the same shipped skill/scripts on both supported instruction hosts. Plugin
manifest compatibility is distinct from actually launching each host. If a CLI or
agent facility is missing, say so; do not simulate successful installation or work.

## Codex

Use actual available `collaboration.spawn_agent` (or its host-provided equivalent)
for a bounded independent task while useful coordinator work continues. Workers
share the filesystem, so assign exact absolute worktree paths and owned files.
Do not assume a spawn changes its working directory. Do not create a new app
thread when the user did not request one; subagents and user-visible tasks differ.

For an explicitly requested CLI launch, grant only the required worktree/run
directories and specific dependency caches. Never add the whole home directory
or weaken the sandbox to make a blocked operation pass; request scoped approval.

Do not override models unless the user selected one. Use read-only reviewers and
explicit implementation workers. Spawn arguments and role names must match the
tools available in this session, not a remembered schema. A successful spawn
returns a handle; record it, then send the dispatch ID back to the worker.
Use host message/wait/status tools for result and liveness evidence. A completed
unit fixture, stored PID, stale handle or narrated “agent” is not a live worker.

Run planned gate commands in the foreground. Do not daemonize a test or leave a
background server/device session holding a resource after the command returns.
A gate is stopped after its planned `timeout` (300 s when the plan names none, at
most 3600 s). For a gate longer than the host's command limit, run the `gate`
command in the background and wait for it rather than cutting it short.
Use explicit cleanup and host session ownership for those resources. The CLI
tracks its runner and direct children, not every detached descendant.

Gate commands execute as exact argument arrays, without an implicit shell. For
an intentional pipeline, declare Bash and pipefail explicitly, for example
`["bash", "-o", "pipefail", "-c", "false | cat"]`. This gate must fail even though
the final pipeline command succeeds; shell syntax in an ordinary argument is
not interpreted.

## Claude Code

Use the available native Agent/subagent facility with the same ownership and
report contract. Do not require a second orchestration plugin. Keep configured
models unless explicitly selected by the user. If the host cannot resume a prior
handle, record its observed absence, preserve the worktree, and register a fresh
dispatch for an authorized retry. Never reuse a prior report as its new result.

### The shared execution loop (preferred on Claude Code)

The plug-in ships `workflows/orchestrate-execute.js`, the pinned orchestrate
execution loop, unchanged. Launch it with the `Workflow` tool by name, passing
`args` as a JSON object: `delivery-harness:orchestrate-execute` when the plug-in is
installed (the Claude manifest declares it), or `orchestrate-execute` when the
script was copied into `~/.claude/workflows/` for the global-skill installation. A
launch by `scriptPath` works only when the skill directory is readable from the
session. Saved workflows load when a session starts. The loop refuses arguments of
another version; refresh the saved copy after an update.

1. `scripts/workflow_steps.py --run ROOT args` prints the arguments: every
   unverified task (or `--task T …`), the lenses (`--lens`, default conformance),
   an optional `--model`/`--effort` only when the user selected one, and the
   engine's report and verdict schemas. `--integration` instead targets the
   integration worktree after `integrate` and the integrated gates.
2. The loop runs each task in dependency order. It calls `workflow_steps.py`, which
   runs ordinary engine commands, through relay agents: `prepare` (`task-prepare`),
   `dispatch` (`task-register --via-workflow`), the implementer (labels `impl:T:L0`,
   `rework:T:L0r<n>`; its report carries the dispatch ID and the staged tree),
   `collect` (`task-import`; a missing, malformed, out-of-scope or tree-mismatched
   result is abandoned with `task-abandon`, which counts as a support round, while a
   journal, host-root or run problem blocks), `gate` (each planned gate with
   `gate --index`; a failing command is a red gate, while a gate that cannot run at
   all, for example without its side-effect authority, blocks), `rework`
   (`task-rework --decision code-fix` with `file|kind` finding keys),
   `review-open` (`review-token` per lens), one reviewer per lens returning its
   token, `review-close` (`review-import` of every lens; the engine's verdict wins),
   and `finish` (the engine's exported patch). Any engine block, spent budget,
   non-converging finding or invalidated attempt ends the task BLOCKED with the
   engine's reason; the coordinator decides as for a manual run.
3. Journal rules: `task-import` finds the one result carrying the dispatch ID in
   the host journal (`~/.claude/projects/<project>/<session>/subagents/workflows/<runId>/journal.jsonl`,
   or `--journal PATH`), requires its earlier `started` line and the agent's
   transcript, and records actor `workflow-agent:<runId>/<agentId>`, the handle and
   a source event bound to the journal line's hash. The report's `tree` must equal
   the worktree's staged tree. Abandon looks for failed agents under the loop's
   implementer labels (and the earlier `implement:<task>`) and cannot tell whether
   an agent is still running: wait for the workflow to finish before acting on a
   task by hand.
4. `review-import` imports exactly one verdict per current token, for every
   requested lens at once, from distinct agents that never wrote the change. Several
   verdicts for one token from distinct independent agents combine to the worst. A
   returned verdict always counts: a FAIL counts even when malformed (unusable
   defects become the key `(review)|other`); an invalid PASS, or a verdict from an
   agent that wrote the change, is skipped and recorded as refused, and the lens
   then needs a correct verdict. Any failing lens fails the review, and its defects
   become finding keys. Every issued token stays tracked: each mutating command
   first harvests verdicts that arrived late, whatever happened to their request,
   and records them against the content their token was issued for. A late FAIL on
   current content returns an unintegrated task to rework, or keeps the run from
   `ready`, `publish`, `merge` and `finish-local` (`late_review_fail`) until the
   content changes or the user records an override; a late FAIL on older content
   guards that content. A result with incomplete provenance fails closed: a FAIL
   counts, a PASS is ignored. Lines that do not decode are skipped; if a journal
   cannot be read, ordinary commands continue and record `harvest_incomplete`,
   while `ready`, `publish`, `merge`, `finish-local`, `review-import` and
   `task-import` refuse until the harvest completes. Any unreadable path under the
   host root, in any project, has this effect for every run that issued tokens; the
   paths are listed under `harvest.unreadable` in status, and restoring read access
   is the way out. Symlinked project, session or run directories are skipped and
   listed, so a project whose journals live only behind a symlink cannot use
   workflow import. Tokens are issued only where their verdicts can still be
   imported. A new request for changed content keeps every earlier lens. While a
   request is open, and for any task dispatched through a workflow, typed `review`
   is refused. Every FAIL, whatever its decision, guards its exact content and
   base: a later PASS on it is refused (`review_reroll`) unless the user recorded
   `authorize --scope decision --code review_override` for that target and content
   after that FAIL.

The engine commands stay available for recovery: after a loop ends, `status` shows
where each task stands, and the coordinator continues with the same commands (or a
new loop for the remaining tasks) instead of repeating recorded steps. The Codex
runner of orchestrate refuses these arguments (it writes no host journal); on
Codex use the native-agent path below.

Omit `model` unless the user selected one; the host's configured model is inherited.
Journals are accepted only under the host projects root the run pinned at `new`
(`~/.claude/projects`, or `DELIVERY_WORKFLOW_HOST_ROOT` when it was set; shown as
`workflow_host_root` in status); imports under another effective root are refused.
A named journal must follow the layout
`<root>/<project>/<session>/subagents/workflows/wf_*/journal.jsonl` and is checked
against every other journal there; `--search-root` may only name the root itself.
Status flags a run whose pinned root is not the default (`workflow_host_root_default`). That is provenance written by the host, not
authentication: whoever can write under that root can forge a journal. Symlinked
journal paths are skipped in a search and refused when named. Journal lines carry
no timestamps, so an imported result is ordered by its `started` line, not by time.
Wait for the workflow's completion before `revise`: revise archives open workflow
dispatches, and their agents could otherwise still write to the preserved worktree.

Claude Code Bash tool permissions do not enforce a read-only filesystem. A
reviewer label or restricted tool list is not that boundary: use actual host
filesystem enforcement where available and inspect repository state before and
after review to detect unexpected writes. Report any enforcement limitation.

## Dispatch brief

Give the worker the run root, plan hash, task JSON, exact worktree/branch, owned
paths, dependency assumptions, acceptance/oracles, gate commands, side-effect
boundaries, and evidence destination. Include:

> You are not alone in this repository. Work only in your assigned worktree and
> owned paths. Do not revert others' changes. Preserve any unexpected work and
> report it. Stage exact intended files. Do not commit, push, open a PR, merge,
> deploy, or mutate product/session records. Report actual tests and limitations.

The returned JSON includes `dispatch_id`, `summary`, `tests`, `limitations`, and
`source_event` identifying the actual host result. The coordinator checks the
worktree instead of trusting claimed changed-file lists. Missing evidence stays
missing, not “probably passed.”

`summary`, `dispatch_id` and `source_event` are strings. `tests` can be a concise
string, an array of strings, or command/outcome objects; `limitations` can be a
string or an array of strings. Empty arrays explicitly mean no tests ran or no
limitations were observed; they never count as passing mechanical gates. Prefer:

```json
{
  "dispatch_id": "the exact ID returned by task-register",
  "summary": "What actually changed and why.",
  "tests": [{"command": ["python3", "-B", "-m", "unittest"], "exit_code": 0, "outcome": "Observed test result and count."}],
  "limitations": ["A specific behavior that was not checked."],
  "source_event": "actual host handle, dispatch and captured final-result reference"
}
```

The engine refuses a task worktree that changed before its first dispatch or
between a failing report and its rework dispatch, a placeholder `source_event`
(such as `agent_registered`), and a `source_event` carrying a timestamp earlier
than the dispatch registration when every timestamp it carries is earlier (a spawn
time may precede registration; zone-less times are read as UTC). The
`workflow-agent:` and `workflow-pending` identity prefixes are reserved for journal
imports.

If the host exposes no opaque event ID, use the actual handle/dispatch plus the
captured final-result artifact and observation timestamp. Label that as an
observation reference, not a native event ID. Fresh dispatches need fresh observed
results; do not reuse an old final message or fabricate a host identifier.

## Independent review brief

Use a different actual actor, not a renamed implementer or another handle for the
same actor. Supply the approved contract, actual diff, current base/head, gate
receipts and claimed result. The reviewer independently runs relevant checks and
returns PASS or FAIL with concrete evidence: a PASS needs at least two lines and 160
characters naming what was observed per acceptance item and each rerun gate. It does not edit code or authority
records. Review the integrated diff as well as tasks, including omissions,
negative requirements, failure paths, security boundaries and cleanup.

For changes to in-flight requests, coalescing, retries or cancellation, exercise
two events in the same turn with collaborators that complete synchronously or
immediately, as well as delayed success and failure. Assert collaborator call
counts and error delivery, not only final state. A fake that never completes or
a queue pump between every event can hide duplicate requests. Identify the
specific behavior change that would make each regression test fail; run the
pre-fix case when practical and preserve the red-to-green evidence. Do not impose
a particular scheduling implementation if it breaks an existing caller contract.

When an analyzer can exit successfully with diagnostics, use the planned
[analyzer comparison](analyzer-checks.md); exit-code success alone is not proof
of zero new warnings. Inspect findings against the fresh base, not just counts.

If independent review cannot run, stop at its gate with a clear explanation or
request an actual human review. Do not create a synthetic review entry to progress.
Test fixtures may use synthetic actors only when labeled as tests, never as proof
that the host's agent workflow executed.

## Resume

The run is external and durable; process memory is not authoritative. After a host
restart, load status, query every relevant handle, inspect worktree identity and
state, then decide which result is still current. Do not launch a second worker
into an active task or its exclusive device/resource. Missing permission, network,
credentials, devices or a material decision are bounded blockers. Use host-native
approval prompts without weakening the sandbox.

Gate jobs also have durable identities. If a runner was interrupted, preserve its
record and resource locks, observe actual process exit, and use the run contract's
`gate-recover` command. Do not mark a gate finished by editing JSON or treat an
unlocked file as evidence that its child stopped.
