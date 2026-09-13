#!/usr/bin/env python3
"""Export only the self-contained plug-in, never upstreams or local run history."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
FILES = ("README.md", "PROVENANCE.md", "VERIFICATION.md", "NOTICE.md")
DIRS = (".codex-plugin", ".claude-plugin", "skills", "scripts", "tests", "examples", "notices")


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True)
    if result.returncode:
        raise ValueError("Git package inventory failed; resolve unstaged changes or invalid index")
    return result.stdout


def allowed(relative):
    return relative in FILES or any(relative.startswith(name + "/") for name in DIRS)


def build(root, destination):
    source = root / "plugins/delivery-harness"
    for path in (root, root / "plugins", source):
        if path.is_symlink():
            raise ValueError("Symlinked package source ancestor")
    destination = Path(destination)
    if destination.name != "delivery-harness" or destination.exists() or destination.is_symlink():
        raise ValueError("Use a new output directory named delivery-harness")
    if root.resolve() == destination.resolve() or root.resolve() in destination.resolve().parents:
        raise ValueError("Export outside the source checkout")
    for parent in destination.parents:
        if parent.is_symlink():
            raise ValueError("Output parents must not be symlinks")
    prefix = "plugins/delivery-harness/"
    for entry in git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", prefix).decode().split("\0"):
        if entry and allowed(entry.removeprefix(prefix)):
            raise ValueError("Untracked package input; inspect and stage only intended files")
    git(root, "diff", "--quiet", "--", prefix)
    selected = []
    for entry in git(root, "ls-files", "--stage", "-z", "--", prefix).decode().split("\0"):
        if not entry:
            continue
        metadata, name = entry.split("\t", 1)
        mode, object_id, stage = metadata.split()
        relative = name.removeprefix(prefix)
        if not allowed(relative):
            continue
        if mode not in {"100644", "100755"} or stage != "0":
            raise ValueError("Package input must be a conflict-free regular Git blob")
        rel = Path(relative)
        if any(part in {".git", "verification", "sessions", ".env", "__pycache__"} for part in rel.parts) or rel.suffix == ".pyc":
            raise ValueError(f"Local-only material in package input: {rel}")
        path = source
        for part in rel.parts:
            path /= part
            if path.is_symlink():
                raise ValueError("Symlinks cannot enter the package")
        selected.append((object_id, rel, mode))
    names = {str(item[1]) for item in selected}
    if not set(FILES).issubset(names) or any(not any(name.startswith(folder + "/") for name in names) for folder in DIRS):
        raise ValueError("Required package inputs must be present in the Git index")
    destination.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for object_id, relative, mode in sorted(selected, key=lambda item: str(item[1])):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git(root, "cat-file", "blob", object_id))
        target.chmod(0o755 if mode == "100755" else 0o644)
        hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    return {"output": str(destination), "files": hashes,
            "content_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(ROOT, args.output), indent=2))
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
