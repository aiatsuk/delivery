# Delivery Harness

One self-contained plugin for product context, precise specifications, isolated
implementation, independent verification and a fresh-main worktree-to-PR flow.
It reconciles five workflows instead of invoking five competing coordinators.

Version 1.1.0 is maintained in [the Delivery repository](https://github.com/aiatsuk/delivery)
under `plugins/delivery-harness`. The repository pins public upstream tools as Git
submodules and provides `tools/upstreams.py` for explicit, content-bound update
review. Those development inputs/tools are intentionally not runtime dependencies.
See its `docs/SYNCHRONIZATION.md`; updating a pin alone never updates this package.

## Use

Invoke the `delivery` skill from this plugin, then describe the task normally:

- Codex plugin skill: `$delivery-harness:delivery Fix … and open a PR.`
- Claude Code plugin skill: `/delivery-harness:delivery Fix … and open a PR.`
- Cursor plugin skill: `/delivery Fix … and open a PR.`
- Resume: `Use Delivery Harness to resume <run root>.`
- Merge: `PR #… has been reviewed; authorize its merge through Delivery Harness.`

Use the exact skill name shown by your host if its namespace presentation differs.
Material choices/specs stop for approval. PR creation stops for review and exact-PR
merge authorization. Deployment is never implied. A status/audit request stays
read-only. Existing repository instructions remain authoritative.

The plugin explicitly coordinates real host workers and independent reviewers.
Its CLI stores durable evidence and guards state; it is not a substitute for a
host's agents, sandbox, credentials, user approvals or background scheduler.

## What is combined

| Original workflow | Responsibility in this plugin |
| --- | --- |
| Product-driven development | Explicit Markdown product/work-item binding and idempotent outcome memory |
| Spec-driven development | Preserved intent, decisions, approved semantic baseline and actual-diff/check engine |
| AI Factory | Eight-axis classification, hard risk escalation, proportional gates and bounded rework |
| Orchestrate | Isolated dependency-aware tasks, actual host dispatch records, independent review and patch integration |
| Platform change flow | Clean primary main, new worktrees, tested commits, leased PR updates, reviewed rebase merge and safe cleanup |

No installed copy of those plugins is needed at runtime. Existing plugins are
left untouched; do not invoke them as additional top-level coordinators inside a
Delivery Harness run. Their useful mechanisms are shipped as internal modules and
one precedence contract. Configured models are inherited, never secretly pinned.

The Orchestrate integration now reflects upstream 0.4.1; see the selective-update
mapping in [provenance](PROVENANCE.md). Upstream plugins can be updated independently,
but this vendored bundle adopts changes only through explicit review, testing and
reinstallation. It does not automatically load newer upstream code.

## Requirements and boundaries

Python 3.10+, Git and macOS/Linux for local coordination; no Python packages are
required. GitHub delivery additionally needs `gh`, authenticated access to the
repository, `origin` pointing to github.com, and `main`. The initial Git provider
does not support other forges, bare primaries, submodule implementation changes
or multi-repository atomic delivery. Host tools supply native build/test devices
and actual agents. Missing capabilities are reported, not simulated.

The primary checkout is never an implementation directory. Runs live outside all
checkouts under `DELIVERY_HOME` (default `~/.local/state/delivery-harness`), with an
explicit `--store` override. Product memory uses explicit Markdown bindings and
the `PRODUCT_MEMORY_HOME`/existing product-home convention. Native session access
is optional, bounded and explicitly selected. The plugin performs no automatic
native-history scan, production deployment or background monitoring.

## Local installation

The bundle has `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json` and
`.cursor-plugin/plugin.json`. Codex's personal marketplace can register the
durable source at `~/plugins/delivery-harness`; install the
`delivery-harness@personal` entry using the normal host plugin installer. Start
a fresh thread after installation.

For Claude Code, a host supporting local plugin directories can load this same
bundle with `claude --plugin-dir /absolute/path/to/delivery-harness`, or add it
through that host's supported local marketplace installation flow. Do not copy
individual engine scripts into separate skills. Manifest validation alone does
not prove the Claude runtime was exercised; see the verification report shipped
with the installed bundle for what was actually run.

For Cursor, copy this directory to `~/.cursor/plugins/local/delivery-harness` and
reload the window, or import the Delivery repository from Customize (it includes
`.cursor-plugin/marketplace.json`) and install `delivery-harness`. A Cloud Agent
cannot write the plugin into a desktop Cursor home directory. Manifest
validation alone does not prove the Cursor runtime was exercised.

A shared global-skill installation can instead link `~/.claude/skills/delivery`
to this bundle's `skills/delivery` directory. That form is invoked as `/delivery`,
without a plugin namespace, and uses the same source rather than a second copy.

## Inspect and test

From the plugin root:

```sh
python3 -B skills/delivery/scripts/delivery.py doctor
python3 -B skills/delivery/scripts/delivery.py --help
python3 -B scripts/validate_bundle.py
python3 -B -m unittest discover -s tests -t .
```

Tests use temporary repositories and local bare remotes. Provider tests explicitly
fake GitHub; they do not claim a live PR or merge. Fixture actors are synthetic;
forward-testing reports separately identify actual host agents. No test needs a
production account, real payment, public service or external deployment.

Read [the skill](skills/delivery/SKILL.md),
[run contract](skills/delivery/references/run-contract.md),
[Git lifecycle](skills/delivery/references/git-delivery.md), and
[provenance](PROVENANCE.md), plus [verification and limitations](VERIFICATION.md).
Use the CLI's returned run root and current revision;
never manually edit run/session state to bypass a guard.

CLI results are command-specific. In particular, `gate` returns a receipt, not a
run object. Reload `status --run <run root>` after a mutation before using the
next `--expected-revision`; do not infer a revision from a gate receipt. Use each
subcommand's `--help` for its arguments. This keeps resume and parallel gate
bookkeeping aligned with the actual durable state.

## Compatibility changes

- Product work items stay Markdown; no parallel project.json/ticket.json universe.
- Git common-directory identity survives worktree changes.
- Optional dependency absence cannot silently accept malformed YAML.
- One coordinator owns approvals and delegates work; internal engines never launch
  other coordinators or hard-pin a conflicting model policy.
- Gate receipts bind actual content/base/logs; reports bind actual dispatches.
- Dependency patches do not pull ignored installation logs into commits.
- Rebase preserves only unchanged semantics, not old actual-diff/test evidence.
- Cleanup checks owned paths and artifacts, not whole-tree equality across tasks.

GitHub head locking is exact; an atomic server-side base lock requires repository
protection. A local tool cannot cryptographically authenticate a human approval,
infer a host process is live from saved state, or decide product policy itself.
