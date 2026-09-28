# Changes

## 1.2.0

Execution control from an audit of nine real runs: the engine now enforces what
coordinators could previously skip or type in, and the run model covers the flows
that were being handled outside it.

- Fix false gate failures on macOS: a gate command that exited before the runner
  identified it failed as exit 127 "Operation not permitted". The receipt is now
  written with its job outcome, and a passing gate takes four run revisions.
- Refuse a task worktree changed before its dispatch or between a failing report
  and its rework, placeholder source events, and results observed before their
  dispatch.
- Add workflow-journal provenance: `task-register --via-workflow`, `task-import`,
  `review-token` and `review-import` bind agent identity, reports and per-lens
  verdicts to the host-written journal. Ship `delivery-implement` and
  `delivery-review` workflow scripts, declared in the Claude manifest.
- Add finding decisions (code fix, test plan, environment, requirements, human)
  for task and integrated reviews, with separate budgets, non-convergence
  blocking, `task-abandon` for dispatches without an importable result, and
  `authorize --scope rework-budget` and `--scope decision` as the only ways to
  resume budget and decision blocks;
  a minimum PASS evidence rule and a status-only oracle lint.
- Accept workflow journals only under the host projects root; record review
  requests so every requested lens must be imported; count every returned
  verdict, harvesting late ones at every command so they can neither deadlock
  a run nor go uncounted; refuse a PASS on content that already failed without the user's
  recorded override; refuse typed reviews of workflow-dispatched tasks; bind an
  imported report to the staged tree.
- Add bounded standing approval (`authorize --scope standing-approval`,
  `approve --standing`), recorded as such.
- Add local-only runs (`new --local-only`, `finish-local`, `local-merge`
  authority) and explicit integration-fix budget extensions
  (`authorize --scope fix-budget --count N`).
- Add Cursor plugin and marketplace manifests so the existing harness can be
  installed from this repository in Cursor.

## 1.1.0

- Put the self-contained plug-in under version control with four pinned public
  upstream submodules, separate from the runtime package.
- Track explicit upstream review dispositions and integration content hashes.
- Add independent-update checks, isolated regression fixtures, package export and
  read-only CI. Preserve the existing integrated runtime and approval boundaries.
