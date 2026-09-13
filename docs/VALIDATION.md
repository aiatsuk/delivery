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

Detailed final test counts and the second independent review are recorded after
the final candidate is checked. Neither a draft review record nor its hashes
authenticate execution. The public source trees for spec/product/factory were
compared byte-for-byte to the previously integrated versions, with no differences.
Orchestrate is pinned to the previously integrated 0.4.1 commit.

## Remaining boundaries

Only mapped integration files invalidate a source review; independent reviewers
must check mapping completeness and semantic compatibility. Hashes do not prove
that a reviewer or a test actually ran. GitHub protected-branch settings are
maintainer-controlled and are not changed by repository scripts.

Actual interactive Claude execution is unavailable in the local environment.
Direct script/schema checks are not a fresh full native-host delivery run. No
real PR merge, deployment or production-side action is part of validation.
