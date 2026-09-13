#!/usr/bin/env python3
"""Spec lifecycle core: the only source of state, gate, baseline, and diff logic.

Lives here:
  - lifecycle validation and revision-checked atomic writes;
  - approval fingerprinting and drift detection;
  - metadata-only directory diff inventory;
  - deterministic package validation.

Does not live here:
  - product decisions or generated specification prose;
  - shell-specific launch behavior;
  - repository-specific build, test, commit, push, or deploy actions.

Rule of extension:
  Portable deterministic policy belongs here. Host routing belongs in a thin
  command adapter, and project semantics remain in reviewed artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSET_ROOT = SKILL_ROOT / "assets"
SESSION_NAME = "spec-session.json"
LOCK_NAME = ".spec-session.lock"
PROJECT_NAME = "project.json"
STORE_ENV = "SPEC_FLOW_HOME"
STORE_DIR = "spec-driven-development"
PACKAGE_KINDS = ("changes", "checks")

PLANNING_STATES = (
    "NEW",
    "INTENT_CAPTURED",
    "CONTEXT_DISCOVERY",
    "DECOMPOSING",
    "SPECIFYING",
    "ANALYZING_IMPACT",
    "DESIGNING_VERIFICATION",
    "PLANNING",
    "REVIEWING",
    "WAITING_APPROVAL",
)
CHECK_STATES = (
    "CHECK_NEW",
    "CHECK_SOURCE_RESOLVED",
    "CHECK_CHANGE_ANALYZED",
    "CHECK_SURFACE_MAPPED",
    "CHECK_RISK_ITERATING",
    "CHECK_CASES_DRAFTED",
    "CHECK_CASES_REVIEWED",
    "CHECK_ENVIRONMENT_PLANNED",
    "CHECK_BRIEF_READY",
    "CHECK_EXECUTING",
    "CHECK_DONE",
)
ALL_STATES = PLANNING_STATES + (
    "APPROVED",
    "IMPLEMENTING",
    "POST_IMPLEMENTATION_REVIEW",
    "VERIFYING",
    "DONE",
    "BLOCKED",
    "REVISING",
) + CHECK_STATES
ORDINARY_TRANSITIONS: dict[str, set[str]] = {
    "NEW": {"INTENT_CAPTURED"},
    "INTENT_CAPTURED": {"CONTEXT_DISCOVERY"},
    "CONTEXT_DISCOVERY": {"DECOMPOSING"},
    "DECOMPOSING": {"SPECIFYING"},
    "SPECIFYING": {"ANALYZING_IMPACT"},
    "ANALYZING_IMPACT": {"DESIGNING_VERIFICATION"},
    "DESIGNING_VERIFICATION": {"PLANNING"},
    "PLANNING": {"REVIEWING"},
    "REVIEWING": {"WAITING_APPROVAL"},
    "IMPLEMENTING": {"POST_IMPLEMENTATION_REVIEW"},
    "POST_IMPLEMENTATION_REVIEW": {"VERIFYING"},
    "REVISING": {"CONTEXT_DISCOVERY", "DECOMPOSING", "SPECIFYING"},
}
CHECK_TRANSITIONS: dict[str, set[str]] = {
    "CHECK_NEW": {"CHECK_SOURCE_RESOLVED"},
    "CHECK_SOURCE_RESOLVED": {"CHECK_CHANGE_ANALYZED"},
    "CHECK_CHANGE_ANALYZED": {"CHECK_SURFACE_MAPPED"},
    "CHECK_SURFACE_MAPPED": {"CHECK_RISK_ITERATING"},
    "CHECK_RISK_ITERATING": {"CHECK_CASES_DRAFTED"},
    "CHECK_CASES_DRAFTED": {"CHECK_CASES_REVIEWED"},
    "CHECK_CASES_REVIEWED": {"CHECK_ENVIRONMENT_PLANNED"},
    "CHECK_ENVIRONMENT_PLANNED": {"CHECK_BRIEF_READY"},
    "CHECK_BRIEF_READY": {"CHECK_RISK_ITERATING"},
}
PRIVILEGED_TARGETS = {"APPROVED", "IMPLEMENTING", "DONE", "CHECK_EXECUTING", "CHECK_DONE"}
OPEN_STATUSES = {"open", "proposed", "needs-disposition", "unresolved"}

TEMPLATE_TARGETS = {
    "intent.template.md": "intent.md",
    "feature-model.template.md": "feature-model.md",
    "proposal.template.md": "proposal.md",
    "decisions.template.md": "decisions.md",
    "spec.template.md": "specs/example/spec.md",
    "design.template.md": "design.md",
    "impact-analysis.template.md": "impact-analysis.md",
    "verification-cases.template.md": "verification-cases.md",
    "test-plan.template.md": "test-plan.md",
    "tasks.template.md": "tasks.md",
    "review.template.md": "review.md",
    "review-brief.template.md": "review-brief.md",
}
POST_IMPLEMENTATION_TEMPLATE_TARGETS = {
    "implementation-evidence.template.md": "implementation-evidence.md",
    "post-implementation-review.template.md": "post-implementation-review.md",
    "verification-report.template.md": "verification-report.md",
}

REQUIRED_PLANNING_FILES = (
    "intent.md",
    "feature-model.md",
    "proposal.md",
    "decisions.md",
    "design.md",
    "impact-analysis.md",
    "verification-cases.md",
    "tasks.md",
    "review.md",
    "review-brief.md",
)

CHECK_TEMPLATE_TARGETS = {
    "check-source.template.md": "source.md",
    "impact-map.template.md": "impact-map.md",
    "case-review.template.md": "case-review.md",
    "check.template.json": "check.json",
}
CHECK_AUTHORED_FILES = ("source.md", "impact-map.md", "case-review.md")
CHECK_GENERATED_FILES = ("verification-cases.md", "verification-brief.md")

CHECK_MODES = ("lite", "standard", "deep")
# The project config sets the baseline for `standard`; the depth mode shifts it. With the default
# minimum of 3 this yields the documented 2 / 3 / 4, and a project that lowers the baseline keeps
# the same relative depth instead of collapsing lite and standard into one another.
MODE_PASS_DELTA = {"lite": -1, "standard": 0, "deep": 1}
ANALYSIS_PASS_KINDS = ("broad", "realism", "adversarial")
PASS_KINDS = ANALYSIS_PASS_KINDS + ("cases", "review")
SEVERITIES = ("critical", "high", "medium", "low")
REALITY_LEVELS = ("likely", "possible", "unlikely", "impossible")
DISPOSITIONS = ("verify", "waived", "dropped")
ORACLE_KINDS = (
    "database",
    "ledger",
    "external-system",
    "event-log",
    "filesystem",
    "ui-state",
    "metric",
    "api-read-back",
    "proof",
    "manual-inspection",
)
SAFETY_LEVELS = ("safe", "needs-approval", "destructive")
AUTHORIZATION_SCOPES = ("safe", "approved", "all")
SCOPE_ALLOWS = {
    "safe": {"safe"},
    "approved": {"safe", "needs-approval"},
    "all": {"safe", "needs-approval", "destructive"},
}
CASE_REVIEW_VERDICTS = ("accepted", "revise", "rejected")

REQUIRED_RISK_FIELDS = (
    "title",
    "mechanism",
    "invariant",
    "evidence",
    "reality",
    "severity",
    "detectability",
    "blast_radius",
    "disposition",
)
REQUIRED_CASE_FIELDS = (
    "title",
    "risks",
    "objective",
    "why_it_catches_the_risk",
    "preconditions",
    "environment",
    "steps",
    "expected_final_state",
    "forbidden_outcomes",
    "oracle",
    "side_effect_count",
    "timeout",
    "flake_policy",
    "safety",
)
# Deliberately optional: `fixtures`, `fault_injection`, `intermediate_checks` and `requirements`
# are empty for legitimate cases, so demanding them would train people to write filler.
OPTIONAL_CASE_FIELDS = ("fixtures", "fault_injection", "intermediate_checks", "requirements")
REQUIRED_STEP_FIELDS = ("action", "expected", "how_to_observe")

LITE_PATH_RE = re.compile(
    r"(^|/)(ui|widget|widgets|view|views|style|styles|css|scss|theme|assets|icons|locale|locales|"
    r"i18n|l10n|strings|docs?|readme)(/|$)|\.(md|css|scss|svg|png|arb|strings)$",
    re.IGNORECASE,
)
CONFIG_RELATIVE_NAMES = (
    ".spec/verification.yml",
    ".spec/verification.yaml",
    ".spec/verification.json",
    "spec/verification.yml",
    "spec/verification.yaml",
    "spec/verification.json",
)
DEFAULT_CHECK_CONFIG: dict[str, Any] = {
    "verification": {
        "default_mode": "auto",
        "minimum_iterations": 3,
        "maximum_iterations": 6,
        "critical_areas": [
            "payments",
            "authentication",
            "permissions",
            "persistence",
            "migrations",
            "concurrency",
            "agent-actions",
            "external-integrations",
        ],
        "required_risk_coverage": {"critical": 100, "high": 100, "medium": 70},
        "require_actual_effect_oracle": True,
        "require_cleanup": True,
        "require_evidence": True,
        "require_references": True,
        "artifact_store": "global",
    },
    "test_environment": {},
    "fault_injection": {},
    "evidence": {},
}

PLACEHOLDER_RE = re.compile(r"<[^<>\n]{1,160}>")


class FlowError(RuntimeError):
    """A structured, user-actionable lifecycle error."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def resolve_root(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


# --------------------------------------------------------------------------------------
# Artifact store: packages live outside the project so they never enter its diff
# --------------------------------------------------------------------------------------


def store_root(override: str | None = None) -> Path:
    """Where specification and check packages live.

    Precedence: explicit argument, then SPEC_FLOW_HOME, then XDG_STATE_HOME, then
    ~/.local/state. Work artifacts are durable records of a machine's activity, not files
    the reviewed repository should carry.
    """
    if override:
        return Path(override).expanduser().resolve()
    if os.environ.get(STORE_ENV):
        return Path(os.environ[STORE_ENV]).expanduser().resolve()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".local" / "state"
    return (root / STORE_DIR).resolve()


def find_project_root(start: str | Path | None = None) -> Path:
    """The repository the work belongs to, or the directory itself when it is not one."""
    current = Path(start).expanduser().resolve() if start else Path.cwd().resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return candidate
    return current


def project_key(project: Path) -> str:
    """One identity per Git common directory, shared by all linked worktrees."""
    identity = project_identity(project)
    resolved = identity["identity"]
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", identity["name"]).strip("-.") or "project"
    return f"{name}-{digest}"


def project_identity(project: Path) -> dict[str, str]:
    """Resolve Git metadata without executing repository hooks or reading remote URLs.

    A non-repository directory retains path identity. Git environment overrides are
    removed so another active checkout cannot redirect this explicit lookup.
    """
    root = find_project_root(project)
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=False, timeout=10, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip()).resolve()
    elif (root / ".git").is_dir():
        common = (root / ".git").resolve()
    elif (root / ".git").is_file():
        pointer = (root / ".git").read_text(encoding="utf-8").strip()
        if not pointer.startswith("gitdir: "):
            raise FlowError("project_identity_invalid", "The checkout's .git file is not a gitdir pointer.")
        gitdir = (root / pointer[len("gitdir: "):]).resolve()
        if not gitdir.is_dir():
            raise FlowError("project_identity_invalid", "The checkout's Git metadata directory is unavailable.")
        common_file = gitdir / "commondir"
        common = (gitdir / common_file.read_text(encoding="utf-8").strip()).resolve() if common_file.is_file() else gitdir
    else:
        return {"identity": str(root), "path": str(root), "name": root.name, "kind": "directory"}
    # The common directory is stable even when a linked worktree has a different name.
    primary = common.parent if common.name == ".git" else common
    return {"identity": str(common), "path": str(primary), "name": primary.name, "kind": "git-common-dir"}


def package_path(project: Path, kind: str, session_id: str, store: str | None = None) -> Path:
    if kind not in PACKAGE_KINDS:
        raise FlowError("unknown_package_kind", "Package kind must be changes or checks.", kind=kind)
    return store_root(store) / project_key(project) / kind / package_slug(session_id)


def package_slug(session_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", session_id).strip("-.")
    if not slug:
        raise FlowError("invalid_session_id", "The session id must contain a usable slug.", session_id=session_id)
    return slug


def record_project(project: Path, store: str | None = None) -> Path:
    """Remember which real directory a store entry belongs to, so history stays readable."""
    home = store_root(store) / project_key(project)
    home.mkdir(parents=True, exist_ok=True)
    marker = home / PROJECT_NAME
    payload = {**project_identity(project), "linked_at": utc_now()}
    if marker.is_file():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
            payload["linked_at"] = existing.get("linked_at", payload["linked_at"])
        except (OSError, json.JSONDecodeError):
            pass
    atomic_write_json(marker, payload)
    return home


def resolve_package_root(args: argparse.Namespace, kind: str) -> Path:
    """An explicit --root always wins; otherwise the package lands in the global store."""
    if getattr(args, "root", None):
        return resolve_root(args.root)
    project = find_project_root(getattr(args, "project", None))
    config = resolve_config(project, getattr(args, "config", None))
    if str(verification_setting(config, "artifact_store")).lower() == "repo":
        return (project / "spec" / kind / package_slug(args.session_id)).resolve()
    record_project(project, getattr(args, "store", None))
    return package_path(project, kind, args.session_id, getattr(args, "store", None))


def store_entries(store: str | None = None) -> list[dict[str, Any]]:
    """Every package the machine holds, newest activity first."""
    home = store_root(store)
    entries: list[dict[str, Any]] = []
    if not home.is_dir():
        return entries
    for project_dir in sorted(path for path in home.iterdir() if path.is_dir()):
        marker = project_dir / PROJECT_NAME
        project_path = None
        if marker.is_file():
            try:
                project_path = json.loads(marker.read_text(encoding="utf-8")).get("path")
            except (OSError, json.JSONDecodeError):
                project_path = None
        for kind in PACKAGE_KINDS:
            kind_dir = project_dir / kind
            if not kind_dir.is_dir():
                continue
            for package in sorted(path for path in kind_dir.iterdir() if path.is_dir()):
                session_file = package / SESSION_NAME
                entry: dict[str, Any] = {
                    "project": project_path or project_dir.name,
                    "kind": kind,
                    "session_id": package.name,
                    "path": str(package),
                    "state": None,
                    "mode": None,
                    "updated_at": None,
                }
                if session_file.is_file():
                    try:
                        session = json.loads(session_file.read_text(encoding="utf-8"))
                        entry["state"] = session.get("state")
                        entry["mode"] = session.get("mode", "spec")
                        history = session.get("history") or []
                        entry["updated_at"] = history[-1].get("at") if history else None
                    except (OSError, json.JSONDecodeError):
                        entry["state"] = "unreadable"
                    entry["updated_at"] = entry["updated_at"] or datetime.fromtimestamp(
                        session_file.stat().st_mtime, timezone.utc
                    ).replace(microsecond=0).isoformat()
                entries.append(entry)
    return sorted(entries, key=lambda item: item["updated_at"] or "", reverse=True)


@dataclass
class SessionLock:
    root: Path
    timeout_seconds: float = 3.0
    poll_seconds: float = 0.025

    def __post_init__(self) -> None:
        self.path = self.root / LOCK_NAME
        self.fd: int | None = None

    def __enter__(self) -> "SessionLock":
        deadline = time.monotonic() + self.timeout_seconds
        payload = json.dumps({"pid": os.getpid(), "created_at": utc_now()})
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, payload.encode("utf-8"))
                os.fsync(self.fd)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise FlowError(
                        "session_locked",
                        "Another writer owns the spec session lock; retry after it finishes.",
                        lock=str(self.path),
                    )
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def read_session(root: Path) -> dict[str, Any]:
    path = root / SESSION_NAME
    if not path.is_file():
        raise FlowError("session_missing", f"No {SESSION_NAME} exists at the requested root.", root=str(root))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FlowError("session_invalid", f"Cannot read a valid session: {error}", path=str(path)) from error
    validate_session(value)
    return value


def validate_session(session: dict[str, Any]) -> None:
    if not isinstance(session, dict):
        raise FlowError("session_invalid", "The session must be an object.")
    required = {"schema_version", "session_id", "state", "revision", "spec_version", "rigor"}
    missing = sorted(required - session.keys())
    if missing:
        raise FlowError("session_invalid", "Session is missing required fields.", missing=missing)
    if not isinstance(session["state"], str) or session["state"] not in ALL_STATES:
        raise FlowError("session_invalid", "Session contains an unknown lifecycle state.", state=session["state"])
    if not isinstance(session["revision"], int) or session["revision"] < 0:
        raise FlowError("session_invalid", "Session revision must be a non-negative integer.")
    if not isinstance(session["spec_version"], int) or session["spec_version"] < 1:
        raise FlowError("session_invalid", "Spec version must be a positive integer.")
    for field in ("blocking_decisions", "critical_findings", "material_findings", "iterations", "completed_phases", "history"):
        if field in session and not isinstance(session[field], list):
            raise FlowError("session_invalid", f"Session field {field} must be a list.", field=field)
    # Downstream code reads these entries as records. Reject a hand-edited session once, here,
    # rather than guarding every reader.
    for field in ("blocking_decisions", "critical_findings", "material_findings", "iterations", "history"):
        for index, item in enumerate(session.get(field) or []):
            if not isinstance(item, dict):
                raise FlowError(
                    "session_invalid", f"Session field {field} must contain objects.", field=field, index=index
                )
    for index, item in enumerate(session.get("completed_phases") or []):
        if not isinstance(item, str):
            raise FlowError("session_invalid", "completed_phases must contain state names.", index=index)
    for field in ("approval", "baseline", "authorization"):
        if session.get(field) is not None and not isinstance(session[field], dict):
            raise FlowError("session_invalid", f"Session field {field} must be an object or null.", field=field)
    if str(session.get("mode", "spec")) not in {"spec", "check"}:
        raise FlowError("session_invalid", "Session mode must be spec or check.", mode=str(session.get("mode")))
    if (str(session.get("mode", "spec")) == "check") != (session["state"] in CHECK_STATES):
        if session["state"] not in {"BLOCKED"}:
            raise FlowError(
                "session_invalid",
                "Session state does not belong to the recorded mode.",
                mode=session.get("mode", "spec"),
                state=session["state"],
            )


def record_event(session: dict[str, Any], event: str, **details: Any) -> None:
    session.setdefault("history", []).append({"at": utc_now(), "event": event, **details})


def mutate_session(
    root: Path,
    expected_revision: int | None,
    event: str,
    callback: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    with SessionLock(root):
        session = read_session(root)
        if expected_revision is not None and session["revision"] != expected_revision:
            raise FlowError(
                "stale_revision",
                "The session changed since it was read; reload status before retrying.",
                expected_revision=expected_revision,
                actual_revision=session["revision"],
            )
        previous_state = session["state"]
        callback(session)
        validate_session(session)
        session["revision"] += 1
        record_event(session, event, from_state=previous_state, to_state=session["state"])
        atomic_write_json(root / SESSION_NAME, session)
        return session


def open_items(values: Any) -> list[dict[str, Any]]:
    """Tolerate a hand-edited session: non-object entries are ignored, never crashed on."""
    return [
        item
        for item in as_list(values)
        if isinstance(item, dict) and str(item.get("status", "open")).lower() in OPEN_STATUSES
    ]


def normalize_semantic_text(text: str, *, task_progress: bool = False) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if task_progress:
        normalized = re.sub(r"^(\s*[-*]\s+\[)[ xX](\])", r"\1 \2", normalized, flags=re.MULTILINE)
    lines = [line.rstrip() for line in normalized.split("\n")]
    return "\n".join(lines).rstrip() + "\n"


def semantic_paths(root: Path) -> list[Path]:
    paths = [root / name for name in REQUIRED_PLANNING_FILES]
    test_plan = root / "test-plan.md"
    evaluation_plan = root / "evaluation-plan.md"
    if test_plan.is_file():
        paths.append(test_plan)
    if evaluation_plan.is_file():
        paths.append(evaluation_plan)
    specs = sorted(path for path in (root / "specs").glob("**/*.md") if path.is_file())
    paths.extend(specs)
    return sorted(set(paths), key=lambda path: path.relative_to(root).as_posix())


def template_placeholders(source_name: str) -> set[str]:
    template = (ASSET_ROOT / source_name).read_text(encoding="utf-8")
    return set(PLACEHOLDER_RE.findall(template))


def source_template_for(relative: str) -> str | None:
    if relative == "evaluation-plan.md":
        return "test-plan.template.md"
    if relative.startswith("specs/") and relative.endswith(".md"):
        return "spec.template.md"
    for source_name, target_name in TEMPLATE_TARGETS.items():
        if relative == target_name:
            return source_name
    return None


def validate_package(root: Path) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED_PLANNING_FILES:
        path = root / name
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            errors.append(f"missing or empty: {name}")
    if not (root / "test-plan.md").is_file() and not (root / "evaluation-plan.md").is_file():
        errors.append("missing: test-plan.md or evaluation-plan.md")
    specs = [path for path in (root / "specs").glob("**/*.md") if path.is_file()]
    if not specs:
        errors.append("missing: specs/<capability>/spec.md")
    for path in semantic_paths(root):
        relative = path.relative_to(root).as_posix()
        source_name = source_template_for(relative)
        if source_name is None or not path.is_file():
            # A missing file is already reported above; reading it would crash the whole command.
            continue
        text = path.read_text(encoding="utf-8")
        remaining = sorted(template_placeholders(source_name) & set(PLACEHOLDER_RE.findall(text)))
        if remaining:
            preview = ", ".join(remaining[:3])
            suffix = " ..." if len(remaining) > 3 else ""
            errors.append(f"unfilled template placeholders: {relative}: {preview}{suffix}")
    return errors


def semantic_fingerprint(root: Path) -> dict[str, Any]:
    errors = validate_package(root)
    if errors:
        raise FlowError("package_not_ready", "The Standard planning package is incomplete.", errors=errors)
    manifest: dict[str, str] = {}
    for path in semantic_paths(root):
        relative = path.relative_to(root).as_posix()
        digest = hashlib.sha256(normalize_semantic_text(path.read_text(encoding="utf-8"), task_progress=relative == "tasks.md").encode("utf-8"))
        manifest[relative] = digest.hexdigest()
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {"algorithm": "sha256", "fingerprint": hashlib.sha256(encoded).hexdigest(), "manifest": manifest}


def fingerprint_delta(expected: dict[str, str], actual: dict[str, str]) -> dict[str, list[str]]:
    expected_keys = set(expected)
    actual_keys = set(actual)
    return {
        "added": sorted(actual_keys - expected_keys),
        "removed": sorted(expected_keys - actual_keys),
        "modified": sorted(key for key in expected_keys & actual_keys if expected[key] != actual[key]),
    }


def assert_baseline(session: dict[str, Any], root: Path) -> dict[str, Any]:
    baseline = session.get("baseline")
    approval = session.get("approval")
    if not baseline or not approval:
        raise FlowError("approval_missing", "No explicit approved baseline exists.")
    if approval.get("spec_version") != session["spec_version"]:
        raise FlowError("approval_stale", "Approval belongs to another spec version.")
    if is_blank(approval.get("actor")) or approval.get("fingerprint") != baseline.get("fingerprint"):
        raise FlowError("approval_stale", "Approval is not bound to the recorded baseline and actor.")
    if (session.get("expected_change_surface", []) != baseline.get("expected_change_surface", [])
            or approval.get("expected_change_surface", []) != baseline.get("expected_change_surface", [])):
        raise FlowError("planned_surface_drift", "The planned change surface changed after approval; revise first.")
    current = semantic_fingerprint(root)
    if baseline.get("fingerprint") != current["fingerprint"]:
        raise FlowError(
            "baseline_drift",
            "Semantic planning artifacts changed after approval; revise and reapprove before apply.",
            delta=fingerprint_delta(baseline.get("manifest", {}), current["manifest"]),
            approved_fingerprint=baseline.get("fingerprint"),
            current_fingerprint=current["fingerprint"],
        )
    return current


def next_actions(session: dict[str, Any]) -> list[str]:
    state = session["state"]
    mapping = {
        "NEW": ["capture raw intent"],
        "INTENT_CAPTURED": ["discover repository context"],
        "WAITING_APPROVAL": ["review the Review Brief, then explicitly approve or revise"],
        "APPROVED": ["invoke apply as a separate action"],
        "IMPLEMENTING": ["complete approved tasks and record implementation evidence"],
        "POST_IMPLEMENTATION_REVIEW": ["disposition every unexpected actual-diff surface"],
        "VERIFYING": ["run mapped verification and record a PASS or governed waiver verdict"],
        "DONE": [],
        "BLOCKED": ["resolve the listed material decision"],
        "REVISING": ["update affected artifacts and return to cross-artifact review"],
        "CHECK_NEW": ["resolve every source and preserve it verbatim in source.md"],
        "CHECK_SOURCE_RESOLVED": ["analyze the actual change and reconcile it with stated intent"],
        "CHECK_CHANGE_ANALYZED": ["map the affected surface and choose the depth mode"],
        "CHECK_SURFACE_MAPPED": ["run risk pass 1 and record it with check-pass"],
        "CHECK_RISK_ITERATING": ["run the next risk pass until the stop rule is satisfied"],
        "CHECK_CASES_DRAFTED": ["review every case against the quality rubric"],
        "CHECK_CASES_REVIEWED": ["plan the environment, fixtures, and missing capabilities"],
        "CHECK_ENVIRONMENT_PLANNED": ["render and validate the Verification Brief"],
        "CHECK_BRIEF_READY": ["present the brief; execution needs explicit check-authorize"],
        "CHECK_EXECUTING": ["run the authorized cases and record evidence"],
        "CHECK_DONE": [],
    }
    return mapping.get(state, ["advance the current planning phase with evidence"])


def render_status(session: dict[str, Any], root: Path) -> dict[str, Any]:
    baseline_status = "none"
    baseline_delta: dict[str, list[str]] | None = None
    if session.get("baseline"):
        try:
            assert_baseline(session, root)
            baseline_status = "matching"
        except FlowError as error:
            baseline_status = "drifted" if error.code in {"baseline_drift", "planned_surface_drift"} else "invalid"
            baseline_delta = error.details.get("delta")
    check_block: dict[str, Any] = {}
    if session.get("mode") == "check":
        check_block = {
            "check_mode": session.get("check_mode", "standard"),
            "iteration": iteration_state(session, resolve_config(root)),
            "authorization": session.get("authorization"),
        }
    return {
        "ok": True,
        "root": str(root),
        "session_id": session["session_id"],
        "mode": session.get("mode", "spec"),
        **check_block,
        "state": session["state"],
        "revision": session["revision"],
        "spec_version": session["spec_version"],
        "rigor": session["rigor"],
        "completed_phases": session.get("completed_phases", []),
        "blocking_decisions": open_items(session.get("blocking_decisions", [])),
        "critical_findings": open_items(session.get("critical_findings", [])),
        "material_findings": session.get("material_findings", []),
        "approval": session.get("approval"),
        "baseline_status": baseline_status,
        "baseline_delta": baseline_delta,
        "expected_change_surface": session.get("expected_change_surface", []),
        "actual_change_surface": session.get("actual_change_surface", []),
        "verification_generation": session.get("verification_generation", 0),
        "next_actions": next_actions(session),
    }


def copy_templates(root: Path, title: str, mapping: dict[str, str] = TEMPLATE_TARGETS) -> None:
    for source_name, target_name in mapping.items():
        target = root / target_name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            continue
        content = (ASSET_ROOT / source_name).read_text(encoding="utf-8")
        target.write_text(content.replace("<change title>", title), encoding="utf-8", newline="\n")


def new_session(session_id: str, rigor: str, state: str, mode: str = "spec") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "session_id": session_id,
        "mode": mode,
        "state": state,
        "revision": 0,
        "spec_version": 1,
        "rigor": rigor,
        "completed_phases": ["NEW"] if state == "INTENT_CAPTURED" else [],
        "blocking_decisions": [],
        "critical_findings": [],
        "approval": None,
        "baseline": None,
        "expected_change_surface": [],
        "actual_change_surface": [],
        "material_findings": [],
        "blocked_from": None,
        "history": [{"at": utc_now(), "event": "session_initialized", "to_state": state}],
    }


def cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_package_root(args, "changes")
    root.mkdir(parents=True, exist_ok=True)
    if (root / SESSION_NAME).exists():
        raise FlowError("session_exists", "Refusing to overwrite an existing spec session.", root=str(root))
    copy_templates(root, args.title or args.session_id)
    state = "NEW"
    if args.intent_file:
        raw = Path(args.intent_file).expanduser().read_text(encoding="utf-8")
        intent_path = root / "intent.md"
        content = intent_path.read_text(encoding="utf-8")
        content = content.replace("<Preserve the user's exact wording or transcript without normalization.>", raw.rstrip())
        intent_path.write_text(content, encoding="utf-8", newline="\n")
        state = "INTENT_CAPTURED"
    session = new_session(args.session_id, args.rigor, state)
    atomic_write_json(root / SESSION_NAME, session)
    result = render_status(session, root)
    result["store"] = str(store_root(getattr(args, "store", None)))
    result["project"] = str(find_project_root(getattr(args, "project", None)))
    return result


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    return render_status(read_session(root), root)


def cmd_validate(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    session = read_session(root)
    errors = validate_package(root)
    fingerprint = None
    if not errors:
        fingerprint = semantic_fingerprint(root)["fingerprint"]
    return {"ok": not errors, "state": session["state"], "errors": errors, "fingerprint": fingerprint}


def cmd_fingerprint(args: argparse.Namespace) -> dict[str, Any]:
    return {"ok": True, **semantic_fingerprint(resolve_root(args.root))}


def cmd_advance(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    target = args.to.upper()
    if target not in ALL_STATES:
        raise FlowError("unknown_state", "The requested target state is unknown.", target=target)
    if target in PRIVILEGED_TARGETS:
        raise FlowError("privileged_transition", "Use approve, apply, or complete for this target.", target=target)

    def change(session: dict[str, Any]) -> None:
        current = session["state"]
        is_check = session.get("mode") == "check"
        table = CHECK_TRANSITIONS if is_check else ORDINARY_TRANSITIONS
        allowed = table.get(current, set())
        if target not in allowed:
            raise FlowError("illegal_transition", "The lifecycle transition is not allowed.", current=current, target=target)
        if is_check:
            advance_check_guard(root, session, target)
        if target == "WAITING_APPROVAL":
            errors = validate_package(root)
            if errors:
                raise FlowError(
                    "package_not_ready",
                    "Fill every Standard planning artifact before requesting approval.",
                    errors=errors,
                )
            blockers = open_items(session.get("blocking_decisions", []))
            critical = open_items(session.get("critical_findings", []))
            if blockers or critical:
                raise FlowError(
                    "review_blocked",
                    "Resolve blocking decisions and Critical findings before requesting approval.",
                    blockers=blockers,
                    critical=critical,
                )
            if not readiness_is_approvable(root):
                raise FlowError(
                    "review_not_ready",
                    "review.md must record READY or READY_WITH_WAIVERS before requesting approval.",
                )
        if current == "POST_IMPLEMENTATION_REVIEW" and target == "VERIFYING":
            unresolved = open_items(session.get("material_findings", []))
            revision_required = [
                item for item in session.get("material_findings", [])
                if item.get("disposition") == "baseline-revision-required"
            ]
            if unresolved or revision_required:
                raise FlowError(
                    "post_review_incomplete",
                    "Disposition all actual-diff findings and revise material baseline deviations first.",
                    unresolved=unresolved,
                    revision_required=revision_required,
                )
            assert_baseline(session, root)
            assert_actual_diff(session, root)
        if current == "IMPLEMENTING" and target == "POST_IMPLEMENTATION_REVIEW":
            raise FlowError("actual_diff_required", "Use begin-verify to record the actual diff before review.")
        session.setdefault("completed_phases", []).append(current)
        session["completed_phases"] = list(dict.fromkeys(session["completed_phases"]))
        session["state"] = target

    session = mutate_session(root, args.expected_revision, "phase_advanced", change)
    return render_status(session, root)


def cmd_block(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)

    def change(session: dict[str, Any]) -> None:
        if session["state"] in {"DONE", "IMPLEMENTING", "VERIFYING", "CHECK_EXECUTING", "CHECK_DONE"}:
            raise FlowError(
                "block_not_allowed",
                "This state cannot be converted into a planning blocker.",
                state=session["state"],
            )
        if any(item.get("id") == args.decision_id for item in session.get("blocking_decisions", [])):
            raise FlowError("decision_exists", "A blocking decision with this ID already exists.")
        session.setdefault("blocking_decisions", []).append(
            {"id": args.decision_id, "summary": args.summary, "status": "open", "created_at": utc_now()}
        )
        if session["state"] != "BLOCKED":
            session["blocked_from"] = session["state"]
        session["state"] = "BLOCKED"

    session = mutate_session(root, args.expected_revision, "session_blocked", change)
    return render_status(session, root)


def cmd_resolve(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)

    def change(session: dict[str, Any]) -> None:
        found = False
        for item in session.get("blocking_decisions", []):
            if item.get("id") == args.decision_id:
                item.update({"status": "resolved", "resolution": args.resolution, "resolved_at": utc_now()})
                found = True
        if not found:
            raise FlowError("decision_missing", "No blocking decision has the requested ID.")
        if session["state"] == "BLOCKED" and not open_items(session.get("blocking_decisions", [])):
            session["state"] = session.get("blocked_from") or "CONTEXT_DISCOVERY"
            session["blocked_from"] = None

    session = mutate_session(root, args.expected_revision, "decision_resolved", change)
    return render_status(session, root)


def readiness_is_approvable(root: Path) -> bool:
    text = (root / "review.md").read_text(encoding="utf-8").upper()
    return bool(re.search(r"READINESS\s*:\s*(READY|READY_WITH_WAIVERS|READY WITH WAIVERS)\b", text))


def cmd_approve(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    if is_blank(args.actor):
        raise FlowError("actor_required", "Approval must name the human who granted it.")

    def change(session: dict[str, Any]) -> None:
        if session["state"] != "WAITING_APPROVAL":
            raise FlowError("approval_not_allowed", "Approval is valid only from WAITING_APPROVAL.", state=session["state"])
        blockers = open_items(session.get("blocking_decisions", []))
        critical = open_items(session.get("critical_findings", []))
        if blockers or critical:
            raise FlowError("approval_blocked", "Open blocking decisions or Critical findings remain.", blockers=blockers, critical=critical)
        if not readiness_is_approvable(root):
            raise FlowError("review_not_ready", "review.md must record READY or READY_WITH_WAIVERS.")
        baseline = semantic_fingerprint(root)
        planned = normalize_planned_paths(getattr(args, "planned", None) or session.get("expected_change_surface", []))
        baseline["expected_change_surface"] = planned
        session["expected_change_surface"] = planned
        approved_at = utc_now()
        session["baseline"] = baseline
        session["approval"] = {
            "actor": args.actor,
            "at": approved_at,
            "note": args.note,
            "spec_version": session["spec_version"],
            "fingerprint": baseline["fingerprint"],
            "expected_change_surface": planned,
        }
        session["state"] = "APPROVED"

    session = mutate_session(root, args.expected_revision, "baseline_approved", change)
    return render_status(session, root)


def cmd_revise(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)

    def change(session: dict[str, Any]) -> None:
        if session["state"] not in {"WAITING_APPROVAL", "APPROVED", "IMPLEMENTING", "POST_IMPLEMENTATION_REVIEW", "VERIFYING"}:
            raise FlowError("revision_not_allowed", "Revision is not valid from the current state.", state=session["state"])
        session["approval"] = None
        session["baseline"] = None
        session["spec_version"] += 1
        session["state"] = "REVISING"
        session.setdefault("revision_reasons", []).append({"at": utc_now(), "reason": args.reason})

    session = mutate_session(root, args.expected_revision, "baseline_invalidated", change)
    return render_status(session, root)


def cmd_apply(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)

    def change(session: dict[str, Any]) -> None:
        if session["state"] != "APPROVED":
            raise FlowError("apply_not_allowed", "Apply requires an explicit current APPROVED baseline.", state=session["state"])
        assert_baseline(session, root)
        copy_templates(root, session["session_id"], POST_IMPLEMENTATION_TEMPLATE_TARGETS)
        session["state"] = "IMPLEMENTING"

    session = mutate_session(root, args.expected_revision, "implementation_started", change)
    return render_status(session, root)


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_manifest(root: Path) -> dict[str, str]:
    manifest: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name != ".git")
        links = [name for name in dirs if (Path(current) / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in links]
        for name in sorted([name for name in files if name != ".git"] + links):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                entry = {"kind": "symlink", "target": os.readlink(path)}
            elif path.is_file():
                entry = {"kind": "file", "executable": bool(path.stat().st_mode & 0o111), "sha256": hash_file(path)}
            else:
                raise FlowError("diff_unsupported_entry", "Only regular files and symlinks can be inventoried.", path=str(path))
            manifest[relative] = hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()
    return manifest


def tree_diff(before: Path, after: Path) -> dict[str, Any]:
    if not before.is_dir() or not after.is_dir():
        raise FlowError("diff_input_missing", "Both before and after paths must be directories.")
    old = tree_manifest(before)
    new = tree_manifest(after)
    files: list[dict[str, Any]] = []
    for path in sorted(set(old) | set(new)):
        if path not in old:
            files.append({"path": path, "status": "added", "before_sha256": None, "after_sha256": new[path]})
        elif path not in new:
            files.append({"path": path, "status": "deleted", "before_sha256": old[path], "after_sha256": None})
        elif old[path] != new[path]:
            files.append({"path": path, "status": "modified", "before_sha256": old[path], "after_sha256": new[path]})
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "before": str(before.resolve()),
        "after": str(after.resolve()),
        "files": files,
        "counts": {
            "added": sum(item["status"] == "added" for item in files),
            "modified": sum(item["status"] == "modified" for item in files),
            "deleted": sum(item["status"] == "deleted" for item in files),
            "total": len(files),
        },
    }


def path_is_planned(path: str, planned: Iterable[str]) -> bool:
    for item in planned:
        normalized = item.strip().strip("/")
        if normalized and (path == normalized or path.startswith(normalized + "/")):
            return True
    return False


def normalize_planned_paths(planned: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in planned:
        if not isinstance(value, str):
            raise FlowError("planned_surface_invalid", "Planned paths must be repository-relative strings.")
        item = value.strip().replace("\\", "/").rstrip("/")
        if not item or item.startswith("/") or re.match(r"^[A-Za-z]:", item) or any(part in {"", ".", ".."} for part in item.split("/")):
            raise FlowError("planned_surface_invalid", "Planned paths must be bounded repository-relative paths.", path=value)
        result.append(item)
    return list(dict.fromkeys(result))


def assert_actual_diff(session: dict[str, Any], root: Path) -> dict[str, Any]:
    path = root / "actual-diff.json"
    if not path.is_file():
        raise FlowError("actual_diff_required", "Record an actual diff with begin-verify before continuing.")
    inventory = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(inventory, dict) or not isinstance(inventory.get("files"), list):
        raise FlowError("diff_input_invalid", "The actual diff must contain a files inventory.")
    if inventory.get("files") != session.get("actual_change_surface"):
        raise FlowError("actual_diff_drift", "The actual diff no longer matches the reviewed session inventory.")
    try:
        current = tree_diff(Path(inventory["before"]), Path(inventory["after"]))
    except (KeyError, TypeError) as error:
        raise FlowError("diff_input_invalid", "The diff must identify its before and after snapshot directories.") from error
    if current["files"] != inventory["files"]:
        raise FlowError("actual_diff_drift", "The implementation changed after its inventory was reviewed; record and review a fresh diff.")
    return inventory


def record_verification_inventory(args: argparse.Namespace, *, reopen: bool = False) -> dict[str, Any]:
    root = resolve_root(args.root)
    inventory = tree_diff(resolve_root(args.before), resolve_root(args.after))

    def change(session: dict[str, Any]) -> None:
        allowed = {"POST_IMPLEMENTATION_REVIEW", "VERIFYING", "DONE"} if reopen else {"IMPLEMENTING"}
        if session["state"] not in allowed:
            raise FlowError("verify_not_allowed", "The current state cannot start this actual-diff review.", state=session["state"], allowed=sorted(allowed))
        assert_baseline(session, root)
        if reopen:
            if is_blank(args.reason):
                raise FlowError("reason_required", "Reopening verification requires the cause, such as a new integration base.")
            revision_required = [item for item in session.get("material_findings", []) if item.get("disposition") == "baseline-revision-required"]
            if revision_required:
                raise FlowError("post_review_incomplete", "Reopening verification cannot clear a finding that requires baseline revision.", revision_required=revision_required)
            previous_report = root / (session.get("verification_report") or "verification-report.md")
            if previous_report.is_file():
                session.setdefault("invalidated_report_hashes", []).append(hash_file(previous_report))
            session.setdefault("verification_history", []).append({
                "at": utc_now(), "reason": args.reason,
                "generation": session.get("verification_generation", 0),
                "actual_change_surface": session.get("actual_change_surface", []),
                "material_findings": session.get("material_findings", []),
                "verdict": session.get("verification_verdict"),
                "report": session.get("verification_report"),
            })
            session["verification_verdict"] = None
            session["verification_report"] = None
        planned = session.get("expected_change_surface", [])
        supplied = normalize_planned_paths(getattr(args, "planned", None) or [])
        if any(not path_is_planned(path, planned) for path in supplied):
            raise FlowError("planned_surface_drift", "begin-verify cannot expand the approved surface; supply --planned during approve.")
        unexpected = [item for item in inventory["files"] if not path_is_planned(item["path"], planned)]
        session["expected_change_surface"] = planned
        session["actual_change_surface"] = inventory["files"]
        session["material_findings"] = [
            {
                "id": f"DIFF-{index:03d}",
                "path": item["path"],
                "status": "needs-disposition",
                "material": None,
                "change_status": item["status"],
            }
            for index, item in enumerate(unexpected, start=1)
        ]
        atomic_write_json(root / "actual-diff.json", inventory)
        session["verification_generation"] = session.get("verification_generation", 0) + 1
        session["state"] = "POST_IMPLEMENTATION_REVIEW"

    session = mutate_session(root, args.expected_revision, "verification_reopened" if reopen else "actual_diff_recorded", change)
    result = render_status(session, root)
    result["actual_diff"] = inventory
    return result


def cmd_begin_verify(args: argparse.Namespace) -> dict[str, Any]:
    return record_verification_inventory(args)


def cmd_reopen_verify(args: argparse.Namespace) -> dict[str, Any]:
    return record_verification_inventory(args, reopen=True)


def cmd_disposition(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)

    def change(session: dict[str, Any]) -> None:
        if session["state"] != "POST_IMPLEMENTATION_REVIEW":
            raise FlowError("disposition_not_allowed", "Diff findings are dispositioned during POST_IMPLEMENTATION_REVIEW.")
        found = False
        for item in session.get("material_findings", []):
            if item.get("path") == args.path:
                item.update(
                    {
                        "status": "dispositioned",
                        "material": args.material,
                        "disposition": args.disposition,
                        "evidence": args.evidence,
                        "dispositioned_at": utc_now(),
                    }
                )
                found = True
        if not found:
            raise FlowError("finding_missing", "No unexpected diff finding matches the requested path.")

    session = mutate_session(root, args.expected_revision, "diff_finding_dispositioned", change)
    return render_status(session, root)


def cmd_complete(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    verdict = args.verdict.upper()

    def change(session: dict[str, Any]) -> None:
        is_check = session.get("mode") == "check"
        allowed_verdicts = {"PASS", "PASS_WITH_WAIVERS", "FAIL"} if is_check else {"PASS", "PASS_WITH_WAIVERS"}
        if verdict not in allowed_verdicts:
            raise FlowError(
                "invalid_verdict",
                "Completion requires one of the allowed verdicts.",
                allowed=sorted(allowed_verdicts),
            )
        expected_state = "CHECK_EXECUTING" if is_check else "VERIFYING"
        if session["state"] != expected_state:
            raise FlowError(
                "completion_not_allowed",
                f"Completion is valid only from {expected_state}.",
                state=session["state"],
            )
        report = root / args.report
        try:
            report.resolve().relative_to(root.resolve())
        except ValueError:
            raise FlowError(
                "report_outside_package",
                "The verification report must live inside the package; an unrelated file cannot satisfy this gate.",
                report=str(report),
                root=str(root),
            ) from None
        if not report.is_file() or not report.read_text(encoding="utf-8").strip():
            raise FlowError("verification_report_missing", "A non-empty verification report is required.", report=str(report))
        placeholders = template_placeholders("verification-report.template.md") & set(PLACEHOLDER_RE.findall(report.read_text(encoding="utf-8")))
        if placeholders:
            raise FlowError("verification_report_incomplete", "Fill the verification report with observed results before completion.", placeholders=sorted(placeholders))
        if hash_file(report) in session.get("invalidated_report_hashes", []):
            raise FlowError("verification_report_stale", "The previous verification report was invalidated; record results for the current revision before completion.")
        if is_check:
            assert_check_execution(root, session)
        else:
            assert_baseline(session, root)
            assert_actual_diff(session, root)
        unresolved = open_items(session.get("blocking_decisions", [])) + open_items(session.get("critical_findings", [])) + open_items(session.get("material_findings", []))
        revision_required = [
            item for item in session.get("material_findings", [])
            if item.get("disposition") == "baseline-revision-required"
        ]
        if unresolved or revision_required:
            raise FlowError("completion_blocked", "Unresolved or revision-requiring findings remain.", findings=unresolved + revision_required)
        session["verification_verdict"] = verdict
        session["verification_report"] = args.report
        session["state"] = "CHECK_DONE" if is_check else "DONE"

    session = mutate_session(root, args.expected_revision, "session_completed", change)
    return render_status(session, root)


# --------------------------------------------------------------------------------------
# Check mode: project configuration
# --------------------------------------------------------------------------------------


def scalar(text: str) -> Any:
    value = text.strip()
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise FlowError("config_invalid", "Invalid quoted scalar in verification YAML.", value=value) from error
        if not isinstance(parsed, str):
            raise FlowError("config_invalid", "Expected a quoted scalar string.")
        return parsed
    if value.startswith("'"):
        if not re.fullmatch(r"'(?:[^']|'')*'", value):
            raise FlowError("config_invalid", "Invalid single-quoted scalar in verification YAML.", value=value)
        return value[1:-1].replace("''", "'")
    if value and (value[0] in "[]{}|>*&!%@`," or re.search(r":(?:\s|$)", value)):
        raise FlowError(
            "config_invalid",
            "Unsupported or invalid YAML scalar; use block mappings and scalar lists, or JSON configuration.",
            value=value,
        )
    lowered = value.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none", "~", ""}:
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def parse_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the restricted YAML subset used by the verification config.

    Supported: nested maps, lists of scalars, scalars, comments, blank lines.
    Anything else is a configuration error rather than a silent misread.
    """
    if any(ord(char) < 32 and char not in "\n\r\t" or ord(char) == 127 for char in text):
        raise FlowError("config_invalid", "Control characters are not valid verification YAML.")
    lines: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        quote = ""
        escaped = False
        end = len(raw)
        for index, char in enumerate(raw):
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif quote == "'" and char == "'" and index + 1 < len(raw) and raw[index + 1] == "'":
                escaped = True
            elif quote and char == quote:
                quote = ""
            elif not quote and char in "\"'" and (index == 0 or raw[index - 1].isspace() or raw[index - 1] == ":"):
                quote = char
            elif not quote and char == "#" and (index == 0 or raw[index - 1].isspace()):
                end = index
                break
        line = raw[:end].rstrip()
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise FlowError("config_invalid", f"Tab indentation at line {number}; use spaces.", line=raw)
        indent = len(line) - len(line.lstrip(" "))
        body = line.strip()
        lines.append((number, indent, body))

    def is_item(body: str) -> bool:
        return body == "-" or body.startswith("- ")

    def block(index: int, indent: int, sequence: bool = False) -> tuple[Any, int]:
        result: Any = [] if sequence else {}
        while index < len(lines):
            number, level, body = lines[index]
            if level < indent or level == indent and sequence and not is_item(body):
                break
            if level != indent:
                raise FlowError("config_invalid", f"Unexpected indentation at line {number}.")
            if sequence:
                value = body[1:].strip()
                if not value:
                    raise FlowError("config_invalid", f"Only scalar list items are supported at line {number}.")
                result.append(scalar(value))
                index += 1
                continue
            match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_.-]*):(?:\s+(.*))?", body)
            if not match:
                raise FlowError("config_invalid", f"Expected an unquoted 'key: value' at line {number}.")
            key, rest = match.group(1), match.group(2)
            if key in result:
                raise FlowError("config_invalid", f"Duplicate mapping key {key!r} at line {number}.")
            index += 1
            if rest:
                result[key] = scalar(rest)
            elif index < len(lines) and (lines[index][1] > indent or lines[index][1] == indent and is_item(lines[index][2])):
                result[key], index = block(index, lines[index][1], is_item(lines[index][2]))
            else:
                result[key] = {}
        return result, index

    if not lines:
        return {}
    if lines[0][1] != 0 or is_item(lines[0][2]):
        raise FlowError("config_invalid", "The verification config must be a top-level mapping without indentation.")
    result, end = block(0, 0)
    if end != len(lines):
        raise FlowError("config_invalid", f"Unexpected YAML content at line {lines[end][0]}.")
    return result


def deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            raise FlowError("config_invalid", "The verification config must be a mapping.", path=str(path))
        return loaded
    try:
        import yaml  # type: ignore
    except ImportError:
        return parse_simple_yaml(text)
    try:
        loaded = yaml.safe_load(text) or {}
    except Exception as error:  # any YAML parse failure must stay a structured config error
        raise FlowError("config_invalid", f"cannot parse the verification config: {error}", path=str(path)) from error
    if not isinstance(loaded, dict):
        raise FlowError("config_invalid", "The verification config must be a mapping.", path=str(path))
    return loaded


def find_config(root: Path) -> Path | None:
    current = root if root.is_dir() else root.parent
    for directory in [current, *current.parents]:
        for relative in CONFIG_RELATIVE_NAMES:
            candidate = directory / relative
            if candidate.is_file():
                return candidate
    return None


def resolve_config(root: Path, override: str | None = None) -> dict[str, Any]:
    """Return the merged verification config; parse failures surface as `_error`."""
    recorded = None
    if not override and (root / SESSION_NAME).is_file():
        try:
            recorded = json.loads((root / SESSION_NAME).read_text(encoding="utf-8")).get("verification_config_path")
        except (OSError, ValueError, AttributeError):
            pass
    selected = override or recorded
    path = Path(selected).expanduser().resolve() if selected else find_config(root)
    if not path:
        return sanitize_config(deep_merge(DEFAULT_CHECK_CONFIG, {"_source": "defaults"}))
    try:
        loaded = load_config_file(path)
    except Exception as error:  # a broken project config must never crash a lifecycle command
        merged = deep_merge(DEFAULT_CHECK_CONFIG, {})
        merged["_source"] = str(path)
        merged["_error"] = f"cannot read verification config: {error}"
        return sanitize_config(merged)
    merged = deep_merge(DEFAULT_CHECK_CONFIG, loaded if isinstance(loaded, dict) else {})
    merged["_source"] = str(path)
    return sanitize_config(merged)


def sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
    """Replace unusable config values with documented defaults and say so.

    A sloppy project config must never crash a lifecycle command and must never silently
    change the rigor of a check; every rejected value is reported in `_warnings`.
    """
    defaults = DEFAULT_CHECK_CONFIG["verification"]
    warnings: list[str] = []
    section = config.get("verification")
    if not isinstance(section, dict):
        if section is not None:
            warnings.append("verification: not a mapping; using defaults")
        section = {}
    clean = dict(defaults)
    for key, value in section.items():
        if key not in defaults:
            warnings.append(f"verification.{key}: unknown setting, ignored")
            continue
        if key in {"minimum_iterations", "maximum_iterations"}:
            try:
                number = int(value)
            except (TypeError, ValueError):
                warnings.append(f"verification.{key}: {value!r} is not a whole number; using {defaults[key]}")
                continue
            if number < 1:
                warnings.append(f"verification.{key}: must be at least 1; using {defaults[key]}")
                continue
            clean[key] = number
        elif key == "default_mode":
            if str(value).lower() in ("auto", *CHECK_MODES):
                clean[key] = str(value).lower()
            else:
                warnings.append(f"verification.default_mode: {value!r} is not a known mode; using auto")
        elif key == "artifact_store":
            if str(value).lower() in ("global", "repo"):
                clean[key] = str(value).lower()
            else:
                warnings.append(f"verification.artifact_store: {value!r} is not global or repo; using global")
        elif key == "critical_areas":
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                clean[key] = value
            else:
                warnings.append("verification.critical_areas: must be a list of strings; using defaults")
        elif key == "required_risk_coverage":
            if not isinstance(value, dict):
                warnings.append("verification.required_risk_coverage: must be a mapping; using defaults")
                continue
            bucket = dict(defaults[key])
            for severity, percent in value.items():
                if str(severity).lower() not in SEVERITIES:
                    warnings.append(f"required_risk_coverage.{severity}: unknown severity, ignored")
                    continue
                try:
                    number = float(percent)
                except (TypeError, ValueError):
                    warnings.append(f"required_risk_coverage.{severity}: {percent!r} is not a number, ignored")
                    continue
                if not 0 <= number <= 100:
                    warnings.append(f"required_risk_coverage.{severity}: must be between 0 and 100, ignored")
                    continue
                bucket[str(severity).lower()] = number
            clean[key] = bucket
        elif isinstance(defaults[key], bool):
            if isinstance(value, bool):
                clean[key] = value
            else:
                warnings.append(f"verification.{key}: must be true or false; using {defaults[key]}")
        else:
            clean[key] = value
    config["verification"] = clean
    if warnings:
        config["_warnings"] = warnings
    return config


def verification_setting(config: dict[str, Any], key: str) -> Any:
    section = config.get("verification")
    if isinstance(section, dict) and key in section:
        return section[key]
    return DEFAULT_CHECK_CONFIG["verification"][key]


# --------------------------------------------------------------------------------------
# Check mode: depth and iteration
# --------------------------------------------------------------------------------------


def suggest_mode(paths: Iterable[str], config: dict[str, Any], hints: Iterable[str] = ()) -> dict[str, Any]:
    configured = str(verification_setting(config, "default_mode") or "auto").lower()
    areas = [str(area).lower() for area in verification_setting(config, "critical_areas") or []]
    haystacks = [str(item).lower().replace("\\", "/") for item in list(paths) + list(hints)]
    matched = sorted({area for area in areas for item in haystacks if area.replace("-", " ") in item.replace("-", " ").replace("_", " ")})
    # normalize separators everywhere, so the same change classifies identically on every platform
    path_list = [str(item).replace("\\", "/") for item in paths]
    if matched:
        mode = "deep"
        reason = f"touches critical areas: {', '.join(matched)}"
    elif configured in CHECK_MODES:
        mode = configured
        reason = f"project config pins default_mode to {configured}"
    elif path_list and len(path_list) <= 10 and all(LITE_PATH_RE.search(item) for item in path_list):
        mode = "lite"
        reason = "local presentation or documentation surface only"
    else:
        mode = "standard"
        reason = "ordinary product surface without a critical-area hit"
    return {
        "ok": True,
        "mode": mode,
        "reason": reason,
        "matched_critical_areas": matched,
        "path_count": len(path_list),
        "minimum_passes": minimum_passes(mode, config),
        "config_source": config.get("_source", "defaults"),
    }


def minimum_passes(mode: str, config: dict[str, Any]) -> int:
    baseline = int(verification_setting(config, "minimum_iterations") or 0)
    return max(1, baseline + MODE_PASS_DELTA.get(mode, 0))


def iteration_state(session: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    mode = str(session.get("check_mode", "standard")).lower()
    if mode not in MODE_PASS_DELTA:
        mode = "standard"
    passes = [item for item in session.get("iterations", []) if item.get("kind") in ANALYSIS_PASS_KINDS]
    minimum = minimum_passes(mode, config)
    maximum = max(minimum, int(verification_setting(config, "maximum_iterations") or minimum))
    # Reopening a finished brief starts a new window: quiet passes recorded before the reopen say
    # nothing about the risk that caused it.
    reset_at = int(session.get("iteration_reset_at") or 0)
    window = passes[reset_at:]
    dry_tail = 0
    for item in reversed(window):
        if item.get("material"):
            break
        dry_tail += 1
    ceiling_reached = len(passes) >= maximum
    may_stop = (len(passes) >= minimum and dry_tail >= 2) or ceiling_reached
    reasons: list[str] = []
    if len(passes) < minimum:
        reasons.append(f"{len(passes)}/{minimum} analysis passes run")
    if dry_tail < 2 and not ceiling_reached:
        since = " since the brief was reopened" if reset_at else ""
        reasons.append(f"{dry_tail}/2 consecutive passes without a new material risk{since}")
    return {
        "mode": mode,
        "analysis_passes": len(passes),
        "passes_since_reopen": len(window) if reset_at else None,
        "minimum": minimum,
        "maximum": maximum,
        "dry_tail": dry_tail,
        "may_stop": may_stop,
        "ceiling_reached": ceiling_reached,
        "blocking_reasons": reasons,
        "recorded": session.get("iterations", []),
    }


# --------------------------------------------------------------------------------------
# Check mode: package evaluation
# --------------------------------------------------------------------------------------


def load_check(root: Path) -> dict[str, Any]:
    path = root / "check.json"
    if not path.is_file():
        raise FlowError("check_missing", "check.json does not exist in this check package.", path=str(path))
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FlowError("check_invalid", f"Cannot read a valid check.json: {error}", path=str(path)) from error
    if not isinstance(value, dict):
        raise FlowError("check_invalid", "check.json must contain a JSON object.")
    return value


def check_template_placeholders() -> set[str]:
    return set(PLACEHOLDER_RE.findall((ASSET_ROOT / "check.template.json").read_text(encoding="utf-8")))


def iter_strings(value: Any, trail: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield trail, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from iter_strings(item, f"{trail}.{key}" if trail else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from iter_strings(item, f"{trail}[{index}]")


def as_list(value: Any) -> list[Any]:
    """Read a container defensively: a hand-edited check.json must fail validation, not crash."""
    return value if isinstance(value, list) else []


def as_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def join_ids(value: Any, separator: str = ", ") -> str:
    """Join identifier lists for display without assuming every entry is already a string."""
    return separator.join(str(item) for item in as_lines(value))


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"tbd", "todo", "n/a", "-"}
    if isinstance(value, (list, dict)):
        return not value
    return False


def coverage_report(check: dict[str, Any]) -> dict[str, Any]:
    cases = {str(item.get("id")): item for item in as_list((check.get("cases"))) if isinstance(item, dict)}
    waived = {str(item.get("risk")) for item in as_list((check.get("waivers"))) if isinstance(item, dict)}
    report: dict[str, Any] = {}
    for severity in SEVERITIES:
        risks = [
            item
            for item in as_list((check.get("risks")))
            if isinstance(item, dict) and item.get("severity") == severity and item.get("disposition") != "dropped"
        ]
        covered: list[str] = []
        for risk in risks:
            if risk.get("disposition") == "waived" and risk.get("id") in waived:
                covered.append(str(risk.get("id")))
                continue
            accepted = [
                str(case_id)
                for case_id in as_list(risk.get("cases"))
                if str(case_id) in cases and as_mapping(cases[str(case_id)].get("review")).get("verdict") == "accepted"
            ]
            if accepted:
                covered.append(str(risk.get("id")))
        total = len(risks)
        report[severity] = {
            "total": total,
            "covered": len(covered),
            "percent": 100.0 if total == 0 else round(100.0 * len(covered) / total, 1),
            "uncovered": [str(item.get("id")) for item in risks if str(item.get("id")) not in covered],
        }
    return report


def validate_check(check: dict[str, Any], config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if config.get("_error"):
        errors.append(str(config["_error"]))

    placeholders = check_template_placeholders()
    for trail, text in iter_strings(check):
        if set(PLACEHOLDER_RE.findall(text)) & placeholders:
            errors.append(f"unfilled template placeholder in check.json: {trail}")

    if is_blank(check.get("title")):
        errors.append("check.json: title is empty")
    if str(check.get("mode", "")).lower() not in CHECK_MODES:
        errors.append("check.json: mode must be lite, standard, or deep")
    for field, kind in (
        ("sources", list), ("references", list), ("risks", list), ("cases", list),
        ("execution_plan", list), ("open_questions", list), ("waivers", list),
        ("changed_surface", list), ("affected_surface", list),
        ("environment", dict), ("intent", dict),
    ):
        if field in check and not isinstance(check[field], kind):
            errors.append(f"check.json: {field} must be a {'list' if kind is list else 'mapping'}")
    if not as_list(check.get("sources")):
        errors.append("check.json: at least one source is required")
    if not as_list(check.get("risks")):
        errors.append(
            "check.json: the risk register is empty; an empty register is a finding, not a pass"
        )
    scheduled = [
        item
        for item in as_list((check.get("risks")))
        if isinstance(item, dict) and str(item.get("disposition", "")).lower() == "verify"
    ]
    if not scheduled and is_blank(check.get("no_material_risk_rationale")):
        errors.append(
            "check.json: nothing is scheduled for verification; record no_material_risk_rationale "
            "explaining which surfaces were examined and why none needs a check"
        )

    for field, label in (("references", "reference"), ("open_questions", "open question")):
        seen_ids: set[str] = set()
        for item in as_list((check.get(field))):
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("id", ""))
            if not identifier:
                errors.append(f"check.json: a {label} has no id")
            elif identifier in seen_ids:
                errors.append(f"duplicate {label} id: {identifier}")
            seen_ids.add(identifier)

    reference_ids = {str(item.get("id")) for item in as_list((check.get("references"))) if isinstance(item, dict)}
    for item in as_list((check.get("references"))):
        if isinstance(item, dict) and is_blank(item.get("location")):
            errors.append(f"reference {item.get('id')}: location is empty")

    case_ids: set[str] = set()
    for case in as_list((check.get("cases"))):
        if not isinstance(case, dict):
            errors.append("check.json: every case must be an object")
            continue
        case_id = str(case.get("id", ""))
        if not case_id:
            errors.append("check.json: a case has no id")
            continue
        if case_id in case_ids:
            errors.append(f"duplicate case id: {case_id}")
        case_ids.add(case_id)
        for field in REQUIRED_CASE_FIELDS:
            if is_blank(case.get(field)):
                errors.append(f"{case_id}: {field} is empty")
        for index, step in enumerate(as_list(case.get("steps")), start=1):
            if not isinstance(step, dict):
                errors.append(f"{case_id}: step {index} must be an object")
                continue
            for field in REQUIRED_STEP_FIELDS:
                if is_blank(step.get(field)):
                    errors.append(f"{case_id}: step {index} has an empty {field}")
        oracle = case.get("oracle") if isinstance(case.get("oracle"), dict) else {}
        if verification_setting(config, "require_actual_effect_oracle"):
            if str(oracle.get("kind", "")).lower() not in ORACLE_KINDS:
                errors.append(f"{case_id}: oracle.kind must be one of {', '.join(ORACLE_KINDS)}")
            for field in ("source", "expected"):
                if is_blank(oracle.get(field)):
                    errors.append(f"{case_id}: oracle.{field} is empty; a status code is not an oracle")
        if verification_setting(config, "require_cleanup") and is_blank(case.get("cleanup")):
            errors.append(f"{case_id}: cleanup is empty")
        if verification_setting(config, "require_evidence") and is_blank(case.get("evidence")):
            errors.append(f"{case_id}: evidence is empty")
        if verification_setting(config, "require_references"):
            refs = as_list(case.get("references"))
            if not refs:
                errors.append(f"{case_id}: cite at least one reference document")
            for ref in refs:
                if str(ref) not in reference_ids:
                    errors.append(f"{case_id}: reference {ref} is not declared in check.json references")
        repeat = case.get("repeat", 1)
        if not isinstance(repeat, int) or isinstance(repeat, bool) or repeat < 1:
            errors.append(f"{case_id}: repeat must be a whole number of runs, at least 1")
        if str(case.get("safety", "")).lower() not in SAFETY_LEVELS:
            errors.append(f"{case_id}: safety must be one of {', '.join(SAFETY_LEVELS)}")
        verdict = as_mapping(case.get("review")).get("verdict")
        if str(verdict or "").lower() not in CASE_REVIEW_VERDICTS:
            errors.append(f"{case_id}: review.verdict must be one of {', '.join(CASE_REVIEW_VERDICTS)}")

    waived_risks = {str(item.get("risk")) for item in as_list((check.get("waivers"))) if isinstance(item, dict)}
    risk_ids: set[str] = set()
    for risk in as_list((check.get("risks"))):
        if not isinstance(risk, dict):
            errors.append("check.json: every risk must be an object")
            continue
        risk_id = str(risk.get("id", ""))
        if not risk_id:
            errors.append("check.json: a risk has no id")
            continue
        if risk_id in risk_ids:
            errors.append(f"duplicate risk id: {risk_id}")
        risk_ids.add(risk_id)
        for field in REQUIRED_RISK_FIELDS:
            if is_blank(risk.get(field)):
                errors.append(f"{risk_id}: {field} is empty")
        if str(risk.get("reality", "")).lower() not in REALITY_LEVELS:
            errors.append(f"{risk_id}: reality must be one of {', '.join(REALITY_LEVELS)}")
        if str(risk.get("severity", "")).lower() not in SEVERITIES:
            errors.append(f"{risk_id}: severity must be one of {', '.join(SEVERITIES)}")
        disposition = str(risk.get("disposition", "")).lower()
        if disposition not in DISPOSITIONS:
            errors.append(f"{risk_id}: disposition must be one of {', '.join(DISPOSITIONS)}")
        if disposition == "verify":
            mapped = [case_id for case_id in as_list(risk.get("cases"))]
            if not mapped:
                errors.append(f"{risk_id}: disposition verify requires at least one case")
            for case_id in mapped:
                if str(case_id) not in case_ids:
                    errors.append(f"{risk_id}: mapped case {case_id} does not exist")
        if disposition == "waived" and risk_id not in waived_risks:
            errors.append(f"{risk_id}: waived risks need a waiver entry with reason, owner, and revisit trigger")

    for case in as_list((check.get("cases"))):
        if isinstance(case, dict):
            for risk_id in as_list(case.get("risks")):
                if str(risk_id) not in risk_ids:
                    errors.append(f"{case.get('id')}: mapped risk {risk_id} does not exist")

    for waiver in as_list((check.get("waivers"))):
        if isinstance(waiver, dict):
            for field in ("reason", "owner", "revisit"):
                if is_blank(waiver.get(field)):
                    errors.append(f"waiver for {waiver.get('risk')}: {field} is empty")
            if str(waiver.get("risk", "")) not in risk_ids:
                errors.append(f"waiver references unknown risk {waiver.get('risk')}")

    for index, item in enumerate(as_list((check.get("execution_plan"))), start=1):
        if not isinstance(item, dict):
            errors.append(f"execution_plan entry {index} must be an object")
            continue
        if str(item.get("case", "")) not in case_ids:
            errors.append(f"execution_plan entry {index} references unknown case {item.get('case')}")

    coverage = coverage_report(check)
    thresholds = as_mapping(verification_setting(config, "required_risk_coverage"))
    for severity, required in thresholds.items():
        bucket = coverage.get(str(severity).lower())
        if bucket and bucket["percent"] < float(required):
            errors.append(
                f"{severity} risk coverage is {bucket['percent']}% but {required}% is required; "
                f"uncovered: {', '.join(bucket['uncovered']) or 'none'}"
            )
    return errors


def evaluate_check(root: Path, session: dict[str, Any] | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    session = session if session is not None else read_session(root)
    config = config if config is not None else resolve_config(root)
    check = load_check(root)
    errors = validate_check(check, config)
    session_mode = str(session.get("check_mode", "standard")).lower()
    if str(check.get("mode", "")).lower() != session_mode:
        errors.append(
            f"check.json mode is {check.get('mode')!r} but the session enforced {session_mode!r}; "
            "the brief would claim a different depth than the stop rule applied"
        )
    coverage = coverage_report(check)

    blocking_questions = [
        str(item.get("id"))
        for item in as_list((check.get("open_questions")))
        if isinstance(item, dict) and item.get("blocking") and str(item.get("status", "open")).lower() in OPEN_STATUSES
    ]
    authorization = as_mapping(session.get("authorization"))
    if authorization and authorization.get("fingerprint") != check_fingerprint(root, config):
        errors.append("execution authorization is stale: the check artifacts or verification configuration changed")
    allowed = SCOPE_ALLOWS.get(str(authorization.get("scope", "")).lower(), set())
    unauthorized = [
        str(case.get("id"))
        for case in as_list((check.get("cases")))
        if isinstance(case, dict) and str(case.get("safety", "safe")).lower() not in allowed | {"safe"}
    ]
    missing_capabilities = [
        str(item.get("capability"))
        for item in as_list(as_mapping(check.get("environment")).get("missing"))
        if isinstance(item, dict)
    ]
    iteration = iteration_state(session, config)

    reasons: list[str] = []
    if errors:
        reasons.append(f"{len(errors)} validation errors")
    if not iteration["may_stop"]:
        reasons.append("risk iteration is not finished: " + join_ids(iteration["blocking_reasons"], "; "))
    if blocking_questions:
        reasons.append(f"blocking questions: {', '.join(blocking_questions)}")
    if unauthorized:
        reasons.append(f"cases needing human authorization: {', '.join(unauthorized)}")
    if missing_capabilities:
        reasons.append(f"missing environment capabilities: {', '.join(missing_capabilities)}")

    warnings: list[str] = [f"project config: {item}" for item in config.get("_warnings", [])]
    if iteration["ceiling_reached"] and iteration["dry_tail"] < 2:
        warnings.append(
            "iteration ceiling reached before two quiet passes; coverage is bounded, not complete"
        )
    planned = {str(item.get("case")) for item in as_list((check.get("execution_plan"))) if isinstance(item, dict)}
    for case in as_list((check.get("cases"))):
        if isinstance(case, dict) and str(case.get("id")) not in planned:
            warnings.append(f"{case.get('id')} is not in the execution plan")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "coverage": coverage,
        "iteration": iteration,
        "config_source": config.get("_source", "defaults"),
        "readiness": {
            "blocking_questions": blocking_questions,
            "cases_needing_authorization": unauthorized,
            "missing_capabilities": missing_capabilities,
            "iteration_ceiling_reached": iteration["ceiling_reached"],
            "iteration_finished": iteration["may_stop"],
            "ready_to_execute": not (errors or blocking_questions or unauthorized or missing_capabilities) and iteration["may_stop"],
            "reasons": reasons,
        },
    }


# --------------------------------------------------------------------------------------
# Check mode: rendering
# --------------------------------------------------------------------------------------

GENERATED_HEADER = "<!-- generated by scripts/spec_flow.py check-render from check.json; edit the JSON, not this file -->"


def as_lines(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [f"{key}: {item}" for key, item in value.items()]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def bullets(value: Any, empty: str = "_none recorded_") -> str:
    lines = as_lines(value)
    return "\n".join(f"- {line}" for line in lines) if lines else empty


def clip(text: str, limit: int = 140) -> str:
    """Shorten to a word boundary and mark the cut, so a table cell never reads as a full sentence."""
    value = " ".join(str(text).split())
    if len(value) <= limit:
        return value
    head = value[:limit].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{head} …"


def md_table(headers: list[str], rows: list[list[str]], empty: str = "_none_") -> str:
    if not rows:
        return empty
    head = "| " + " | ".join(headers) + " |"
    rule = "|" + "|".join("---" for _ in headers) + "|"
    body = ["| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def render_cases_markdown(check: dict[str, Any]) -> str:
    references = {str(item.get("id")): item for item in as_list((check.get("references"))) if isinstance(item, dict)}
    parts = [GENERATED_HEADER, "", f"# Verification cases: {check.get('title', '')}", ""]
    if not as_list(check.get("cases")):
        parts.append("_No cases recorded._")
        return "\n".join(parts) + "\n"
    for case in as_list((check.get("cases"))):
        if not isinstance(case, dict):
            continue
        cited = [
            f"{ref} — {as_mapping(references.get(ref)).get('kind', '')}: "
            f"{as_mapping(references.get(ref)).get('title', '')} ({as_mapping(references.get(ref)).get('location', '')})"
            if ref in references
            else f"{ref} — undeclared reference"
            for ref in (str(item) for item in as_list(case.get("references")))
        ]
        oracle = case.get("oracle") if isinstance(case.get("oracle"), dict) else {}
        steps = [
            [
                str(step.get("n", index)),
                step.get("action", ""),
                step.get("expected", ""),
                step.get("how_to_observe", ""),
            ]
            for index, step in enumerate(as_list(case.get("steps")), start=1)
            if isinstance(step, dict)
        ]
        parts += [
            f"## {case.get('id')} — {case.get('title', '')}",
            "",
            f"- Catches: {join_ids(case.get('risks')) or 'unmapped'}",
            f"- Requirements: {join_ids(case.get('requirements')) or 'not specified'}",
            f"- Safety: {case.get('safety', 'unknown')}",
            f"- Repeat: {case.get('repeat', 1)} | Timeout: {case.get('timeout', 'not specified')}",
            f"- Review verdict: {as_mapping(case.get('review')).get('verdict', 'pending')}",
            "",
            "### Objective",
            "",
            str(case.get("objective", "")),
            "",
            "### Why this catches the risk",
            "",
            str(case.get("why_it_catches_the_risk", "")),
            "",
            "### Documentation",
            "",
            bullets(cited, "_no documents cited_"),
            "",
            "### Preconditions",
            "",
            bullets(case.get("preconditions")),
            "",
            "### Environment",
            "",
            bullets(case.get("environment")),
            "",
            "### Fixtures",
            "",
            bullets(case.get("fixtures")),
            "",
            "### Fault injection",
            "",
            bullets(case.get("fault_injection"), "_none_"),
            "",
            "### Steps",
            "",
            md_table(["#", "Action", "Expected", "How to observe"], steps),
            "",
            "### Intermediate checks",
            "",
            bullets(case.get("intermediate_checks")),
            "",
            "### Expected final state",
            "",
            bullets(case.get("expected_final_state")),
            "",
            "### Forbidden outcomes",
            "",
            bullets(case.get("forbidden_outcomes")),
            "",
            "### Authoritative oracle",
            "",
            f"- Kind: {oracle.get('kind', 'unspecified')}",
            f"- Source: {oracle.get('source', '')}",
            f"- Query: {oracle.get('query', '')}",
            f"- Expected: {oracle.get('expected', '')}",
            f"- Exact side-effect count: {case.get('side_effect_count', 'not specified')}",
            "",
            "### Evidence",
            "",
            bullets(case.get("evidence")),
            "",
            "### Cleanup",
            "",
            bullets(case.get("cleanup")),
            "",
            "### Flake policy",
            "",
            str(case.get("flake_policy", "not specified")),
            "",
        ]
        findings = as_list(as_mapping(case.get("review")).get("findings"))
        if findings:
            parts += ["### Review findings", "", bullets(findings), ""]
    return "\n".join(parts).rstrip() + "\n"


def render_brief_markdown(check: dict[str, Any], evaluation: dict[str, Any], session: dict[str, Any]) -> str:
    cases = {str(item.get("id")): item for item in as_list((check.get("cases"))) if isinstance(item, dict)}
    coverage = evaluation["coverage"]
    readiness = evaluation["readiness"]
    iteration = evaluation["iteration"]
    intent = as_mapping(check.get("intent"))
    environment = as_mapping(check.get("environment"))

    def check_approach(risk: dict[str, Any]) -> str:
        if str(risk.get("disposition", "")).lower() == "waived":
            return "waived, see below"
        approaches: list[str] = []
        for case_id in as_list(risk.get("cases")):
            case = cases.get(str(case_id), {})
            approaches += as_lines(case.get("fault_injection")) or [str(case.get("title", ""))]
        return clip(join_ids(approaches, "; ")) if approaches else "no check designed"

    risk_rows = [
        [
            f"{item.get('id')} {item.get('title', '')}",
            str(item.get("reality", "")),
            str(item.get("severity", "")),
            join_ids(item.get("cases")) or ("waived" if item.get("disposition") == "waived" else "uncovered"),
            check_approach(item),
        ]
        for item in sorted(
            (risk for risk in as_list((check.get("risks"))) if isinstance(risk, dict) and risk.get("disposition") != "dropped"),
            key=lambda risk: SEVERITIES.index(str(risk.get("severity", "low")).lower())
            if str(risk.get("severity", "low")).lower() in SEVERITIES
            else len(SEVERITIES),
        )
    ]
    plan_rows = [
        [
            str(item.get("order", index)),
            f"{item.get('case')} {cases.get(str(item.get('case')), {}).get('title', '')}",
            join_ids(as_mapping(cases.get(str(item.get("case")))).get("risks")),
            str(cases.get(str(item.get("case")), {}).get("safety", "unknown")),
        ]
        for index, item in enumerate(as_list((check.get("execution_plan"))), start=1)
        if isinstance(item, dict)
    ]
    missing_rows = [
        [
            str(item.get("capability", "")),
            join_ids(item.get("needed_for")),
            str(item.get("request", "")),
        ]
        for item in as_list(environment.get("missing"))
        if isinstance(item, dict)
    ]
    question_rows = [
        [str(item.get("id")), str(item.get("question", "")), str(item.get("why", "")), "yes" if item.get("blocking") else "no"]
        for item in as_list((check.get("open_questions")))
        if isinstance(item, dict)
    ]
    waiver_rows = [
        [str(item.get("risk")), str(item.get("reason", "")), str(item.get("owner", "")), str(item.get("revisit", ""))]
        for item in as_list((check.get("waivers")))
        if isinstance(item, dict)
    ]
    source_lines = [
        f"{item.get('kind', '')} {item.get('ref', '')} ({item.get('authority', '')}) — {item.get('location', '')}"
        for item in as_list((check.get("sources")))
        if isinstance(item, dict)
    ]
    changed_lines = [
        f"`{item.get('path', '')}` ({item.get('status', '')}) — {item.get('summary', '')}"
        for item in as_list((check.get("changed_surface")))
        if isinstance(item, dict)
    ]
    affected_lines = [
        f"{item.get('area', '')} — {item.get('why', '')}"
        for item in as_list((check.get("affected_surface")))
        if isinstance(item, dict)
    ]

    readiness_block = "\n".join(
        [
            f"Critical risks: {coverage['critical']['total']}   covered: {coverage['critical']['covered']}",
            f"High risks:     {coverage['high']['total']}   covered: {coverage['high']['covered']}",
            f"Medium risks:   {coverage['medium']['total']}   covered: {coverage['medium']['covered']}",
            f"Blocking questions: {len(readiness['blocking_questions'])}",
            f"Cases needing approval: {len(readiness['cases_needing_authorization'])}",
            f"Ready to execute: {'YES' if readiness['ready_to_execute'] else 'NO'}",
        ]
    )

    parts = [
        GENERATED_HEADER,
        "",
        f"# Verification Brief: {check.get('title', '')}",
        "",
        f"- Mode: {check.get('mode', 'standard')} — {check.get('mode_reason', 'blast radius of the change')}",
        f"- Analysis passes: {iteration['analysis_passes']} (minimum {iteration['minimum']}, ceiling {iteration['maximum']})",
        f"- Config: {evaluation['config_source']}",
        f"- Session state: {session.get('state', 'unknown')}",
        "",
        "## Sources",
        "",
        bullets(source_lines),
        "",
        "## What changed",
        "",
        bullets(changed_lines),
        "",
        "## Intent versus implementation",
        "",
        f"- Stated: {intent.get('stated', 'not stated')}",
        f"- Implemented: {intent.get('reconstructed', 'not reconstructed')}",
        "",
        "Divergences:",
        "",
        bullets(intent.get("divergences"), "- none observed"),
        "",
        "## What this could affect",
        "",
        bullets(affected_lines),
        "",
        "## Main risks",
        "",
        md_table(["Risk", "Reality", "Severity", "Covered by", "Check approach"], risk_rows),
        "",
        "## Checks to run",
        "",
        md_table(["#", "Case", "Proves", "Safety"], plan_rows),
        "",
        "Full steps, oracles, and evidence live in `verification-cases.md`.",
        "",
        "## What must be set up",
        "",
        bullets(environment.get("required")),
        "",
        "## Missing capabilities",
        "",
        md_table(["Capability", "Blocks", "Request"], missing_rows),
        "",
        "## Open questions",
        "",
        md_table(["ID", "Question", "What it changes", "Blocking"], question_rows),
        "",
        "## Waivers",
        "",
        md_table(["Risk", "Reason", "Owner", "Revisit"], waiver_rows),
        "",
        "## Readiness",
        "",
        "```text",
        readiness_block,
        "```",
        "",
    ]
    if readiness["reasons"]:
        parts += ["Not ready because:", "", bullets(readiness["reasons"]), ""]
    if evaluation["warnings"]:
        parts += ["Coverage warnings:", "", bullets(evaluation["warnings"]), ""]
    parts += [
        "Execution requires an explicit `check-authorize`. This brief never grants production,",
        "destructive, payment, or publication authority.",
    ]
    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------------------
# Check mode: commands
# --------------------------------------------------------------------------------------


def require_filled(root: Path, name: str, template_name: str) -> None:
    path = root / name
    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
        raise FlowError("check_artifact_missing", f"{name} is missing or empty.", artifact=name)
    template = set(PLACEHOLDER_RE.findall((ASSET_ROOT / template_name).read_text(encoding="utf-8")))
    remaining = sorted(template & set(PLACEHOLDER_RE.findall(path.read_text(encoding="utf-8"))))
    if remaining:
        raise FlowError(
            "check_artifact_incomplete",
            f"{name} still contains template placeholders.",
            artifact=name,
            placeholders=remaining[:5],
        )


def advance_check_guard(root: Path, session: dict[str, Any], target: str) -> None:
    if target == "CHECK_SOURCE_RESOLVED":
        require_filled(root, "source.md", "check-source.template.md")
    if target == "CHECK_SURFACE_MAPPED":
        require_filled(root, "impact-map.md", "impact-map.template.md")
    if target == "CHECK_CASES_DRAFTED":
        state = iteration_state(session, resolve_config(root))
        if not state["may_stop"]:
            raise FlowError(
                "iteration_incomplete",
                "Risk iteration has not satisfied the stop rule yet.",
                iteration=state,
            )
    if target == "CHECK_RISK_ITERATING" and session["state"] == "CHECK_BRIEF_READY":
        analysed = [item for item in as_list(session.get("iterations")) if item.get("kind") in ANALYSIS_PASS_KINDS]
        session["iteration_reset_at"] = len(analysed)
        session.setdefault("reopened", []).append({"at": utc_now(), "after_passes": len(analysed)})
    if target == "CHECK_CASES_REVIEWED":
        require_filled(root, "case-review.md", "case-review.template.md")
    if target == "CHECK_BRIEF_READY":
        evaluation = evaluate_check(root, session)
        if not evaluation["ok"]:
            raise FlowError(
                "check_not_ready",
                "The check package does not satisfy its own coverage and quality rules.",
                errors=evaluation["errors"][:20],
            )


def cmd_check_init(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_package_root(args, "checks")
    root.mkdir(parents=True, exist_ok=True)
    if (root / SESSION_NAME).exists():
        raise FlowError("session_exists", "Refusing to overwrite an existing spec session.", root=str(root))
    title = args.title or args.session_id
    copy_templates(root, title, CHECK_TEMPLATE_TARGETS)
    state = "CHECK_NEW"
    if args.source_file:
        raw = Path(args.source_file).expanduser().read_text(encoding="utf-8")
        source_path = root / "source.md"
        content = source_path.read_text(encoding="utf-8")
        content = content.replace(
            "<Preserve every provided source exactly: ticket text, PR description, transcript, or message. Never\nreplace raw wording with a summary.>",
            raw.rstrip(),
        )
        source_path.write_text(content, encoding="utf-8", newline="\n")
    project = find_project_root(getattr(args, "project", None))
    config = resolve_config(project if getattr(args, "project", None) else root, args.config)
    mode = str(args.mode or "auto").lower()
    suggestion = suggest_mode(args.path or [], config, args.hint or [])
    if mode == "auto":
        mode = suggestion["mode"]
    if mode not in CHECK_MODES:
        raise FlowError("unknown_mode", "Depth mode must be auto, lite, standard, or deep.", mode=mode)
    if suggestion["matched_critical_areas"]:
        mode = "deep"
    session = new_session(args.session_id, args.rigor, state, mode="check")
    session["check_mode"] = mode
    session["check_mode_reason"] = suggestion["reason"] if args.mode in (None, "auto") or suggestion["matched_critical_areas"] else "explicitly requested"
    if config.get("_source") != "defaults":
        session["verification_config_path"] = config["_source"]
    session["iterations"] = []
    session["authorization"] = None
    atomic_write_json(root / SESSION_NAME, session)
    result = render_status(session, root)
    result["mode_suggestion"] = suggestion
    result["store"] = str(store_root(getattr(args, "store", None)))
    result["project"] = str(find_project_root(getattr(args, "project", None)))
    return result


def cmd_check_diff(args: argparse.Namespace) -> dict[str, Any]:
    """Record the actual change inventory for a check package.

    Repository-native diff tooling stays authoritative when it is available; this is the
    portable fallback and the only way a check session gets a machine-readable surface.
    """
    root = resolve_root(args.root)
    inventory = tree_diff(resolve_root(args.before), resolve_root(args.after))

    def change(session: dict[str, Any]) -> None:
        if session.get("mode") != "check":
            raise FlowError("check_only", "check-diff applies to check sessions; planning uses begin-verify.")
        if session["state"] in {"CHECK_EXECUTING", "CHECK_DONE"}:
            raise FlowError("diff_not_allowed", "An executing or finished check requires a new check package for another diff.")
        if session["state"] == "CHECK_BRIEF_READY":
            session["state"] = "CHECK_RISK_ITERATING"
            session["iterations"] = []
        session["actual_change_surface"] = inventory["files"]
        atomic_write_json(root / "actual-diff.json", inventory)

    session = mutate_session(root, getattr(args, "expected_revision", None), "check_actual_diff_recorded", change)
    paths = [str(item.get("path")) for item in inventory["files"]]
    return {
        "ok": True,
        "root": str(root),
        "revision": session["revision"],
        "counts": inventory["counts"],
        "paths": paths,
        "mode_suggestion": suggest_mode(paths, resolve_config(root, args.config)),
    }


def cmd_check_mode(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    paths = list(args.path or [])
    if args.from_diff:
        source = root / args.from_diff
        if not source.is_file():
            raise FlowError(
                "diff_input_missing",
                "No change inventory exists yet; run check-diff or write the repository diff there first.",
                expected=str(source),
            )
        try:
            inventory = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise FlowError("diff_input_invalid", f"Cannot read a valid change inventory: {error}", path=str(source)) from error
        paths += [str(item.get("path")) for item in inventory.get("files", [])]
    return suggest_mode(paths, resolve_config(root, args.config), args.hint or [])


def cmd_check_pass(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    kind = args.kind.lower()
    if kind not in PASS_KINDS:
        raise FlowError("unknown_pass_kind", "Pass kind must be one of " + ", ".join(PASS_KINDS), kind=kind)
    counters = {
        "new_critical": args.new_critical,
        "new_high": args.new_high,
        "new_medium": args.new_medium,
        "uncovered_invariants": args.uncovered_invariants,
    }
    negative = sorted(name for name, value in counters.items() if int(value or 0) < 0)
    if negative:
        raise FlowError("invalid_pass_counts", "Risk counts cannot be negative.", fields=negative)
    if is_blank(args.summary):
        raise FlowError("pass_summary_required", "Record what this pass actually produced.")

    def change(session: dict[str, Any]) -> None:
        if session.get("mode") != "check":
            raise FlowError("check_only", "Risk passes are recorded only in check sessions.")
        if session["state"] == "CHECK_DONE":
            raise FlowError("pass_not_allowed", "A completed check cannot record more passes.")
        material = bool(args.new_critical or args.new_high or args.surface_changed or args.uncovered_invariants)
        session.setdefault("iterations", []).append(
            {
                "pass": len(session.get("iterations", [])) + 1,
                "kind": kind,
                "at": utc_now(),
                "new_critical": int(args.new_critical or 0),
                "new_high": int(args.new_high or 0),
                "new_medium": int(args.new_medium or 0),
                "uncovered_invariants": int(args.uncovered_invariants or 0),
                "surface_changed": bool(args.surface_changed),
                "material": material,
                "summary": args.summary,
            }
        )

    session = mutate_session(root, args.expected_revision, "risk_pass_recorded", change)
    return render_status(session, root)


def cmd_check_validate(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    session = read_session(root)
    if session.get("mode") != "check":
        raise FlowError("check_only", "check-validate applies to check sessions.")
    return evaluate_check(root, session, resolve_config(root, args.config))


def cmd_check_render(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    session = read_session(root)
    if session.get("mode") != "check":
        raise FlowError("check_only", "check-render applies to check sessions.")
    config = resolve_config(root, args.config)
    evaluation = evaluate_check(root, session, config)
    check = load_check(root)
    (root / "verification-cases.md").write_text(render_cases_markdown(check), encoding="utf-8", newline="\n")
    (root / "verification-brief.md").write_text(
        render_brief_markdown(check, evaluation, session), encoding="utf-8", newline="\n"
    )
    evaluation["rendered"] = list(CHECK_GENERATED_FILES)
    return evaluation


def check_fingerprint(root: Path, config: dict[str, Any] | None = None) -> str:
    """Bind authority to the authored case plan, its sources, diff, and effective config."""
    manifest: dict[str, Any] = {"check": load_check(root)}
    for name in (*CHECK_AUTHORED_FILES, "actual-diff.json"):
        path = root / name
        manifest[name] = hash_file(path) if path.is_file() else None
    resolved = config if config is not None else resolve_config(root)
    manifest["configuration"] = {key: value for key, value in resolved.items() if key != "_source"}
    return hashlib.sha256(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def assert_check_execution(root: Path, session: dict[str, Any]) -> dict[str, Any]:
    authorization = as_mapping(session.get("authorization"))
    if not authorization or is_blank(authorization.get("actor")):
        raise FlowError("authorization_missing", "Record explicit check authorization before execution.")
    if authorization.get("fingerprint") != check_fingerprint(root):
        raise FlowError("authorization_drift", "The authorized check artifacts or configuration changed; obtain fresh authorization.")
    evaluation = evaluate_check(root, session)
    if not evaluation["readiness"]["ready_to_execute"]:
        raise FlowError("check_execution_blocked", "The current case plan cannot execute under this authorization.", readiness=evaluation["readiness"], errors=evaluation["errors"])
    return evaluation


def cmd_check_guard(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    session = read_session(root)
    if session.get("mode") != "check" or session["state"] != "CHECK_EXECUTING":
        raise FlowError("execution_not_allowed", "Execution requires an authorized CHECK_EXECUTING session.")
    evaluation = assert_check_execution(root, session)
    return {"ok": True, "state": session["state"], "revision": session["revision"], "authorization": session["authorization"], "readiness": evaluation["readiness"]}


def cmd_check_authorize(args: argparse.Namespace) -> dict[str, Any]:
    root = resolve_root(args.root)
    scope = args.scope.lower()
    if scope not in AUTHORIZATION_SCOPES:
        raise FlowError("unknown_scope", "Scope must be safe, approved, or all.", scope=scope)
    if is_blank(args.actor):
        raise FlowError("actor_required", "Authorization must name the human who granted it.")
    if scope != "safe" and is_blank(args.acknowledge):
        raise FlowError(
            "acknowledgement_required",
            "Scopes beyond safe need an explicit --acknowledge stating the accepted effects.",
        )

    def change(session: dict[str, Any]) -> None:
        if session.get("mode") != "check":
            raise FlowError("check_only", "Authorization applies to check sessions.")
        if session["state"] != "CHECK_BRIEF_READY":
            raise FlowError(
                "authorization_not_allowed",
                "Authorization is valid only from CHECK_BRIEF_READY.",
                state=session["state"],
            )
        evaluation = evaluate_check(root, session)
        if evaluation["errors"]:
            raise FlowError("check_not_ready", "Resolve validation errors before executing.", errors=evaluation["errors"][:20])
        for name in CHECK_AUTHORED_FILES:
            template = next(source for source, target in CHECK_TEMPLATE_TARGETS.items() if target == name)
            require_filled(root, name, template)
        session["authorization"] = {
            "actor": args.actor,
            "at": utc_now(),
            "scope": scope,
            "note": args.note,
            "acknowledgement": args.acknowledge,
            "fingerprint": check_fingerprint(root),
        }
        assert_check_execution(root, session)
        session["state"] = "CHECK_EXECUTING"

    session = mutate_session(root, args.expected_revision, "execution_authorized", change)
    return render_status(session, root)


def cmd_where(args: argparse.Namespace) -> dict[str, Any]:
    """Answer 'where do this project's packages live' without needing to remember paths."""
    project = find_project_root(args.project)
    home = store_root(args.store)
    result: dict[str, Any] = {
        "ok": True,
        "store": str(home),
        "project": str(project),
        "project_key": project_key(project),
        "project_home": str(home / project_key(project)),
    }
    if args.session_id:
        path = package_path(project, args.kind, args.session_id, args.store)
        result["kind"] = args.kind
        result["session_id"] = args.session_id
        result["path"] = str(path)
        result["exists"] = path.is_dir()
    else:
        result["packages"] = [item for item in store_entries(args.store) if item["project"] == project_identity(project)["path"]]
    return result


def cmd_list(args: argparse.Namespace) -> dict[str, Any]:
    """The machine's history of specification and check work, newest activity first."""
    entries = store_entries(args.store)
    if args.project:
        project = project_identity(find_project_root(args.project))["path"]
        entries = [item for item in entries if item["project"] == project]
    if args.kind:
        entries = [item for item in entries if item["kind"] == args.kind]
    if args.state:
        wanted = args.state.upper()
        entries = [item for item in entries if str(item["state"]).upper() == wanted]
    if args.limit:
        entries = entries[: args.limit]
    return {"ok": True, "store": str(store_root(args.store)), "count": len(entries), "packages": entries}


def cmd_env(_: argparse.Namespace) -> dict[str, Any]:
    return {
        "ok": True,
        "os": platform.system().lower(),
        "python": platform.python_version(),
        "skill_root": str(SKILL_ROOT),
        "atomic_replace": True,
        "platform_dir": None,
        "next_step": "Run status with an explicit --root, or init a new change session.",
    }


def add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", required=True, help="Specification change directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic lifecycle guard for spec-driven development")
    sub = parser.add_subparsers(dest="command", required=True)

    env = sub.add_parser("env", help="Describe the portable runtime")
    env.set_defaults(func=cmd_env)

    init = sub.add_parser("init", help="Create a new Standard package and session")
    init.add_argument("--root", help="Explicit package directory; defaults to the global artifact store")
    init.add_argument("--project", help="Project the work belongs to; defaults to the working directory")
    init.add_argument("--store", help="Override the artifact store root")
    init.add_argument("--session-id", required=True)
    init.add_argument("--title")
    init.add_argument("--rigor", choices=("standard", "high-risk"), default="standard")
    init.add_argument("--intent-file")
    init.set_defaults(func=cmd_init)

    where = sub.add_parser("where", help="Show where a project's packages are stored")
    where.add_argument("--project")
    where.add_argument("--session-id")
    where.add_argument("--kind", choices=PACKAGE_KINDS, default="checks")
    where.add_argument("--store")
    where.set_defaults(func=cmd_where)

    listing = sub.add_parser("list", help="List the machine's specification and check history")
    listing.add_argument("--project")
    listing.add_argument("--kind", choices=PACKAGE_KINDS)
    listing.add_argument("--state")
    listing.add_argument("--limit", type=int)
    listing.add_argument("--store")
    listing.set_defaults(func=cmd_list)

    status = sub.add_parser("status")
    add_root(status)
    status.set_defaults(func=cmd_status)

    validate = sub.add_parser("validate")
    add_root(validate)
    validate.set_defaults(func=cmd_validate)

    fingerprint = sub.add_parser("fingerprint")
    add_root(fingerprint)
    fingerprint.set_defaults(func=cmd_fingerprint)

    advance = sub.add_parser("advance")
    add_root(advance)
    advance.add_argument("--to", required=True)
    advance.add_argument("--expected-revision", type=int)
    advance.set_defaults(func=cmd_advance)

    block = sub.add_parser("block")
    add_root(block)
    block.add_argument("--decision-id", required=True)
    block.add_argument("--summary", required=True)
    block.add_argument("--expected-revision", type=int)
    block.set_defaults(func=cmd_block)

    resolve = sub.add_parser("resolve")
    add_root(resolve)
    resolve.add_argument("--decision-id", required=True)
    resolve.add_argument("--resolution", required=True)
    resolve.add_argument("--expected-revision", type=int)
    resolve.set_defaults(func=cmd_resolve)

    approve = sub.add_parser("approve")
    add_root(approve)
    approve.add_argument("--actor", required=True)
    approve.add_argument("--note", default="")
    approve.add_argument("--planned", action="append", help="Approved repository-relative file or directory; repeat for each surface")
    approve.add_argument("--expected-revision", type=int)
    approve.set_defaults(func=cmd_approve)

    revise = sub.add_parser("revise")
    add_root(revise)
    revise.add_argument("--reason", required=True)
    revise.add_argument("--expected-revision", type=int)
    revise.set_defaults(func=cmd_revise)

    apply = sub.add_parser("apply")
    add_root(apply)
    apply.add_argument("--expected-revision", type=int)
    apply.set_defaults(func=cmd_apply)

    begin_verify = sub.add_parser("begin-verify")
    add_root(begin_verify)
    begin_verify.add_argument("--before", required=True)
    begin_verify.add_argument("--after", required=True)
    begin_verify.add_argument("--planned", action="append")
    begin_verify.add_argument("--expected-revision", type=int)
    begin_verify.set_defaults(func=cmd_begin_verify)

    reopen_verify = sub.add_parser("reopen-verify", help="Reopen actual-diff review after integration or rebase without changing an unchanged semantic approval")
    add_root(reopen_verify)
    reopen_verify.add_argument("--before", required=True)
    reopen_verify.add_argument("--after", required=True)
    reopen_verify.add_argument("--reason", required=True)
    reopen_verify.add_argument("--expected-revision", type=int)
    reopen_verify.set_defaults(func=cmd_reopen_verify)

    disposition = sub.add_parser("disposition")
    add_root(disposition)
    disposition.add_argument("--path", required=True)
    disposition.add_argument(
        "--disposition",
        required=True,
        choices=(
            "expected-indirect",
            "implementation-defect",
            "verified-non-material",
            "baseline-revision-required",
            "waived",
        ),
    )
    disposition.add_argument("--material", action=argparse.BooleanOptionalAction, required=True)
    disposition.add_argument("--evidence", required=True)
    disposition.add_argument("--expected-revision", type=int)
    disposition.set_defaults(func=cmd_disposition)

    check_init = sub.add_parser("check-init", help="Create a verification check package and session")
    check_init.add_argument("--root", help="Explicit package directory; defaults to the global artifact store")
    check_init.add_argument("--project", help="Project the work belongs to; defaults to the working directory")
    check_init.add_argument("--store", help="Override the artifact store root")
    check_init.add_argument("--session-id", required=True)
    check_init.add_argument("--title")
    check_init.add_argument("--rigor", choices=("standard", "high-risk"), default="standard")
    check_init.add_argument("--mode", choices=("auto", *CHECK_MODES), default="auto")
    check_init.add_argument("--source-file")
    check_init.add_argument("--config")
    check_init.add_argument("--path", action="append", help="Changed path used to suggest the depth mode")
    check_init.add_argument("--hint", action="append", help="Extra area hint such as payments or migrations")
    check_init.set_defaults(func=cmd_check_init)

    check_diff = sub.add_parser("check-diff", help="Record the actual change inventory for a check")
    add_root(check_diff)
    check_diff.add_argument("--before", required=True)
    check_diff.add_argument("--after", required=True)
    check_diff.add_argument("--config")
    check_diff.add_argument("--expected-revision", type=int)
    check_diff.set_defaults(func=cmd_check_diff)

    check_mode = sub.add_parser("check-mode", help="Suggest the depth mode from blast radius")
    add_root(check_mode)
    check_mode.add_argument("--path", action="append")
    check_mode.add_argument("--hint", action="append")
    check_mode.add_argument("--from-diff", help="Read paths from an actual-diff.json inside the root")
    check_mode.add_argument("--config")
    check_mode.set_defaults(func=cmd_check_mode)

    check_pass = sub.add_parser("check-pass", help="Record one risk-analysis pass and test the stop rule")
    add_root(check_pass)
    check_pass.add_argument("--kind", required=True, choices=PASS_KINDS)
    check_pass.add_argument("--summary", required=True)
    check_pass.add_argument("--new-critical", type=int, default=0)
    check_pass.add_argument("--new-high", type=int, default=0)
    check_pass.add_argument("--new-medium", type=int, default=0)
    check_pass.add_argument("--uncovered-invariants", type=int, default=0)
    check_pass.add_argument("--surface-changed", action="store_true")
    check_pass.add_argument("--expected-revision", type=int)
    check_pass.set_defaults(func=cmd_check_pass)

    check_validate = sub.add_parser("check-validate", help="Validate check.json against project coverage rules")
    add_root(check_validate)
    check_validate.add_argument("--config")
    check_validate.set_defaults(func=cmd_check_validate)

    check_render = sub.add_parser("check-render", help="Render the cases and the Verification Brief")
    add_root(check_render)
    check_render.add_argument("--config")
    check_render.set_defaults(func=cmd_check_render)

    check_authorize = sub.add_parser("check-authorize", help="Record explicit human authorization to execute")
    add_root(check_authorize)
    check_authorize.add_argument("--actor", required=True)
    check_authorize.add_argument("--scope", default="safe", choices=AUTHORIZATION_SCOPES)
    check_authorize.add_argument("--note", default="")
    check_authorize.add_argument("--acknowledge", default="", help="Explicit statement of the accepted effects")
    check_authorize.add_argument("--expected-revision", type=int)
    check_authorize.set_defaults(func=cmd_check_authorize)

    check_guard = sub.add_parser("check-guard", help="Recheck case authorization, drift, safety scope, and readiness immediately before execution")
    add_root(check_guard)
    check_guard.set_defaults(func=cmd_check_guard)

    complete = sub.add_parser("complete")
    add_root(complete)
    complete.add_argument("--verdict", required=True)
    complete.add_argument("--report", default="verification-report.md")
    complete.add_argument("--expected-revision", type=int)
    complete.set_defaults(func=cmd_complete)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        payload = args.func(args)
        emit(payload)
        # A gate command must fail the shell too, so `check-validate && deploy` cannot proceed
        # on a negative verdict.
        return 0 if payload.get("ok", True) else 1
    except FlowError as error:
        emit(error.payload())
        return 1
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        emit({"ok": False, "error": "runtime_error", "message": str(error)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
