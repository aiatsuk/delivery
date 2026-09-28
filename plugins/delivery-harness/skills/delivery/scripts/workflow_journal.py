"""Host-written workflow journals as agent provenance.

A workflow host writes ``<host-root>/<project>/<session>/subagents/workflows/<runId>/journal.jsonl``
with ``started``, ``result`` and ``failed`` lines, and keeps each agent's transcript as
``agent-<agentId>.jsonl`` beside the journal. The host root is ``DELIVERY_WORKFLOW_HOST_ROOT``
when set (tests), else ``~/.claude/projects``, resolved; a run records it when it is created and
passes it here. Only journals inside it count: a search always covers that whole root, and a named
journal elsewhere is refused. The host, not the coordinator, supplies the agent identity. A result counts only after an earlier ``started`` line for the same key and agent, and
only while its transcript exists.

This is provenance, not authentication: anyone who can write under the host root can forge a
journal. A named journal behind a symlink anywhere below the host root is refused; a search
skips symlinked paths. A named journal does not hide other results: the whole host root is
scanned as well, so a second result for the same match is still seen.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

DEFAULT_SEARCH_ROOT = "~/.claude/projects"
HOST_ROOT_ENV = "DELIVERY_WORKFLOW_HOST_ROOT"
PATTERN = "*/*/subagents/workflows/wf_*/journal.jsonl"
RUN_ID = re.compile(r"wf_[A-Za-z0-9-]{1,64}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _engine():
    import run_engine
    return run_engine


def host_root() -> Path:
    return Path(os.environ.get(HOST_ROOT_ENV) or DEFAULT_SEARCH_ROOT).expanduser().resolve()


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while current != stop and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _named(journal, host: Path) -> Path:
    e = _engine()
    path = Path(os.path.abspath(Path(journal).expanduser()))
    e.require(_within(path.resolve(), host), "journal_outside_host", f"A workflow journal must be inside the host projects root {host}.")
    # Every component below the host root must be a real directory or file; the host root
    # itself may be reached through an alias, because that alias still names the host's files.
    current = path
    while current.resolve() != host:
        e.require(current != current.parent, "journal_outside_host", f"A workflow journal must be inside the host projects root {host}.")
        e.require(not current.is_symlink(), "journal_symlink", f"Refusing a symlinked workflow journal path: {current}")
        current = current.parent
    e.require(path.is_file(), "journal_missing", "The selected workflow journal is not a regular file.")
    return path.resolve()


def _search(root: Path) -> list[Path]:
    # A search never follows a symlinked project, session, run directory or journal: such
    # paths are skipped, so they can neither supply a result nor break unrelated imports.
    return [path for path in sorted(root.glob(PATTERN)) if not _has_symlink(path, root)]


def _journals(journal, search_root, host: Path) -> list[Path]:
    e = _engine()
    if journal is not None:
        return [_named(journal, host)]
    if search_root is not None:
        root = Path(search_root).expanduser().resolve()
        e.require(_within(root, host), "journal_outside_host", f"A journal search root must be the host projects root {host}.")
        e.require(root == host, "search_root_mismatch", f"A journal search covers the whole host projects root {host}; a narrower search root could hide a second result.")
    e.require(host.is_dir(), "journal_missing", "The workflow host projects root is not a directory.")
    return _search(host)


def _entries(path: Path):
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield line, entry


def _scan(path: Path, match: Callable[[dict], Any], host: Path) -> list[dict]:
    e = _engine()
    started, found = {}, []
    for line, entry in _entries(path):
        key, agent = entry.get("key"), entry.get("agentId")
        if entry.get("type") == "started" and isinstance(key, str) and isinstance(agent, str):
            started.setdefault((key, agent), entry)
        if entry.get("type") != "result" or not isinstance(entry.get("result"), dict) or not match(entry["result"]):
            continue
        run_id = path.parent.name
        e.require(RUN_ID.fullmatch(run_id), "journal_identity", f"The workflow run directory is not a valid run ID: {run_id}")
        e.require(isinstance(key, str) and key and isinstance(agent, str) and AGENT_ID.fullmatch(agent), "journal_identity", "A workflow result must carry a key and a valid agentId.")
        begin = started.get((key, agent))
        e.require(begin is not None, "journal_order", "A workflow result has no earlier started line for the same key and agent in its journal.")
        transcript = path.parent / f"agent-{agent}.jsonl"
        e.require(transcript.is_file() and not transcript.is_symlink(), "journal_transcript_missing", f"The workflow agent transcript is missing or not a regular file: {transcript.name}")
        found.append({"result": entry["result"], "journal": str(path), "run_id": run_id, "agent_id": agent, "key": key,
                      "label": begin.get("label") if isinstance(begin.get("label"), str) else None,
                      "line_sha256": hashlib.sha256(line).hexdigest(),
                      "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest(), "host_root": str(host)})
    return found


def find_results(match: Callable[[dict], Any], *, journal=None, search_root=None, host=None) -> list[dict]:
    """Return every matching agent result with its journal line and transcript hashes.

    ``host`` is the run's recorded host root (default: the effective one). With a named journal
    the whole host root is scanned too, so another matching result elsewhere is returned as well
    and the caller sees it instead of a hand-picked journal.
    """
    host = Path(host).resolve() if host is not None else host_root()
    paths = _journals(journal, search_root, host)
    found = [item for path in paths for item in _scan(path, match, host)]
    if journal is not None and host.is_dir():
        seen = {(item["journal"], item["line_sha256"]) for item in found}
        found += [item for path in _search(host) for item in _scan(path, match, host) if (item["journal"], item["line_sha256"]) not in seen]
    return found


def observe_dispatch(label: str, dispatch_id: str, *, since: float | None = None, host=None) -> dict:
    """Record what the host journals show for an unreturned workflow dispatch, without judging it.

    ``failed`` lists failed agents whose started line carries ``label``; ``results`` lists result
    lines naming ``dispatch_id``. Journals last written before ``since`` (epoch seconds) are skipped.
    ``host`` is the run's recorded host root (default: the effective one).
    """
    host = Path(host).resolve() if host is not None else host_root()
    failed, results = [], []
    for path in (_search(host) if host.is_dir() else []):
        if since is not None and path.stat().st_mtime < since:
            continue
        labels = {}
        for line, entry in _entries(path):
            key, agent = entry.get("key"), entry.get("agentId")
            if entry.get("type") == "started" and isinstance(key, str) and isinstance(agent, str):
                labels.setdefault((key, agent), entry.get("label"))
            record = {"journal": str(path), "run_id": path.parent.name, "agent_id": agent, "key": key, "line_sha256": hashlib.sha256(line).hexdigest()}
            if entry.get("type") == "failed" and (entry.get("label") or labels.get((key, agent))) == label:
                failed.append({**record, "label": label})
            if entry.get("type") == "result" and isinstance(entry.get("result"), dict) and entry["result"].get("dispatch_id") == dispatch_id:
                results.append(record)
    return {"host_root": str(host), "label": label, "failed": failed, "results": results, "result_present": bool(results)}
