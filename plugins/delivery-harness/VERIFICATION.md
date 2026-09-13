# Verification boundaries

The source repository records current results in `docs/VALIDATION.md`. Reproduce
the plug-in checks from this package root:

```sh
python3 -S -B scripts/validate_bundle.py .
python3 -S -B skills/delivery/scripts/delivery.py doctor
python3 -S -B -m unittest discover -s tests -t . -q
```

The baseline suite has 383 tests using temporary local Git repositories and fake
publication providers. It does not require initialized upstream submodules or
Python packages. Synthetic fixture actors are not native-host execution evidence.

Manifest/schema validation and direct CLI smoke do not establish a complete
interactive host run. The local environment has no Claude executable; actual
Claude execution is not claimed. No live merge, deployment or production effect
is part of the test suite. Existing tests do not authenticate human authority or
eliminate the remote-base race without repository-side branch protection.

Local native-session and previous machine-specific reports were intentionally
not imported into the public repository or package.
