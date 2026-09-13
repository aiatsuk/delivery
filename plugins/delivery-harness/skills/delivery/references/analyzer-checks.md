# Analyzer diagnostics as evidence

Use this check when the project's analyzer permits info/warning diagnostics with
exit code zero. Capture a fresh-base log and a current integration log using the
same analyzer version, options and working-directory convention. Keep both outside
all checkouts in the run's evidence directory. Never run an auto-fixer as a check.
An analyzer process failure remains a failed gate even if no issue lines appear.

Plan both the analyzer command and the comparison as mechanical gates. Resolve
the bundled helper path from the skill directory, then use argv equivalent to:

```sh
python3 -S -B /resolved/skill/scripts/analyzer_delta.py --baseline /run/evidence/base.log --current /run/evidence/current.log
```

The comparison returns 1 for new diagnostics, 0 for none, and 2 for invalid input.
It preserves occurrence counts and ESLint stylish file headings, so a warning
moving between files cannot disappear merely because its text is identical.
The default recognizes severity-first Dart/Flutter output and ESLint stylish
`line:column warning|error` output. ANSI color is ignored; issue location changes
are conservatively new diagnostics. Other formats need an explicit
`--issue-regex` or the project's structured-output comparison. Check a real known
diagnostic against the chosen format; an empty match set does not establish that
an arbitrary analyzer format is supported.

The helper reads files only; it does not attest that the analyzer ran, bind logs
to a Git base, or replace process-exit gates. Reviewers verify that the logs came
from the current recorded commands/base. Keep their hashes with the evidence and
recapture the baseline after a base refresh. Do not reuse an old comparison receipt
after editing either input. Missing or stale evidence blocks approval.
