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

## Claude Code

Use the available native Agent/subagent facility with the same ownership and
report contract. Do not require a second orchestration plugin. Keep configured
models unless explicitly selected by the user. If the host cannot resume a prior
handle, record its observed absence, preserve the worktree, and register a fresh
dispatch for an authorized retry. Never reuse a prior report as its new result.

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

If the host exposes no opaque event ID, use the actual handle/dispatch plus the
captured final-result artifact and observation timestamp. Label that as an
observation reference, not a native event ID. Fresh dispatches need fresh observed
results; do not reuse an old final message or fabricate a host identifier.

## Independent review brief

Use a different actual actor, not a renamed implementer or another handle for the
same actor. Supply the approved contract, actual diff, current base/head, gate
receipts and claimed result. The reviewer independently runs relevant checks and
returns PASS or FAIL with concrete evidence. It does not edit code or authority
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
