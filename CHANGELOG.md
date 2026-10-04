# Changes

## 1.5.0

A new loop contract: an interrupted task resumes at the gate.

- Pin orchestrate 0.8.0 (e4d73a7); the shipped loop is the upstream 0.8.0 file
  (`SCRIPT_VERSION` 0.8.0), which accepts `resume_at: "gate"` from the
  authority's prepare step. Saved copies of the loop must be refreshed, since the
  loop refuses arguments of another version.
- A task the engine holds as REPORTED (its implementer's result was imported,
  then the host session or loop stopped, also after its gates ran) is no longer
  refused by a new loop: `prepare` returns `resume_at: "gate"` and the loop reruns
  the gates on the current content, then review, rework and finish as usual,
  without a new dispatch or implementer. `prepare` changes nothing in the run.
- `prepare` still refuses, with the way out in its message: a REPORTED task whose
  worktree changed since its report or whose content has an unanswered FAIL
  (`task-rework`), any task while the run is blocked or no longer in unintegrated
  implementation, a DISPATCHED task (wait, then `task-import` or `task-abandon`)
  and a VERIFIED task. The step schema lists `resume_at`.
- End-to-end cleanup tests: `cleanup` on a merged run whose repository has an
  uninitialized submodule removes the task worktree (receipt `removed: true`),
  and a stray empty directory still refuses with `cleanup_artifacts`.
- Spec-driven-development, product-driven-development and AI Factory pins are
  unchanged; new review records for all four sources.

## 1.4.1

Cleanup fix for repositories with submodules.

- `cleanup` and `remove_worktree` no longer treat the empty directory Git leaves
  for an uninitialized submodule (a gitlink in the worktree's HEAD or index) as
  an artifact, so such a worktree is removed after the usual identity,
  cleanliness and content checks. Removal still uses plain `git worktree remove`
  without `--force`. A stray empty directory still blocks with
  `cleanup_artifacts`, and an initialized submodule checkout is still preserved
  (`cleanup_artifacts` / `WORKTREE_ARTIFACTS`).

## 1.4.0

Upstream sync and two fixes.

- Medium/Large verification works in repositories with submodules. Snapshots
  record each gitlink (path and commit) without copying its content instead of
  refusing the whole tree with `UNSUPPORTED_TREE`. `capture-verification` refuses
  with `submodule_changed`, naming each path and commit, when the integration adds,
  moves or removes a gitlink, since the actual-diff review cannot inspect it;
  unchanged submodules pass, also when `.gitmodules` sets `ignore = all`. Other
  unusual file modes are still refused.
- Task gates and integrated cases may declare `timeout` (whole seconds, 1–3600) in
  the plan. `gate` uses it when `--timeout` is not given (default still 300 s), the
  loop's gate step passes it on, plan validation refuses other values with
  `invalid_timeout`, and the gate receipt records `timeout_seconds`. A long but
  legitimate gate such as a full test suite is no longer killed at 300 s.
- Pin orchestrate 0.7.1 (6679439), AI Factory 1.0.2 (ba64c62) and
  spec-driven-development 1.0.1 (e9c04b6); product-driven-development stays at
  ebe92fb. The shipped loop is the upstream 0.7.1 file (`SCRIPT_VERSION` 0.7.1).
  Factory 1.0.2 is wording in a skill this bundle does not vendor, and the
  spec-driven-development 1.0.1 fallback YAML parser is already identical here.
  New review records for all four sources.

## 1.3.1

Fixes for the shared execution loop found in real runs.

- The step schema in `workflow_steps.py args` lists every field the loop reads off
  a recorded step (prepare, dispatch, collect, review-open, review-close) and the
  gate result rows. A relay returning structured output dropped them, so every task
  ended BLOCKED "the preparation named no worktree or starting commit". A test
  checks the schemas against the shipped loop, and the test harness now keeps only
  schema fields, as a relay does.
- `commit` refuses a message that names an agent before any state change, with
  `HISTORY_POLICY`; it no longer invalidates the run and forces re-verification.
- A second commit on the integration branch no longer fails when an earlier commit
  deleted a file: only paths whose staged state differs from HEAD reach
  `git commit --only`.
- `workflow_steps.py args --integration` reports the integration's base after
  `refresh`, so integration reviewers diff against the new base.
- Gates run with `PYTHONDONTWRITEBYTECODE=1`: a passing Python gate in a repository
  that does not ignore `__pycache__/` no longer fails as changing the worktree.
- `commit` refuses with `GIT_IDENTITY` before any state change when git cannot
  resolve an author and committer, instead of failing inside the commit and
  sending the run back to verification. A failed commit is recorded in the run
  history as `integration_commit_failed`, not `integration_committed`.
- Prompt audit: `SKILL.md` asks for a short progress update when a run changes
  state and drops a repository-specific line, the "no hidden tier pins" phrase and
  a maintainer-only compatibility claim; the spec-engine and product-memory
  vendoring notes moved into `PROVENANCE.md`.
- Review records renewed at the unchanged pins (orchestrate 3e5886a, factory
  c431b34, spec-driven-development 40b0c1f, product-driven-development ebe92fb):
  `docs/integrations/orchestrate/0.7.0-shared-loop-fixes.json` now covers all six
  fixes and the prompt-audit edits, with new
  `docs/integrations/factory/1.3.1-shared-loop-fixes.json`,
  `docs/integrations/spec-driven-development/1.3.1-prompt-audit.json` and
  `docs/integrations/product-driven-development/1.3.1-prompt-audit.json`.

## 1.3.0

One execution loop for Delivery and orchestrate.

- Replace the `delivery-implement` and `delivery-review` workflows with the pinned
  orchestrate execution loop (`orchestrate-execute`, 0.7.0), shipped verbatim and
  run under this engine's authority: implement, gate, review and rework are code,
  and every step is recorded by the ordinary engine commands.
- Add `scripts/workflow_steps.py`: the loop's arguments (`args`, with
  `--integration` for the integrated review) and its eight authority steps. Engine
  refusals that another round cannot repair end the task BLOCKED with the reason.
- `task-abandon` recognizes the loop's implementer labels (`impl:T:L<n>`,
  `rework:T:L<n>r<n>`).
- Pin orchestrate 0.7.0 (3e5886a) with a new review record; renew the factory record
  for the changed shared files.

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
