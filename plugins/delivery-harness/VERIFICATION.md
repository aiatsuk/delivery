# Verification boundaries

The source repository records current results in `docs/VALIDATION.md`. Reproduce
the plug-in checks from this package root:

```sh
python3 -S -B scripts/validate_bundle.py .
python3 -S -B skills/delivery/scripts/delivery.py doctor
python3 -S -B -m unittest discover -s tests -t . -q
```

The baseline suite has 504 tests using temporary local Git repositories and fake
publication providers; the workflow-script tests need `node` and are skipped without it. It does not require initialized upstream submodules or
Python packages. Synthetic fixture actors are not native-host execution evidence.

Manifest/schema validation and direct CLI smoke do not establish a complete
interactive host run. For 1.2.0 one live local-only run on a throwaway repository
was driven through the shipped workflows in Claude Code 2.1.283 (implementation,
two-lens task review and integrated review agents), with `task-import` and
`review-import` reading the real host journal, and ended COMPLETE with an
authorized fast-forward. That is one run, not a guarantee for every host version. No live merge, deployment or production effect
is part of the test suite. Existing tests do not authenticate human authority or
eliminate the remote-base race without repository-side branch protection.

Local native-session and previous machine-specific reports were intentionally
not imported into the public repository or package.
