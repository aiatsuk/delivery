# Impact and verification

## Contents

- Consequence analysis
- Verification cases
- Actual-diff review
- Completion evidence

## Consequence analysis

Start from behavior, state, data, contracts, permissions, dependencies, compatibility, operations,
and performance. For each material hypothesis record:

- mechanism and violated invariant;
- evidence, plausibility, severity, detectability, and blast radius;
- reproduction prerequisites and controlled dependencies;
- prevention, mitigation, verification, owner, or explicit waiver.

Consider duplication, loss, reordering, stale state, partial success, retry ambiguity, races,
permission leakage, compatibility regression, rollout/rollback failure, observability gaps, and
performance degradation only where the system makes them plausible.

## Verification cases

For Critical, High, and correctness-critical Medium risks define:

- exact setup and fixtures;
- ordered actions with expected result and how to observe it;
- intermediate observations that localize a failure;
- final stable state and prohibited outcomes;
- authoritative oracle and exact side-effect count where relevant;
- the ticket, design, spec, or ADR that defines the expectation;
- evidence path, cleanup, repetition, timeout, flake policy, and safety class.

A case is written for someone who did not design the change. Read
[test-case-quality.md](test-case-quality.md) for the full field contract, the step and oracle
contracts, the documentation binding, and the reviewer rubric; the same bar applies to cases
written inside a planning package and to cases produced by the check engine.

Map every changed requirement scenario to a test, evaluation, proof, or approved waiver. Structural
validation supplements domain checks; it never replaces them.

## Actual-diff review

Inventory actual added, modified, deleted, renamed, generated, configuration, dependency, data, and
contract surfaces. Compare them with the planned change surface. Every unexpected surface needs a
disposition:

- expected indirect consequence;
- implementation defect to fix;
- material baseline deviation requiring revision and reapproval;
- verified non-material change with evidence;
- explicit waiver with owner and revisit trigger.

The delivery coordinator's Git inventory remains authoritative for repository identity, exact
commits, ancestry, reviewed integration, and publication. From the engine directory,
`scripts/spec_flow.py begin-verify --root <package> --before <snapshot> --after <snapshot>`
provides a deterministic directory-tree inventory. It includes regular-file content, executable
bits, symlink targets, and generated/cache files, while excluding Git administrative metadata.
Directory symlinks are inventoried without traversal. Retain the before/after snapshots: review
and completion reject changed or unavailable input trees.

Bind the planned repository-relative paths with repeatable `approve --planned <path>` arguments.
`begin-verify` cannot expand those paths. An unexpected path must receive an explicit disposition.
After a rebase, `reopen-verify --before <snapshot> --after <snapshot> --reason <cause>` can reopen
`POST_IMPLEMENTATION_REVIEW`, `VERIFYING`, or `DONE`. It preserves an unchanged semantic approval,
captures a fresh actual diff, returns to post-implementation review, and invalidates the prior
verification report. A material finding requiring baseline revision cannot be cleared this way.
Record new results before completion; reusing the prior report contents fails the gate.

## Completion evidence

`verification-report.md` records commands/scenarios, environment, revision, expected oracle, actual
result, evidence, residual risk, owner, and verdict. Completion requires:

- approved baseline still matches or an explicitly reapproved revision;
- all actual surfaces reviewed;
- no unresolved material finding or blocker;
- required checks pass;
- verdict is `PASS` or governed `PASS_WITH_WAIVERS`.

Do not convert skipped, unavailable, flaky, or stale checks into a passing claim.
