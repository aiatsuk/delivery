# Case review: PAY-142 payment retry

Independent pass-5 audit of the drafted cases.

## Verdicts

| Case | Catches its risk | Reproducible | Objective oracle | Observes real effect | Unambiguous steps | Verdict |
|---|---|---|---|---|---|---|
| CASE-001 | yes | yes | yes | yes, provider ledger | yes | accepted |
| CASE-002 | yes | yes | yes | yes, status vs ledger | yes | accepted |
| CASE-003 | yes | only with the replay hook | yes | yes, ledger row count | yes | accepted |
| CASE-004 | yes | yes, state is seeded | yes | yes, cycle log plus status | yes | accepted |

## Findings

| ID | Case | Severity | Finding | Required change |
|---|---|---|---|---|
| REV-001 | CASE-001 | high | The first draft asserted only "no second charge" by response code | Oracle changed to the provider ledger entry count and distinct idempotency_key count |
| REV-002 | CASE-003 | medium | Replay depended on resetting the whole consumer offset, which would replay unrelated events | Narrowed to a single delivery id and recorded the missing hook as a capability request |
| REV-003 | CASE-002 | low | "Wait for reconciliation" was unbounded | Bounded to one full cycle with an explicit log oracle |

## Duplicates and overlaps

- CASE-002 and CASE-004 both end in a converged status. They are kept separate because CASE-002
  exercises the retry exhaustion path and CASE-004 isolates the reconciliation query itself.

## Uncovered risks

| Risk | Why uncovered | Resolution |
|---|---|---|
| RISK-005 | Amplification is bounded by provider 429s that this loop does not retry | Waived with owner payments-lead and a revisit trigger |

## Reviewer verdict

- Critical gaps: 0
- Cases requiring revision: 0 after REV-001 to REV-003 were applied
- Verdict: READY
