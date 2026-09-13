"""Conservative Git operations for an externally coordinated delivery run.

No function stashes, resets, force-deletes, resolves conflicts, or stages a broad
pathspec. Review and publication authority belong to the caller. Paths passed as
file scopes are repository-relative literal file names, never directories or
patterns. The primary checkout is identified through Git's common directory.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tempfile
from typing import Iterable


class GitError(RuntimeError):
    """An actionable refusal or Git failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REMOTE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_AGENT_NAME = re.compile(r"\b(?:claude|codex|grok|copilot|gpt(?:-\w+)?|gemini)\b", re.I)
_CONTEXT_ENV = {
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS", "GIT_PREFIX",
}


def _git(path: Path, *args: str, data: bytes | None = None,
         env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess:
    process_env = {
        key: value for key, value in os.environ.items()
        if key not in _CONTEXT_ENV
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    process_env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
    if env:
        process_env.update(env)
    command = [
        "git", "--literal-pathspecs", "-C", str(path),
        "-c", "color.ui=false", "-c", "core.fsmonitor=false", *args,
    ]
    try:
        result = subprocess.run(command, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=process_env, check=False)
    except OSError as exc:
        raise GitError("GIT_UNAVAILABLE", f"Cannot run Git: {exc}") from exc
    if check and result.returncode:
        message = os.fsdecode(result.stderr).strip() or os.fsdecode(result.stdout).strip()
        raise GitError("GIT_FAILED", message or "Git command failed.",
                       {"operation": args[0] if args else "git", "returncode": result.returncode})
    return result


def _text(path: Path, *args: str, **kwargs) -> str:
    return os.fsdecode(_git(path, *args, **kwargs).stdout).strip()


def _digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def _root(path: str | Path) -> Path:
    candidate = Path(path).resolve()
    if not candidate.is_dir():
        raise GitError("NOT_REPOSITORY", f"Repository directory does not exist: {candidate}")
    result = _git(candidate, "rev-parse", "--is-bare-repository", check=False)
    if result.returncode:
        raise GitError("NOT_REPOSITORY", f"Not a Git repository: {candidate}")
    if result.stdout.strip() == b"true":
        raise GitError("BARE_REPOSITORY", "A bare repository has no working checkout.")
    result = _git(candidate, "rev-parse", "--show-toplevel", check=False)
    if result.returncode:
        raise GitError("NOT_REPOSITORY", f"Not inside a working checkout: {candidate}")
    return Path(os.fsdecode(result.stdout).strip()).resolve()


def _common(path: Path) -> Path:
    return Path(_text(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def _worktrees(path: Path) -> list[dict]:
    output = _git(path, "worktree", "list", "--porcelain", "-z").stdout
    result: list[dict] = []
    record: dict = {}
    for item in output.split(b"\0"):
        if not item:
            if record:
                result.append(record)
                record = {}
            continue
        key, separator, value = os.fsdecode(item).partition(" ")
        record[key] = value if separator else True
    if record:
        result.append(record)
    return result


def canonical_repo(path: str | Path) -> Path:
    """Return the real primary checkout for any directory in one of its worktrees."""
    current = _root(path)
    worktrees = _worktrees(current)
    if not worktrees or worktrees[0].get("bare"):
        raise GitError("BARE_REPOSITORY", "Working operations require a non-bare primary checkout.")
    primary = Path(worktrees[0]["worktree"]).resolve()
    if _root(primary) != primary or _common(primary) != _common(current):
        raise GitError("REPOSITORY_IDENTITY", "The primary checkout does not share this Git common directory.")
    return primary


def _head(path: Path) -> str:
    result = _git(path, "rev-parse", "--verify", "HEAD^{commit}", check=False)
    if result.returncode:
        raise GitError("UNBORN_HEAD", "The repository needs an initial commit.")
    return result.stdout.decode("ascii").strip()


def _branch(path: Path) -> str | None:
    result = _git(path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return os.fsdecode(result.stdout).strip() if result.returncode == 0 else None


def _sha(path: Path, value: str) -> str:
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise GitError("INVALID_SHA", "A full lowercase commit SHA is required.")
    result = _git(path, "rev-parse", "--verify", "--end-of-options", value + "^{commit}", check=False)
    if result.returncode or result.stdout.decode("ascii").strip() != value:
        raise GitError("UNKNOWN_COMMIT", f"Commit is not present in this repository: {value}")
    return value


def _git_path(path: Path, name: str) -> Path:
    return Path(_text(path, "rev-parse", "--path-format=absolute", "--git-path", name))


def _operations(path: Path) -> list[str]:
    names = ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-apply",
             "rebase-merge", "sequencer", "BISECT_LOG", "index.lock")
    directory = Path(_text(path, "rev-parse", "--absolute-git-dir"))
    return [name for name in names if (directory / name).exists()]


def _no_operations(path: Path) -> None:
    operations = _operations(path)
    if operations:
        raise GitError("OPERATION_IN_PROGRESS", "Finish or explicitly abort the existing Git operation first.",
                       {"in_progress": operations})


def _status(path: Path) -> dict:
    raw = _git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all",
               "--ignored=matching", "--no-renames").stdout
    entries = []
    staged, unstaged, untracked, ignored, unmerged = [], [], [], [], []
    for chunk in raw.split(b"\0"):
        if not chunk:
            continue
        state, name = chunk[:2].decode("ascii"), os.fsdecode(chunk[3:])
        entries.append({"path": name, "status": state})
        if state == "??":
            untracked.append(name)
        elif state == "!!":
            ignored.append(name)
        else:
            if state[0] != " ":
                staged.append(name)
            if state[1] != " ":
                unstaged.append(name)
            if "U" in state or state in {"AA", "DD"}:
                unmerged.append(name)
    hidden = sorted(os.fsdecode(item[2:])
                    for item in _git(path, "ls-files", "-v", "-z").stdout.split(b"\0")
                    if item and (item[:1] == b"S" or item[:1].islower()))
    return {"entries": entries, "staged": sorted(staged), "unstaged": sorted(unstaged),
            "untracked": sorted(untracked), "ignored": sorted(ignored),
            "unmerged": sorted(unmerged), "hidden": hidden,
            "clean": not (staged or unstaged or untracked or hidden),
            "artifact_free": not entries}


def _visible(state: dict) -> None:
    if state["hidden"]:
        raise GitError("HIDDEN_INDEX_STATE", "Assume-unchanged or skip-worktree flags can hide working changes; inspect and clear them explicitly.",
                       {"files": state["hidden"]})


def _clean(path: Path, *, ignored: bool = False) -> dict:
    status = _status(path)
    _visible(status)
    if not status["clean"] or (ignored and status["ignored"]):
        raise GitError("DIRTY_WORKTREE", "The checkout has changes or artifacts that must be preserved.", status)
    return status


def _remote(path: Path, remote: str) -> str:
    if not isinstance(remote, str) or not _REMOTE.fullmatch(remote):
        raise GitError("INVALID_REMOTE", "Use a configured Git remote name.")
    result = _git(path, "remote", "get-url", remote, check=False)
    if result.returncode:
        raise GitError("MISSING_REMOTE", f"No configured remote named {remote}.")
    return os.fsdecode(result.stdout).strip()


def _base_ref(path: Path, remote: str, base: str) -> str:
    _remote(path, remote)
    if not isinstance(base, str) or base.startswith(("-", "refs/")):
        raise GitError("INVALID_BRANCH", "Use a plain branch name.")
    result = _git(path, "check-ref-format", "--branch", base, check=False)
    if result.returncode or "@{" in base:
        raise GitError("INVALID_BRANCH", "Invalid base branch name.")
    return f"refs/remotes/{remote}/{base}"


def _ref_sha(path: Path, ref: str) -> str | None:
    result = _git(path, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}", check=False)
    return result.stdout.decode("ascii").strip() if result.returncode == 0 else None


def _fetch(path: Path, remote: str, base: str) -> tuple[str, str]:
    ref = _base_ref(path, remote, base)
    _git(path, "fetch", "--no-tags", remote, f"+refs/heads/{base}:{ref}")
    sha = _ref_sha(path, ref)
    if sha is None:
        raise GitError("MISSING_BASE", f"The remote does not have branch {base}.")
    return ref, sha


def inspect_repo(path: str | Path) -> dict:
    current = _root(path)
    primary = canonical_repo(current)
    origin = _git(current, "remote", "get-url", "origin", check=False)
    remote_url = os.fsdecode(origin.stdout).strip() if origin.returncode == 0 else None
    return {"primary": str(primary), "worktree": str(current), "common_dir": str(_common(current)),
            "branch": _branch(current), "head": _head(current), "remote": "origin",
            "remote_url": remote_url, "origin_url": remote_url,
            "status": _status(current), "in_progress": _operations(current)}


def _names(path: Path, *args: str, env: dict | None = None) -> list[str]:
    return sorted(os.fsdecode(item) for item in _git(path, *args, env=env).stdout.split(b"\0") if item)


def _diff_names(path: Path, *revisions: str, cached: bool = False,
                env: dict | None = None) -> list[str]:
    flags = ["--cached"] if cached else []
    return _names(path, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                  "--name-only", "-z", *flags, *revisions, "--", env=env)


def _ignored_collisions(path: Path, target_sha: str) -> None:
    ignored = [name.rstrip("/") for name in _status(path)["ignored"]]
    if not ignored:
        return
    tracked = _names(path, "ls-tree", "-r", "--name-only", "-z", target_sha)
    collisions = sorted({name for name in ignored for other in tracked
                         if name == other or name.startswith(other + "/") or other.startswith(name + "/")})
    if collisions:
        raise GitError("IGNORED_COLLISION", "The target tree would overwrite ignored artifacts.",
                       {"files": collisions})


def prepare_primary(path: str | Path, remote: str = "origin", base: str = "main") -> dict:
    """Freshen a clean primary with fast-forward-only operations, never a reset."""
    primary = canonical_repo(path)
    _no_operations(primary)
    _clean(primary)
    ref, target = _fetch(primary, remote, base)
    local = _ref_sha(primary, f"refs/heads/{base}")
    if local is not None and _git(primary, "merge-base", "--is-ancestor", local, target,
                                  check=False).returncode:
        raise GitError("MAIN_DIVERGED", "The primary base branch is ahead of or diverged from its remote; it was preserved.",
                       {"local_head": local, "base_sha": target})
    _ignored_collisions(primary, target)
    _no_operations(primary)
    _clean(primary)
    if _branch(primary) != base:
        if local is None:
            _git(primary, "switch", "--no-overwrite-ignore", "--create", base, "--track", ref)
        else:
            _git(primary, "switch", "--no-overwrite-ignore", base)
    # Pull the exact fetched tracking ref locally. A second network fetch here
    # could introduce uninspected files over an ignored artifact.
    _git(primary, "-c", "merge.autostash=false", "-c", "rebase.autoStash=false",
         "pull", "--ff-only", "--no-rebase", "--no-autostash", ".", ref)
    if _head(primary) != target or _ref_sha(primary, ref) != target:
        raise GitError("BASE_CHANGED", "The base changed during preparation; take a fresh snapshot.")
    result = inspect_repo(primary)
    result.update({"remote": remote, "remote_url": _remote(primary, remote),
                   "base": base, "base_sha": target})
    return result


def main_snapshot(primary: str | Path, remote: str = "origin", base: str = "main",
                  *, fetch: bool = True) -> dict:
    """Read the primary and remote base for race checks; refresh the remote by default."""
    root = canonical_repo(primary)
    if fetch:
        _, sha = _fetch(root, remote, base)
    else:
        sha = _ref_sha(root, _base_ref(root, remote, base))
        if sha is None:
            raise GitError("MISSING_BASE", "Fetch the remote base before taking a snapshot.")
    return {"primary": str(root), "head": _head(root), "branch": _branch(root),
            "base_sha": sha, "base": base, "remote": remote,
            "remote_url": _remote(root, remote), "in_progress": _operations(root)}


def _feature_branch(path: Path, name: str) -> None:
    if not isinstance(name, str) or name.startswith(("-", "refs/")) or "@{" in name:
        raise GitError("INVALID_BRANCH", "Use a plain feature branch name.")
    if _git(path, "check-ref-format", "--branch", name, check=False).returncode:
        raise GitError("INVALID_BRANCH", "Invalid feature branch name.")
    if name in {"main", "master", _branch(canonical_repo(path))}:
        raise GitError("PROTECTED_BRANCH", "Working operations require a separate feature branch.")
    if _AGENT_NAME.search(name):
        raise GitError("HISTORY_POLICY", "Branch names must describe the change without naming an agent.")


def _task_root(path: str | Path) -> Path:
    root = _root(path)
    if root == canonical_repo(root):
        raise GitError("PRIMARY_WRITE", "Use a task worktree for repository changes.")
    branch = _branch(root)
    if branch is None:
        raise GitError("DETACHED_HEAD", "Working operations require an attached feature branch.")
    _feature_branch(root, branch)
    return root


def _exact_location(path: str | Path) -> Path:
    raw = Path(path)
    if not raw.is_absolute() or ".." in raw.parts:
        raise GitError("INVALID_PATH", "Use an absolute path with no parent traversal.")
    resolved = raw.resolve()
    if raw != resolved:
        raise GitError("INVALID_PATH", "Use a canonical path without symlink aliases.")
    return resolved


def create_worktree(primary: str | Path, path: str | Path, branch: str,
                    base_sha: str) -> dict:
    root = canonical_repo(primary)
    target = _exact_location(path)
    _no_operations(root)
    _clean(root)
    _feature_branch(root, branch)
    _sha(root, base_sha)
    if _head(root) != base_sha:
        raise GitError("STALE_BASE", "The requested worktree base is not the prepared primary HEAD.")
    if target == root or root in target.parents or target.exists() or target.is_symlink():
        raise GitError("WORKTREE_PATH_IN_USE", "The task worktree needs an unused path outside the primary checkout.")
    if not target.parent.is_dir():
        raise GitError("INVALID_PATH", "The task worktree's parent directory must already exist.")
    if _ref_sha(root, f"refs/heads/{branch}") is not None:
        raise GitError("BRANCH_EXISTS", "Use a new branch; an existing branch is not this task's work.")
    if any(Path(item["worktree"]).resolve() == target for item in _worktrees(root)):
        raise GitError("WORKTREE_PATH_IN_USE", "Git already records this worktree path.")
    _git(root, "worktree", "add", "--no-track", "-b", branch, "--", str(target), base_sha)
    result = inspect_repo(target)
    if result["primary"] != str(root) or result["head"] != base_sha or result["branch"] != branch:
        raise GitError("WORKTREE_IDENTITY", "The created worktree has an unexpected identity.", result)
    result["base_sha"] = base_sha
    return result


def _paths(root: Path, values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise GitError("INVALID_SCOPE", "File scope must be a list of exact relative file paths.")
    result = []
    for value in values:
        if not isinstance(value, str) or not value or "\0" in value or "\\" in value:
            raise GitError("INVALID_PATH", "Invalid literal file path.")
        pure = PurePosixPath(value)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
            raise GitError("INVALID_PATH", "File paths must be relative and cannot contain parent traversal.")
        if any(part.lower() == ".git" for part in pure.parts):
            raise GitError("INVALID_PATH", "Git metadata is never a reviewed source path.")
        candidate = root.joinpath(*pure.parts)
        for parent in candidate.parents:
            if parent == root:
                break
            if parent.is_symlink():
                raise GitError("UNSAFE_SYMLINK", "A reviewed path cannot traverse a symlink.")
        if candidate.is_dir() and not candidate.is_symlink():
            raise GitError("DIRECTORY_SCOPE", "Declare exact files, not directories.", {"path": value})
        result.append(value)
    if len(result) != len(set(result)):
        raise GitError("INVALID_SCOPE", "A reviewed path must appear only once.")
    return sorted(result)


def _working_record(root: Path, name: str) -> dict:
    target = root / name
    try:
        mode = target.lstat().st_mode
    except FileNotFoundError:
        return {"path": name, "sha256": None, "mode": None}
    if stat.S_ISLNK(mode):
        payload = os.fsencode(os.readlink(target))
        git_mode = "120000"
    elif stat.S_ISREG(mode):
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"path": name, "sha256": digest.hexdigest(),
                "mode": "100755" if mode & 0o111 else "100644"}
    else:
        raise GitError("UNSUPPORTED_FILE_TYPE", "Changed paths must be regular files or symlinks.", {"path": name})
    return {"path": name, "sha256": hashlib.sha256(payload).hexdigest(), "mode": git_mode}


def _index_entries(root: Path, *, env: dict | None = None) -> dict:
    result: dict = {}
    for entry in _git(root, "ls-files", "--stage", "-z", env=env).stdout.split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, blob, stage = metadata.decode("ascii").split()
        result.setdefault(os.fsdecode(raw_name), []).append({"mode": mode, "blob": blob, "stage": stage})
    return result


def inventory(worktree: str | Path, base_sha: str) -> dict:
    """Snapshot changes, including index blobs and a commit-stable content digest.

    ``fingerprint`` includes HEAD, staged/unstaged state, ignored path names and
    operation state. ``content_fingerprint`` includes only the full base SHA and
    final changed file content/mode. Ignored artifacts are listed but never count
    as deliverable files. ``tree`` is the staged index tree, or None if unmerged.
    """
    root = _root(worktree)
    primary = canonical_repo(root)
    _sha(root, base_sha)
    state = _status(root)
    _visible(state)
    content_paths = sorted(set(_diff_names(root, base_sha)) | set(state["untracked"]))
    files = sorted(set(content_paths) | set(state["staged"]) | set(state["unstaged"]))
    _paths(root, files)
    index = _index_entries(root)
    records = []
    for name in files:
        record = _working_record(root, name)
        record.update({"index": index.get(name, []), "staged": name in state["staged"],
                       "unstaged": name in state["unstaged"], "untracked": name in state["untracked"]})
        records.append(record)
    content_records = [{key: record[key] for key in ("path", "sha256", "mode")}
                       for record in records if record["path"] in content_paths]
    tree = None if state["unmerged"] else _text(root, "write-tree")
    result = {"primary": str(primary), "worktree": str(root), "head": _head(root),
              "branch": _branch(root), "base_sha": base_sha, "files": files, "records": records,
              "content_records": content_records, "tree": tree,
              "staged": state["staged"], "unstaged": state["unstaged"],
              "untracked": state["untracked"], "ignored": state["ignored"],
              "in_progress": _operations(root)}
    result["content_fingerprint"] = _digest({"base_sha": base_sha, "files": content_records})
    result["fingerprint"] = _digest(result)
    return result


def _scope(root: Path, allowed: list[str], state: dict) -> None:
    actual = set(state["staged"]) | set(state["unstaged"]) | set(state["untracked"])
    unexpected = sorted(actual - set(allowed))
    if unexpected:
        raise GitError("OUT_OF_SCOPE", "The worktree contains changes outside the reviewed file scope.",
                       {"files": unexpected})


def export_patch(worktree: str | Path, output: str | Path,
                 allowed_paths: Iterable[str]) -> dict:
    """Export an already-staged binary patch. This function never stages files."""
    root = _task_root(worktree)
    _no_operations(root)
    allowed = _paths(root, allowed_paths)
    state = _status(root)
    _visible(state)
    _scope(root, allowed, state)
    if state["unstaged"] or state["untracked"]:
        raise GitError("UNSTAGED_CHANGES", "Stage the exact reviewed files before exporting the patch.", state)
    if not state["staged"]:
        raise GitError("EMPTY_PATCH", "There are no staged changes to export.")
    if state["unmerged"]:
        raise GitError("UNMERGED_INDEX", "Resolve the existing index conflicts before exporting.")
    destination = _exact_location(output)
    primary = canonical_repo(root)
    if destination == root or root in destination.parents or destination == primary or primary in destination.parents:
        raise GitError("EVIDENCE_IN_REPOSITORY", "Keep exported evidence outside repository checkouts.")
    if not destination.parent.is_dir():
        raise GitError("INVALID_PATH", "The patch output directory must already exist.")
    before = inventory(root, _head(root))
    payload = _git(root, "diff", "--cached", "--binary", "--full-index", "--no-renames",
                   "--no-ext-diff", "--no-textconv", "HEAD", "--", *allowed).stdout
    if not payload:
        raise GitError("EMPTY_PATCH", "There are no staged changes to export.")
    if inventory(root, before["head"])["fingerprint"] != before["fingerprint"]:
        raise GitError("WORKTREE_CHANGED", "The worktree changed while the patch was being exported.")
    try:
        with destination.open("xb") as stream:
            stream.write(payload)
    except FileExistsError as exc:
        raise GitError("OUTPUT_EXISTS", "The patch output already exists; choose a new evidence path.") from exc
    return {"path": str(destination), "sha256": hashlib.sha256(payload).hexdigest(),
            "allowed_paths": allowed, "files": state["staged"], "head": before["head"],
            "tree": before["tree"], "fingerprint": before["fingerprint"],
            "content_fingerprint": before["content_fingerprint"], "records": before["records"]}


def _patch_paths(root: Path, payload: bytes) -> list[str]:
    result = _git(root, "apply", "--numstat", "-z", "-", data=payload, check=False)
    if result.returncode:
        raise GitError("INVALID_PATCH", "Git could not parse the reviewed patch.")
    chunks = iter(result.stdout.split(b"\0"))
    names = []
    for chunk in chunks:
        if not chunk:
            continue
        fields = chunk.split(b"\t", 2)
        if len(fields) != 3:
            raise GitError("INVALID_PATCH", "Malformed patch path inventory.")
        if fields[2]:
            names.append(os.fsdecode(fields[2]))
        else:
            try:
                names.extend((os.fsdecode(next(chunks)), os.fsdecode(next(chunks))))
            except StopIteration as exc:
                raise GitError("INVALID_PATCH", "Malformed rename path inventory.") from exc
    return sorted(set(names))


def integrate_patches(worktree: str | Path, patches: list[dict], base_sha: str) -> dict:
    """Preflight every patch in order, then apply one aggregate patch to a clean tree.

    Clean sequential changes to the same path are supported for dependencies and
    reported in ``overlaps``. A conflict, invalid hash, undeclared file, ignored
    collision, or dirty integration checkout fails before any real file/index
    changes. Temporary indices and checkouts are private disposable artifacts.
    """
    root = _task_root(worktree)
    _sha(root, base_sha)
    _no_operations(root)
    _clean(root)
    before = inventory(root, base_sha)
    if _git(root, "merge-base", "--is-ancestor", base_sha, before["head"], check=False).returncode:
        raise GitError("BASE_MISMATCH", "The integration checkout does not descend from the reviewed base.")
    if not isinstance(patches, list) or not patches:
        raise GitError("EMPTY_PATCH_SET", "At least one reviewed patch is required.")
    checked = []
    union: set[str] = set()
    overlaps: set[str] = set()
    for item in patches:
        if not isinstance(item, dict) or not {"path", "sha256", "allowed_paths"} <= item.keys():
            raise GitError("INVALID_PATCH_RECORD", "A patch needs its path, hash and exact reviewed file scope.")
        allowed = _paths(root, item["allowed_paths"])
        source = _exact_location(item["path"])
        if source.is_symlink() or not source.is_file():
            raise GitError("INVALID_PATCH", "The reviewed patch must be a regular file.")
        payload = source.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item["sha256"]:
            raise GitError("PATCH_HASH_MISMATCH", "A patch changed after review.", {"path": str(source)})
        names = _paths(root, _patch_paths(root, payload))
        if not names:
            raise GitError("EMPTY_PATCH", "Every reviewed patch must change at least one file.")
        unexpected = sorted(set(names) - set(allowed))
        if unexpected:
            raise GitError("OUT_OF_SCOPE", "A patch modifies files outside its reviewed scope.", {"files": unexpected})
        overlaps.update(union & set(names))
        union.update(names)
        checked.append((source, payload, names))
    with tempfile.TemporaryDirectory(prefix="delivery-git-preflight-") as temporary:
        folder = Path(temporary).resolve()
        scratch = folder / "worktree"
        scratch.mkdir()
        env = {"GIT_INDEX_FILE": str(folder / "index"), "GIT_WORK_TREE": str(scratch),
               "GIT_DIR": _text(root, "rev-parse", "--absolute-git-dir")}
        _git(root, "read-tree", before["head"], env=env)
        _git(scratch, "checkout-index", "--all", "--index", env=env)
        previous_tree = _text(scratch, "write-tree", env=env)
        for source, payload, names in checked:
            applied = _git(scratch, "apply", "--index", "--binary", "--whitespace=nowarn", "-",
                           data=payload, env=env, check=False)
            if applied.returncode:
                raise GitError("PATCH_CONFLICT", "Reviewed patches cannot be applied together; the integration checkout was preserved.",
                               {"path": str(source), "reason": os.fsdecode(applied.stderr).strip(),
                                "overlaps": sorted(overlaps)})
            actual = _diff_names(scratch, previous_tree, cached=True, env=env)
            if actual != names:
                raise GitError("PATCH_INVENTORY_MISMATCH", "The applied patch changed an unexpected set of files.",
                               {"expected": names, "actual": actual})
            entries = _index_entries(scratch, env=env)
            if any(record["mode"] == "160000" for name in actual for record in entries.get(name, [])):
                raise GitError("UNSUPPORTED_FILE_TYPE", "Submodule changes require a separate explicit integration workflow.")
            previous_tree = _text(scratch, "write-tree", env=env)
        aggregate = _git(scratch, "diff", "--cached", "--binary", "--full-index", "--no-renames",
                         "--no-ext-diff", "--no-textconv", before["head"], "--", *sorted(union), env=env).stdout
        if not aggregate:
            raise GitError("EMPTY_PATCH_SET", "The reviewed patches have no final change.")
        _ignored_collisions(root, previous_tree)
        _no_operations(root)
        _clean(root)
        if inventory(root, base_sha)["fingerprint"] != before["fingerprint"]:
            raise GitError("WORKTREE_CHANGED", "The integration checkout changed during patch preflight.")
        result = _git(root, "apply", "--check", "--index", "--binary", "-", data=aggregate, check=False)
        if result.returncode:
            raise GitError("PATCH_CONFLICT", "The aggregate patch cannot be applied; the integration checkout was preserved.",
                           {"reason": os.fsdecode(result.stderr).strip()})
        # Without --reject, Git checks the complete patch before updating the
        # worktree and takes the index lock for the single aggregate application.
        _git(root, "apply", "--index", "--binary", "--whitespace=nowarn", "-", data=aggregate)
    after = inventory(root, base_sha)
    if set(after["staged"]) - union or after["unstaged"] or after["untracked"]:
        raise GitError("WORKTREE_CHANGED", "Unexpected changes appeared during integration; preserve and inspect the checkout.", after)
    after["overlaps"] = sorted(overlaps)
    after["patches"] = [{"path": str(source), "sha256": hashlib.sha256(payload).hexdigest(), "files": names}
                        for source, payload, names in checked]
    return after


def _artifact_inventory(root: Path) -> list[str]:
    """Find all untracked artifacts, including ignored files and empty directories."""
    tracked = set(_names(root, "ls-files", "-z"))
    expected_dirs = {str(parent) for name in tracked for parent in PurePosixPath(name).parents
                     if str(parent) != "."}
    artifacts = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        if current == root:
            dirs[:] = [name for name in dirs if name != ".git"]
            files = [name for name in files if name != ".git"]
        for name in dirs[:]:
            candidate = current / name
            relative = candidate.relative_to(root).as_posix()
            if candidate.is_symlink():
                dirs.remove(name)
                if relative not in tracked:
                    artifacts.append(relative)
            elif relative not in expected_dirs:
                artifacts.append(relative + "/")
                dirs.remove(name)
        for name in files:
            relative = (current / name).relative_to(root).as_posix()
            if relative not in tracked:
                artifacts.append(relative)
    return sorted(set(artifacts))


def remove_worktree(primary: str | Path, path: str | Path,
                    expected_branch: str, expected_head: str) -> dict:
    root = canonical_repo(primary)
    target = _exact_location(path)
    if target == root:
        raise GitError("PRIMARY_WRITE", "The primary checkout cannot be removed.")
    registered = {Path(item["worktree"]).resolve(): item for item in _worktrees(root)}
    if target not in registered or canonical_repo(target) != root or _root(target) != target:
        raise GitError("WORKTREE_IDENTITY", "This is not the exact registered worktree owned by the task.")
    _sha(target, expected_head)
    if _branch(target) != expected_branch or _head(target) != expected_head:
        raise GitError("WORKTREE_IDENTITY", "The worktree branch or HEAD differs from the task's ownership record.")
    if registered[target].get("locked"):
        raise GitError("WORKTREE_LOCKED", "The task worktree is locked; its owner must unlock it explicitly.")
    _no_operations(target)
    _clean(target, ignored=True)
    artifacts = _artifact_inventory(target)
    if artifacts:
        raise GitError("WORKTREE_ARTIFACTS", "The worktree contains artifacts, including possibly empty directories, that must be preserved.",
                       {"files": artifacts})
    _no_operations(target)
    _clean(target, ignored=True)
    if _branch(target) != expected_branch or _head(target) != expected_head:
        raise GitError("WORKTREE_IDENTITY", "The worktree identity changed during the cleanup checks.")
    _git(root, "worktree", "remove", "--", str(target))
    return {"primary": str(root), "worktree": str(target), "branch": expected_branch,
            "head": expected_head, "removed": True, "branch_preserved": True}


def rebase_worktree(worktree: str | Path, remote: str = "origin", base: str = "main") -> dict:
    root = _task_root(worktree)
    _no_operations(root)
    _clean(root)
    previous = _head(root)
    ref, base_sha = _fetch(root, remote, base)
    _ignored_collisions(root, base_sha)
    _no_operations(root)
    _clean(root)
    result = _git(root, "-c", "rebase.autoStash=false", "rebase", "--no-autostash", ref, check=False)
    if result.returncode:
        raise GitError("REBASE_CONFLICT" if _operations(root) else "REBASE_FAILED",
                       "Rebase needs inspection and explicit conflict resolution; no resolution or abort was attempted.",
                       {"worktree": str(root), "base_sha": base_sha, "previous_head": previous,
                        "reason": os.fsdecode(result.stderr).strip(), "status": _status(root),
                        "in_progress": _operations(root)})
    if _ref_sha(root, ref) != base_sha:
        raise GitError("BASE_CHANGED", "The remote base changed during rebase; repeat the base check and validation.")
    result = inspect_repo(root)
    result.update({"previous_head": previous, "base_sha": base_sha, "base": base, "remote": remote})
    return result


def commit_changes(worktree: str | Path, paths: Iterable[str], message: str) -> dict:
    """Stage and commit only the declared files on a task branch.

    The caller supplies an English change description. No attribution trailers
    are added, and agent names are rejected in the supplied history text.
    """
    root = _task_root(worktree)
    _no_operations(root)
    allowed = _paths(root, paths)
    if not isinstance(message, str) or not message.strip() or "\0" in message or _AGENT_NAME.search(message):
        raise GitError("HISTORY_POLICY", "Use a nonempty English change description without agent attribution.")
    state = _status(root)
    _visible(state)
    _scope(root, allowed, state)
    if state["unmerged"]:
        raise GitError("UNMERGED_INDEX", "Resolve existing index conflicts before committing.")
    if not allowed or not (state["staged"] or state["unstaged"] or state["untracked"]):
        raise GitError("NO_CHANGES", "There are no declared changes to commit.")
    previous = _head(root)
    indexed = _index_entries(root)
    # A deletion already staged has no remaining index entry to give to add.
    # Its path remains in the exact commit scope through HEAD.
    to_stage = [name for name in allowed if name in indexed or (root / name).exists()
                or (root / name).is_symlink()]
    if to_stage:
        _git(root, "add", "--", *to_stage)
    staged = _status(root)
    _scope(root, allowed, staged)
    if staged["unstaged"] or staged["untracked"] or not staged["staged"]:
        raise GitError("WORKTREE_CHANGED", "Files changed while preparing the exact commit; inspect the index.")
    tree = _text(root, "write-tree")
    # --only constrains the commit even if another process stages a different
    # file after our inventory check. Every path is passed after -- literally.
    _git(root, "commit", "--only", "-m", message, "--", *allowed)
    head = _head(root)
    actual = _diff_names(root, previous, head)
    if set(actual) - set(allowed) or _text(root, "rev-parse", head + "^{tree}") != tree:
        raise GitError("COMMIT_CHANGED", "A hook or concurrent edit changed the committed tree; review the actual commit.",
                       {"head": head, "files": actual})
    return {"primary": str(canonical_repo(root)), "worktree": str(root), "branch": _branch(root),
            "head": head, "previous_head": previous, "tree": tree, "files": actual,
            "status": _status(root)}


def _remote_head(root: Path, remote: str, branch: str) -> str | None:
    ref = f"refs/heads/{branch}"
    result = _git(root, "ls-remote", "--heads", remote, ref)
    values = [line.split(b"\t", 1) for line in result.stdout.splitlines()]
    matches = [sha.decode("ascii") for sha, name in values if os.fsdecode(name) == ref]
    if len(matches) > 1:
        raise GitError("REMOTE_IDENTITY", "The remote returned more than one exact branch match.")
    return matches[0] if matches else None


def push_branch(worktree: str | Path, expected_remote_head: str | None = None) -> dict:
    """Publish only the exact feature ref, with an explicit expected old value.

    None means an initial push: an empty lease requires the remote ref not to
    exist. Updating an existing branch requires its complete expected old SHA.
    This primitive does not grant publication authority to its caller.
    """
    root = _task_root(worktree)
    _no_operations(root)
    _clean(root)
    remote_url = _remote(root, "origin")
    push_urls = os.fsdecode(_git(root, "remote", "get-url", "--push", "--all", "origin").stdout).splitlines()
    if push_urls != [remote_url]:
        raise GitError("REMOTE_PUSH_URL_MISMATCH", "Publication requires one push URL matching the inspected origin URL.")
    branch = _branch(root)
    assert branch is not None
    head = _head(root)
    if expected_remote_head is not None and (not isinstance(expected_remote_head, str)
                                             or not _SHA.fullmatch(expected_remote_head)):
        raise GitError("INVALID_SHA", "An update requires the complete expected remote head SHA.")
    actual = _remote_head(root, "origin", branch)
    if actual != expected_remote_head:
        raise GitError("REMOTE_BRANCH_EXISTS" if expected_remote_head is None else "REMOTE_CHANGED",
                       "The remote feature branch does not match the explicit publication lease.",
                       {"expected_remote_head": expected_remote_head, "remote_head": actual})
    _no_operations(root)
    _clean(root)
    if _head(root) != head or _branch(root) != branch:
        raise GitError("HEAD_CHANGED", "The feature branch changed before publication.")
    ref = f"refs/heads/{branch}"
    result = _git(root, "push", "--porcelain", f"--force-with-lease={ref}:{expected_remote_head or ''}",
                  "origin", f"{head}:{ref}", check=False)
    if result.returncode:
        raise GitError("PUSH_REJECTED", "The feature push was rejected; inspect its explicit lease and remote protection.",
                       {"reason": os.fsdecode(result.stderr).strip(), "remote_head": _remote_head(root, "origin", branch)})
    if _remote_head(root, "origin", branch) != head:
        raise GitError("REMOTE_CHANGED", "The remote feature branch changed immediately after publication.")
    _git(root, "branch", "--set-upstream-to", f"origin/{branch}", branch)
    return {"primary": str(canonical_repo(root)), "worktree": str(root), "branch": branch,
            "head": head, "remote": "origin", "remote_head": head,
            "expected_remote_head": expected_remote_head}
