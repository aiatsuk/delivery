"""Host-written workflow journals as agent provenance.

A workflow host writes ``<search-root>/<project>/<session>/subagents/workflows/<runId>/journal.jsonl``
(default search root ``~/.claude/projects``) with ``started``, ``result`` and ``failed`` lines,
and keeps each agent's transcript as ``agent-<agentId>.jsonl`` beside the journal. The host,
not the coordinator, supplies the agent identity. A result counts only after an earlier
``started`` line for the same key and agent, and only while its transcript exists.

This is provenance, not authentication: anyone who can write under the search root can
forge a journal. An explicitly named journal behind a symlink is refused; a search skips
symlinked paths.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable

DEFAULT_SEARCH_ROOT = "~/.claude/projects"
PATTERN = "*/*/subagents/workflows/wf_*/journal.jsonl"
RUN_ID = re.compile(r"wf_[A-Za-z0-9-]{1,64}")
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _engine():
    import run_engine
    return run_engine


def _refuse_symlinks(path: Path, stop: Path) -> None:
    e = _engine()
    current = path
    while current != stop and current != current.parent:
        e.require(not current.is_symlink(), "journal_symlink", f"Refusing a symlinked workflow journal path: {current}")
        current = current.parent


def _has_symlink(path: Path, stop: Path) -> bool:
    current = path
    while current != stop and current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


def _journals(journal, search_root) -> list[Path]:
    e = _engine()
    if journal is not None:
        path = Path(journal).expanduser().absolute()
        _refuse_symlinks(path, path.parent.parent)
        e.require(path.is_file(), "journal_missing", "The selected workflow journal is not a regular file.")
        return [path.resolve()]
    root = Path(search_root or DEFAULT_SEARCH_ROOT).expanduser().resolve()
    e.require(root.is_dir(), "journal_missing", "The workflow journal search root is not a directory.")
    # A search never follows a symlinked project, session, run directory or journal: such
    # paths are skipped, so they can neither supply a result nor break unrelated imports.
    return [path for path in sorted(root.glob(PATTERN)) if not _has_symlink(path, root)]


def _scan(path: Path, match: Callable[[dict], Any]) -> list[dict]:
    e = _engine()
    started, found = {}, []
    for line in path.read_bytes().split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
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
                      "transcript_sha256": hashlib.sha256(transcript.read_bytes()).hexdigest()})
    return found


def find_results(match: Callable[[dict], Any], *, journal=None, search_root=None) -> list[dict]:
    """Return every matching agent result with its journal line and transcript hashes."""
    return [item for path in _journals(journal, search_root) for item in _scan(path, match)]
