# Validation

## Reproduce

Run `make check` and `make test` from a recursive clone. Tests use isolated local
fixtures; no production accounts, sessions, remote publications or devices.
CI pins official action commits, checks out exact submodule gitlinks and uses
read-only repository permissions. It does not grant PR-merge authority.

The standalone export is built from exact indexed package blobs. Smoke-test it
from outside the repository without submodule access using its bundle validator
and `skills/delivery/scripts/delivery.py doctor`.

## Initial repository review

The first independent design pass identified discovery/identity risks from
putting upstream skills inside the package. The runtime is therefore isolated
under `plugins/delivery-harness`; submodules are outside the discoverable skill
and validation roots.

The first code review reproduced two package leaks: an untracked file within a
runtime directory and a symlinked package root. Export now uses exact staged Git
blobs, rejects untracked/unstaged package inputs and source symlink ancestors,
and has regressions for both cases. Initial 16 tooling tests passed before those
additional probes; that success did not establish packaging safety.

The second code review passed the package corrections and found incomplete spec,
product and Orchestrate mappings. Those templates/references/shared contracts are
now included. The third code review returned PASS after independently running
all 20 tooling tests and targeted public-content scans. It also checked serialized
record writes, lock symlink refusal and mapped-file drift. Neither review prose
nor hashes authenticate human identity. The public source trees for spec/product/factory were
compared byte-for-byte to the previously integrated versions, with no differences.
Orchestrate is pinned to the previously integrated 0.4.1 commit.

## Observed local results, September 13, 2026

- Final tooling suite: 20/20 passed (25.426 seconds); also independently repeated.
- Fresh recursive clone of the committed feature branch: `make check` passed and
  20/20 tooling tests passed again (21.535 seconds).
- Complete runtime suite: 383/383 passed with zero errors, failures or skips.
  Three shards each imported the same full catalog; exact disjoint coverage was
  verified. Counts/durations: 128 in 316.266 seconds, 128 in 374.726 seconds,
  127 in 334.340 seconds. See [shard 0](evidence/plugin-shard-0.json),
  [shard 1](evidence/plugin-shard-1.json), [shard 2](evidence/plugin-shard-2.json).
  Focused and clean-clone reruns are not added to the unique test count of 403.
- Official plug-in and skill validators: passed. Dependency-free bundle validation:
  passed on source and standalone exports; exactly one discoverable skill.
- Package built from the clean clone without source dependencies: doctor and CLI
  help passed. Exported skills/scripts/tests match the tested runtime checksum
  `a2c643c502c70efafd9760d1c574713a6efc35b09abebf50e9f436ea1d07a7e1`.
- Final package file/hash manifest digest:
  `7824aac133f02713b623009f7333db81d32c13b0dbae5766c6a8d20f7667d208`.
- Local host: macOS, Python 3.14.7. Linux/Python 3.12 execution is delegated to
  the configured GitHub workflow and is not claimed by these local results.

The clean clone repeated tooling checks; the full runtime suite ran from the
feature worktree. Package smoke and exact runtime comparison are not described as
a second full runtime suite from the clean clone. Original local session reports
were excluded; only public-safe test IDs/counts are retained here.

## Remaining boundaries

Only mapped integration files invalidate a source review; independent reviewers
must check mapping completeness and semantic compatibility. Hashes do not prove
that a reviewer or a test actually ran. GitHub protected-branch settings are
maintainer-controlled and are not changed by repository scripts.

Actual interactive Claude execution is unavailable in the local environment.
Direct script/schema checks are not a fresh full native-host delivery run. No
real PR merge, deployment or production-side action is part of validation.

## 1.2.0 execution control, September 27 and 28, 2026

- Plug-in suite: 504/504 passed, run in six parallel shards with
  `tools/run_test_shard.py` (about nine minutes). New modules:
  `test_execution_control`, `test_local_mode`, `test_workflows`; gate-runner and
  integration-fix regressions extended.
- Each new rule was mutation-checked by its implementer: removing the rule made
  its named test fail.
- An independent read-only review ran in rounds, each reproducing its findings
  against the code; every defect it reported (stuck dispatches, forged or dropped
  verdicts, budget and decision bypasses, host-root substitution, re-rolled
  reviews) was fixed and re-verified before the review records were renewed.
- Live host runs: two local-only runs (the second on the final code) on throwaway
  repositories through the shipped
  `delivery-implement` and `delivery-review` workflows in Claude Code 2.1.283,
  journal imports from the real host journal, two-lens task review, integrated
  review, commit and `finish-local --fast-forward-main`; state COMPLETE.
- Autonomous end-to-end runs on September 28, 2026, on a fresh repository without a
  remote, one request each (add a helper with tests, export and README line,
  local-only, fast-forward main):
  - Claude Code headless with `--plugin-dir` and `/delivery-harness:delivery`: the
    coordinator chose the workflow path on its own (`task-register --via-workflow`,
    `task-import`, workflow reviews of the task and the integration with
    `review-import`, `finish-local --fast-forward-main`); COMPLETE, every actor a
    journal identity; the engine refused a commit message with an attribution
    trailer the host had suggested.
  - Codex (`codex exec`, workspace-write sandbox, run store under the workspace):
    native subagents registered with the manual commands, two independent reviews,
    `finish-local --fast-forward-main`; COMPLETE. Reviewer read-only mode was a
    convention there, recorded as such by the coordinator.
- Not verified: Cursor hosts with the new commands; live GitHub
  publication of a workflow-dispatched run; Linux process-identity paths of the
  gate runner changes.

## 1.3.0 shared execution loop (September 28, 2026)

- The delivery-specific workflows were replaced by the orchestrate 0.7.0 loop,
  shipped verbatim and checked against the pinned submodule by
  `tests/test_workflows.py`.
- `tests/test_workflows.py` runs that loop under node against the real engine
  through `workflow_steps.py`, with scripted agents and a synthetic host journal:
  prepare to VERIFIED with journal provenance, a red gate reworked under the engine
  budget, a review FAIL, a failed implementer abandoned and redispatched, the
  non-convergence block, a dependent prepared on the verified patch, an
  unauthorized side-effect gate blocking, and the integrated review. It found two
  defects before release (a dispatch step result that made the journal import
  ambiguous, fixed in orchestrate 0.7.0; a pathspec bug in the scope check).
- Full plug-in suite 505/505 in six shards; independent review FAIL then PASS.
- Not yet covered: a live Claude Code host run of 1.3.0.

## 1.3.1 shared-loop fixes (October 1, 2026)

- Four defects from real 1.3.0 runs, each with a regression test seen failing
  first: the step schema dropped fields the loop reads (every task BLOCKED "the
  preparation named no worktree or starting commit"; `tests/workflow_harness.js`
  now keeps only schema fields of a relay result, and `tests/test_workflows.py`
  cross-checks the schemas against the shipped loop), a late commit-message
  refusal that invalidated the run, a second commit failing on a path an earlier
  commit deleted, and integration review args with the pre-refresh base.
- Full plug-in suite 515/515 in six shards, tools suite 20/20, `make check`, all
  for these four fixes (at ab25cdc); independent review PASS with each fix
  reverted to confirm its test fails.
- Two later fixes with regression tests, added in 75f9991: gates run with
  `PYTHONDONTWRITEBYTECODE=1` so a passing Python gate no longer reports bytecode
  as a worktree change, and `commit` refuses with `GIT_IDENTITY` before any state
  change and records a failed commit as `integration_commit_failed`. They were not
  in the October 1 review; the October 2 review of the whole fixes diff
  (conformance and adversary PASS) covered them, confirmed the bytecode test fails
  without its fix and reran the targeted tests (41 OK). The identity and
  failed-commit tests pass but were not seen failing without their fixes, and the
  full plug-in suite has not run since these two fixes. Open minor note: the
  identity check also runs on an idempotent commit retry.
- Prompt-audit edits to `SKILL.md`, `PROVENANCE.md` and the spec and product
  internal guides: conformance review PASS (byte-identical to the approved diff),
  bundle validation passes.
- Review records renewed for all four sources at the unchanged pins (3e5886a,
  c431b34, 40b0c1f, ebe92fb): the orchestrate record now covers all six fixes
  above (four plus two later) and the prompt-audit edits; new factory
  `1.3.1-shared-loop-fixes.json` and spec and product `1.3.1-prompt-audit.json`
  records. `tools/upstreams.py status` reports
  all four reviewed and `make check` passes.
- Not yet covered: a full plug-in suite run with the two later fixes, and a live
  Claude Code host run of 1.3.1.

## 1.4.0 upstream sync and two fixes (October 2, 2026)

- Pins moved to orchestrate 0.7.1 (6679439), AI Factory 1.0.2 (ba64c62) and
  spec-driven-development 1.0.1 (e9c04b6); product-driven-development stays at
  ebe92fb. The shipped loop is `cmp`-identical to upstream
  `skill/workflows/orchestrate-execute.js` at 6679439. The spec-driven-development
  1.0.1 parser functions are byte-identical to the bundled ones.
- Submodules: regression tests in `tests/test_snapshots.py` and
  `tests/test_rich_integration.py` were seen failing first (an unchanged submodule
  refused with `UNSUPPORTED_TREE`; a moved gitlink refused only with
  `DIRECTORY_SCOPE`), then pass: unchanged submodules capture, a moved gitlink is
  refused with `submodule_changed` before any state change.
- Gate timeouts: `GateTimeoutTests` in `tests/test_run_engine.py` and the gate-step
  test in `tests/test_workflows.py` were seen failing first, then pass.
- Tools tests 20/20 and the plug-in catalog 529/529 in eight shards (0 skipped,
  0 failures); `make check` passes with all four sources reviewed. Package export
  smoke, bundle validation and `delivery.py doctor` in the exported package pass.
- Independent read-only review of b277b27..8f615d1: PASS for the upstream diffs,
  submodule handling (rename, type change, added gitlink, non-ASCII path), gate
  timeouts (bad values refused; explicit > planned > 300), records, lock and
  versions. Its one finding, a gitlink move hidden by `submodule.<name>.ignore=all`
  from the early check, is fixed with `--ignore-submodules=none` and two tests seen
  failing first. A separate error-path review found no defect.
- Not yet covered: an independent re-review of that follow-up fix and a live host
  run of 1.4.0.
