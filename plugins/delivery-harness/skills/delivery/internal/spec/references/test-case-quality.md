# Test-case quality

A case is written for someone who did not design the change. It must state what to check, how to
check it, what proves the result, and which document defines the expectation. A reader who follows
it must reach the same verdict every time.

## Contents

- Required fields
- Step contract
- Oracle contract
- Documentation binding
- Reviewer rubric
- Safety classification

## Required fields

Every case in `check.json.cases` carries:

| Field | Meaning |
|---|---|
| `id`, `title` | stable `CASE-*` ID and a one-line intent |
| `risks` | the `RISK-*` IDs this case exists to catch |
| `requirements` | requirement or scenario IDs when a spec exists |
| `references` | `REF-*` IDs: ticket, design, spec, ADR, doc, dashboard |
| `objective` | the question this case answers |
| `why_it_catches_the_risk` | the causal link between the case and the risk mechanism |
| `preconditions` | system, account, feature-flag, and data state before step 1 |
| `environment` | build, service versions, config, seeds, clock, network posture |
| `fixtures` | exact data records and identifiers |
| `fault_injection` | injected failure, timing, or restart, with the tool used |
| `steps` | ordered actions; see the step contract |
| `intermediate_checks` | observations between steps that localize a failure |
| `expected_final_state` | the stable end state, stated as observable facts |
| `forbidden_outcomes` | what must never happen even if the case otherwise passes |
| `oracle` | authoritative source of truth; see the oracle contract |
| `side_effect_count` | exact expected count, or `not applicable` with a reason |
| `evidence` | artifacts to capture and where they are stored |
| `cleanup` | how to restore the environment |
| `repeat`, `timeout`, `flake_policy` | repetition count, limits, and what a flake means |
| `safety` | `safe`, `needs-approval`, or `destructive` |

Empty, `TBD`, or placeholder values are validation errors, not drafts.

Four fields may legitimately be empty and are not enforced: `fixtures` and `fault_injection` for a
case that injects nothing, `intermediate_checks` for a single-step case, and `requirements` when no
spec exists. Everything else must carry real content, including `side_effect_count` — write
`not applicable: <reason>` rather than leaving it blank. `repeat` is a whole number of runs of at
least 1; omitting it means one run.

## Step contract

Each step records three things:

```json
{"n": 3, "action": "Kill the payment worker between provider charge and local commit",
 "expected": "Charge exists at the provider, local payment row stays in PENDING",
 "how_to_observe": "provider sandbox ledger for merchant_ref MR-42; SELECT status FROM payments WHERE id=42"}
```

- `action` is executable by a reader: exact endpoint, screen, command, or signal, with inputs.
- `expected` describes observable state, not internal intent.
- `how_to_observe` names the exact query, log line, screen, metric, or file to look at.

Never write a step that only says "call the API" or "check that it works". Boundary values,
timings, and identifiers are part of the step, not context.

## Oracle contract

`oracle` has `kind`, `source`, `query`, and `expected`. `kind` must be one of:

```text
database | ledger | external-system | event-log | filesystem | ui-state
metric | api-read-back | proof | manual-inspection
```

A response status code is never an oracle by itself. The case must observe the actual effect: the
provider ledger, the persisted row, the emitted event count, the file on disk, the rendered state.
`side_effect_count` matters wherever duplication or loss is plausible; state the exact number.

## Documentation binding

Every case cites at least one `REF-*`, and each `REF-*` resolves in `check.json.references` with a
real location: ticket key, PR URL, design link, spec path, ADR path, code path with line, or
dashboard URL. Cite the source of the expectation, not just the source of the change.

When a document and the implementation disagree, that is a risk with `reality: likely`, not a case
comment. When no document defines the expected behavior, record an open question and mark the
expectation `assumed` in the case objective.

## Reviewer rubric

The pass-5 reviewer records a verdict per case in `case-review.md` and in `case.review`:

- the case actually catches the mapped risk mechanism;
- the described reproduction is possible in the available environment;
- the oracle is objective and observes the real side effect;
- steps are unambiguous and executable by someone new to the change;
- expected and forbidden outcomes are stated as observable facts;
- fixtures and identifiers are concrete;
- no duplicate of another case;
- no material risk left without a case or waiver;
- cleanup restores the environment and does not corrupt shared state.

Verdicts are `accepted`, `revise`, or `rejected`. A `revise` or `rejected` case does not count as
coverage. A Critical gap blocks `CHECK_BRIEF_READY`.

## Safety classification

- `safe`: sandboxed, reversible, no real money, no production data.
- `needs-approval`: touches shared staging, real accounts, or external sandboxes with cost.
- `destructive`: irreversible, production-affecting, or capable of a real payment.

Any case above `safe` forces `ready_to_execute: NO` until `check-authorize` records an explicit
human actor and scope covering every case in the current plan. Run `check-guard` immediately
before executing that plan. Authorization binds the check, authored sources, diff inventory, and
effective verification configuration; changed artifacts invalidate it. Missing capabilities and
open blocking questions prevent execution. This engine grants no destructive or production
authority of its own.
