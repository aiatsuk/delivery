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

### Workflow dispatch and review (preferred on Claude Code)

The plug-in ships two workflow scripts under `workflows/`. Launch them with the
`Workflow` tool by name, passing `args` as a JSON object: `delivery-harness:delivery-implement`
and `delivery-harness:delivery-review` when the plug-in is installed (the Claude manifest
declares them), or `delivery-implement` and `delivery-review` when the scripts were copied
into `~/.claude/workflows/` for the global-skill installation. A launch by `scriptPath` works
only when the skill directory is readable from the session; a session started elsewhere is
refused. Saved workflows load when a session starts.

1. `task-prepare --task T` as usual, then `task-register --task T --via-workflow`
   records a pending dispatch without any typed identity; read its `dispatch_id` from
   `status`.
2. `delivery-implement.js`, args `{run_root, tasks: [{task, dispatch_id, worktree,
   branch, brief, paths, acceptance, gates, instructions?, model?, effort?}]}`:
   one agent per task; each returns `{dispatch_id, summary, tests, limitations, tree}`,
   where `tree` is the worktree's `git write-tree` after its final staging.
3. `task-import --task T` finds the result carrying that dispatch ID in the host
   journal (`~/.claude/projects/<project>/<session>/subagents/workflows/<runId>/journal.jsonl`,
   or `--journal PATH`), requires its earlier `started` line and the agent's
   transcript, and records actor `workflow-agent:<runId>/<agentId>`, the handle and
   a source event bound to the journal line's hash. The report's `tree` must equal
   the worktree's staged tree. If the workflow ended without an importable result
   for a dispatch (a `failed` line, a skipped agent, a report the engine refuses),
   `task-abandon --task T --reason …` records what the journal shows and returns the
   task to rework; it counts as a support round. If the agent committed or switched
   branches, abandon invalidates the attempt instead and `task-prepare` creates a new
   worktree; budgets carry over. The same happens when the task worktree no longer
   exists. Abandon looks for the label `implement:<task>` the
   shipped script uses, and cannot tell whether an agent is still running: wait for
   the workflow to finish first.
4. After the task gates: `review-token --task T --lens L` per lens (each call is a
   recorded review request for the current content), then
   `delivery-review.js`, args `{run_root, targets: [{task (null for the
   integration), worktree, base_sha, acceptance, requirements?, gates, brief?,
   lenses: [{lens, token, model?, effort?}]}]}`; then
   `review-import [--task T] --lens L …` imports exactly one verdict per current
   token, for every requested lens at once, from distinct agents that never wrote the
   change. Several verdicts for one token from distinct independent agents combine to
   the worst. A returned verdict always counts: a FAIL counts even when malformed
   (unusable defects become the key `(review)|other`); an invalid PASS, or a verdict
   from an agent that wrote the change, is skipped and recorded as refused, and the
   lens then needs a correct verdict (rerun the review workflow with the same tokens).
   Any failing lens fails the review, and its defects become finding keys. Every
   issued token stays tracked: each mutating command first harvests verdicts that
   arrived late, whatever happened to their request, and records them against the
   content their token was issued for. A late FAIL on current content returns an
   unintegrated task to rework, or keeps the run from `ready`, `publish`, `merge`
   and `finish-local` (`late_review_fail`) until the content changes or the user
   records an override; a late FAIL on older content guards that content. A result
   with incomplete provenance fails closed: a FAIL counts, a PASS is ignored. Lines
   that do not decode are skipped; if a journal cannot be read, ordinary commands
   continue and record `harvest_incomplete`, while `ready`, `publish`, `merge`,
   `finish-local`, `review-import` and `task-import` refuse until the harvest
   completes. Any unreadable path under the host root, in any project, has this
   effect for every run that issued tokens; the paths are listed under
   `harvest.unreadable` in status, and restoring read access is the way out.
   Symlinked project, session or run directories are skipped and listed, so a
   project whose journals live only behind a symlink cannot use workflow import. Tokens are
   issued only where their verdicts can still be imported. A new request for changed content keeps every
   earlier lens. While a request is open, and for any task dispatched through a
   workflow, typed `review` is refused. Every FAIL, whatever its decision, guards its
   exact content and base: a later PASS on it is refused (`review_reroll`) unless the
   user recorded `authorize --scope decision --code review_override` for that target
   and content after that FAIL.

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
