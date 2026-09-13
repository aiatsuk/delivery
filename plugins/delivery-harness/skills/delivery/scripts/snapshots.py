"""Exact, external Git-tree snapshots, unaffected by export attributes/filters."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import subprocess

import git_ops


def capture_tree(worktree: str, tree: str, destination: str) -> dict:
    """Copy exact Git blobs, not a mutable filesystem or archive transform.

    A failed export is preserved. Destinations are new and outside all repository
    worktrees. Symlinks are represented but never followed while copying. Neither
    export-ignore nor export-subst can hide or alter implementation evidence.
    """
    root, target = Path(worktree).resolve(), Path(destination).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", tree):
        raise git_ops.GitError("INVALID_TREE", "Use an exact Git object ID for the snapshot.")
    actual = git_ops._text(root, "rev-parse", "--verify", "--end-of-options", tree + "^{tree}")
    for entry in git_ops._worktrees(root):
        checkout = Path(entry["worktree"]).resolve()
        if target == checkout or checkout in target.parents:
            raise git_ops.GitError("EVIDENCE_IN_REPOSITORY", "Snapshots belong outside repository worktrees.")
    if target.exists() or Path(destination).is_symlink():
        raise git_ops.GitError("OUTPUT_EXISTS", "Snapshot directories are immutable; use a new path.")
    entries = []
    for record in git_ops._git(root, "ls-tree", "-r", "-z", actual).stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        name = PurePosixPath(os.fsdecode(raw_name))
        if name.is_absolute() or any(p in {"..", ".git"} for p in name.parts) or not name.parts:
            raise git_ops.GitError("UNSAFE_TREE", "Unsafe path in Git tree.")
        if kind != "blob" or mode not in {"100644", "100755", "120000"}:
            raise git_ops.GitError("UNSUPPORTED_TREE", "Submodules and unusual file modes need separately scoped verification.")
        entries.append((mode, oid, name))
    if len(entries) > 200_000:
        raise git_ops.GitError("SNAPSHOT_LIMIT", "Snapshot exceeds the bounded 200,000-file limit.")
    environment = {k: v for k, v in os.environ.items() if k not in git_ops._CONTEXT_ENV and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    target.mkdir(parents=True)
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    total = 0
    try:
        for mode, oid, name in entries:
            path = target.joinpath(*name.parts)
            if any(p.is_symlink() for p in path.parents if p != target and target in p.parents):
                raise git_ops.GitError("UNSAFE_TREE", "Tree entry traverses a symlink.")
            path.parent.mkdir(parents=True, exist_ok=True)
            process.stdin.write((oid + "\n").encode("ascii"))
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").strip().split()
            if len(header) != 3 or header[:2] != [oid, "blob"]:
                raise git_ops.GitError("SNAPSHOT_FAILED", "Git did not return the requested blob.")
            size = int(header[2])
            total += size
            if total > 2 * 1024 ** 3:
                raise git_ops.GitError("SNAPSHOT_LIMIT", "Snapshot exceeds the bounded 2 GiB limit.")
            if mode == "120000":
                path.symlink_to(os.fsdecode(process.stdout.read(size)))
            else:
                with path.open("xb") as output:
                    remaining = size
                    while remaining:
                        chunk = process.stdout.read(min(remaining, 1024 * 1024))
                        if not chunk:
                            raise git_ops.GitError("SNAPSHOT_FAILED", "Git ended a blob before its declared size.")
                        output.write(chunk)
                        remaining -= len(chunk)
                path.chmod(0o755 if mode == "100755" else 0o644)
            if process.stdout.read(1) != b"\n":
                raise git_ops.GitError("SNAPSHOT_FAILED", "Invalid Git batch delimiter.")
        process.stdin.close()
        stderr = process.stderr.read().decode(errors="replace")
        if process.wait() != 0:
            raise git_ops.GitError("SNAPSHOT_FAILED", stderr.strip() or "Git batch copy failed.")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    return {"path": str(target), "tree": actual, "files": len(entries), "bytes": total}
