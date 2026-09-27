# Provenance and integration decisions

This bundle integrates independently maintained workflows with its own coordination
code. Its source repository is https://github.com/aiatsuk/delivery. Four public Git
submodules pin inspected inputs; `upstreams.lock.json` binds explicit review records
to integrated files. Runtime installations need neither submodules nor cache paths.
See NOTICE.md for ownership and licensing boundaries; no blanket license is asserted.

| Source | Version inspected | What is retained |
| --- | --- | --- |
| spec-driven-development | 1.0.0 | Specification/check engine, templates, technical references and regressions; exact source hashes in its engine guide |
| product-driven-development | 0.5.0 | Markdown model and bounded session evidence helper/tests; provenance in internal/product |
| ai-factory | 1.0.1 | Risk taxonomy, proportional quality gates, explicit side effects, bounded rework and honest release evidence, re-expressed in the unified contract |
| orchestrate | 0.5.0 (225818ce57741831f9c224f1d03d247b9d6fce7e) | Dependency-aware isolated work, reviewed patch integration, async review and analyzer-delta contracts; independently implemented Git/runtime protections replace broad staging |
| platform-change-flow | repository/local skill inspected September 13, 2026 | Fresh main, new worktrees/branches, reviewed PR/rebase merge and conservative cleanup |

No license file was present in the inspected spec/product/factory repositories. No
upstream orchestration shell implementation was copied. The new implementation
does not assert a blanket license for material without a supplied license notice.
Names here identify source workflows and supported hosts, not repository authorship.

The public spec/product/factory pins match the source skill trees previously used
for the integration byte-for-byte. Orchestrate is selectively reviewed through
0.5.0 as described below. Source pins alone do not replace runtime implementations
or imply adoption of later upstream updates.

## Explicit conflict resolutions

One root run owns state and user interaction. Product Markdown is canonical;
Factory does not create a second work-item schema. Shared Git identity derives
from the common directory, with explicit run-owned spec/check roots. Native
sessions are evidence only when explicitly bound; no implicit reconstruction.

The host's configured model is the default. Delegation uses real host tools,
isolated worktrees, actual dispatch identity and independent reviews. The old
conflict between “never delegate to another plugin” and mandated orchestration
tiers disappears because these are internal roles under one coordinator.

Implementation, publication, test side effects and exact-PR merge authority are
separate. Repository rules may explicitly include publication in a change request;
the source of that authority is recorded rather than assumed globally. Approval
of a semantic spec is never approval of arbitrary execution side effects.

Task/dependency receipts lock exact patches and content. Integrated readiness
checks approved scope again. Every base change invalidates integrated evidence.
Cleanup preserves ignored artifacts and historical attempts rather than force
discarding them. Authentication, sandbox enforcement and live host availability
remain outside the deterministic engine's claim boundary.

## Orchestrate 0.4.1 integration

Compared local upstream commits 850e305..6117a3461c15282949cecbea2c1f62d5d3967099.
Version 0.4.1 adds evaluation evidence; its runtime fixes are from 0.4.0.

- Adopted same-turn/immediate-completion async review scenarios and discriminating
  regression evidence, including collaborator counts rather than state alone.
- Added a read-only analyzer-delta helper with ESLint stylish and severity-first
  diagnostics, occurrence counts, file context and input hashes. It is a planned
  gate utility, not another coordinator or automatic execution authorization.
- Signal exits already failed; receipts now also explain the signal outcome.
  Repeat-log preservation already uses unique exclusive temporary files.
- Ordered all-or-nothing patch preflight, exact-path staging, external dependency
  logs, patch/scope validation and conservative worktree cleanup already exist
  in the independent runtime and are retained with their regression coverage.
- The original install/uninstall script is not part of this bundle. Its owned-
  symlink uninstall correction is inapplicable to marketplace installation.
- Upstream routing measurements remain upstream observations, not evidence for
  changing this bundle's configured/inherited models. No model pins were added.
- The upstream instruction to delete stray files is not adopted: preserve and
  report unexpected work. No broad staging or cleanup authorization is introduced.

This is a selective, tested integration, not automatic tracking of upstream or a
claim that every original command/installer is included.

## Orchestrate 0.5.0 integration

Compared 6117a3461c15282949cecbea2c1f62d5d3967099 through
225818ce57741831f9c224f1d03d247b9d6fce7e. Existing runtime protections are retained:

- Commands execute exact argument arrays without an implicit shell. Intentional
  Bash pipelines must explicitly request `-o pipefail`; the host reference and
  regressions cover failing and successful pipelines without rewriting commands.
- Exclusive temporary log creation preserves prior attempts, including partial
  evidence left by an interrupted attempt.
- Analyzer deltas already key by diagnostic and file and count repeated occurrences.
- Rename scope includes both source and destination through `--no-renames`;
  destination-only authorization cannot conceal an out-of-scope deletion.
- Transactional integration already refuses untracked and ignored-file collisions.
- Claude tool restrictions alone do not enforce a read-only filesystem. The host
  reference distinguishes permissions, filesystem enforcement and content checks.
- Upstream installer/uninstaller changes remain inapplicable: no upstream installer
  is shipped or executed. Model pins and broad cleanup remain excluded.

The release contract and runtime code are unchanged, so the plug-in retains 1.1.0.
The source review record binds this update to its inspected commit and mapped files.
