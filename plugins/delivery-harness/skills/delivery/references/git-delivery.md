# Fresh-main worktrees and GitHub delivery

The controller implements platform-change-flow mechanics without changing the
platform repository's instructions. It assumes a non-bare Git repository, a clean
primary checkout, `origin`, and `main`. A repository with another policy needs an
explicit adapter; do not quietly rename its base or rewrite history.

## Prepare and integrate

Inspect all worktrees and preserve existing changes. Fetch origin/main, reject
local main ahead/diverged, switch clean primary to main, then fast-forward against
that exact fetched reference. Ignored files that would be overwritten are blockers.
New task branches/worktrees use that verified base. Do not use the primary as an
implementation workspace. Task artifacts, dependency logs and build evidence stay
outside checkouts whenever possible.

Use literal owned file paths to stage and export reviewed binary patches. Never
`git add -A`, `git add .`, wildcard stage, or include dependency-install logs.
Integration checks every patch's exact hash and path set, preflights the combined
result, and applies it once. Conflict failures preserve the actual checkout.
Repository hooks and test commands remain subject to host permissions; the Git
helper is conservative plumbing, not a sandbox for malicious repository code.

## Commit and publish

`commit --message-file …` requires current release readiness. The commit contains
exact reviewed working bytes and is checked afterward; hooks that alter content
invalidate evidence. Descriptions are English and name the change, not an agent.
Never add attribution trailers identifying an AI tool.

`publish --title … --body-file …` requires current publish authority, tested
content/base, a clean committed feature branch and the exact inspected GitHub
origin. Only github.com is supported by the initial provider. Custom Git hosts
need a separate reviewed provider, not a guessed URL.

Publication writes a durable run-specific ownership intent/marker before remote
effects. A retry may reuse only the same owned PR and matching branch/repository;
an unrelated existing PR is a blocker. New pushes are feature-only. Updates use
an explicit expected remote head with `--force-with-lease`, never plain force.
PR descriptions include what changed, why, tests and actual limitations. Do not
publish internal source logs or raw native session material.

## Merge authorization and freshness

PR creation stops the workflow. After actual review and explicit authorization
for that exact PR, record the merge grant, then `refresh`. It fetches the newest
main and rebases the owned integration branch. Preserve conflicts, resolve them
within scope, rerun checks, collect new independent review, and update the PR.
No old green receipt survives a base change. For Medium/Large, use
`capture-verification` again and complete a fresh rich check generation.

`merge` confirms the PR's identity, current tested head, base main, non-draft/open
state, review status, and successful non-pending hosted checks. No hosted checks
is not the same as passing hosted checks: record their absence, retain local
evidence, and require explicit human merge authority. Unknown or failing states
stop. Never use admin bypass or automatic merge to skip review.

Recheck remote base adjacent to `gh pr merge --rebase --match-head-commit …`.
The exact head is server-locked. GitHub's CLI does not atomically lock the base
for this operation; repository branch protection is needed to eliminate that
race. Do not advertise absolute race freedom. If the base advances when detected,
repeat rebase and verification. Confirm MERGED and its merge commit afterward.

## Cleanup and outcome

Fetch and fast-forward primary main after confirmed merge, then verify it contains
the merge commit. Remove only current run-owned clean worktrees with matching
branch/head. Reviewed staged task patches may be committed to make their exact
owned tree clean before removal; no broad cleanup or discard is allowed.

Ignored artifacts, untracked files, unexpected branches, locks, in-progress Git
operations and even empty untracked directories block removal. Report their exact
paths and preserve them for the user; never force-delete. Local branches and
historical attempts are retained unless separately and safely authorized for
removal. “Merged” can be true while cleanup is incomplete—report both.

Write one idempotent outcome note only for a product/work item explicitly bound
at run creation. It links evidence, states what was and was not verified, and
records the next action. This is not a product-policy decision or permission to
rewrite existing notes. Mark COMPLETE only when required current-run cleanup and
the scoped outcome write are actually finished.
