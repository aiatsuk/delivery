# Validation

## Reproduce

Run `make check` and `make test` from a recursive clone. Tests use isolated local
fixtures; no production accounts, sessions, remote publications or devices.
CI pins official action commits, checks out exact submodule gitlinks and uses
read-only repository permissions. It does not grant PR-merge authority.

The standalone export is built from exact indexed package blobs. Smoke-test it
from outside the repository without submodule access using its bundle validator
and `skills/delivery/scripts/delivery.py doctor`.

## Initial repository review

The first independent design pass identified discovery/identity risks from
putting upstream skills inside the package. The runtime is therefore isolated
under `plugins/delivery-harness`; submodules are outside the discoverable skill
and validation roots.

The first code review reproduced two package leaks: an untracked file within a
runtime directory and a symlinked package root. Export now uses exact staged Git
blobs, rejects untracked/unstaged package inputs and source symlink ancestors,
and has regressions for both cases. Initial 16 tooling tests passed before those
additional probes; that success did not establish packaging safety.

The second code review passed the package corrections and found incomplete spec,
product and Orchestrate mappings. Those templates/references/shared contracts are
now included. The third code review returned PASS after independently running
all 20 tooling tests and targeted public-content scans. It also checked serialized
record writes, lock symlink refusal and mapped-file drift. Neither review prose
nor hashes authenticate human identity. The public source trees for spec/product/factory were
compared byte-for-byte to the previously integrated versions, with no differences.
Orchestrate is pinned to the previously integrated 0.4.1 commit.

## Observed local results, September 13, 2026

- Final tooling suite: 20/20 passed (25.426 seconds); also independently repeated.
- Fresh recursive clone of the committed feature branch: `make check` passed and
  20/20 tooling tests passed again (21.535 seconds).
- Complete runtime suite: 383/383 passed with zero errors, failures or skips.
  Three shards each imported the same full catalog; exact disjoint coverage was
  verified. Counts/durations: 128 in 316.266 seconds, 128 in 374.726 seconds,
  127 in 334.340 seconds. See [shard 0](evidence/plugin-shard-0.json),
  [shard 1](evidence/plugin-shard-1.json), [shard 2](evidence/plugin-shard-2.json).
  Focused and clean-clone reruns are not added to the unique test count of 403.
- Official plug-in and skill validators: passed. Dependency-free bundle validation:
  passed on source and standalone exports; exactly one discoverable skill.
- Package built from the clean clone without source dependencies: doctor and CLI
  help passed. Exported skills/scripts/tests match the tested runtime checksum
  `a2c643c502c70efafd9760d1c574713a6efc35b09abebf50e9f436ea1d07a7e1`.
- Final package file/hash manifest digest:
  `7824aac133f02713b623009f7333db81d32c13b0dbae5766c6a8d20f7667d208`.
- Local host: macOS, Python 3.14.7. Linux/Python 3.12 execution is delegated to
  the configured GitHub workflow and is not claimed by these local results.

The clean clone repeated tooling checks; the full runtime suite ran from the
feature worktree. Package smoke and exact runtime comparison are not described as
a second full runtime suite from the clean clone. Original local session reports
were excluded; only public-safe test IDs/counts are retained here.

## Remaining boundaries

Only mapped integration files invalidate a source review; independent reviewers
must check mapping completeness and semantic compatibility. Hashes do not prove
that a reviewer or a test actually ran. GitHub protected-branch settings are
maintainer-controlled and are not changed by repository scripts.

Actual interactive Claude execution is unavailable in the local environment.
Direct script/schema checks are not a fresh full native-host delivery run. No
real PR merge, deployment or production-side action is part of validation.
