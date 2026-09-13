# Bundled specification and check engine

This is an internal deterministic module of the delivery plugin. The delivery skill owns
classification, user interaction, run identity, agents, Git, integration, review, and publication.
This directory has no discoverable skill entrypoint and does not route slash commands or start
another run. It needs Python 3.10 or newer; the baseline runtime has no third-party dependencies.

## Interface

From any working directory, invoke the absolute bundled script path:

```text
python3 <plugin>/skills/delivery/internal/spec/scripts/spec_flow.py <command> --root <run>/spec
```

The Python module also exposes `build_parser()`, `main(argv)`, and its `cmd_*` functions. Prefer the
CLI boundary when wrapping it. Commands return one JSON object on stdout. A successful gate exits
0; a refused gate exits 1; an I/O or decoding failure exits 2. Mutating lifecycle commands support
`--expected-revision`; supply the last returned revision and reload status after a stale-revision
error. A local exclusive lock protects revision-checked session writes.

| Purpose | Commands |
| --- | --- |
| Runtime and compatibility lookup | `env`, `where`, `list` |
| Semantic planning | `init`, `status`, `validate`, `fingerprint`, `advance`, `block`, `resolve` |
| Baseline authority | `approve`, `revise`, `apply` |
| Implementation verification | `begin-verify`, `reopen-verify`, `disposition`, `complete` |
| Rich case engine | `check-init`, `check-diff`, `check-mode`, `check-pass`, `check-validate`, `check-render`, `check-authorize`, `check-guard` |

Use `--help` on a command for its exact arguments. `init` and `check-init` accept `--project`; an
explicit `--root` always controls the package destination. Direct `where`/`list` lookups use the Git
common directory to find the same repository across linked worktrees. They never inspect remote
URLs or execute repository hooks. Non-repository paths keep directory identity.

## Semantic gates

`approve --actor <actual human> --note <source of approval> --planned <path>` records the baseline
and expected repository-relative surface. Repeat `--planned` for the union of approved task scopes.
The coordinator must cite actual user input; these fields are an audit record, not authentication.
The engine must never invent the actor or treat a task request as baseline approval.

Approval does not apply the plan. `apply` is a separate action and refuses absent, stale, or drifted
approval. The fingerprint covers all required planning artifacts, every capability spec, and both
test/evaluation plans if both exist. Only task-progress checkboxes and insignificant line endings
or trailing spaces are normalized. Other checkboxes remain semantic.

`status` returns `baseline_status` (`none`, `matching`, `drifted`, or `invalid`), the approval with its
fingerprint, the expected/actual change surfaces, revision, and verification generation. The
approved fingerprint must match at apply, actual-diff capture, verification start, and completion.

`begin-verify --before <snapshot> --after <snapshot>` starts actual-diff review. The snapshots must
remain available and the coordinator must keep the package outside them. Unexpected paths become
findings. `advance --to VERIFYING` refuses unresolved findings, required baseline revisions, or
changed snapshots. `complete --verdict PASS --report verification-report.md` requires the current
approval, unchanged inventory, and a filled report. A report is an evidence record; the engine does
not execute its claimed checks. The delivery coordinator binds real gate receipts and independent
reviews to the authoritative Git revision before accepting the result.

`reopen-verify --before <new-before> --after <new-after> --reason <cause>` reopens an integration
review after a rebase from `POST_IMPLEMENTATION_REVIEW`, `VERIFYING`, or `DONE`. An unchanged semantic
approval survives. The prior diff findings/verdict are retained in session history, a fresh diff
must be reviewed, and old report contents cannot finish the new verification generation. The
coordinator still invalidates and reruns every Git-bound gate and review receipt.

## Check execution

The check engine retains source reconstruction, documented risk mechanisms, critical-area depth,
recorded risk passes, coverage thresholds, case quality, authoritative effect oracles, side-effect
counts, environment capabilities, review verdicts, and detailed Markdown rendering. Critical-area
hits require `deep` even if project configuration or an explicit mode requested a lower depth.

`check-authorize --actor <actual human> --scope safe|approved|all` records execution authority for
the exact check plan. Non-safe scopes additionally require a nonblank `--acknowledge` describing
accepted effects. Every case must fit the scope. Open blocking questions, missing capabilities,
unfinished risk iteration, and validation failures prevent authorization.

Call `check-guard` immediately before executing the cases. Authorization fingerprints bind
`check.json`, authored source/impact/review files, any actual-diff inventory, and the effective
verification configuration. Changing any of them invalidates execution authority and completion.
An executing or finished check is immutable through `check-diff`; use a new check package for a
changed case plan. This module runs no tests, payments, deployments, or external mutations itself.

The fallback YAML parser accepts the shipped block mappings, scalar lists, scalars, quoted strings,
and comments. Unsupported flow collections, tags, aliases, multiline values, malformed quotes,
duplicate keys, invalid indentation, and control characters fail as configuration errors. JSON
configuration supports richer structures without an optional dependency. PyYAML, when present,
can parse broader YAML syntax; it is never required to load the shipped configuration.

## Reproducible medium-flow lifecycle

The regression `WorktreeIdentityRegressionTests.test_full_cli_medium_flow_and_rebase_reverification`
in `tests/spec/test_delivery_spec_regressions.py` is a complete executable recipe. It creates a local
Git primary checkout and linked worktree, uses subprocess CLI calls for every state transition,
records only explicit test actors, reaches `DONE`, advances the base and rebases, then proves that
the old report is refused before a new report can reach `DONE`. It uses no mocked lifecycle state
and makes no network or publication calls.

For integration, the exact command sequence is:

```text
init --root RUN/spec --project REPO --session-id RUN_ID
<fill every planning artifact and capability spec>
validate --root RUN/spec
advance --root RUN/spec --to INTENT_CAPTURED
advance --root RUN/spec --to CONTEXT_DISCOVERY
advance --root RUN/spec --to DECOMPOSING
advance --root RUN/spec --to SPECIFYING
advance --root RUN/spec --to ANALYZING_IMPACT
advance --root RUN/spec --to DESIGNING_VERIFICATION
advance --root RUN/spec --to PLANNING
advance --root RUN/spec --to REVIEWING
advance --root RUN/spec --to WAITING_APPROVAL
approve --root RUN/spec --actor ACTUAL_HUMAN --note APPROVAL_SOURCE --planned src
apply --root RUN/spec
<implement and capture authoritative before/after snapshots>
begin-verify --root RUN/spec --before BEFORE --after AFTER
<disposition every unexpected actual-diff finding>
advance --root RUN/spec --to VERIFYING
<run real checks and fill verification-report.md>
complete --root RUN/spec --verdict PASS
```

If `init --intent-file` was used, it already entered `INTENT_CAPTURED`; start subsequent advances
at `CONTEXT_DISCOVERY`. A later rebase uses `reopen-verify`, dispositions, `advance --to VERIFYING`,
fresh verification evidence, and `complete`, without reapproving unchanged semantics.

## Bundled material and provenance

Vendored from the locally installed `spec-driven-development` plugin, version `1.0.0`, under its
`skills/spec-driven-development/` directory:

- `scripts/spec_flow.py` (source SHA-256 `59d34fc7700063099e577776120c9f5cf97eaaca6da517d7b87f6ffc7db5a05e`);
- every file in `assets/`;
- `references/artifact-contract.md`, `references/test-case-quality.md`, and `references/verification.md`;
- `tests/test_spec_flow.py` (source SHA-256 `8314a91d7c6e87a16a2d04822f179e0d6a03c0110b4849942f29a7921e00bff0`)
  and the `tests/fixtures/payment-retry/` package, now under this plugin's `tests/spec/`.

Adaptations include relative bundle paths, canonical repository identity, strict fallback parsing,
critical-area escalation, approval/scope/diff drift checks, execution authorization binding, rebase
reverification, and focused adversarial/CLI regressions. The upstream skill entrypoint, host routing,
workflow orchestration, evaluator scenarios, and packaging validator are intentionally not bundled.
No license or notice file was present in the installed source bundle inspected for this copy.

Run the engine suite from a temporary working directory with:

```text
PYTHONDONTWRITEBYTECODE=1 TMPDIR=/private/tmp python3 -m unittest discover -s <plugin>/tests/spec
```
