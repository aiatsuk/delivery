# Verification cases: <change title>

Write each case for someone who did not design the change: what to check, how to check it, and
what proves the result. See `references/test-case-quality.md`.

## Reference documents

| ID | Kind | Title | Location | Defines |
|---|---|---|---|---|
| REF-1 | <ticket/design/spec/adr/doc> | <document title> | <url, path, or key> | <which expectation it defines> |

## VER-001 — <Case name>

- Mapped requirements/risks: <IDs>
- Documents: <REF IDs>
- Invariant: <Protected invariant>
- Safety: <safe / needs-approval / destructive>

### Objective

<The question this case answers, and why it catches the mapped risk.>

### Preconditions and environment

- Preconditions: <System, account, flag, and data state before step 1>
- Environment: <Build, service versions, config, clock, and network posture>
- Fixtures: <Exact records and identifiers>
- Fault injection: <Injected failure, timing, or restart, and the tool used>

### Steps

| # | Action | Expected | How to observe |
|---|---|---|---|
| 1 | <Exact executable action with inputs> | <Observable state after this action> | <Exact query, log line, screen, metric, or file> |

### Expected outcome

- Intermediate observations: <Signals and timing that localize a failure>
- Final stable state: <Expected result as observable facts>
- Prohibited outcomes: <What must not happen>

### Oracle and evidence

- Authoritative oracle: <Kind, source, query, and exact expected value>
- Exact side-effect count: <N or not applicable>
- Evidence path: <Artifact/log/report and where it is stored>
- Cleanup: <Reversible cleanup>
- Repetition, timeout, and flake policy: <Policy>
