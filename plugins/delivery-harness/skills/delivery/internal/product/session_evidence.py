#!/usr/bin/env python3
"""Bounded, read-only evidence lookup for native Codex, Claude, and Grok sessions.

The command intentionally has no write path.  It validates a selected Product Workspace
binding before it traverses fixed provider roots, emits JSON only, and never includes an
absolute path, the search query, raw records, or exception text in its result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence


SCHEMA_VERSION = 1
SUPPORTED_HOSTS = ("claude", "codex", "grok")
SUPPORTED_SCOPES = ("messages", "messages-and-tools")


@dataclass(frozen=True)
class Limits:
    binding_bytes: int = 64 * 1024
    resolution_entries: int = 100_000
    resolution_seconds: float = 15.0
    auxiliary_file_bytes: int = 8 * 1024 * 1024
    auxiliary_file_records: int = 10_000
    auxiliary_record_bytes: int = 1 * 1024 * 1024
    auxiliary_file_seconds: float = 5.0
    auxiliary_total_bytes: int = 32 * 1024 * 1024
    auxiliary_total_records: int = 30_000
    query_characters: int = 512
    search_bytes: int = 512 * 1024 * 1024
    search_records: int = 500_000
    search_seconds: float = 30.0
    search_record_bytes: int = 16 * 1024 * 1024
    field_bytes: int = 8 * 1024 * 1024
    matches: int = 20
    fragment_characters: int = 2_000


@dataclass
class EvidenceError(Exception):
    code: str
    outcome: str = "error"
    exit_code: int = 5
    source_state: str = "not_read"
    coverage_state: str = "not_applicable"
    sources: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Revision:
    device: int
    inode: int
    starting_size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> "Revision":
        return cls(
            device=int(value.st_dev),
            inode=int(value.st_ino),
            starting_size=int(value.st_size),
            mtime_ns=int(value.st_mtime_ns),
        )

    def as_json(self) -> dict[str, int]:
        return {
            "device": self.device,
            "inode": self.inode,
            "starting_size": self.starting_size,
            "mtime_ns": self.mtime_ns,
        }


@dataclass
class Candidate:
    host: str
    session_id: str
    message_path: Path
    root_path: Path
    root_id: str
    identity: tuple[int, int]
    extra_sources: list[tuple[str, Path, str, Path]] = field(default_factory=list)
    guard_revisions: list[tuple[Path, Revision]] = field(default_factory=list)


@dataclass
class ResolutionBudget:
    limits: Limits
    started: float = field(default_factory=time.monotonic)
    entries: int = 0
    auxiliary_bytes: int = 0
    auxiliary_records: int = 0

    def check(self) -> None:
        if time.monotonic() - self.started > self.limits.resolution_seconds:
            raise EvidenceError("resolution_truncated", exit_code=3)

    def entry(self) -> None:
        self.check()
        self.entries += 1
        if self.entries > self.limits.resolution_entries:
            raise EvidenceError("resolution_truncated", exit_code=3)

    def consume_auxiliary(self, byte_count: int, record_count: int = 0) -> None:
        self.check()
        self.auxiliary_bytes += byte_count
        self.auxiliary_records += record_count
        if (
            self.auxiliary_bytes > self.limits.auxiliary_total_bytes
            or self.auxiliary_records > self.limits.auxiliary_total_records
        ):
            raise EvidenceError("resolution_truncated", exit_code=3)


@dataclass
class PrefixRecord:
    line: int
    complete_through_byte: int
    raw: Optional[bytes]
    oversized: bool = False


@dataclass
class PrefixScan:
    complete_through_line: int
    complete_through_byte: int
    records_scanned: int
    limit_reasons: list[str]
    unterminated_tail: bool


@dataclass
class TextField:
    role: str
    text: str
    block_index: int
    revision_key: Optional[str] = None
    canonical_message: bool = True


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # pragma: no cover - message is intentionally dropped
        raise EvidenceError("invalid_request", exit_code=2)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_revision() -> dict[str, None]:
    return {"device": None, "inode": None, "starting_size": None, "mtime_ns": None}


def empty_coverage() -> dict[str, Any]:
    return {
        "state": "not_applicable",
        "records_scanned": 0,
        "complete_through_line": None,
        "complete_through_byte": None,
        "malformed_records": 0,
        "unknown_records": 0,
        "oversized_records": 0,
        "oversized_fields": 0,
        "limit_reasons": [],
    }


def empty_diagnostics() -> dict[str, Any]:
    return {
        "duration_ms": 0,
        "entries_scanned": 0,
        "records_scanned": 0,
        "candidates_found": 0,
        "matches_returned": 0,
        "redaction_count": 0,
    }


def base_result(command: Optional[str], host: Optional[str], session_id: Optional[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error",
        "code": "internal_error",
        "outcome": "error",
        "host": host,
        "session_id": session_id,
        "observed_at": utc_now(),
        "binding_mode": None,
        "source_state": "not_read",
        "revision": empty_revision(),
        "sources": [],
        "coverage": empty_coverage(),
        "matches": [],
        "diagnostics": empty_diagnostics(),
    }


def canonical_session_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError, TypeError) as exc:
        raise EvidenceError("invalid_request", exit_code=2) from exc
    canonical = str(parsed)
    if value != canonical:
        raise EvidenceError("invalid_request", exit_code=2)
    return canonical


ISO_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
RELATED_WORK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*(?:, ?[A-Za-z0-9][A-Za-z0-9._/-]*)*$")

BINDING_KEYS = (
    "Schema",
    "Product",
    "Host",
    "Session ID",
    "Binding",
    "Related work",
    "Last verified",
    "Source state",
    "Coverage state",
)


def _regular_nonsymlink(path: Path, *, directory: bool = False) -> os.stat_result:
    try:
        value = path.lstat()
    except PermissionError as exc:
        raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc
    except OSError as exc:
        raise EvidenceError("invalid_request", exit_code=2) from exc
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if stat.S_ISLNK(value.st_mode) or not expected:
        raise EvidenceError("invalid_request", exit_code=2)
    return value


def validate_product_binding(
    product_root_arg: str,
    host: str,
    session_id: str,
    allow_unbound: bool,
    limits: Limits,
) -> tuple[Path, str]:
    root = Path(product_root_arg).expanduser()
    _regular_nonsymlink(root, directory=True)
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise EvidenceError("invalid_request", exit_code=2) from exc
    product_file = root / "product.md"
    _regular_nonsymlink(product_file)

    sessions_dir = root / "sessions"
    binding = sessions_dir / f"{host}-{session_id}.md"
    # An explicit one-time scope does not excuse an unsafe binding directory.
    # Inspect the directory before the binding so a dangling symlink cannot
    # disguise itself as an absent optional binding.
    try:
        sessions_info = sessions_dir.lstat()
    except FileNotFoundError:
        if allow_unbound:
            return root, "unbound"
        raise EvidenceError("binding_required", exit_code=5)
    except OSError as exc:
        raise EvidenceError("binding_invalid", exit_code=5) from exc
    if stat.S_ISLNK(sessions_info.st_mode) or not stat.S_ISDIR(sessions_info.st_mode):
        raise EvidenceError("binding_invalid", exit_code=5)
    try:
        binding_info = binding.lstat()
    except FileNotFoundError:
        if allow_unbound:
            return root, "unbound"
        raise EvidenceError("binding_required", exit_code=5)
    except PermissionError as exc:
        raise EvidenceError("binding_invalid", exit_code=5) from exc
    except OSError as exc:
        raise EvidenceError("binding_invalid", exit_code=5) from exc

    try:
        sessions_info = sessions_dir.lstat()
    except OSError as exc:
        raise EvidenceError("binding_invalid", exit_code=5) from exc
    if (
        stat.S_ISLNK(sessions_info.st_mode)
        or not stat.S_ISDIR(sessions_info.st_mode)
        or stat.S_ISLNK(binding_info.st_mode)
        or not stat.S_ISREG(binding_info.st_mode)
        or binding_info.st_size > limits.binding_bytes
    ):
        raise EvidenceError("binding_invalid", exit_code=5)

    descriptor: Optional[int] = None
    try:
        descriptor = open_readonly_nofollow(binding)
        start = os.fstat(descriptor)
        if (
            int(start.st_dev) != int(binding_info.st_dev)
            or int(start.st_ino) != int(binding_info.st_ino)
            or int(start.st_size) > limits.binding_bytes
        ):
            raise EvidenceError("binding_invalid", exit_code=5)
        data = bytearray()
        while len(data) <= limits.binding_bytes:
            chunk = os.read(
                descriptor,
                min(64 * 1024, limits.binding_bytes + 1 - len(data)),
            )
            if not chunk:
                break
            data.extend(chunk)
        end = os.fstat(descriptor)
        current_binding = binding.lstat()
        current_sessions = sessions_dir.lstat()
        if (
            len(data) > limits.binding_bytes
            or int(end.st_size) != int(start.st_size)
            or int(end.st_mtime_ns) != int(start.st_mtime_ns)
            or int(current_binding.st_dev) != int(start.st_dev)
            or int(current_binding.st_ino) != int(start.st_ino)
            or stat.S_ISLNK(current_binding.st_mode)
            or int(current_sessions.st_dev) != int(sessions_info.st_dev)
            or int(current_sessions.st_ino) != int(sessions_info.st_ino)
            or stat.S_ISLNK(current_sessions.st_mode)
        ):
            raise EvidenceError("binding_invalid", exit_code=5)
        raw = bytes(data)
        text = raw.decode("utf-8", errors="strict")
    except (EvidenceError, OSError, UnicodeError, ValueError) as exc:
        raise EvidenceError("binding_invalid", exit_code=5) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > limits.binding_bytes or "\x00" in text:
        raise EvidenceError("binding_invalid", exit_code=5)

    lines = text.splitlines()
    if not lines or lines[0] != "# Session binding":
        raise EvidenceError("binding_invalid", exit_code=5)
    metadata_lines: list[str] = []
    for line in lines[1:]:
        if line == "##" or line.startswith("## "):
            break
        if line.strip():
            metadata_lines.append(line)
    if len(metadata_lines) != len(BINDING_KEYS):
        raise EvidenceError("binding_invalid", exit_code=5)

    values: dict[str, str] = {}
    for expected_key, line in zip(BINDING_KEYS, metadata_lines):
        prefix = f"- {expected_key}: "
        if not line.startswith(prefix):
            raise EvidenceError("binding_invalid", exit_code=5)
        value = line[len(prefix) :]
        if not value or expected_key in values:
            raise EvidenceError("binding_invalid", exit_code=5)
        values[expected_key] = value

    if (
        values["Schema"] != "product-session-binding/v1"
        or values["Product"] != root.name
        or values["Host"] != host
        or values["Session ID"] != session_id
    ):
        raise EvidenceError("binding_invalid", exit_code=5)
    if values["Binding"] == "revoked":
        raise EvidenceError("binding_revoked", exit_code=5)
    if values["Binding"] != "linked":
        raise EvidenceError("binding_invalid", exit_code=5)
    if values["Related work"] != "none":
        related_items = [item.strip() for item in values["Related work"].split(",")]
        if "none" in related_items or not RELATED_WORK_RE.fullmatch(values["Related work"]):
            raise EvidenceError("binding_invalid", exit_code=5)
    if values["Last verified"] != "never":
        if not ISO_TIMESTAMP_RE.fullmatch(values["Last verified"]):
            raise EvidenceError("binding_invalid", exit_code=5)
        try:
            datetime.fromisoformat(values["Last verified"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise EvidenceError("binding_invalid", exit_code=5) from exc
    if values["Source state"] not in {
        "stable", "appended", "truncated", "replaced", "modified", "unavailable", "unknown"
    }:
        raise EvidenceError("binding_invalid", exit_code=5)
    if values["Coverage state"] not in {
        "complete", "partial_parse", "limit_truncated", "unavailable", "unknown"
    }:
        raise EvidenceError("binding_invalid", exit_code=5)
    return root, "linked"


def open_readonly_nofollow(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except PermissionError as exc:
        raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc
    except FileNotFoundError as exc:
        raise EvidenceError("source_unavailable", "source_unavailable", 4, "unavailable", "unavailable") from exc
    except OSError as exc:
        raise EvidenceError("unsupported_format", exit_code=5) from exc
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        os.close(descriptor)
        raise EvidenceError("unsupported_format", exit_code=5)
    return descriptor


def open_candidate_message(candidate: Candidate) -> int:
    """Open the exact inode validated during resolution or fail without content."""
    for path, revision in candidate.guard_revisions:
        guard = open_readonly_nofollow(path)
        try:
            state = source_state_after_read(guard, path, revision)
        finally:
            os.close(guard)
        if state != "stable":
            raise EvidenceError(
                "resolution_truncated",
                exit_code=3,
                source_state=state,
                coverage_state="unavailable",
            )
    descriptor = open_readonly_nofollow(candidate.message_path)
    info = os.fstat(descriptor)
    if (int(info.st_dev), int(info.st_ino)) != candidate.identity:
        os.close(descriptor)
        raise EvidenceError(
            "source_unavailable",
            "source_unavailable",
            4,
            "replaced",
            "unavailable",
        )
    return descriptor


def _read_auxiliary_jsonl(
    path: Path,
    budget: ResolutionBudget,
    handler: Callable[[dict[str, Any], int], bool],
    *,
    require_eof: bool,
) -> Revision:
    limits = budget.limits
    descriptor = open_readonly_nofollow(path)
    file_started = time.monotonic()
    file_bytes = 0
    file_records = 0
    pending = bytearray()
    reached_eof = False
    stopped = False
    try:
        start = os.fstat(descriptor)
        revision = Revision.from_stat(start)
        while file_bytes < start.st_size:
            budget.check()
            if time.monotonic() - file_started > limits.auxiliary_file_seconds:
                raise EvidenceError("resolution_truncated", exit_code=3)
            if file_bytes >= limits.auxiliary_file_bytes:
                raise EvidenceError("resolution_truncated", exit_code=3)
            amount = min(64 * 1024, start.st_size - file_bytes, limits.auxiliary_file_bytes - file_bytes)
            chunk = os.read(descriptor, amount)
            if not chunk:
                break
            file_bytes += len(chunk)
            budget.consume_auxiliary(len(chunk))
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    if len(pending) > limits.auxiliary_record_bytes:
                        raise EvidenceError("resolution_truncated", exit_code=3)
                    break
                record = bytes(pending[:newline])
                del pending[: newline + 1]
                file_records += 1
                budget.consume_auxiliary(0, 1)
                if file_records > limits.auxiliary_file_records or len(record) > limits.auxiliary_record_bytes:
                    raise EvidenceError("resolution_truncated", exit_code=3)
                if not record.strip():
                    continue
                try:
                    parsed = json.loads(record.decode("utf-8", errors="strict"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise EvidenceError("unsupported_format", exit_code=5) from exc
                if not isinstance(parsed, dict):
                    raise EvidenceError("unsupported_format", exit_code=5)
                if handler(parsed, file_records):
                    stopped = True
                    break
            if stopped:
                break
        reached_eof = file_bytes == start.st_size
        if require_eof:
            if not reached_eof or pending:
                raise EvidenceError("resolution_truncated", exit_code=3)
            state = source_state_after_read(descriptor, path, revision)
            if state != "stable":
                raise EvidenceError(
                    "resolution_truncated",
                    exit_code=3,
                    source_state=state,
                    coverage_state="unavailable",
                )
        return revision
    finally:
        os.close(descriptor)


def _read_bounded_json_file(path: Path, budget: ResolutionBudget) -> Optional[Any]:
    """Read an optional Claude index hint. Invalid/over-limit hints are ignored."""
    limits = budget.limits
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        if info.st_size > limits.auxiliary_file_bytes:
            return None
        if budget.auxiliary_bytes + info.st_size > limits.auxiliary_total_bytes:
            return None
        descriptor = open_readonly_nofollow(path)
    except EvidenceError:
        return None
    started = time.monotonic()
    data = bytearray()
    try:
        while len(data) < info.st_size:
            if (
                time.monotonic() - started > limits.auxiliary_file_seconds
                or time.monotonic() - budget.started > limits.resolution_seconds
            ):
                return None
            chunk = os.read(descriptor, min(64 * 1024, info.st_size - len(data)))
            if not chunk:
                return None
            data.extend(chunk)
        budget.consume_auxiliary(len(data), 1)
        return json.loads(bytes(data).decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError, EvidenceError):
        return None
    finally:
        os.close(descriptor)


def _check_root(root: Path) -> bool:
    try:
        info = root.lstat()
    except FileNotFoundError:
        return False
    except PermissionError as exc:
        raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc
    except OSError as exc:
        raise EvidenceError("source_unavailable", "source_unavailable", 4, "unavailable", "unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise EvidenceError("unsupported_format", exit_code=5)
    return True


def _walk_files(root: Path, budget: ResolutionBudget) -> Iterable[Path]:
    if not _check_root(root):
        return
    stack = [root]
    while stack:
        budget.check()
        directory = stack.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    budget.entry()
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError as exc:
                        raise EvidenceError(
                            "permission_denied", "source_unavailable", 4, "unavailable", "unavailable"
                        ) from exc
        except PermissionError as exc:
            raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc
        except OSError as exc:
            raise EvidenceError("source_unavailable", "source_unavailable", 4, "unavailable", "unavailable") from exc


def _file_revision(path: Path) -> Revision:
    descriptor = open_readonly_nofollow(path)
    try:
        return Revision.from_stat(os.fstat(descriptor))
    finally:
        os.close(descriptor)


def _validate_codex_identity(path: Path, session_id: str, budget: ResolutionBudget) -> Revision:
    seen = False

    def handler(record: dict[str, Any], line: int) -> bool:
        nonlocal seen
        seen = True
        payload = record.get("payload")
        if record.get("type") != "session_meta" or not isinstance(payload, dict):
            raise EvidenceError("unsupported_format", exit_code=5)
        if payload.get("id") != session_id:
            raise EvidenceError("identity_mismatch", exit_code=5)
        return True

    revision = _read_auxiliary_jsonl(path, budget, handler, require_eof=False)
    if not seen:
        raise EvidenceError("unsupported_format", exit_code=5)
    return revision


def _validate_claude_identity(path: Path, session_id: str, budget: ResolutionBudget) -> Revision:
    found = False

    def handler(record: dict[str, Any], line: int) -> bool:
        nonlocal found
        if "sessionId" not in record:
            return False
        value = record.get("sessionId")
        if not isinstance(value, str) or value != session_id:
            raise EvidenceError("identity_mismatch", exit_code=5)
        found = True
        return True

    revision = _read_auxiliary_jsonl(path, budget, handler, require_eof=False)
    if not found:
        raise EvidenceError("unsupported_format", exit_code=5)
    return revision


def _validate_grok_identity(path: Path, session_id: str, budget: ResolutionBudget) -> Revision:
    identities = 0

    def handler(record: dict[str, Any], line: int) -> bool:
        nonlocal identities
        if record.get("type") != "turn_started":
            return False
        value = record.get("session_id")
        if not isinstance(value, str):
            raise EvidenceError("unsupported_format", exit_code=5)
        identities += 1
        if value != session_id:
            raise EvidenceError("identity_mismatch", exit_code=5)
        return False

    revision = _read_auxiliary_jsonl(path, budget, handler, require_eof=True)
    if identities == 0:
        raise EvidenceError("unsupported_format", exit_code=5)
    return revision


def _deduplicate_candidates(candidates: list[Candidate]) -> list[Candidate]:
    result: list[Candidate] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        if candidate.identity in seen:
            continue
        seen.add(candidate.identity)
        result.append(candidate)
    return result


def _resolve_codex(home: Path, session_id: str, budget: ResolutionBudget) -> list[Candidate]:
    candidates: list[Candidate] = []
    roots = (
        ("codex_active", home / ".codex" / "sessions"),
        ("codex_archived", home / ".codex" / "archived_sessions"),
    )
    target_suffix = f"-{session_id}.jsonl"
    for root_id, root in roots:
        for path in _walk_files(root, budget):
            if not path.name.endswith(target_suffix):
                continue
            validated = _validate_codex_identity(path, session_id, budget)
            candidates.append(
                Candidate(
                    "codex",
                    session_id,
                    path,
                    root,
                    root_id,
                    (validated.device, validated.inode),
                )
            )
    return _deduplicate_candidates(candidates)


def _safe_claude_hint_paths(index: Path, root: Path, session_id: str, budget: ResolutionBudget) -> set[Path]:
    value = _read_bounded_json_file(index, budget)
    if not isinstance(value, dict) or not isinstance(value.get("entries"), list):
        return set()
    entries = value["entries"]
    if len(entries) > budget.limits.auxiliary_file_records:
        return set()
    started = time.monotonic()
    hints: set[Path] = set()
    # Treat index paths only as bounded lexical hints. They never authorize a
    # candidate and must not resolve an untrusted path outside the Claude root;
    # the explicit top-level directory scan remains authoritative.
    canonical_root = Path(os.path.abspath(os.path.normpath(os.fspath(root))))
    for entry in entries:
        if time.monotonic() - started > budget.limits.auxiliary_file_seconds:
            return set()
        if not isinstance(entry, dict) or entry.get("sessionId") != session_id:
            continue
        raw_path = entry.get("fullPath")
        if not isinstance(raw_path, str):
            continue
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            continue
        candidate = Path(os.path.abspath(os.path.normpath(os.fspath(candidate))))
        try:
            candidate.relative_to(canonical_root)
        except ValueError:
            continue
        if candidate.name != f"{session_id}.jsonl" or "subagents" in candidate.parts:
            continue
        hints.add(candidate)
    return hints


def _resolve_claude(home: Path, session_id: str, budget: ResolutionBudget) -> list[Candidate]:
    root = home / ".claude" / "projects"
    if not _check_root(root):
        return []
    candidates: set[Path] = set()
    hints: set[Path] = set()
    try:
        with os.scandir(root) as projects:
            for project in projects:
                budget.entry()
                if project.is_symlink() or not project.is_dir(follow_symlinks=False):
                    continue
                project_path = Path(project.path)
                try:
                    with os.scandir(project_path) as entries:
                        for entry in entries:
                            budget.entry()
                            if entry.is_symlink():
                                if entry.name == f"{session_id}.jsonl":
                                    raise EvidenceError("unsupported_format", exit_code=5)
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            path = Path(entry.path)
                            if entry.name == "sessions-index.json":
                                hints.update(_safe_claude_hint_paths(path, root, session_id, budget))
                            elif entry.name == f"{session_id}.jsonl":
                                candidates.add(path)
                except PermissionError as exc:
                    raise EvidenceError(
                        "permission_denied", "source_unavailable", 4, "unavailable", "unavailable"
                    ) from exc
    except PermissionError as exc:
        raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc
    candidates.update(hints.intersection(candidates))
    resolved: list[Candidate] = []
    for path in sorted(candidates):
        if "subagents" in path.parts:
            continue
        validated = _validate_claude_identity(path, session_id, budget)
        resolved.append(
            Candidate(
                "claude",
                session_id,
                path,
                root,
                "claude_projects",
                (validated.device, validated.inode),
            )
        )
    return _deduplicate_candidates(resolved)


def _resolve_grok(home: Path, session_id: str, budget: ResolutionBudget) -> list[Candidate]:
    root = home / ".grok" / "sessions"
    if not _check_root(root):
        return []
    session_dirs: list[Path] = []
    try:
        with os.scandir(root) as workspaces:
            for workspace in workspaces:
                budget.entry()
                if workspace.is_symlink() or not workspace.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(workspace.path) as sessions:
                        for entry in sessions:
                            budget.entry()
                            if entry.name != session_id:
                                continue
                            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                                raise EvidenceError("unsupported_format", exit_code=5)
                            session_dirs.append(Path(entry.path))
                except PermissionError as exc:
                    raise EvidenceError(
                        "permission_denied", "source_unavailable", 4, "unavailable", "unavailable"
                    ) from exc
    except PermissionError as exc:
        raise EvidenceError("permission_denied", "source_unavailable", 4, "unavailable", "unavailable") from exc

    candidates: list[Candidate] = []
    for directory in session_dirs:
        messages = directory / "chat_history.jsonl"
        events = directory / "events.jsonl"
        try:
            message_info = messages.lstat()
            event_info = events.lstat()
        except FileNotFoundError as exc:
            raise EvidenceError("unsupported_format", exit_code=5) from exc
        if (
            stat.S_ISLNK(message_info.st_mode)
            or not stat.S_ISREG(message_info.st_mode)
            or stat.S_ISLNK(event_info.st_mode)
            or not stat.S_ISREG(event_info.st_mode)
        ):
            raise EvidenceError("unsupported_format", exit_code=5)
        event_revision = _validate_grok_identity(events, session_id, budget)
        extras: list[tuple[str, Path, str, Path]] = [
            ("identity_events", events, "grok_sessions", root)
        ]
        prompt = directory / "prompt_context.json"
        try:
            prompt_info = prompt.lstat()
            if stat.S_ISREG(prompt_info.st_mode) and not stat.S_ISLNK(prompt_info.st_mode):
                extras.append(("prompt_context", prompt, "grok_sessions", root))
        except OSError:
            pass
        message_revision = _file_revision(messages)
        candidates.append(
            Candidate(
                host="grok",
                session_id=session_id,
                message_path=messages,
                root_path=root,
                root_id="grok_sessions",
                identity=(message_revision.device, message_revision.inode),
                extra_sources=extras,
                guard_revisions=[(events, event_revision)],
            )
        )
    return _deduplicate_candidates(candidates)


def resolve_candidate(home: Path, host: str, session_id: str, budget: ResolutionBudget) -> tuple[Candidate, int]:
    if host == "codex":
        candidates = _resolve_codex(home, session_id, budget)
    elif host == "claude":
        candidates = _resolve_claude(home, session_id, budget)
    elif host == "grok":
        candidates = _resolve_grok(home, session_id, budget)
    else:  # guarded by argparse; retained for direct callers
        raise EvidenceError("invalid_request", exit_code=2)
    if not candidates:
        raise EvidenceError("source_unavailable", "source_unavailable", 4, "unavailable", "unavailable")
    if len(candidates) > 1:
        sources = [source_metadata(candidate.message_path, "messages", candidate.root_id, candidate.root_path) for candidate in candidates]
        raise EvidenceError("ambiguous_source", "ambiguous_source", 5, sources=sources)
    return candidates[0], len(candidates)


def opaque_candidate_id(path: Path, root_id: str, root: Path) -> str:
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        relative = path.name
    return hashlib.sha256(f"{root_id}:{relative}".encode("utf-8")).hexdigest()[:12]


def source_metadata(path: Path, role: str, root_id: str, root: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise EvidenceError("source_unavailable", "source_unavailable", 4, "unavailable", "unavailable") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise EvidenceError("unsupported_format", exit_code=5)
    return {
        "role": role,
        "root_id": root_id,
        "candidate_id": opaque_candidate_id(path, root_id, root),
        "basename": path.name,
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
    }


def candidate_sources(candidate: Candidate) -> list[dict[str, Any]]:
    values = [source_metadata(candidate.message_path, "messages", candidate.root_id, candidate.root_path)]
    for role, path, root_id, root in candidate.extra_sources:
        values.append(source_metadata(path, role, root_id, root))
    return values


def source_state_after_read(descriptor: int, path: Path, revision: Revision) -> str:
    end = os.fstat(descriptor)
    try:
        current = path.lstat()
    except OSError:
        return "replaced"
    if stat.S_ISLNK(current.st_mode) or int(current.st_dev) != revision.device or int(current.st_ino) != revision.inode:
        return "replaced"
    if int(end.st_size) < revision.starting_size:
        return "truncated"
    if int(end.st_size) > revision.starting_size:
        return "appended"
    if int(end.st_mtime_ns) != revision.mtime_ns:
        return "modified"
    return "stable"


def inspect_candidate(candidate: Candidate, after_read: Optional[Callable[[Path], None]]) -> tuple[Revision, str]:
    descriptor = open_candidate_message(candidate)
    try:
        revision = Revision.from_stat(os.fstat(descriptor))
        if after_read is not None:
            after_read(candidate.message_path)
        state = source_state_after_read(descriptor, candidate.message_path, revision)
        return revision, state
    finally:
        os.close(descriptor)


def _scan_search_prefix(
    descriptor: int,
    starting_size: int,
    limits: Limits,
    on_record: Callable[[PrefixRecord], None],
) -> PrefixScan:
    byte_cap = min(starting_size, limits.search_bytes)
    started = time.monotonic()
    bytes_read = 0
    pending = bytearray()
    dropping_oversized = False
    records_scanned = 0
    line = 0
    complete_byte = 0
    reasons: list[str] = []
    stopped = False

    while bytes_read < byte_cap and not stopped:
        if time.monotonic() - started > limits.search_seconds:
            reasons.append("time")
            break
        chunk = os.read(descriptor, min(64 * 1024, byte_cap - bytes_read))
        if not chunk:
            break
        base = bytes_read
        bytes_read += len(chunk)
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            end = newline if newline >= 0 else len(chunk)
            segment = chunk[cursor:end]
            if not dropping_oversized:
                if len(pending) + len(segment) > limits.search_record_bytes:
                    pending.clear()
                    dropping_oversized = True
                else:
                    pending.extend(segment)
            if newline < 0:
                break
            line += 1
            records_scanned += 1
            complete_byte = base + newline + 1
            on_record(
                PrefixRecord(
                    line=line,
                    complete_through_byte=complete_byte,
                    raw=None if dropping_oversized else bytes(pending),
                    oversized=dropping_oversized,
                )
            )
            pending.clear()
            dropping_oversized = False
            cursor = newline + 1
            if records_scanned >= limits.search_records:
                if complete_byte < starting_size:
                    reasons.append("records")
                stopped = True
                break
            if time.monotonic() - started > limits.search_seconds:
                if complete_byte < starting_size:
                    reasons.append("time")
                stopped = True
                break

    if starting_size > limits.search_bytes and "bytes" not in reasons:
        reasons.append("bytes")
    unterminated = bool(pending) or dropping_oversized
    return PrefixScan(line, complete_byte, records_scanned, reasons, unterminated)


PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----[\s\S]*?"
    r"-----END (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----",
    re.IGNORECASE,
)
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    PRIVATE_KEY,
    re.compile(
        r"(?i)\b(?:proxy-)?authorization\b(?:\"|')?\s*:\s*"
        r"(?P<secret>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
    ),
    re.compile(
        r"(?i)\b(?:set-)?cookie\b(?:\"|')?\s*:\s*"
        r"(?P<secret>\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\r\n]+)"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://(?P<secret>[^\s/@:]+:[^\s/@]+)@"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|passwd|secret|token)\b(?:\"|')?\s*(?:=|:)\s*"
        r"(?P<secret>\"[^\"\r\n]+\"|'[^'\r\n]+'|[^\s,;]+)"
    ),
    re.compile(
        r"(?i)--(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|secret|token)(?:=|\s+)(?P<secret>[^\s]+)"
    ),
    re.compile(r"(?P<secret>\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})\b)"),
    re.compile(r"(?P<secret>\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b)"),
)


def sensitive_spans(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for pattern in SENSITIVE_PATTERNS:
        for match in pattern.finditer(text):
            try:
                start, end = match.span("secret")
            except (IndexError, KeyError):
                start, end = match.span()
            if end > start:
                found.append((start, end))
    if not found:
        return []
    found.sort()
    merged = [found[0]]
    for start, end in found[1:]:
        old_start, old_end = merged[-1]
        if start <= old_end:
            merged[-1] = (old_start, max(old_end, end))
        else:
            merged.append((start, end))
    return merged


def is_sensitive_query(query: str) -> bool:
    return bool(sensitive_spans(query))


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    return any(span_start < end and start < span_end for span_start, span_end in spans)


def _redact_excerpt(text: str, start: int, end: int, spans: Sequence[tuple[int, int]]) -> tuple[str, int]:
    parts: list[str] = []
    cursor = start
    count = 0
    for span_start, span_end in spans:
        if span_end <= start or span_start >= end:
            continue
        clipped_start = max(start, span_start)
        clipped_end = min(end, span_end)
        if clipped_start > cursor:
            parts.append(text[cursor:clipped_start])
        parts.append("[REDACTED]")
        cursor = max(cursor, clipped_end)
        count += 1
    if cursor < end:
        parts.append(text[cursor:end])
    return "".join(parts), count


def make_match(
    field: TextField,
    line: int,
    expression: re.Pattern[str],
    limits: Limits,
) -> Optional[dict[str, Any]]:
    match = expression.search(field.text)
    if match is None:
        return None
    spans = sensitive_spans(field.text)
    if _overlaps(match.start(), match.end(), spans):
        return {
            "role": field.role,
            "line": line,
            "block_index": field.block_index,
            "fragment": "[REDACTED:SENSITIVE_MATCH]",
            "sensitive_match": True,
            "fragment_truncated": False,
            "redaction_count": 1,
        }
    maximum = limits.fragment_characters
    left = max(0, match.start() - maximum // 2)
    right = min(len(field.text), left + maximum)
    if right - left < maximum:
        left = max(0, right - maximum)
    excerpt, redactions = _redact_excerpt(field.text, left, right, spans)
    was_truncated = left > 0 or right < len(field.text) or len(excerpt) > maximum
    excerpt = excerpt[:maximum]
    return {
        "role": field.role,
        "line": line,
        "block_index": field.block_index,
        "fragment": excerpt,
        "sensitive_match": False,
        "fragment_truncated": was_truncated,
        "redaction_count": redactions,
    }


class MatchStore:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._matches: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self.truncated = False

    def update(self, key: str, value: Optional[dict[str, Any]]) -> None:
        if key in self._matches:
            if value is None:
                del self._matches[key]
            else:
                self._matches[key] = value
            return
        if value is not None:
            if len(self._matches) < self.maximum:
                self._matches[key] = value
            else:
                self.truncated = True

    def remove_prefix(self, prefix: str) -> None:
        for key in tuple(self._matches):
            if key.startswith(prefix):
                del self._matches[key]

    def clear(self) -> None:
        self._matches.clear()
        self.truncated = False

    def values(self) -> list[dict[str, Any]]:
        return list(self._matches.values())


KNOWN_CODEX_TOP = {
    "compacted",
    "event_msg",
    "inter_agent_communication_metadata",
    "response_item",
    "session_meta",
    "turn_context",
    "world_state",
}

KNOWN_CODEX_EVENT_TELEMETRY = {
    "item_completed",
    "task_complete",
    "task_started",
    "thread_settings_applied",
    "token_count",
    "turn_aborted",
}


def _strict_text_blocks(content: Any, allowed_types: set[str]) -> tuple[list[tuple[int, str]], int]:
    if not isinstance(content, list):
        return [], 1
    values: list[tuple[int, str]] = []
    unknown = 0
    known_excluded = {"image", "input_image", "thinking", "redacted_thinking", "tool_use", "encrypted_content"}
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            unknown += 1
            continue
        block_type = block.get("type")
        if block_type in allowed_types:
            key = "text"
            value = block.get(key)
            if not isinstance(value, str):
                unknown += 1
            else:
                values.append((index, value))
        elif block_type not in known_excluded:
            unknown += 1
    return values, unknown


def extract_codex(record: dict[str, Any], scope: str) -> tuple[list[TextField], int, bool]:
    record_type = record.get("type")
    if record_type not in KNOWN_CODEX_TOP:
        return [], 1, False
    if record_type == "event_msg":
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return [], 1, False
        kind = payload.get("type")
        if kind in {"user_message", "agent_message"}:
            if isinstance(payload.get("message"), str):
                role = "user" if kind == "user_message" else "assistant"
                return [TextField(role, payload["message"], 0, canonical_message=False)], 0, False
            return [], 1, False
        if kind in KNOWN_CODEX_EVENT_TELEMETRY:
            return [], 0, False
        return [], 1, False
    if record_type != "response_item":
        return [], 0, False
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return [], 1, False
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        if role not in {"user", "assistant"}:
            return [], 0, False
        blocks, unknown = _strict_text_blocks(payload.get("content"), {"input_text", "output_text"})
        fields = [TextField(role, text, index) for index, text in blocks]
        return fields, unknown, bool(fields)
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        if scope != "messages-and-tools":
            return [], 0, False
        value = payload.get("output")
        if isinstance(value, str):
            # Tool results are canonical response items.  They must remain visible when
            # the file has no canonical user/assistant message; only legacy event-message
            # fallbacks are discarded in that case.
            return [TextField("tool", value, 0)], 0, True
        if isinstance(value, list):
            blocks, unknown = _strict_text_blocks(
                value,
                {"input_text", "output_text"},
            )
            fields = [TextField("tool", text, index) for index, text in blocks]
            return fields, unknown, bool(fields)
        return [], 1, False
    if item_type in {
        "reasoning", "function_call", "custom_tool_call", "agent_message"
    }:
        return [], 0, False
    return [], 1, False


KNOWN_CLAUDE_TOP = {
    "ai-title",
    "assistant",
    "attachment",
    "file-history-delta",
    "file-history-snapshot",
    "last-prompt",
    "mode",
    "permission-mode",
    "queue-operation",
    "system",
    "user",
}


def _claude_message_id(record: dict[str, Any], line: int) -> Optional[str]:
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    message_id = message.get("id")
    if isinstance(message_id, str):
        return message_id
    value = record.get("uuid")
    return value if isinstance(value, str) else f"line:{line}"


def extract_claude(record: dict[str, Any], scope: str, line: int) -> tuple[list[TextField], int, bool]:
    record_type = record.get("type")
    if record_type not in KNOWN_CLAUDE_TOP:
        return [], 1, False
    if record_type not in {"user", "assistant"}:
        return [], 0, False
    if record.get("isMeta") or record.get("isSidechain"):
        return [], 0, False
    message = record.get("message")
    if not isinstance(message, dict):
        return [], 1, False
    if message.get("role") != record_type:
        return [], 1, False
    content = message.get("content")
    message_id = _claude_message_id(record, line)
    if message_id is None:  # guarded by the message mapping check above
        return [], 1, False
    fields: list[TextField] = []
    unknown = 0
    if isinstance(content, str):
        role = "user" if record_type == "user" else "assistant"
        fields.append(TextField(role, content, 0, f"{message_id}:0"))
        return fields, 0, True
    if not isinstance(content, list):
        return [], 1, False
    for index, block in enumerate(content):
        if not isinstance(block, dict):
            unknown += 1
            continue
        block_type = block.get("type")
        if block_type == "text":
            value = block.get("text")
            if isinstance(value, str):
                role = "user" if record_type == "user" else "assistant"
                fields.append(TextField(role, value, index, f"{message_id}:{index}"))
            else:
                unknown += 1
        elif block_type == "tool_result" and record_type == "user":
            if scope != "messages-and-tools":
                continue
            value = block.get("content")
            if isinstance(value, str):
                fields.append(TextField("tool", value, index, f"{message_id}:{index}"))
            elif isinstance(value, list):
                text_blocks, nested_unknown = _strict_text_blocks(value, {"text"})
                unknown += nested_unknown
                for nested_index, text in text_blocks:
                    combined = index * 1_000_000 + nested_index
                    fields.append(TextField("tool", text, combined, f"{message_id}:{combined}"))
            else:
                unknown += 1
        elif block_type in {"thinking", "redacted_thinking", "tool_use", "image"}:
            continue
        else:
            unknown += 1
    return fields, unknown, bool(fields)


KNOWN_GROK_TOP = {"assistant", "reasoning", "system", "tool_result", "user"}


def extract_grok(record: dict[str, Any], scope: str) -> tuple[list[TextField], int, bool]:
    record_type = record.get("type")
    if record_type not in KNOWN_GROK_TOP:
        return [], 1, False
    if record_type == "assistant":
        value = record.get("content")
        if isinstance(value, str):
            return [TextField("assistant", value, 0)], 0, True
        return [], 1, False
    if record_type == "user":
        if record.get("synthetic_reason"):
            return [], 0, False
        content = record.get("content")
        blocks, unknown = _strict_text_blocks(content, {"text"})
        return [TextField("user", text, index) for index, text in blocks], unknown, bool(blocks)
    if record_type == "tool_result":
        if scope != "messages-and-tools":
            return [], 0, False
        value = record.get("content")
        if isinstance(value, str):
            return [TextField("tool", value, 0)], 0, False
        return [], 1, False
    return [], 0, False


def search_candidate(
    candidate: Candidate,
    query: str,
    scope: str,
    limits: Limits,
    after_read: Optional[Callable[[Path], None]],
) -> tuple[Revision, str, dict[str, Any], list[dict[str, Any]], int]:
    descriptor = open_candidate_message(candidate)
    revision = Revision.from_stat(os.fstat(descriptor))
    malformed = 0
    unknown = 0
    oversized_records = 0
    oversized_fields = 0
    root_identity_checked = False
    claude_identity_seen = False
    canonical_codex_seen = False
    expression = re.compile(re.escape(query), re.IGNORECASE)
    matches = MatchStore(limits.matches)
    legacy_matches = MatchStore(limits.matches)

    def process_record(prefix_record: PrefixRecord) -> None:
        nonlocal malformed
        nonlocal unknown
        nonlocal oversized_records
        nonlocal oversized_fields
        nonlocal root_identity_checked
        nonlocal claude_identity_seen
        nonlocal canonical_codex_seen

        if prefix_record.oversized or prefix_record.raw is None:
            if candidate.host == "codex" and not root_identity_checked:
                root_identity_checked = True
                raise EvidenceError("unsupported_format", exit_code=5)
            oversized_records += 1
            return
        if not prefix_record.raw.strip():
            return
        first_codex_record = candidate.host == "codex" and not root_identity_checked
        if first_codex_record:
            root_identity_checked = True
        try:
            record = json.loads(prefix_record.raw.decode("utf-8", errors="strict"))
        except (UnicodeError, json.JSONDecodeError):
            if first_codex_record:
                raise EvidenceError("unsupported_format", exit_code=5)
            malformed += 1
            return
        if not isinstance(record, dict):
            if first_codex_record:
                raise EvidenceError("unsupported_format", exit_code=5)
            unknown += 1
            return

        if candidate.host == "codex":
            if first_codex_record:
                payload = record.get("payload")
                if (
                    record.get("type") != "session_meta"
                    or not isinstance(payload, dict)
                    or payload.get("id") != candidate.session_id
                ):
                    raise EvidenceError("identity_mismatch", exit_code=5)
            fields, record_unknown, canonical = extract_codex(record, scope)
            if canonical and not canonical_codex_seen:
                canonical_codex_seen = True
                legacy_matches.clear()
        elif candidate.host == "claude":
            if "sessionId" in record:
                value = record.get("sessionId")
                if not isinstance(value, str) or value != candidate.session_id:
                    raise EvidenceError("identity_mismatch", exit_code=5)
                claude_identity_seen = True
            message_id = _claude_message_id(record, prefix_record.line)
            if (
                message_id is not None
                and record.get("type") in {"user", "assistant"}
                and not record.get("isMeta")
                and not record.get("isSidechain")
            ):
                # Claude may append complete streaming revisions with the same
                # message ID.  Remove prior matching blocks first so omitted or
                # changed blocks cannot survive as stale evidence.
                matches.remove_prefix(f"{message_id}:")
            fields, record_unknown, canonical = extract_claude(record, scope, prefix_record.line)
        else:
            fields, record_unknown, canonical = extract_grok(record, scope)
        unknown += record_unknown

        for index, text_field in enumerate(fields):
            try:
                field_size = len(text_field.text.encode("utf-8", errors="strict"))
            except UnicodeError:
                oversized_fields += 1
                continue
            if field_size > limits.field_bytes:
                oversized_fields += 1
                continue
            value = make_match(text_field, prefix_record.line, expression, limits)
            key = text_field.revision_key or f"{prefix_record.line}:{text_field.block_index}:{index}"
            if candidate.host == "codex" and not text_field.canonical_message:
                if not canonical_codex_seen:
                    legacy_matches.update(key, value)
            else:
                matches.update(key, value)

    try:
        scan = _scan_search_prefix(descriptor, revision.starting_size, limits, process_record)

        if candidate.host == "codex" and not root_identity_checked and not scan.limit_reasons:
            raise EvidenceError("unsupported_format", exit_code=5)
        if candidate.host == "claude" and not claude_identity_seen and not scan.limit_reasons:
            raise EvidenceError("unsupported_format", exit_code=5)
        if candidate.host == "codex" and not canonical_codex_seen:
            matches = legacy_matches
        if after_read is not None:
            after_read(candidate.message_path)
        state = source_state_after_read(descriptor, candidate.message_path, revision)
    finally:
        os.close(descriptor)

    partial_parse = bool(
        malformed
        or unknown
        or oversized_records
        or oversized_fields
        or scan.unterminated_tail
    )
    limit_reasons = list(scan.limit_reasons)
    if matches.truncated and "matches" not in limit_reasons:
        limit_reasons.append("matches")
    coverage_state = "limit_truncated" if limit_reasons else "partial_parse" if partial_parse else "complete"
    coverage = {
        "state": coverage_state,
        "records_scanned": scan.records_scanned,
        "complete_through_line": scan.complete_through_line,
        "complete_through_byte": scan.complete_through_byte,
        "malformed_records": malformed,
        "unknown_records": unknown,
        "oversized_records": oversized_records,
        "oversized_fields": oversized_fields,
        "limit_reasons": limit_reasons + (["unterminated_tail"] if scan.unterminated_tail else []),
    }
    values = matches.values()
    redactions = sum(int(value.get("redaction_count", 0)) for value in values)
    return revision, state, coverage, values, redactions


def build_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(add_help=False)
    subcommands = parser.add_subparsers(dest="command", required=True, parser_class=SafeArgumentParser)
    for name in ("inspect", "search"):
        command = subcommands.add_parser(name, add_help=False)
        command.add_argument("--host", required=True, choices=SUPPORTED_HOSTS)
        command.add_argument("--session-id", required=True)
        command.add_argument("--product-root", required=True)
        command.add_argument("--allow-unbound", action="store_true")
        if name == "search":
            command.add_argument("--query", required=True)
            command.add_argument("--scope", choices=SUPPORTED_SCOPES, default="messages")
    return parser


def _emit(result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    home: Optional[Path | str] = None,
    limits: Optional[Limits] = None,
    after_read: Optional[Callable[[Path], None]] = None,
) -> int:
    arguments = list(argv if argv is not None else sys.argv[1:])
    command_hint = arguments[0] if arguments and arguments[0] in {"inspect", "search"} else None
    host_hint: Optional[str] = None
    if "--host" in arguments:
        position = arguments.index("--host") + 1
        if position < len(arguments) and arguments[position] in SUPPORTED_HOSTS:
            host_hint = arguments[position]
    result = base_result(command_hint, host_hint, None)
    started = time.monotonic()
    active_limits = limits or Limits()
    exit_code = 70
    budget: Optional[ResolutionBudget] = None
    try:
        parsed = build_parser().parse_args(arguments)
        session_id = canonical_session_id(parsed.session_id)
        result = base_result(parsed.command, parsed.host, session_id)
        _, binding_mode = validate_product_binding(
            parsed.product_root,
            parsed.host,
            session_id,
            bool(parsed.allow_unbound),
            active_limits,
        )
        result["binding_mode"] = binding_mode
        if parsed.command == "search":
            query = parsed.query
            if (
                not isinstance(query, str)
                or not query.strip()
                or len(query) > active_limits.query_characters
                or is_sensitive_query(query)
            ):
                raise EvidenceError("invalid_request", exit_code=2)

        native_home = Path(home).expanduser() if home is not None else Path.home()
        budget = ResolutionBudget(active_limits)
        candidate, candidate_count = resolve_candidate(native_home, parsed.host, session_id, budget)
        result["sources"] = candidate_sources(candidate)
        result["diagnostics"]["entries_scanned"] = budget.entries
        result["diagnostics"]["candidates_found"] = candidate_count
        # For source-backed results, observation time belongs next to the
        # canonical open rather than the earlier CLI/binding phase.
        result["observed_at"] = utc_now()

        if parsed.command == "inspect":
            revision, state = inspect_candidate(candidate, after_read)
            result.update(
                {
                    "status": "ok",
                    "code": "inspected",
                    "outcome": "inspected",
                    "source_state": state,
                    "revision": revision.as_json(),
                }
            )
            exit_code = 0 if state == "stable" else 3
            if state != "stable":
                result["status"] = "partial"
        else:
            revision, state, coverage, matches, redactions = search_candidate(
                candidate,
                parsed.query,
                parsed.scope,
                active_limits,
                after_read,
            )
            result["source_state"] = state
            result["revision"] = revision.as_json()
            result["coverage"] = coverage
            result["matches"] = matches
            result["diagnostics"]["records_scanned"] = coverage["records_scanned"]
            result["diagnostics"]["matches_returned"] = len(matches)
            result["diagnostics"]["redaction_count"] = redactions
            incomplete = coverage["state"] != "complete" or state != "stable"
            if matches:
                result["status"] = "partial" if incomplete else "ok"
                result["code"] = "sensitive_match" if any(item["sensitive_match"] for item in matches) else "matched"
                result["outcome"] = "supported"
                exit_code = 3 if incomplete else 0
            elif incomplete:
                result["status"] = "partial"
                result["code"] = "limit_truncated" if coverage["state"] == "limit_truncated" else "partial_parse"
                result["outcome"] = "unverified"
                exit_code = 3
            else:
                result["status"] = "ok"
                result["code"] = "not_found"
                result["outcome"] = "not_found_in_source"
                exit_code = 0
    except EvidenceError as exc:
        result["status"] = "partial" if exc.exit_code == 3 else "error"
        result["code"] = exc.code
        result["outcome"] = "unverified" if exc.exit_code == 3 else exc.outcome
        result["source_state"] = exc.source_state
        result["coverage"]["state"] = exc.coverage_state
        result["sources"] = exc.sources
        exit_code = exc.exit_code
    except (Exception, SystemExit):
        result["status"] = "error"
        result["code"] = "internal_error"
        result["outcome"] = "error"
        exit_code = 70
    finally:
        if budget is not None:
            result["diagnostics"]["entries_scanned"] = budget.entries
        result["diagnostics"]["duration_ms"] = max(0, int((time.monotonic() - started) * 1000))
        _emit(result)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
