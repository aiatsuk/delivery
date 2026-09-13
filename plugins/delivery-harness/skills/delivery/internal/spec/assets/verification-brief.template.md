# Verification Brief: <change title>

This is the check engine's human-facing brief. `scripts/spec_flow.py check-render`
generates it from `check.json`; this template documents the contract. Edit `check.json` and
re-render instead of hand-editing the generated file.

- Mode: <lite/standard/deep> — <reason>
- Sources: <kind ref (authority)>
- Passes run: <n>

## Sources

- <kind ref (authority)> — <location>

## What changed

- <Changed path or contract> — <what changed there>

## Intent versus implementation

- Stated: <what the source said>
- Implemented: <what the diff does>
- Divergences: <list, or "none observed">

## What this could affect

- <Affected area> — <mechanism>

## Main risks

| Risk | Reality | Severity | Covered by | Check approach |
|---|---|---|---|---|
| RISK-001 <title> | likely | critical | CASE-001 | <fault injection, replay, restart, boundary> |

## Checks to run

| # | Case | Proves | Safety |
|---|---|---|---|
| 1 | CASE-001 <title> | RISK-001 | safe |

Full steps, oracles, and evidence live in `verification-cases.md`.

## What must be set up

- <Required capability or fixture>

## Missing capabilities

- <Capability> — blocks <CASE-*> — <concrete request>

## Open questions

- Q-1 <question> — <what it changes> — blocking: <yes/no>

## Waivers

- RISK-00X — <reason> — owner <owner> — revisit <trigger>

## Readiness

```text
Critical risks: <n>   covered: <n>
High risks:     <n>   covered: <n>
Medium risks:   <n>   covered: <n>
Blocking questions: <n>
Cases needing approval: <n>
Ready to execute: <YES/NO>
```

<When NO, one line per reason.>
