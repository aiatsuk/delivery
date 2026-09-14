# Changes

## Unreleased

- Add Cursor plugin and marketplace manifests so the existing harness can be
  installed from this repository in Cursor.

## 1.1.0

- Put the self-contained plug-in under version control with four pinned public
  upstream submodules, separate from the runtime package.
- Track explicit upstream review dispositions and integration content hashes.
- Add independent-update checks, isolated regression fixtures, package export and
  read-only CI. Preserve the existing integrated runtime and approval boundaries.
