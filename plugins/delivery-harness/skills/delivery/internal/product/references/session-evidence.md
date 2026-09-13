# Native session evidence

Read this reference only when curated product knowledge requires exact native-session evidence.
The helper is a local read-only evidence query, not a transcript importer.

## Decision table

| Product state or request | Behavior |
|---|---|
| Confirmed/accepted answer; no stale/conflict marker; no exact-source request | Answer from curated Markdown; do not invoke the helper |
| Exact session, wording, key, command, link, or provenance requested | Native fallback required |
| Required detail absent or relevant record candidate/unverified/stale/partial | Native fallback required |
| Curated sources conflict | Native fallback required; show both sources and do not overwrite |
| Current external status requested | Refresh the owning live system regardless of transcript evidence |

## Scope authorization

Both `inspect` and `search` require:

1. an explicitly selected product root containing `product.md`;
2. exact `host + canonical UUID session_id`; and
3. either a valid linked binding at `sessions/<host>-<session-id>.md` or `--allow-unbound` for a
   one-time check explicitly requested by the user.

The binding grammar is defined in [artifacts.md](artifacts.md). A revoked, malformed, duplicate-key,
or conflicting binding fails closed and cannot be bypassed with `--allow-unbound`. The helper never
chooses a latest/prefix/similar session, scans another host, or searches another product.

## Commands

Run the bundled script with Python 3:

```text
python3 <skill-root>/internal/product/session_evidence.py inspect \
  --host codex --session-id <uuid> --product-root <product-root>

python3 <skill-root>/internal/product/session_evidence.py search \
  --host codex --session-id <uuid> --product-root <product-root> \
  --query 'literal text'
```

Add `--allow-unbound` only for the explicit one-time case. Search defaults to user/assistant visible
messages. Add `--scope messages-and-tools` only when evidence likely lives in a visible tool result.
Tool arguments remain excluded.

stdout is one JSON object. The query, absolute paths, raw records, reasoning, and unsanitized parser
errors are never printed.

## Result contract

Read these fields independently:

- `outcome`: `inspected`, `supported`, `not_found_in_source`, `unverified`,
  `source_unavailable`, `ambiguous_source`, or `error`;
- `source_state`: `not_read`, `stable`, `appended`, `truncated`, `replaced`, `modified`, or
  `unavailable`;
- `coverage.state`: `not_applicable`, `complete`, `partial_parse`, `limit_truncated`, or
  `unavailable`;
- `matches[]`: bounded sanitized fragments with role and exact line/block coordinates;
- `revision` and `observed_at`: the starting file observation, not a permanent content hash.

A match can be `supported` while the source appended or overall coverage was partial. A no-match is
`not_found_in_source` only with stable and complete allowlisted coverage; otherwise it is
`unverified`. `sensitive_match` confirms only that safe provenance exists and returns no value.

Exit codes:

- `0`: conclusive inspect, supported match, sensitive match, or complete not-found;
- `2`: invalid invocation or unsupported host;
- `3`: usable but incomplete/changed/limited evidence;
- `4`: unavailable, unreadable, or permission denied;
- `5`: binding, identity, unsafe-format, or ambiguity failure;
- `70`: sanitized unexpected failure.

## Fixed limits

Search is case-insensitive literal matching, never user regex. One invocation is limited to a
512-character query, one message file, 512 MiB, 500,000 complete records, 30 seconds, 20 matches,
2,000 output characters per fragment, 16 MiB per JSONL record, and 8 MiB per allowlisted text field.

Resolution is limited to fixed provider roots, 100,000 directory entries, and 15 seconds. Identity
and index/event helpers have smaller aggregate and per-file byte/record/time limits. Any incomplete
identity coverage fails before message content access. Hitting a content-search limit returns exact
complete-through line/byte coverage and never turns a no-match into absence.

## Provider boundaries

- Codex: active and archived exact rollout file; first root `session_meta` owns identity. Search
  user/assistant response text and, only in tool scope, visible tool result output. Later child
  metadata does not replace root identity.
- Claude: bounded index hints plus exact primary UUID file, excluding `subagents/**`. Search
  non-meta/non-sidechain user/assistant text and optional visible tool results. Thinking, signatures,
  tool inputs, attachments, and images are excluded.
- Grok: exact session directory; complete bounded event identity must match before reading
  `chat_history.jsonl`. Search non-synthetic user/assistant text and optional visible tool results.
  System, reasoning/encrypted content, tool calls, backend events, and images are excluded.

Unknown record/block types are never converted with a generic string fallback. They are counted and
may make coverage partial.

## Privacy model

This is a single-user local accidental-disclosure control, not perfect data-loss prevention.
Native files are opened read-only and no snapshot is written. Only allowlisted visible fields are
searched. Sensitive spans are detected over the complete bounded field before excerpt slicing;
common authorization/cookie values, credential URLs, secret assignments, private-key blocks, token
prefixes, and JWT-shaped values are redacted on a best-effort basis.

If the query itself looks sensitive, stop before native traversal. If a match overlaps a sensitive
span, return only `[REDACTED:SENSITIVE_MATCH]` and safe provenance. Never copy a raw fragment into
Product Workspace; review and paraphrase the durable conclusion.

## Answer and capture

When native evidence was used, report:

```text
Result: <reviewed conclusion>
Evidence: <curated artifact if any>; <host>:<session-id>, line/block range, observed_at
Outcome: <outcome>
Source state: <source_state>
Coverage state: <coverage.state and complete-through coordinate>
Limitations: <none or exact limitation>
Capture: <artifact path, proposed, or not written>
```

Say that the local original session was checked only after successful native access. Durable
capture stores owner, status, reviewed paraphrase, provenance, observation time, and coverage. Only
explicit user or authoritative-owner confirmation changes a candidate/proposed result to
confirmed/accepted.
