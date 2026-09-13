# Durable run contract

## One source of authority per concern

| Concern | Authority | It does not authorize |
| --- | --- | --- |
| Intent and material choices | Actual user input plus approved semantic fingerprint | External effects or deployment |
| Product context | Explicitly selected Markdown product/work item | Invented product decisions or implicit session access |
| Scope and risk | Versioned plan, requirements and classification | Lowering hard triggers for speed |
| Implementation | Scoped request or separately recorded authority | Publication or merge unless explicitly included |
| Execution evidence | Real host result + actual command receipt + current Git content | A stale PASS on new content |
| Independent review | A real actor who has not implemented the change | Self-review under a replacement handle |
| Publication | Current publish grant + exact tested integration head | Main pushes, unrelated PR adoption or merge |
| Merge | Explicit exact-PR grant + fresh base/head/checks | Deployment or protection bypass |

The Python state is not an authentication or sandbox system. The host controls
who is speaking, available tools, filesystem/network permissions and real process
identity. Never manufacture authority strings to make a gate pass. They preserve
provenance, not cryptographic proof of a person.

## Artifacts and identity

`DELIVERY_HOME` defaults to `~/.local/state/delivery-harness`. A run lives at
`<store>/<primary-name>-<canonical-path-hash>/runs/<id>`; explicit `--store` is
supported. Git common-directory resolution maps linked worktrees to the primary.
Stores must be outside every checkout. Repository, product and work-item bindings
stay stable. Do not move a run root or hand-edit its identity.

`run.json` is revision-locked state; `plan.json` is its semantic plan artifact;
`intent.md` preserves the input. `tasks/`, `patches/`, `evidence/`, `spec/`,
`checks/` and `spec-snapshots/` retain task contracts and evidence. Worktrees are
under `worktrees/v<version>/`. Failed attempts and earlier versions are preserved,
never silently replaced. Avoid secrets in every artifact.

Use `--expected-revision` from the last status when a mutating command supports
it. A concurrent state change requires reload and reconciliation. A lock guards
the write, not arbitrary out-of-band file edits. Artifact drift is fail-closed.

## Planning shape

The plan's eight scores are integers 0–2. Allowed Large triggers are `payments`,
`auth`, `security`, `privacy`, `migration`, `new_domain`, `breaking_api`,
`data_loss`, `multi_repo`, `staged_rollout`. Medium triggers are `backend_client`,
`sync`, `complex_state`, `optimistic_update`, `important_edges`, `runtime_qa`,
`feature_flag`. Unknown triggers are errors, not an implicit Small classification.

Each requirement has `id`, `behavior`, `oracle`. Each task has `id`, `title`,
`acceptance`, `paths`, `depends_on`, `requirements`, `gates`, and optional named
`resources`. Paths are literal repository-relative files/directories, not globs,
root paths, traversal or Git metadata. Dependency cycles and un-serialized overlap
are errors. A task owns changes only in its scope and returns a staged patch.

Task gates and integrated cases have `command` (an argv array), `risk` (`safe`,
`external`, `destructive`), `oracle`, `cleanup`. Integrated cases also have `id`
and `requirements`; non-Small cases require `check_case`. An integrated case may
declare its own named `resources`; otherwise it holds every task resource. In the rich check JSON,
that case's `delivery_command` must exactly equal the argv array. Rich safety maps
`external` to `needs-approval`; the other two labels stay identical.

“Safe” is a reviewed semantic classification, not a command-name allowlist. A
Python, shell, test or build command can still have external effects. Classify
the actual operation and environment. The subprocess runner does not grant
network or sandbox exemptions. External/destructive authority must list exact
targets (`task-id:gate-index` or integrated case ID); `*` means all matching-risk
planned gates and may be recorded only if the user actually authorized all.

## Progress and invalidation

The normal run states are DISCOVERY → READY_FOR_APPROVAL → APPROVED →
IMPLEMENTING → VERIFYING → READY_TO_PUBLISH → PR_OPEN → MERGED → COMPLETE.
Small request-scoped work can enter IMPLEMENTING without pretending there was
semantic approval. BLOCKED preserves the prior state and a concrete reason.

Task state is preparation → dispatch → returned report → gates/review → verified.
A stored handle always requires a host liveness query. Reports bind a dispatch ID,
not just an actor label. Prior implementation actors remain ineligible to review.
Dependency receipt changes invalidate consumers; preserve old trees and rebuild
from the new verified prerequisites. Do not reclaim an active worker's resources.

Mechanical receipts bind base SHA, final file paths/content/modes, command, exit
status, timeout and log hash. Staging/committing identical reviewed bytes may
preserve content evidence, but publication also locks the exact commit. A command
that mutates deliverable content does not pass its own gate. Review reports and
patch hashes bind the same content. Independent integrated review cannot expand
the plan's approved path scope.

Each actual gate has a durable job ID, runner/child process identities and a final
receipt. Nonblocking locks serialize the same gate and shared named resources
across runs using the same store; disjoint gates may run concurrently. Revisions
can advance during execution as each job checkpoints. A RUNNING record remains
an exclusion even if its operating-system lock disappeared in a crash.

Rich semantic fingerprints and actual-diff fingerprints are different. A rebase
can preserve the former but always invalidates the latter. Capture fresh exact
Git blobs, review the actual diff, build a new check package, rerun commands and
review. Do not edit an executing/finished check plan in place.

## Failure and recovery

Use two bounded review-fix rounds, then reassess the spec or ask for a concrete
decision/capability. A partial Git operation is not permission to reset. Durable
creation/publication receipts permit a safe retry only when identity still matches.
If an operation completed between Git and state persistence, inspect its exact
result before reconciling; never adopt an existing path merely because its name fits.

For a stranded gate, inspect `status` and the actual local processes, then use
`gate-recover --job … --evidence-file …`. Recovery verifies that its recorded
runner and direct children ended; it never kills them. A different host, reused
PID or unobservable identity fails closed. Keep lock files and metadata; deleting
them is not recovery. Resource names must be consistent and use one shared store
for work on the same device/service. Separate stores and unregistered background
daemons require explicit host coordination; the local gate runner is not a
distributed scheduler or a process-container boundary.

Material revision uses `revise`, invalidates authority and retains prior work. An
open PR needs explicit scope reconciliation; the controller refuses silently
replacing it. For a same-scope integrated fix, keep the owned branch, collect new
implementation provenance, rerun actual-diff checks and independent review before
updating the same PR. Do not claim old task receipts cover newly written code.

Unattended/background scheduling is not built into this plugin. Use the host's
own user-authorized scheduling mechanism when requested, and resume this run.
