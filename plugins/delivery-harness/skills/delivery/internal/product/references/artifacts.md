# Product memory artifacts

Keep artifacts short, human-readable, and useful without a database or a previous chat.

## Product

`product.md` should contain:

```markdown
# <Product name>

## Purpose
## Product map
## Repositories and services
## Authoritative links
## Active work
```

`memory.md` is a compact list of durable facts that are relevant to many tasks. Do not turn it into
an activity log. Link to detailed notes.

## Project or ticket

`project.md` or `ticket.md` should contain identity, desired outcome, scope, related work items,
repositories, and authoritative links. Put volatile progress in `status.md`:

```markdown
# Status

- Updated: <ISO date>
- State: <local state; external status must name its source and observed time>
- Blockers: <none or list>
- Next action: <one concrete action>

## Completed
## In progress
## Open questions
```

## Milestone note

Name notes `YYYY-MM-DD-HHMM-<short-slug>.md` and use:

```markdown
# <Outcome-oriented title>

- Date: <ISO timestamp with timezone>
- Product: <product id>
- Owner: <product | project ID | ticket ID>
- Kind: <result | discovery | discussion | verification | blocker | handoff>

## Context
## Result
## Decisions
- Accepted: <explicitly accepted decisions only>
- Proposed: <unaccepted candidates>

## Evidence and links
## Verification
## Open questions
## Next action
```

Omit empty sections. Paraphrase discussion; do not copy raw transcripts or tool output.

## Decision

Use `decisions/<slug>.md` only when a stable decision record adds value:

```markdown
# <Decision title>

- Status: proposed | accepted | rejected | superseded
- Updated: <ISO timestamp>
- Owner: <product | project ID | ticket ID>

## Context
## Decision
## Consequences
## Evidence
## Supersedes / superseded by
```

Only explicit user or authoritative-owner acceptance changes `proposed` to `accepted`.

## Session binding and summary

Use `sessions/<host>-<session-id>.md` only after the session is explicitly linked to the product.
The file is both the exact native-evidence binding and an optional curated navigation aid. Its
metadata block is machine-readable and must use this exact shape before the first `##` heading:

```markdown
# Session binding

- Schema: product-session-binding/v1
- Product: <exact product directory name>
- Host: codex | claude | grok
- Session ID: <canonical UUID>
- Binding: linked | revoked
- Related work: <comma-separated project/ticket IDs or none>
- Last verified: <ISO-8601 timestamp or never>
- Source state: <stable | appended | truncated | replaced | modified | unavailable | unknown>
- Coverage state: <complete | partial_parse | limit_truncated | unavailable | unknown>

## Summary
## Important results
## Decisions
## Artifacts and links
## Open questions
## Next action
```

Every metadata key occurs exactly once. Keep Product, Host, and Session ID equal to the owning
directory and filename. `linked` permits future evidence lookup; `revoked` denies it. A one-time
explicit check is not a durable binding and does not create this file automatically.

The summary is curated and may omit empty sections. Record paraphrases, evidence coordinates,
observation time, and source/coverage state. Do not call it a complete transcript and never copy raw
fragments, secrets, reasoning, or tool payloads into it.
