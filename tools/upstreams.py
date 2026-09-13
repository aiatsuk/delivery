#!/usr/bin/env python3
"""Inspect pinned development inputs; record explicit, content-bound reviews.

No network, installers, source copying or code execution. Gitlinks pin source;
the lock records reviewed revisions separately. Records attest evidence, not human
authentication or semantic correctness. Use an independent reviewer and CI.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {name: f"https://github.com/aiatsuk/{name}.git" for name in (
    "orchestrate", "spec-driven-development", "product-driven-development", "factory")}
LOCK = "upstreams.lock.json"
PLUGIN = "plugins/delivery-harness/"


class Invalid(ValueError):
    pass


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if result.returncode:
        raise Invalid(result.stderr.strip() or "Git inspection failed")
    return result.stdout.strip()


def file_path(root, value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix):
        raise Invalid(f"Expected a path under {prefix}")
    parts = PurePosixPath(value).parts
    if str(PurePosixPath(value)) != value or ".." in parts or "\\" in value:
        raise Invalid("Non-canonical evidence path")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink():
            raise Invalid("Symlinks cannot supply integration evidence")
    if not path.is_file():
        raise Invalid(f"Missing integration evidence: {value}")
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_lock(root):
    path = root / LOCK
    if path.is_symlink():
        raise Invalid("Refuse symlinked integration lock")
    if not path.exists():
        return {"schema": 1, "sources": {}}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("sources"), dict):
        raise Invalid("Unsupported integration lock schema")
    if set(data["sources"]) - set(SOURCES):
        raise Invalid("Unknown source in integration lock")
    return data


def source(root, name):
    module = root / "upstream" / name
    expected = f"upstream/{name}"
    if module.is_symlink() or (root / "upstream").is_symlink():
        raise Invalid("Symlinked upstream directory")
    actual = git(root, "config", "--file", ".gitmodules", "--get", f"submodule.{expected}.url")
    path = git(root, "config", "--file", ".gitmodules", "--get", f"submodule.{expected}.path")
    if actual != SOURCES[name] or path != expected:
        raise Invalid("Upstream path/URL differs from the public allowlist")
    index = git(root, "ls-files", "--stage", "--", expected).split()
    if len(index) != 4 or index[0] != "160000" or index[2] != "0" or index[3] != expected:
        raise Invalid("Expected exactly one conflict-free gitlink")
    revision = index[1]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise Invalid("Expected a full source commit")
    if not (module / ".git").exists():
        raise Invalid("Submodule is uninitialized; initialize the pinned checkout first")
    if git(module, "rev-parse", "--show-toplevel") != str(module.resolve()):
        raise Invalid("Upstream is not its own Git worktree")
    if git(module, "rev-parse", "HEAD") != revision:
        raise Invalid("Submodule checkout differs from staged gitlink")
    if git(module, "status", "--porcelain", "--untracked-files=all", "--ignore-submodules=none"):
        raise Invalid("Submodule contains uncommitted or untracked work")
    return revision


def review_data(root, name, review_file, revision):
    path = file_path(root, review_file, f"docs/integrations/{name}/")
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("source_revision") != revision:
        raise Invalid("Review must identify the exact source revision")
    if data.get("disposition") not in {"integrated", "no-applicable-change", "deferred"}:
        raise Invalid("Invalid review disposition")
    for field in ("rationale", "reviewer", "tests"):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise Invalid(f"Review requires {field}")
    upstream_paths = data.get("upstream_paths")
    if not isinstance(upstream_paths, list) or not upstream_paths:
        raise Invalid("List the inspected upstream paths")
    for value in upstream_paths:
        if not isinstance(value, str) or value.startswith("/") or ".." in PurePosixPath(value).parts:
            raise Invalid("Invalid upstream review path")
        if not git(root / "upstream" / name, "ls-tree", revision, "--", value):
            raise Invalid(f"Unknown inspected upstream path: {value}")
    paths = data.get("integration_paths")
    if not isinstance(paths, list) or not paths or len(paths) != len(set(paths)):
        raise Invalid("List unique integration files")
    hashes = {value: sha(file_path(root, value, PLUGIN)) for value in paths}
    return {"reviewed_revision": revision, "disposition": data["disposition"],
            "review_file": review_file, "review_sha256": sha(path), "integration_sha256": hashes}


def status(root):
    lock = load_lock(root)
    result = {}
    keys = git(root, "config", "--file", ".gitmodules", "--name-only", "--get-regexp", r"^submodule\..*\.path$").splitlines()
    expected = {f"submodule.upstream/{name}.path" for name in SOURCES}
    if len(keys) != len(expected) or set(keys) != expected:
        raise Invalid("Unexpected or duplicate submodule configuration")
    for name in SOURCES:
        try:
            revision = source(root, name)
            record = lock["sources"].get(name)
            if not isinstance(record, dict):
                result[name] = {"source_revision": revision, "state": "review-required"}
                continue
            current = review_data(root, name, record["review_file"], revision)
            if current != record:
                raise Invalid("Review or mapped integration content changed; renew review")
            result[name] = {"source_revision": revision, "reviewed_revision": record["reviewed_revision"],
                            "state": "deferred" if record["disposition"] == "deferred" else "reviewed"}
        except (Invalid, OSError, ValueError, KeyError, TypeError) as error:
            result[name] = {"state": "review-required", "reason": str(error)}
    return {"ok": all(row["state"] == "reviewed" for row in result.values()), "sources": result}


def _record(root, name, review_file):
    data = load_lock(root)
    revision = source(root, name)
    data["sources"][name] = review_data(root, name, review_file, revision)
    if (root / LOCK).is_symlink():
        raise Invalid("Refuse symlinked integration lock")
    fd, temp = tempfile.mkstemp(prefix=".upstream-lock-", dir=root)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(data, out, indent=2, sort_keys=True)
            out.write("\n")
        os.replace(temp, root / LOCK)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return {"recorded": name, "reviewed_revision": revision, "disposition": data["sources"][name]["disposition"]}


def record(root, name, review_file):
    # Serialize independent source records without replacing another writer's
    # update. The persistent advisory lock is local-only, never review evidence.
    lock_path = root / ".upstreams-review.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _record(root, name, review_file)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    commands.add_parser("check")
    rec = commands.add_parser("record")
    rec.add_argument("name", choices=SOURCES)
    rec.add_argument("--review", required=True)
    args = parser.parse_args(argv)
    try:
        result = record(ROOT, args.name, args.review) if args.command == "record" else status(ROOT)
        print(json.dumps(result, indent=2))
        return 1 if args.command == "check" and not result["ok"] else 0
    except (Invalid, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
