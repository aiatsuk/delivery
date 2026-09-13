# Independent upstream synchronization

Submodules solve reproducible source selection, not semantic integration. Git
records the source revision; `upstreams.lock.json` separately records the reviewed
revision, disposition, review hash and exact mapped plug-in file hashes.
No runtime script imports from `upstream/` or starts another coordinator.

## A normal update

1. Start a new task worktree from fresh main. Initialize the pinned sources with
   `git submodule update --init --recursive`. Never use `--remote` in CI.
2. Run `python3 tools/upstreams.py status`. Preserve any dirty upstream or local
   work; do not reset it to make the checks green.
3. For one selected source, fetch its confirmed public origin and inspect the
   changes from the current gitlink to the intended full commit. Example:

   ```sh
   git -C upstream/orchestrate fetch origin
   git -C upstream/orchestrate log --oneline HEAD..origin/main
   git -C upstream/orchestrate diff HEAD..origin/main
   git -C upstream/orchestrate checkout --detach <reviewed-full-commit>
   git add -- upstream/orchestrate
   python3 tools/upstreams.py status
   ```

   The status must now require review. No command here copies upstream code into
   the plug-in, rewrites other pins, executes installers or grants new authority.
4. Map applicable upstream changes to the existing integration. Preserve local
   fixes, API contracts, one coordinator, inherited model policy, clean worktrees,
   explicit side-effect approval and exact-PR merge approval. For non-applicable
   changes explain why; do not replace an adapted engine wholesale.
5. Add a new JSON record under `docs/integrations/<source>/`. Include exact
   `source_revision`, `disposition` (`integrated`, `no-applicable-change`, or
   `deferred`), `rationale`, actual `reviewer`, actual `tests`, inspected
   `upstream_paths`, and exact repository-relative `integration_paths`.
   Retain prior records in Git history; use a new filename for a new revision.
6. Run relevant regressions and full checks. Have a different reviewer inspect
   actual source changes and tests. A claimed review or test string is not an
   authenticated result; hashes cannot establish semantic correctness.
7. After review, record its current binding:

   ```sh
   python3 tools/upstreams.py record orchestrate --review docs/integrations/orchestrate/<record>.json
   make check
   make test
   ```

   `record` writes only the lock, not source pins/code. It cannot turn a mismatched
   commit or missing integration file into a reviewed source. `deferred` is an
   honest audit disposition but blocks release: keep main on the prior accepted
   pin until the work is ready.
8. Export and smoke-test the package without upstreams. For code/API changes run
   package tests as well. Review the staged diff, including pin and review changes,
   and open a PR. Bump the plug-in version for a changed release contract, not just
   to refresh an installation cache. Do not auto-merge dependency updates.

Changing a shared integration file can invalidate multiple source records. Review
all affected contracts rather than masking that coupling. Updating only one pin
does not advance the reviewed revision of other tools.

## What the checks enforce

- Exactly four allowlisted public HTTPS source URLs and conflict-free Git gitlinks.
- Initialized, clean source checkouts matching the indexed pins.
- The review names that exact revision and real upstream paths.
- Review text and mapped integration content still match their stored hashes.
- Deferred, missing or stale reviews fail the release check.
- Runtime packaging excludes source submodules and local execution history.

These checks are not a tamper-proof approval service. A person with write access
can forge review text or change tooling; independent PR review and protected main
are still required. The tool does not attest unlisted files: reviewers own complete
mapping coverage. CI receives read-only permissions and does not publish releases.

## Platform workflow adapter

Its generic Git lifecycle is maintained in the plug-in's Git reference/runtime;
the original platform repository is not cloned, referenced as a Git dependency,
or supplied to public CI. If a separate public workflow repository is created
later, add it only through an explicitly reviewed allowlist/schema change.
