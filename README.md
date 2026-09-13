# Delivery

One product-to-PR workflow, with independently maintained upstream tools and
explicitly reviewed integrations. The installable plug-in is
[`plugins/delivery-harness`](plugins/delivery-harness), version 1.1.0.

```text
Pinned upstream commits → reviewed integration → tested self-contained plug-in
```

The four Git submodules are development inputs, not runtime dependencies:

| Input | Responsibility |
| --- | --- |
| [Orchestrate](https://github.com/aiatsuk/orchestrate) | Independent tasks/reviews and patch integration |
| [Spec-driven Development](https://github.com/aiatsuk/spec-driven-development) | Semantic specification and actual-diff verification |
| [Product-driven Development](https://github.com/aiatsuk/product-driven-development) | Product memory and bounded session evidence |
| [AI Factory](https://github.com/aiatsuk/factory) | Risk classification and proportional quality gates |

The platform-change-flow adapter is maintained inside the plug-in. No private
platform repository, session archive or installed plug-in cache is a dependency.
Updating one upstream does not update another or silently alter the plug-in.

## Develop

```sh
git clone --recurse-submodules https://github.com/aiatsuk/delivery.git
cd delivery
python3 tools/upstreams.py status
make check
make test
```

Python 3.10+ and Git are required; baseline tests have no Python package or live
service dependency. Use a task branch/worktree and open a PR; merge is a separate
authorization. See [repository instructions](AGENTS.md).

## Update an individual tool

Follow [the synchronization flow](docs/SYNCHRONIZATION.md). The short version:
fetch only that upstream, inspect and pin an exact commit, selectively integrate,
test, independently review, record the revision and file hashes, then open a PR.
A changed pin without a renewed review fails `make check`. No scheduled update,
automatic merge or installer execution is enabled.

## Install and use

Install the **nested plug-in directory**, not the repository root. It contains
one discoverable skill and works without initialized upstream submodules.

- Codex: register `plugins/delivery-harness` using the supported local marketplace
  flow, then invoke `$delivery-harness:delivery`.
- Claude Code: load `plugins/delivery-harness` with its local plug-in loader and
  invoke `/delivery-harness:delivery`, or link its `skills/delivery` as the global
  `/delivery` skill.

Describe the task normally. Material specs stop for approval; a PR stops for
review and exact-PR merge authorization. Runtime host compatibility and verification
limits are documented in the [plug-in README](plugins/delivery-harness/README.md).

To export a standalone package, choose a new directory outside the checkout:

```sh
python3 tools/package.py --output /absolute/new/location/delivery-harness
```

The exporter reads exact staged Git blobs in the package allowlist, excludes
upstreams and local verification history, rejects unstaged/untracked package
inputs, symlinks and overwrites, and prints a file/hash manifest. See [validation](docs/VALIDATION.md)
and [ownership/notices](plugins/delivery-harness/NOTICE.md).
