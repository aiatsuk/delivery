# Markdown product memory

The public module is `../../scripts/product.py`. It uses only Python's standard
library and reads no native sessions. Product selection is always an explicit
directory or `product.md` path. There is no latest-product or latest-chat fallback.

`product_home(override=None)` checks an explicit override, then
`PRODUCT_MEMORY_HOME`, then `~/.local/share/product-driven-development/products`.
Resolving the home and listing a missing home never creates directories.

`list_products(home)` lists metadata from direct child `product.md` files.
`load_product(path)` returns that metadata, the map, optional `memory.md`, and
navigation links found in those two documents. Links are untrusted references:
the loader neither follows nor reads them. Relative link targets are interpreted
relative to the product root. A linked repository may be outside product memory.

`validate_work_item(product_root, item_path)` accepts a contained directory or
its exact `project.md` or `ticket.md`. There must be exactly one of those two
Markdown documents in the owner directory. Legacy JSON cannot satisfy this
binding. Every directory under the selected product and the Markdown file itself
is opened without following symlinks. An absolute path can use the supplied
product-root spelling or its canonical spelling.

`create_product(home, slug, title, purpose, repository)` reserves a new directory
and publishes only `product.md` and `memory.md`. Existing paths are never adopted
or overwritten. Product IDs use lowercase letters, digits, and internal hyphens.

`record_outcome(product_root, run_id, summary, evidence, next_action,
work_item=None)` writes a bounded note to the narrowest supplied owner. Evidence
contains local artifact paths or ordinary URLs, not artifact contents. Relative
evidence links are caller-supplied paths relative to the note's own directory;
prefer absolute artifact paths when linking delivery runs. A note never creates
an accepted decision, approval, verification claim, or external status by itself.
The caller is responsible for supplying reviewed, accurate prose and links and
for obtaining any required write authorization.

One note is allowed per product, owner, and run ID. Notes use
`YYYY-MM-DD-HHMM-delivery-<run-id>.md`. A directory lock serializes cooperating
writers without introducing an index or a lock file. Files are fully written and
synced before atomic no-clobber publication. An exact replay returns the existing
path with `created: false`; changed prose, links, next action, or manually edited
content causes `note_conflict`. More than one matching note also fails closed.
The run engine must keep its product and work-item binding immutable; this module
does not create a global run index or infer ownership from other directories.

The APIs raise `ProductError(code, message)` with bounded messages that do not
echo potentially sensitive input. Markdown reads are limited to 256 KiB per
file, note writes to 16 KiB, and evidence to 20 links. Secret-shaped text and
credential-bearing URLs are rejected before publication. This catches common
accidental disclosures; it is not a guarantee that all arbitrary secrets can be
recognized. Review summaries before recording them. Full native transcripts,
tool payloads, credentials, and private keys do not belong in product memory.

Standalone CLI:

```sh
python3 skills/delivery/scripts/product.py list --home /path/to/products
python3 skills/delivery/scripts/product.py show /path/to/products/storefront
python3 skills/delivery/scripts/product.py create --home /path/to/products \
  --slug storefront --title Storefront --purpose 'Sell useful products.' \
  --repository /path/to/repository
python3 skills/delivery/scripts/product.py note --product-root /path/to/products/storefront \
  --work-item tickets/T-123/ticket.md --run-id run-123 \
  --summary 'The request timeout now preserves the current cart.' \
  --evidence /path/to/delivery/runs/run-123/verification.md \
  --next-action 'Review the pull request.'
```

CLI success is `{"ok": true, "result": ...}` with exit status zero. Domain
errors are `{"ok": false, "error": {"code": ..., "message": ...}}` with exit
status two. Standard argument help and invocation errors use argparse.

## Explicit native evidence

Read [references/session-evidence.md](references/session-evidence.md) before
invoking the separate [session_evidence.py](session_evidence.py) command. It
requires an exact host, canonical session UUID, selected product, and linked
Markdown binding. It does not run from any product CRUD API. Its one-time
`--allow-unbound` option requires explicit user scope and cannot override a
revoked or malformed binding. Session summaries use the unchanged
`product-session-binding/v1` metadata grammar in
[references/artifacts.md](references/artifacts.md).

## Provenance

The Markdown model, templates, explicit selection and narrow ownership rules,
native evidence helper, its 39 regression tests, and its references originate
from `product-driven-development` version `0.5.0`. The helper and tests are
vendored in this bundle; there is no runtime import from the old plugin or a
developer-specific path. Relocation changes the helper path in the test and
reference. A local fix rejects unsafe `sessions/` directories even when an exact
binding is absent and a one-time unbound lookup was requested. The remaining
public API and its tests are the delivery integration.
