"""One delivery run, with revision-locked state and evidence-bound gates.

Recorded human/host attestations are audit records, not an authentication system.
The host must obtain actual authority and enforce its filesystem/network sandbox.
"""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

import git_ops
import gate_jobs

SCHEMA = "delivery-run/v1"
AXES = ("product", "ux", "technical", "integration", "risk", "qa", "rollout", "uncertainty")
LARGE = {"payments", "auth", "security", "privacy", "migration", "new_domain", "breaking_api", "data_loss", "multi_repo", "staged_rollout"}
MEDIUM = {"backend_client", "sync", "complex_state", "optimistic_update", "important_edges", "runtime_qa", "feature_flag"}
TERMINAL = {"COMPLETE", "CANCELLED"}
LEVELS = {"small": 0, "medium": 1, "large": 2}
DECISIONS = ("code-fix", "test-plan", "environment", "requirements", "human")
PLACEHOLDER_EVENTS = {"agent_registered", "task_registered", "registered", "dispatch", "dispatched", "none", "null", "n/a", "unknown", "tbd", "todo"}
# An oracle that is only one of these statuses names no observable result.
STATUS_ONLY = tuple(re.compile(pattern) for pattern in (
    r"(?:the )?(?:command |process |script |program |it |build )?(?:exits?|exit code|exit status|return code|returns?|rc|status(?: code)?)(?: is| equals| of| with| =| ==)? (?:0|zero)",
    r"(?:the )?(?:http )?(?:status|response)?(?: code)?(?: is| equals| of| =| ==)? ?(?:http )?200(?: ok)?",
    r"(?:it |the (?:endpoint|server|request|api|service) )?returns?(?: an?)?(?: http)?(?: status)?(?: code)? 200(?: ok)?",
    r"(?:all )?(?:of )?(?:the )?(?:unit |integration |e2e |end-to-end )?(?:tests?|checks?|suite|test suite|gates?)(?: all)? (?:pass(?:es|ed)?|succeed(?:s|ed)?|are green|is green|are passing|is passing|run green)",
    r"(?:it |everything |the build |build |the command |command )?(?:works|succeeds|succeeded|passes|passed|pass|is green|green|ok|runs)",
    r"(?:ok|success|successful|no errors?|no failures?|without errors)",
))
WORKFLOW_HOST = "claude-code-workflow"
BUDGET_BLOCKERS = {"rework_budget", "support_budget", "non_converging"}
DECISION_BLOCKERS = {"requirements_finding", "human_decision"}
DECISION_CODES = DECISION_BLOCKERS | {"review_override"}
INSTANT = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:[.,]([0-9]+))?(Z|[+-][0-9]{2}(?::?[0-9]{2})?)?", re.IGNORECASE)


def workflow_journal_host_root() -> Path:
    import workflow_journal
    return workflow_journal.host_root()


def workflow_host(run: dict) -> str:
    """The run's recorded workflow host root; imports refuse when the effective root differs."""
    effective = str(workflow_journal_host_root())
    pinned = run.get("workflow_host_root")
    require(pinned is None or pinned == effective, "host_root_changed",
            f"This run reads workflow journals from {pinned}, but the effective host root is now {effective}; restore DELIVERY_WORKFLOW_HOST_ROOT or the host setup.")
    return pinned or effective


class RunError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def require(condition: Any, code: str, message: str) -> None:
    if not condition:
        raise RunError(code, message)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def identifier(value: str) -> str:
    require(isinstance(value, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value), "invalid_id", "Use a short lowercase, hyphenated identifier.")
    return value


def text(value: Any, label: str) -> str:
    require(isinstance(value, str) and value.strip() and len(value) <= 100_000, "missing_text", f"{label} must contain concrete text.")
    return value.strip()


def _instant(match: re.Match) -> tuple[dt.datetime, int]:
    """Return an aware instant and its fractional-second precision; no zone means UTC."""
    year, month, day, hour, minute, second, fraction, zone = match.groups()
    zone, fraction = (zone or "Z").upper(), fraction or ""
    sign = -1 if zone.startswith("-") else 1
    offset = dt.timedelta(0) if zone == "Z" else sign * dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[3:].lstrip(":") or 0))
    try:
        value = dt.datetime(int(year), int(month), int(day), int(hour), int(minute), int(second),
                            int(fraction[:6].ljust(6, "0")), tzinfo=dt.timezone(offset))
    except ValueError as exc:
        raise RunError("invalid_timestamp", f"Not a valid ISO-8601 instant: {match.group(0)}") from exc
    return value, min(len(fraction), 6)


def parse_instant(value: Any, label: str) -> dt.datetime:
    match = INSTANT.fullmatch(value.strip()) if isinstance(value, str) else None
    require(match and match.group(8), "invalid_timestamp", f"{label} must be an ISO-8601 date and time with an offset or Z.")
    return _instant(match)[0]


def oracle(value: Any, label: str) -> str:
    value = text(value, label)
    words = value.split()
    status = " ".join(words).lower()
    status = status[:-1].rstrip() if status.endswith(".") else status
    require(len(words) >= 3 and not any(pattern.fullmatch(status) for pattern in STATUS_ONLY), "weak_oracle", f"{label} must name an observable result, not only a status such as exit 0 or passes.")
    return value


def safe_path(value: str) -> str:
    require(isinstance(value, str) and value and not value.startswith(("/", "\\")), "unsafe_path", "Scope paths must be repository-relative.")
    parts = value.replace("\\", "/").split("/")
    require(all(p not in {"", ".", "..", ".git"} for p in parts), "unsafe_path", f"Unsafe scope path: {value}")
    require(not any(c in value for c in "*?[]\x00\n\r"), "unsafe_path", "Use explicit files or directories, not patterns.")
    return "/".join(parts)


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def covered(path: str, scopes: list[str]) -> bool:
    return any(path == s or path.startswith(s + "/") for s in scopes)


def atomic_json(path: Path, value: Any) -> None:
    require(not path.is_symlink(), "symlink_state", "Refusing a symlink state file.")
    fd, temp = tempfile.mkstemp(prefix=".delivery-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(root: str | Path) -> dict:
    root = Path(root).expanduser().resolve()
    path = root / "run.json"
    require(path.is_file() and not path.is_symlink(), "missing_run", "Select an existing run.json using its run root.")
    try:
        value = json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise RunError("invalid_run", f"Cannot read run state: {exc}") from exc
    require(isinstance(value, dict) and value.get("schema") == SCHEMA and value.get("root") == str(root), "invalid_run", "Run schema/root mismatch; preserve state and inspect it.")
    require(isinstance(value.get("revision"), int) and isinstance(value.get("history"), list), "invalid_run", "Run state is malformed.")
    return value


@contextlib.contextmanager
def transaction(root: str | Path, event: str, expected_revision: int | None = None):
    root = Path(root).expanduser().resolve()
    require(root.is_dir(), "missing_run", "The run directory is missing.")
    lock = root / ".run.lock"
    require(not lock.is_symlink(), "symlink_state", "Refusing a symlink lock.")
    with lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        value = load(root)
        if expected_revision is not None:
            require(value["revision"] == expected_revision, "stale_revision", "Reload the run; another writer changed it.")
        require(value["state"] not in TERMINAL, "terminal_run", "This run has ended.")
        # Harvest returned review verdicts first and persist them on their own, so a command that
        # then refuses (for example ready after a late FAIL) never loses what was harvested. A
        # harvest problem never stops the command here; release commands check the harvest record.
        outcome = harvest_reviews(value)
        if outcome:
            value["revision"] += 1
            value["updated_at"] = now()
            value["history"].append({"revision": value["revision"], "at": value["updated_at"], "state": value["state"], **outcome})
            atomic_json(root / "run.json", value)
        yield value
        value["revision"] += 1
        value["updated_at"] = now()
        # A command that records a failure names it, so its history entry does not read as the success event.
        event = value.pop("failed_event", None) or event
        value["history"].append({"revision": value["revision"], "at": value["updated_at"], "event": event, "state": value["state"]})
        atomic_json(root / "run.json", value)


def create(repo: str, run_id: str, request: str, *, store: str | None = None, product_root: str | None = None, work_item: str | None = None, delivery_mode: str = "github") -> dict:
    run_id, request = identifier(run_id), text(request, "Original user request")
    require(delivery_mode in {"github", "local"}, "invalid_delivery_mode", "Use the github delivery mode or the explicit local-only mode.")
    primary = Path(git_ops.canonical_repo(repo)).resolve()
    home = Path(store or os.environ.get("DELIVERY_HOME", "~/.local/state/delivery-harness")).expanduser().resolve()
    checkouts = [Path(item["worktree"]).resolve() for item in git_ops._worktrees(primary)]
    checkouts.append(Path(git_ops.inspect_repo(primary)["common_dir"]).resolve())
    require(not any(inside(home, checkout) for checkout in checkouts), "artifact_in_repo", "Delivery artifacts must stay outside every checkout and Git metadata directory.")
    key = primary.name + "-" + hashlib.sha256(str(primary).encode()).hexdigest()[:10]
    root = (home / key / "runs" / run_id).resolve()
    require(not any(inside(root, checkout) for checkout in checkouts), "artifact_in_repo", "The resolved artifact destination is inside a repository checkout.")
    require(not root.exists(), "run_exists", "This run already exists; resume it instead.")
    context = None
    if product_root:
        import product
        context = product.load_product(product_root)
        if work_item:
            context["work_item"] = product.validate_work_item(product_root, work_item)
    else:
        require(work_item is None, "missing_product", "A work item requires an explicitly selected product.")
    root.mkdir(parents=True)
    (root / "intent.md").write_text(request + "\n", encoding="utf-8")
    value = {"schema": SCHEMA, "id": run_id, "root": str(root), "primary": str(primary), "revision": 0,
             "state": "DISCOVERY", "created_at": now(), "updated_at": now(), "intent_hash": digest(request),
             "product": context, "plan": None, "plan_hash": None, "spec_version": 0,
             "approval": None, "authorizations": {}, "tasks": {}, "integration": None,
             "gates": [], "reviews": [], "history": [], "blocker": None, "pr": None,
             "spec_root": str(root / "spec"), "check_root": str(root / "check"), "delivery_mode": delivery_mode,
             "workflow_host_root": str(workflow_journal_host_root())}
    atomic_json(root / "run.json", value)
    return value


def classify(scores: dict, triggers: list[str]) -> dict:
    require(isinstance(scores, dict) and set(scores) == set(AXES), "invalid_scores", "Score all eight risk axes.")
    require(all(type(v) is int and 0 <= v <= 2 for v in scores.values()), "invalid_scores", "Each score is an integer from 0 to 2.")
    require(isinstance(triggers, list) and all(isinstance(t, str) and t in LARGE | MEDIUM for t in triggers), "invalid_trigger", "Unknown risk trigger.")
    total = sum(scores.values())
    level = "large" if total >= 11 or LARGE.intersection(triggers) else "medium" if total >= 6 or MEDIUM.intersection(triggers) else "small"
    return {"level": level, "total": total, "scores": scores, "triggers": sorted(set(triggers))}


def command(value: Any) -> list[str]:
    require(isinstance(value, list) and value and all(isinstance(v, str) and v and "\x00" not in v for v in value), "invalid_command", "Commands are argument arrays, never shell strings.")
    return value


def ancestors(tasks: dict, task_id: str) -> set[str]:
    seen, active = set(), set()
    def visit(current):
        require(current not in active, "dependency_cycle", "Task dependencies contain a cycle.")
        active.add(current)
        for dep in tasks[current]["depends_on"]:
            require(dep in tasks and dep != current, "invalid_dependency", f"Unknown or self dependency: {dep}")
            if dep not in seen:
                visit(dep)
                seen.add(dep)
        active.remove(current)
    visit(task_id)
    return seen


DEFAULT_GATE_TIMEOUT = 300


def gate_timeout(entry: dict) -> int:
    """A planned gate's or case's optional timeout: whole seconds 1–3600, else the 300 s default."""
    if "timeout" not in entry:
        return DEFAULT_GATE_TIMEOUT
    value = entry["timeout"]
    require(type(value) is int and 1 <= value <= 3600, "invalid_timeout", "A planned gate timeout is a whole number of seconds from 1 to 3600.")
    return value


def validate_plan(plan: dict) -> dict:
    require(isinstance(plan, dict), "invalid_plan", "Plan must be an object.")
    text(plan.get("goal"), "Goal")
    require(isinstance(plan.get("non_goals"), list) and plan["non_goals"], "invalid_plan", "Record concrete non-goals.")
    requirements = plan.get("requirements", [])
    require(isinstance(requirements, list) and requirements, "invalid_plan", "Requirements need observable behavior and oracles.")
    ids = set()
    for req in requirements:
        rid = identifier(req.get("id", ""))
        require(rid not in ids, "duplicate_id", "Requirement IDs must be unique.")
        ids.add(rid)
        text(req.get("behavior"), "Required behavior")
        oracle(req.get("oracle"), "Requirement oracle")
    decisions = plan.get("decisions", [])
    require(isinstance(decisions, list), "invalid_plan", "Decisions must be a list.")
    require(all(d.get("status") in {"accepted", "not-material"} and d.get("reason") for d in decisions), "open_decisions", "Resolve material decisions before implementation.")
    tasks = {}
    for item in plan.get("tasks", []):
        tid = identifier(item.get("id", ""))
        require(tid not in tasks, "duplicate_id", "Task IDs must be unique.")
        text(item.get("title"), "Task title")
        text(item.get("acceptance"), "Task acceptance")
        paths = item.get("paths", [])
        require(isinstance(paths, list) and paths, "invalid_plan", "Every editing task needs explicit owned paths.")
        for path in paths:
            safe_path(path)
        require(isinstance(item.get("depends_on", []), list), "invalid_dependency", "Dependencies must be task IDs.")
        require(set(item.get("requirements", [])) and set(item["requirements"]) <= ids, "coverage_gap", "Every task must trace to existing requirements.")
        gates = item.get("gates", [])
        require(isinstance(gates, list) and gates, "missing_gate", "Each task needs a mechanical gate.")
        for gate in gates:
            require(isinstance(gate, dict), "invalid_gate", "Task gates include command, risk, oracle and cleanup, like integrated cases.")
            command(gate.get("command"))
            require(gate.get("risk") in {"safe", "external", "destructive"}, "invalid_risk", "Classify each task gate's side effects explicitly.")
            oracle(gate.get("oracle"), "Task gate oracle")
            text(gate.get("cleanup"), "Task gate cleanup")
            gate_timeout(gate)
        require(isinstance(item.get("resources", []), list) and all(isinstance(r, str) and r.strip() for r in item.get("resources", [])), "invalid_resource", "Exclusive resources are named strings.")
        tasks[tid] = {**item, "depends_on": item.get("depends_on", []), "resources": sorted({r.strip() for r in item.get("resources", [])})}
    require(tasks, "invalid_plan", "No implementation tasks are defined.")
    dependency_map = {tid: ancestors(tasks, tid) for tid in tasks}
    for i, (aid, a) in enumerate(tasks.items()):
        for bid, b in list(tasks.items())[i + 1:]:
            conflict = any(covered(p, b["paths"]) or any(covered(q, [p]) for q in b["paths"]) for p in a["paths"])
            conflict = conflict or bool(set(a["resources"]) & set(b["resources"]))
            require(not conflict or aid in dependency_map[bid] or bid in dependency_map[aid], "overlapping_tasks", f"Serialize shared paths/resources: {aid}, {bid}")
    cases = plan.get("verification", [])
    require(isinstance(cases, list) and cases, "missing_verification", "Define integrated verification cases.")
    case_ids, coverage = set(), set()
    for case in cases:
        cid = identifier(case.get("id", ""))
        require(cid not in case_ids, "duplicate_id", "Case IDs must be unique.")
        case_ids.add(cid)
        command(case.get("command"))
        require(case.get("risk") in {"safe", "external", "destructive"}, "invalid_risk", "Classify test side effects explicitly.")
        oracle(case.get("oracle"), "Observable verification oracle")
        text(case.get("cleanup"), "Verification cleanup")
        gate_timeout(case)
        require(isinstance(case.get("resources", []), list) and all(isinstance(r, str) and r.strip() for r in case.get("resources", [])), "invalid_resource", "Integrated resources are named strings.")
        require(set(case.get("requirements", [])) and set(case["requirements"]) <= ids, "coverage_gap", "Cases must trace to requirements.")
        coverage.update(case["requirements"])
    require(coverage == ids, "coverage_gap", "Every requirement needs integrated verification.")
    risk = classify(plan.get("scores", {}), plan.get("triggers", []))
    if risk["level"] != "small":
        require(all(isinstance(c.get("check_case"), str) and c["check_case"].strip() for c in cases), "missing_check_mapping", "Every non-trivial integrated case must map to a rich check_case ID.")
    return {**copy.deepcopy(plan), "tasks": list(tasks.values()), "classification": risk}


def set_plan(root, plan: dict, expected_revision=None) -> dict:
    validated = validate_plan(plan)
    with transaction(root, "plan_recorded", expected_revision) as run:
        require(run["state"] in {"DISCOVERY", "READY_FOR_APPROVAL", "APPROVED"}, "revision_required", "Use revise before changing an active plan.")
        run.update(plan=validated, plan_hash=digest(validated), spec_version=run["spec_version"] + 1,
                   state="READY_FOR_APPROVAL", approval=None, authorizations={}, tasks={}, gates=[], reviews=[])
        atomic_json(Path(root) / "plan.json", validated)
    return load(root)


def assert_plan(run: dict) -> None:
    require(run.get("plan") and run.get("plan_hash") == digest(run["plan"]), "plan_drift", "Stored plan changed outside revision control.")
    try:
        current = json.loads((Path(run["root"]) / "plan.json").read_text())
    except (ValueError, OSError) as exc:
        raise RunError("plan_drift", "Cannot read the approved plan artifact.") from exc
    require(digest(current) == run["plan_hash"], "plan_drift", "Plan artifact changed; revise and review before continuing.")


def attestation(actor: str, evidence: str) -> dict:
    return {"actor": text(actor, "Human or host actor"), "evidence": text(evidence, "Original authorization or host-result reference"), "at": now()}


def _test_targets(run: dict, scope: str) -> dict:
    risk = scope.removeprefix("test-")
    values = {f"{task['id']}:{index}": digest(gate)
              for task in run["plan"]["tasks"] for index, gate in enumerate(task["gates"])
              if gate["risk"] == risk}
    values.update({case["id"]: digest(case) for case in run["plan"]["verification"] if case["risk"] == risk})
    return values


def _authorize_standing(root, actor: str, evidence: str, expected_revision=None, *, targets=None, until=None, max_level=None, triggers=None) -> dict:
    """Record a bounded standing approval; it survives plan changes but never widens beyond its level and triggers."""
    require(targets is None, "invalid_authorization_target", "Gate targets apply only to test authority.")
    require(max_level in LEVELS, "invalid_level", "A standing approval names max_level small, medium or large.")
    triggers = [] if triggers is None else triggers
    require(isinstance(triggers, list) and all(isinstance(t, str) and t in LARGE | MEDIUM for t in triggers), "invalid_trigger", "Unknown risk trigger.")
    ends = parse_instant(until, "Standing approval until")
    with transaction(root, "standing_approval_recorded", expected_revision) as run:
        current = parse_instant(now(), "Current time")
        require(ends > current, "invalid_standing_approval", "A standing approval must end in the future; to end a grant, record one that ends sooner.")
        require(ends <= current + dt.timedelta(days=30), "invalid_standing_approval", "A standing approval can last at most 30 days.")
        grant = {**attestation(actor, evidence), "until": ends.astimezone(dt.timezone.utc).isoformat(), "max_level": max_level, "triggers": sorted(set(triggers))}
        grant["id"] = digest(grant)
        if run.get("standing_approval"):
            run.setdefault("standing_history", []).append(run["standing_approval"])
        run["standing_approval"] = grant
    return load(root)


def authorize(root, scope: str, actor: str, evidence: str, expected_revision=None, *, targets=None, count=None, until=None, max_level=None, triggers=None, task=None, code=None) -> dict:
    require(scope in {"implement", "publish", "merge", "test-external", "test-destructive", "local-merge", "fix-budget", "rework-budget", "decision", "standing-approval"}, "invalid_scope", "Unknown authorization scope; deploy is deliberately separate.")
    require(task is None or scope in {"rework-budget", "decision"}, "invalid_authorization_task", "Only rework-budget and decision authority name a task.")
    require(code is None or scope == "decision", "invalid_authorization_code", "Only decision authority names a blocker code.")
    if scope == "decision":
        require(count is None and targets is None and until is None and max_level is None and triggers is None, "invalid_decision_grant", "A decision grant takes only --code, an optional --task, the actor and the user's decision as evidence.")
        return _authorize_decision(root, actor, evidence, expected_revision, task=task, code=code)
    if scope == "standing-approval":
        require(count is None, "invalid_authorization_count", "Only fix-budget and rework-budget authority take a count.")
        return _authorize_standing(root, actor, evidence, expected_revision, targets=targets, until=until, max_level=max_level, triggers=triggers)
    require(until is None and max_level is None and triggers is None, "invalid_standing_approval", "until, max_level and triggers apply only to a standing approval.")
    if scope == "fix-budget":
        require(type(count) is int and 1 <= count <= 3, "invalid_fix_budget", "A fix-budget extension needs an explicit integer count from 1 to 3.")
    elif scope == "rework-budget":
        require(type(count) is int and 1 <= count <= 2, "invalid_rework_budget", "A rework-budget extension needs an explicit integer count from 1 to 2.")
    else:
        require(count is None, "invalid_authorization_count", "Only fix-budget and rework-budget authority take a count.")
    if scope == "rework-budget":
        return _authorize_rework_budget(root, actor, evidence, expected_revision, task=task, count=count, targets=targets)
    with transaction(root, "authority_recorded", expected_revision) as run:
        assert_plan(run)
        if scope == "merge":
            require(run.get("pr"), "missing_pr", "Merge authority must name an existing run PR.")
        if scope == "local-merge":
            require(run.get("delivery_mode", "github") == "local", "local_merge_scope", "Local-merge authority applies only to a local-only run; GitHub runs merge through their PR.")
        if scope == "fix-budget":
            require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "fix_budget_state", "Extend the integration-fix budget only for an existing unmerged integration.")
        grant = {**attestation(actor, evidence), "plan_hash": run["plan_hash"], "scope": scope}
        if scope.startswith("test-"):
            require(isinstance(targets, list) and targets and all(isinstance(target, str) for target in targets), "authorization_targets_required", "Name the explicitly authorized task gates or integrated cases; use ['*'] only for explicit approval of all matching-risk gates.")
            require(len(set(targets)) == len(targets), "invalid_authorization_target", "Authorization targets must be unique.")
            available = _test_targets(run, scope)
            selected = sorted(available) if targets == ["*"] else targets
            require(selected and all(target in available for target in selected), "invalid_authorization_target", "Every authorization target must be a planned gate with the matching risk.")
            grant.update(targets={target: available[target] for target in selected}, all_matching=targets == ["*"])
        else:
            require(targets is None, "invalid_authorization_target", "Gate targets apply only to test authority.")
        if scope == "merge":
            grant["pr"] = run["pr"]["number"]
        if scope == "fix-budget":
            # Append-only: each explicit extension stays auditable and counts
            # only while its plan hash is the current plan's.
            run.setdefault("fix_budget_grants", []).append({**grant, "count": count, "spec_version": run["spec_version"]})
        else:
            run["authorizations"][scope] = grant
    return load(root)


def _authorize_rework_budget(root, actor: str, evidence: str, expected_revision=None, *, task=None, count=None, targets=None) -> dict:
    """Record explicit authority for more rounds, bound to the current budget blocker, plan hash and version."""
    require(targets is None, "invalid_authorization_target", "Gate targets apply only to test authority.")
    with transaction(root, "rework_budget_recorded", expected_revision) as run:
        assert_plan(run)
        if task is not None:
            task_plan(run, identifier(task))
        blocker = run.get("blocker") or {}
        require(run["state"] == "BLOCKED" and blocker.get("code") in BUDGET_BLOCKERS and blocker.get("task") == task, "rework_budget_state",
                "Record rework-budget authority after a rework_budget, support_budget or non_converging block, for the task it names (no --task for the integration).")
        kind = "support" if blocker["code"] == "support_budget" else "code-fix" if task is not None else "integration"
        run.setdefault("rework_budget_grants", []).append({**attestation(actor, evidence), "scope": "rework-budget", "plan_hash": run["plan_hash"], "spec_version": run["spec_version"],
                                                           "task": task, "count": count, "kind": kind, "blocker_code": blocker["code"], "blocker_at": blocker["at"]})
    return load(root)


def _authorize_decision(root, actor: str, evidence: str, expected_revision=None, *, task=None, code=None) -> dict:
    """Record a user decision: resolving a requirements or human-decision block, or overriding a FAIL review.

    A review_override is bound to one target (a task or the integration) and its exact current
    content and base, which must already have a FAIL review; the evidence says why that FAIL is
    wrong. It covers only the FAILs recorded before it, so a later FAIL on the same content needs a
    new override before a PASS can be recorded.
    """
    require(code in DECISION_CODES, "invalid_decision_grant", "A decision grant names --code requirements_finding, human_decision or review_override.")
    with transaction(root, "decision_recorded", expected_revision) as run:
        assert_plan(run)
        if task is not None:
            task_plan(run, identifier(task))
        if code == "review_override":
            require(task is None or task in run["tasks"], "missing_worktree", "Prepare the owned task worktree first.")
            snap = snapshot(run, task)
            failures = _failed_reviews(run, task, snap)
            require(failures, "decision_state", "Record a review override only for the exact content and base that already received a FAIL review.")
            grant = {**attestation(actor, evidence), "scope": "decision", "plan_hash": run["plan_hash"], "spec_version": run["spec_version"], "task": task, "code": code,
                     "content": snap["content"], "base_sha": snap["base_sha"], "failed_reviews": [_review_key(review) for review in failures]}
        else:
            blocker = run.get("blocker") or {}
            require(run["state"] == "BLOCKED" and blocker.get("code") == code and blocker.get("task") == task, "decision_state",
                    "Record a decision grant for the current requirements_finding or human_decision block, with the task it names (no --task for the integration).")
            grant = {**attestation(actor, evidence), "scope": "decision", "plan_hash": run["plan_hash"], "spec_version": run["spec_version"],
                     "task": task, "code": code, "blocker_at": blocker["at"]}
        run.setdefault("decision_grants", []).append(grant)
    return load(root)


def rework_limits(run: dict, task_id: str | None) -> dict:
    """Two code-fix and two test-plan/environment rounds per task, plus explicit rework-budget grants of this plan version."""
    grants = [grant for grant in run.get("rework_budget_grants", []) if grant.get("task") == task_id
              and run.get("plan_hash") and grant.get("plan_hash") == run["plan_hash"] and grant.get("spec_version") == run.get("spec_version")]
    return {kind: 2 + sum(grant["count"] for grant in grants if grant["kind"] == kind) for kind in ("code-fix", "support", "integration")}


def need_authority(run: dict, scope: str, *, target: str | None = None) -> None:
    grant = run["authorizations"].get(scope)
    require(grant and grant.get("plan_hash") == run["plan_hash"], "authorization_required", f"Obtain explicit, current {scope} authority first.")
    if scope.startswith("test-"):
        available = _test_targets(run, scope)
        require(isinstance(target, str) and target in available and grant.get("targets", {}).get(target) == available[target], "authorization_required", f"Obtain explicit, current {scope} authority for gate {target or '<unspecified>'} first.")


def _standing_grant(run: dict) -> dict:
    grant = run.get("standing_approval")
    require(grant, "standing_approval_insufficient", "No standing approval is recorded; obtain an individual approval.")
    require(parse_instant(now(), "Current time") < parse_instant(grant["until"], "Standing approval until"), "standing_approval_insufficient", f"The standing approval expired at {grant['until']}; obtain an individual approval.")
    classification = run["plan"]["classification"]
    require(LEVELS[classification["level"]] <= LEVELS[grant["max_level"]], "standing_approval_insufficient", f"The plan level {classification['level']} exceeds the standing approval maximum {grant['max_level']}; obtain an individual approval.")
    extra = sorted(set(classification["triggers"]) - set(grant["triggers"]))
    require(not extra, "standing_approval_insufficient", "The plan's risk triggers exceed the standing approval: " + ", ".join(extra) + "; obtain an individual approval.")
    return grant


def approve(root, actor: str, evidence: str, expected_revision=None, *, standing=False) -> dict:
    with transaction(root, "semantic_baseline_approved", expected_revision) as run:
        require(run["state"] == "READY_FOR_APPROVAL", "approval_state", "Review the current plan before approving it.")
        assert_plan(run)
        grant = _standing_grant(run) if standing else None
        record = attestation(actor, evidence)
        approver = f"standing:{grant['actor']}" if grant else actor
        if run["plan"]["classification"]["level"] != "small":
            scope = sorted({p for task in run["plan"]["tasks"] for p in task["paths"]})
            spec_command(root, ["approve", "--actor", approver, "--note", evidence, *[arg for path in scope for arg in ("--planned", path)]])
        if grant:
            record = {"actor": approver, "recorded_by": record["actor"], "evidence": record["evidence"], "kind": "standing", "grant_id": grant["id"], "at": record["at"]}
        else:
            record["kind"] = "individual"
        run["approval"] = {**record, "plan_hash": run["plan_hash"], "version": run["spec_version"]}
        run.setdefault("approval_history", []).append(run["approval"])
        run["state"] = "APPROVED"
    return load(root)


def spec_command(root, arguments: list[str], *, check: bool = False, _run: dict | None = None) -> dict:
    run = _run if _run is not None else load(root)
    require(arguments and all(v not in {"--root", "--project", "--store"} and not v.startswith(("--root=", "--project=", "--store=")) for v in arguments), "spec_root_override", "The run owns specification paths; do not override them.")
    path = Path(__file__).resolve().parent.parent / "internal/spec/scripts/spec_flow.py"
    target = run["check_root"] if check else run["spec_root"]
    require(arguments[0] not in {"where", "list"}, "run_owned_lookup", "Use delivery list/status for run-owned spec/check roots; do not start another artifact-store lookup.")
    argv = [sys.executable, "-B", str(path), *arguments]
    if arguments[0] not in {"env", "--help", "-h"}:
        argv += ["--root", target]
    if arguments[0] in {"init", "check-init"}:
        argv += ["--project", run["primary"], "--session-id", run["id"]]
        if arguments[0] == "init":
            argv += ["--intent-file", str(Path(root) / "intent.md")]
    result = subprocess.run(argv, cwd=run["primary"], capture_output=True, text=True)
    if result.returncode == 0 and any(arg in {"--help", "-h"} for arg in arguments):
        return {"ok": True, "help": result.stdout}
    try:
        payload = json.loads(result.stdout)
    except ValueError as exc:
        raise RunError("spec_engine_failed", result.stderr[-4000:] or result.stdout[-4000:]) from exc
    if result.returncode:
        raise RunError("spec_gate", json.dumps(payload, ensure_ascii=False))
    return payload


def assert_spec(run: dict, *, starting=False) -> None:
    if run["plan"]["classification"]["level"] == "small":
        return
    result = spec_command(run["root"], ["status"])
    require(result.get("baseline_status") == "matching" and result.get("approval"), "spec_approval_required", "A non-trivial change needs a valid approved rich specification.")
    allowed = {"APPROVED", "IMPLEMENTING"} if starting else {"IMPLEMENTING", "POST_IMPLEMENTATION_REVIEW", "VERIFYING", "DONE"}
    require(result.get("state") in allowed, "spec_state", "Advance the specification through its guarded lifecycle.")


def start(root, *, within_request=False, expected_revision=None) -> dict:
    with transaction(root, "implementation_started", expected_revision) as run:
        assert_plan(run)
        need_authority(run, "implement")
        quick = within_request and run["plan"]["classification"]["level"] == "small"
        require(run["state"] == "APPROVED" or (quick and run["state"] == "READY_FOR_APPROVAL"), "approval_required", "Approve the baseline first; only bounded Small work may use existing request authority.")
        if not quick:
            require(run.get("approval", {}).get("plan_hash") == run["plan_hash"], "stale_approval", "Semantic approval is stale.")
        assert_spec(run, starting=True)
        if run.get("delivery_mode", "github") == "local":
            primary = git_ops.prepare_local_primary(run["primary"])
        else:
            primary = git_ops.prepare_primary(run["primary"])
        run["base_sha"] = primary["base_sha"]
        if run["plan"]["classification"]["level"] != "small":
            current = spec_command(root, ["status"])
            if current["state"] == "APPROVED":
                spec_command(root, ["apply"])
        run["implementation_basis"] = "scoped-user-request" if quick else "approved-specification"
        run["state"] = "IMPLEMENTING"
    return load(root)


def task_plan(run: dict, task_id: str) -> dict:
    for task in run["plan"]["tasks"]:
        if task["id"] == task_id:
            return task
    raise RunError("unknown_task", "Task is not in the approved plan.")


def task_snapshot(worktree: str, base_sha: str) -> dict:
    result = git_ops.inventory(worktree, base_sha)
    return {"content": result["content_fingerprint"], "head": result["head"], "base_sha": base_sha, "files": result["files"]}


def snapshot(run: dict, task_id: str | None = None) -> dict:
    owner = run["tasks"].get(task_id) if task_id else run.get("integration")
    require(owner, "missing_worktree", "Prepare the owned worktree first.")
    identity = git_ops.inspect_repo(owner["path"])
    require(identity["primary"] == run["primary"] and identity["branch"] == owner["branch"], "worktree_identity", "The owned worktree's repository or branch changed.")
    if task_id:
        require(identity["head"] == owner["base_sha"], "task_commit_forbidden", "Implementers return reviewed patches; do not commit or rewrite the task base.")
    return task_snapshot(owner["path"], owner["base_sha"])


def _implementation_state(run: dict) -> None:
    assert_plan(run)
    require(run["state"] == "IMPLEMENTING" and not run.get("integration"), "wrong_state", "Implementation dispatch and rework require an active, unintegrated run.")
    need_authority(run, "implement")


def _collection_state(run: dict) -> None:
    """Collecting or abandoning an existing dispatch also works while implementation is blocked; dispatching does not."""
    assert_plan(run)
    blocked = run["state"] == "BLOCKED" and (run.get("blocker") or {}).get("from") == "IMPLEMENTING"
    require((run["state"] == "IMPLEMENTING" or blocked) and not run.get("integration"), "wrong_state", "Collecting a dispatch requires an active or blocked, unintegrated implementation.")
    need_authority(run, "implement")


def _task_order(run: dict) -> list[str]:
    tasks = {task["id"]: task for task in run["plan"]["tasks"]}
    ordered, visited = [], set()
    def visit(task_id):
        if task_id in visited:
            return
        for dep in tasks[task_id]["depends_on"]:
            visit(dep)
        visited.add(task_id)
        ordered.append(task_id)
    for task_id in tasks:
        visit(task_id)
    return ordered


def _dependency_ids(run: dict, task_id: str) -> list[str]:
    dependencies = ancestors({task["id"]: task for task in run["plan"]["tasks"]}, task_id)
    return [key for key in _task_order(run) if key in dependencies]


def _dependency_bindings(run: dict, task_id: str) -> dict:
    bindings = {}
    for dep in _dependency_ids(run, task_id):
        owner = run["tasks"].get(dep)
        require(owner and owner["status"] == "VERIFIED" and owner.get("patch"), "dependency_not_ready", f"Task {dep} is not verified.")
        bindings[dep] = {"attempt": owner.get("attempt", 1), "base_sha": owner["base_sha"],
                         "patch_sha256": owner["patch"]["sha256"], "patch_path": owner["patch"]["path"],
                         "dispatch_id": owner.get("agent", {}).get("dispatch_id")}
    return bindings


def _require_current_dependencies(run: dict, task_id: str, *, collecting: bool = False) -> None:
    # Collecting a dependent's result never waits for a late FAIL on its dependency: that FAIL's
    # rework may itself be waiting for this dispatch to return.
    for dep in task_plan(run, task_id)["depends_on"]:
        verify_task_current(run, dep, late=not collecting)
    owner = run["tasks"][task_id]
    require(owner.get("dependencies", {}) == _dependency_bindings(run, task_id), "stale_dependency", "A dependency changed. Preserve this worktree and prepare a new task attempt.")


def _worktree_location(run: dict, purpose: str, attempt: int) -> tuple[Path, str, Path]:
    require(type(attempt) is int and attempt > 0, "invalid_worktree_receipt", "Worktree attempt must be a positive integer.")
    if purpose == "integration":
        suffix = f"integration/a{attempt}"
    else:
        require(purpose.startswith("task-"), "invalid_worktree_receipt", "Unknown worktree purpose.")
        task_id = identifier(purpose[5:])
        task_plan(run, task_id)
        suffix = f"tasks/{task_id}/a{attempt}"
    path = Path(run["root"]) / "worktrees" / f"v{run['spec_version']}" / suffix
    branch = f"delivery/{run['id']}/v{run['spec_version']}/{suffix}"
    receipt = Path(run["root"]) / "ownership" / f"v{run['spec_version']}" / f"{purpose}-a{attempt}.json"
    require(path.resolve() == path and receipt.resolve() == receipt, "symlink_state", "Worktree paths and ownership receipts cannot traverse symlink aliases.")
    return path, branch, receipt


def _load_worktree_receipt(run: dict, purpose: str, attempt: int) -> dict:
    path, branch, receipt_path = _worktree_location(run, purpose, attempt)
    require(receipt_path.is_file() and not receipt_path.is_symlink(), "missing_worktree_receipt", "A worktree can be resumed only from its recorded successful creation.")
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError) as exc:
        raise RunError("invalid_worktree_receipt", "Cannot read the recorded worktree ownership.") from exc
    expected = {"schema": "delivery-worktree/v1", "run_root": run["root"], "primary": run["primary"],
                "plan_hash": run["plan_hash"], "version": run["spec_version"], "purpose": purpose,
                "attempt": attempt, "path": str(path), "branch": branch, "ownership_receipt": str(receipt_path)}
    require(isinstance(receipt, dict) and all(receipt.get(key) == value for key, value in expected.items()), "invalid_worktree_receipt", "Worktree ownership does not match this exact run, attempt and approved plan.")
    require(receipt.get("created_head") == receipt.get("initial_base_sha") and receipt.get("created_at"), "invalid_worktree_receipt", "Worktree creation was not successfully recorded.")
    return receipt


def owned_worktree_record(run: dict, owner: dict, *, task_id: str | None = None) -> dict:
    """Validate durable ownership independently from mutable code or commit HEAD."""
    purpose = "task-" + task_id if task_id is not None else "integration"
    receipt = _load_worktree_receipt(run, purpose, owner.get("attempt", 0))
    require(all(owner.get(key) == receipt[key] for key in ("path", "branch", "ownership_receipt")), "worktree_identity", "The current worktree owner differs from its durable creation record.")
    identity = git_ops.inspect_repo(owner["path"])
    require(identity["primary"] == run["primary"] and identity["worktree"] == owner["path"] and identity["branch"] == owner["branch"], "worktree_identity", "The recorded worktree repository, exact path or branch changed.")
    return receipt


def _save_worktree_checkpoint(receipt: dict, phase: str) -> dict:
    inventory = git_ops.inventory(receipt["path"], receipt["initial_base_sha"])
    result = {**receipt, "phase": phase, "checkpoint": {key: inventory[key] for key in
              ("head", "fingerprint", "content_fingerprint", "tree", "files", "staged", "unstaged", "untracked", "in_progress")}, "checkpoint_at": now()}
    atomic_json(Path(receipt["ownership_receipt"]), result)
    return result


def _worktree_receipts(run: dict, purpose: str) -> list[dict]:
    folder = Path(run["root"]) / "ownership" / f"v{run['spec_version']}"
    require(folder.resolve() == folder, "symlink_state", "Ownership records cannot traverse symlink aliases.")
    records = []
    for path in folder.glob(f"{purpose}-a*.json"):
        match = re.fullmatch(re.escape(purpose) + r"-a([1-9][0-9]*)\.json", path.name)
        if match:
            records.append(_load_worktree_receipt(run, purpose, int(match.group(1))))
    return records


def _previous_worktree_attempts(run: dict, purpose: str, current_attempt: int) -> list[dict]:
    return [{**record, "base_sha": record["checkpoint"]["head"], "status": "PRESERVED"}
            for record in sorted(_worktree_receipts(run, purpose), key=lambda item: item["attempt"])
            if record["attempt"] != current_attempt]


def _owned_preparation(run: dict, purpose: str, inputs: dict, *, minimum_attempt: int = 1) -> dict:
    """Resume only a successful creation receipt whose exact checkpoint survives."""
    folder = Path(run["root"]) / "ownership" / f"v{run['spec_version']}"
    require(folder.resolve() == folder, "symlink_state", "Ownership records cannot traverse symlink aliases.")
    folder.mkdir(parents=True, exist_ok=True)
    records = _worktree_receipts(run, purpose)
    input_hash = digest(inputs)
    candidates = [record for record in records if record["attempt"] >= minimum_attempt and record.get("input_hash") == input_hash]
    if candidates:
        receipt = max(candidates, key=lambda item: item["attempt"])
        owned_worktree_record(run, receipt, task_id=purpose[5:] if purpose.startswith("task-") else None)
        current = git_ops.inventory(receipt["path"], receipt["initial_base_sha"])
        require(current["fingerprint"] == receipt.get("checkpoint", {}).get("fingerprint"), "worktree_checkpoint_drift", "The prepared worktree changed after its durable checkpoint; preserve it and reconcile or revise before retrying.")
        return receipt
    attempt = max([minimum_attempt, *[record["attempt"] + 1 for record in records]])
    path, branch, receipt_path = _worktree_location(run, purpose, attempt)
    path.parent.mkdir(parents=True, exist_ok=True)
    created = git_ops.create_worktree(run["primary"], str(path), branch, run["base_sha"])
    receipt = {"schema": "delivery-worktree/v1", "run_root": run["root"], "primary": run["primary"],
               "plan_hash": run["plan_hash"], "version": run["spec_version"], "purpose": purpose,
               "attempt": attempt, "path": str(path), "branch": branch, "ownership_receipt": str(receipt_path),
               "initial_base_sha": run["base_sha"], "created_head": created["head"], "created_at": now(),
               "input_hash": input_hash, "inputs": inputs}
    return _save_worktree_checkpoint(receipt, "CREATED")


def prepare_task(root, task_id: str, expected_revision=None) -> dict:
    with transaction(root, "task_worktree_prepared", expected_revision) as run:
        _implementation_state(run)
        task = task_plan(run, task_id)
        previous = run["tasks"].get(task_id)
        require(previous is None or previous["status"] == "INVALIDATED", "task_exists", "Resume the existing task worktree; only an invalidated dependency attempt needs a new worktree.")
        for dep in task["depends_on"]:
            verify_task_current(run, dep)
        dependencies = _dependency_bindings(run, task_id)
        ordered = _dependency_ids(run, task_id)
        receipt = _owned_preparation(run, "task-" + task_id, {"task": task, "dependencies": dependencies},
                                     minimum_attempt=previous.get("attempt", 1) + 1 if previous else 1)
        if receipt["phase"] == "CREATED" and ordered:
            git_ops.integrate_patches(receipt["path"], [run["tasks"][key]["patch"] for key in ordered], run["base_sha"])
            receipt = _save_worktree_checkpoint(receipt, "DEPENDENCIES_APPLIED")
        if receipt["phase"] == "DEPENDENCIES_APPLIED":
            git_ops.commit_changes(receipt["path"], receipt["checkpoint"]["staged"], "Prepare verified task dependencies")
            receipt = _save_worktree_checkpoint(receipt, "PREPARED")
        elif receipt["phase"] == "CREATED":
            receipt = _save_worktree_checkpoint(receipt, "PREPARED")
        require(receipt["phase"] == "PREPARED", "worktree_checkpoint_phase", "The task worktree has an unexpected preparation phase.")
        prior_by_path = {record["path"]: record for record in _previous_worktree_attempts(run, "task-" + task_id, receipt["attempt"])}
        if previous:
            prior_by_path.update({record["path"]: record for record in previous.get("previous_attempts", [])})
            prior_by_path[previous["path"]] = {key: value for key, value in previous.items() if key != "previous_attempts"}
        prior_attempts = list(prior_by_path.values())
        owner = {"path": receipt["path"], "branch": receipt["branch"], "attempt": receipt["attempt"],
                 "ownership_receipt": receipt["ownership_receipt"], "base_sha": receipt["checkpoint"]["head"],
                 "dependencies": dependencies, "previous_attempts": prior_attempts,
                 "status": "PREPARED", "agent": None, "dispatches": [], "report": None, "patch": None}
        if previous and (previous.get("invalidated_by") or {}).get("abandoned"):
            # A new attempt after an abandoned dispatch keeps the task's round counts, so budgets still bind.
            owner.update({key: previous[key] for key in ("rework_rounds", "support_rounds") if key in previous})
        run["tasks"][task_id] = owner
        spec = {"task": task, "worktree": owner["path"], "branch": owner["branch"], "base_sha": owner["base_sha"],
                "attempt": owner["attempt"], "dependencies": dependencies, "run_root": str(root), "plan_hash": run["plan_hash"],
                "spec_root": run["spec_root"], "limits": ["No commit, push, PR, merge or deployment", "Only owned paths", "Stage exact intended files", "Return the registered dispatch_id and actual source event with the report", "Report actual tests, failures and evidence"]}
        target = Path(root) / "tasks"
        target.mkdir(exist_ok=True)
        atomic_json(target / f"{task_id}.json", spec)
    return load(root)


def _writer_actors(run: dict) -> set[str]:
    writers = {entry["actor"] for entry in run.get("writer_history", []) if entry.get("actor")}
    def remember(owner):
        if owner.get("agent"):
            writers.add(owner["agent"]["actor"])
        writers.update(entry["actor"] for entry in owner.get("dispatches", []) if entry.get("actor"))
        for previous in owner.get("previous_attempts", []):
            remember(previous)
    for owner in run["tasks"].values():
        remember(owner)
    for version in run.get("previous_versions", []):
        for owner in version.get("tasks", {}).values():
            remember(owner)
    return writers


def _exclusive_dispatch_available(run: dict, task_id: str) -> None:
    resources = {resource.strip() for resource in task_plan(run, task_id)["resources"]}
    for other_id, owner in run["tasks"].items():
        if other_id != task_id and owner["status"] == "DISPATCHED":
            other = {resource.strip() for resource in task_plan(run, other_id)["resources"]}
            require(not resources.intersection(other), "resource_in_use", f"Task {other_id} still holds an exclusive resource; collect its actual return before dispatching {task_id}.")
    for job in run.get("gate_jobs", {}).values():
        if job["status"] == "RUNNING":
            require(job["task"] != task_id and not resources.intersection(job["resources"]), "resource_in_use", "A recorded gate job still holds this task worktree or an exclusive resource; collect its actual result or explicitly recover its ended processes.")


def _rework_baseline(run: dict, task_id: str, owner: dict) -> dict | None:
    """The content the engine itself last established in this task worktree.

    Accepted reports of the attempt and snapshots recorded when a dispatch was abandoned set
    the baseline. A gate run in the same worktree moves it to the gate's after-snapshot only
    when the gate started from the baseline, so only the gate's own writes are adopted and a
    write made before the gate is not laundered by running one.
    """
    events = [(report["at"], "set", report["snapshot"]["content"], None) for report in owner.get("reports", []) if report.get("snapshot")]
    events += [(entry["at"], "set", entry["baseline"]["content"], None) for entry in owner.get("rework_history", []) if entry.get("baseline")]
    events += [(gate["at"], "gate", gate["after"]["content"], gate["snapshot"]["content"]) for gate in run["gates"]
               if gate.get("task") == task_id and (gate.get("after") or {}).get("path") == owner["path"]]
    baseline = None
    for at, kind, content, before in sorted(events, key=lambda item: parse_instant(item[0], "Observation time")):
        if kind == "set" or (baseline and before == baseline["content"]):
            baseline = {"at": at, "content": content}
    return baseline


def _pre_dispatch_content(run: dict, task_id: str, owner: dict, snap: dict) -> None:
    """Nothing may write the task worktree between preparation or the engine's last observation and the next dispatch."""
    if owner["status"] == "PREPARED":
        require(not snap["files"], "pre_dispatch_changes", "The task worktree already differs from its prepared base before any dispatch; preserve it and find out who wrote it.")
        return
    baseline = _rework_baseline(run, task_id, owner)
    if baseline:
        require(snap["content"] == baseline["content"], "pre_dispatch_changes", f"The task worktree changed after the engine last observed it ({baseline['at']}) and before the new dispatch; preserve it and find out who wrote it.")


def _typed_actor(actor, label: str) -> str:
    actor = text(actor, label)
    require(not actor.startswith(("workflow-agent:", "workflow-pending")), "reserved_actor", "Identities starting with workflow-agent: or workflow-pending are reserved for workflow-journal imports; register a workflow dispatch with --via-workflow.")
    return actor


def register_agent(root, task_id: str, actor: str | None = None, host: str | None = None, handle: str | None = None, expected_revision=None, *, via_workflow=False) -> dict:
    if not via_workflow and actor is not None:
        _typed_actor(actor, "Agent identity")
    with transaction(root, "agent_registered", expected_revision) as run:
        _implementation_state(run)
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] in {"PREPARED", "REWORK"}, "task_state", "Task is not ready for dispatch.")
        owned_worktree_record(run, owner, task_id=task_id)
        _pre_dispatch_content(run, task_id, owner, snapshot(run, task_id))
        _require_current_dependencies(run, task_id)
        _exclusive_dispatch_available(run, task_id)
        sequence = len(owner.get("dispatches", [])) + 1
        if via_workflow:
            require(actor is None and host is None and handle is None, "workflow_identity_supplied", "A workflow dispatch takes its agent identity from the host journal; do not supply actor, host or handle.")
            actor, host, handle = "workflow-pending", WORKFLOW_HOST, f"workflow-pending:{task_id}:a{owner['attempt']}:d{sequence}"
        actor, host, handle = text(actor, "Agent identity"), text(host, "Host"), text(handle, "Successful spawn handle")
        require(not any(other_id != task_id and other["status"] == "DISPATCHED" and other.get("agent", {}).get("host") == host and other.get("agent", {}).get("handle") == handle for other_id, other in run["tasks"].items()), "handle_in_use", "A live host handle cannot be assigned to two task dispatches.")
        dispatch_id = digest({"run": run["root"], "plan": run["plan_hash"], "task": task_id,
                              "attempt": owner["attempt"], "sequence": sequence,
                              "revision": run["revision"] + 1, "actor": actor, "host": host, "handle": handle})
        agent = {"actor": actor, "host": host, "handle": handle, "dispatch_id": dispatch_id,
                 "registered_at": now(), "liveness": "must-query-host"}
        if via_workflow:
            agent.update(via_workflow=True, liveness="workflow-journal")
        if owner.get("agent") and not owner.get("dispatches"):
            owner.setdefault("dispatches", []).append(copy.deepcopy(owner["agent"]))
        owner.setdefault("dispatches", []).append(agent)
        run.setdefault("writer_history", []).append({**agent, "task": task_id, "attempt": owner["attempt"]})
        owner["agent"] = agent
        owner["status"] = "DISPATCHED"
    return load(root)


def validate_report(result: dict) -> None:
    require(isinstance(result, dict), "invalid_report", "A report is a JSON object.")
    try:
        size = len(json.dumps(result, ensure_ascii=False))
    except (ValueError, RecursionError, TypeError) as exc:
        raise RunError("invalid_report", "The report cannot be serialized; return plain JSON.") from exc
    require(size <= 100_000, "report_limit", "Keep the report bounded; link detailed external evidence.")
    text(result.get("summary"), "summary")
    text(result.get("source_event"), "source_event")
    tests = result.get("tests")
    if isinstance(tests, str):
        text(tests, "tests")
    else:
        require(isinstance(tests, list), "invalid_report", "tests is a string or an array of actual command/outcome records; [] explicitly means no tests ran.")
        for test in tests:
            if isinstance(test, str):
                text(test, "Test observation")
                continue
            require(isinstance(test, dict), "invalid_report", "Each test observation is text or an object.")
            if isinstance(test.get("command"), list):
                command(test["command"])
            else:
                text(test.get("command"), "Observed command")
            text(test.get("outcome", test.get("result")), "Observed outcome")
            if "exit_code" in test:
                require(type(test["exit_code"]) is int, "invalid_report", "Observed exit_code is an integer.")
    limitations = result.get("limitations")
    if isinstance(limitations, str):
        text(limitations, "limitations")
    else:
        require(isinstance(limitations, list), "invalid_report", "limitations is text or an array of strings; [] explicitly means none were observed.")
        for limitation in limitations:
            text(limitation, "Limitation")


def check_source_event(source_event: str, dispatch: dict) -> None:
    """Refuse registration placeholders and results observed before their dispatch existed.

    Every ISO-8601 timestamp in the source event is read; one without a zone is UTC. The event
    is refused only when all of them are earlier than the registration: the latest one must be
    at or after it, compared at that timestamp's own precision. An event may still quote an
    earlier time, for example the registration itself, next to its observation time.
    """
    value = source_event.strip().lower()
    echoes = {str(dispatch.get(key) or "").strip().lower() for key in ("dispatch_id", "handle")} - {""}
    require(value not in PLACEHOLDER_EVENTS and value not in echoes, "placeholder_source_event", "source_event must reference the actual host result, not a registration placeholder, the dispatch ID or the handle.")
    registered = parse_instant(dispatch.get("registered_at"), "Dispatch registration time")
    observed = []
    for match in INSTANT.finditer(source_event):
        instant, digits = _instant(match)
        floor = registered.replace(microsecond=registered.microsecond - registered.microsecond % 10 ** (6 - digits))
        observed.append((instant >= floor, match.group(0)))
    require(not observed or any(later for later, _ in observed), "source_event_predates_dispatch",
            f"Every time in the source event ({', '.join(stamp for _, stamp in observed)}) predates its dispatch registration at {dispatch['registered_at']}; collect the current dispatch's actual result.")


def _accept_task_report(run: dict, task_id: str, result: dict) -> None:
    """Shared acceptance for a direct report and a journal import of the current dispatch."""
    owner = run["tasks"][task_id]
    validate_report(result)
    check_source_event(result["source_event"], owner["agent"])
    _require_current_dependencies(run, task_id, collecting=True)
    owned_worktree_record(run, owner, task_id=task_id)
    event = digest({"host": owner["agent"]["host"], "handle": owner["agent"]["handle"], "source_event": result["source_event"]})
    require(event not in run.get("accepted_source_events", {}), "source_event_reused", "This host result event was already accepted; collect the current dispatch's actual return.")
    snap = snapshot(run, task_id)
    paths = task_plan(run, task_id)["paths"]
    require(snap["files"] and all(covered(p, paths) for p in snap["files"]), "scope_violation", "Task changes must be nonempty and confined to approved paths.")
    report = {**result, "actor": owner["agent"]["actor"], "host": owner["agent"]["host"], "handle": owner["agent"]["handle"], "snapshot": snap, "at": now()}
    owner.update(status="REPORTED", report=report)
    owner.setdefault("reports", []).append(report)
    run.setdefault("accepted_source_events", {})[event] = {"task": task_id, "dispatch_id": owner["agent"]["dispatch_id"], "source_event": result["source_event"]}


def report_task(root, task_id: str, actor: str, result: dict, expected_revision=None) -> dict:
    with transaction(root, "task_report_received", expected_revision) as run:
        _collection_state(run)
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] == "DISPATCHED" and owner["agent"]["actor"] == actor, "agent_mismatch", "Only the registered task actor can supply its report.")
        require(not owner["agent"].get("via_workflow"), "workflow_report_required", "A workflow dispatch reports only through task-import from the host-written journal.")
        require(isinstance(result, dict), "invalid_report", "A task report must be a structured result.")
        require(result.get("dispatch_id") == owner["agent"]["dispatch_id"], "dispatch_mismatch", "The report must name the current registered dispatch_id; a prior handle's result cannot complete replacement work.")
        _accept_task_report(run, task_id, result)
    return load(root)


def _journal_evidence(found: dict) -> dict:
    return {"path": found["journal"], **{key: found[key] for key in ("run_id", "agent_id", "key", "label", "line_sha256", "transcript_sha256", "host_root")}}


def import_task_report(root, task_id: str, *, journal=None, search_root=None, expected_revision=None) -> dict:
    """Accept a workflow dispatch's result from the host-written journal, which supplies the agent identity."""
    import workflow_journal
    run = load(root)
    host = workflow_host(run)
    owner = run["tasks"].get(task_id)
    agent = (owner or {}).get("agent") or {}
    require(owner and owner["status"] == "DISPATCHED" and agent.get("via_workflow"), "not_workflow_dispatch", "Only a dispatched task registered with --via-workflow imports its result from a workflow journal.")
    dispatch_id = agent["dispatch_id"]
    matches = workflow_journal.find_results(lambda result: isinstance(result.get("dispatch_id"), str) and result["dispatch_id"] == dispatch_id,
                                            journal=journal, search_root=search_root, host=host)
    require(matches, "journal_result_missing", "No workflow journal result names this dispatch_id; wait for the actual return or query the host.")
    require(len(matches) == 1, "journal_result_ambiguous", f"{len(matches)} workflow journal results name this dispatch_id; select the exact journal after inspecting them.")
    found = matches[0]
    with transaction(root, "task_report_imported", expected_revision) as run:
        _collection_state(run)
        require(workflow_host(run) == host, "host_root_changed", "The run's workflow host root changed while the journal was searched.")
        run.setdefault("workflow_host_root", host)
        owner = run["tasks"].get(task_id)
        current = (owner or {}).get("agent") or {}
        require(owner and owner["status"] == "DISPATCHED" and current.get("via_workflow") and current.get("dispatch_id") == dispatch_id, "not_workflow_dispatch", "The workflow dispatch changed while its journal was searched; search again.")
        report = {**found["result"], "source_event": f"workflow-journal:{found['run_id']}/{found['agent_id']}#{found['line_sha256'][:16]}"}
        validate_report(report)
        if "tree" in report:
            index = git_ops.inventory(owner["path"], owner["base_sha"])["tree"]
            require(isinstance(report["tree"], str) and report["tree"].strip() == index, "import_tree_mismatch",
                    f"The imported result reports tree {report['tree']!r} but the task worktree's staged tree is {index}; the worktree changed after the agent returned or the result belongs elsewhere.")
        identity = {"actor": f"workflow-agent:{found['run_id']}/{found['agent_id']}", "handle": f"{found['run_id']}/{found['agent_id']}",
                    "journal": _journal_evidence(found), "liveness": "returned"}
        current.update(copy.deepcopy(identity))
        if owner.get("dispatches"):
            owner["dispatches"][-1].update(copy.deepcopy(identity))
        run.setdefault("writer_history", []).append({**copy.deepcopy(current), "task": task_id, "attempt": owner["attempt"]})
        _accept_task_report(run, task_id, report)
    return load(root)


def _signal_gate_group(process, number) -> None:
    # macOS refuses to signal a group whose only member is the exited, not yet
    # reaped leader. Reap that leader instead of reporting a command that never ran.
    try:
        os.killpg(process.pid, number)
    except ProcessLookupError:
        pass
    except PermissionError:
        if process.poll() is None:
            raise


@gate_jobs.guarded
def execute_gate(root, *, task_id: str | None = None, case_id: str | None = None, gate_index: int = 0, timeout: int | None = None, expected_revision=None) -> dict:
    """Run one planned gate; ``timeout`` overrides the gate's planned timeout (default 300 s)."""
    require(timeout is None or (type(timeout) is int and 1 <= timeout <= 3600), "invalid_timeout", "Gate timeout must be between 1 and 3600 seconds.")
    run = load(root)
    require(expected_revision is None or run["revision"] == expected_revision, "stale_revision", "Reload the run before executing a gate.")
    assert_plan(run)
    require(run["state"] in {"IMPLEMENTING", "VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "gate_state", "Gates are not executable in this state.")
    if task_id:
        task = task_plan(run, task_id)
        require(0 <= gate_index < len(task["gates"]), "unknown_gate", "Unknown planned task gate.")
        gate = task["gates"][gate_index]
        argv, key, risk = gate["command"], f"{task_id}:{gate_index}", gate["risk"]
        planned = gate
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] in {"REPORTED", "VERIFIED"}, "task_state", "Collect the real task report first.")
    else:
        case = next((c for c in run["plan"]["verification"] if c["id"] == case_id), None)
        require(case, "unknown_case", "Select a planned integrated verification case.")
        argv, key, risk = case["command"], case["id"], case["risk"]
        planned = case
        owner = run.get("integration")
        fix = run.get("integration_fix")
        require(not fix or fix.get("status") == "REPORTED", "integration_report_required", "Wait for the current integration-fix dispatch's real report before verification.")
    require(owner, "missing_worktree", "No owned worktree for the gate.")
    if timeout is None:
        timeout = gate_timeout(planned)
    if risk != "safe":
        need_authority(run, "test-" + risk, target=key)
    repeat = 1
    if not task_id:
        rich_cases = assert_rich_checks(run, for_execution=True)
        if rich_cases:
            repeat = rich_cases[case["check_case"]]["repeat"]
            require(type(repeat) is int and 1 <= repeat <= 100, "invalid_repeat", "Bound rich repeats to 1–100 actual executions per gate.")
            rich_plan = json.loads((Path(run["check_root"]) / "check.json").read_text())
            by_rich_id = {c["check_case"]: c["id"] for c in run["plan"]["verification"]}
            current_snapshot = snapshot(run)
            for entry in sorted(rich_plan["execution_plan"], key=lambda item: item["order"]):
                if entry["case"] == case["check_case"]:
                    break
                prior = [g for g in run["gates"] if g["task"] is None and g["key"] == by_rich_id[entry["case"]]]
                require(prior and evidence_current(prior[-1], current_snapshot), "case_order", "Execute the authorized rich cases in their reviewed order.")
                require(not entry.get("stop_on_fail", True) or prior[-1]["passed"], "case_stop_on_fail", "A preceding stop-on-fail case failed; resolve it before later effects.")
    before = snapshot(run, task_id)
    logs = Path(root) / "evidence"
    logs.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="gate-", suffix=".log", dir=logs)
    start = time.monotonic()
    code, timed_out, attempts = -1, False, []
    with os.fdopen(fd, "w") as output:
        for attempt in range(repeat):
            output.write(f"Delivery gate attempt {attempt + 1}/{repeat}\n")
            output.flush()
            try:
                # Python bytecode is a side effect of the gate, not of the change: without it an untracked
                # __pycache__/ in a repository that does not ignore it marks a passing gate as changing the worktree.
                process = subprocess.Popen(argv, cwd=owner["path"], stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
                try:
                    gate_jobs.set_process(process.pid)
                    remaining = max(0.01, timeout - (time.monotonic() - start))
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _signal_gate_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        _signal_gate_group(process, signal.SIGKILL)
                        process.wait()
                    code = process.returncode
                except BaseException:
                    # This process group belongs to this invocation. A failed
                    # identity checkpoint must not leave its child running.
                    _signal_gate_group(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        _signal_gate_group(process, signal.SIGKILL)
                        process.wait()
                    raise
            except OSError as error:
                code = 127
                output.write(f"The planned command could not execute: {error}\n")
            outcome = "timed out" if timed_out else ("ok" if code == 0 else (f"killed by signal {-code}" if code < 0 else f"exit {code}"))
            attempts.append({"attempt": attempt + 1, "exit_code": code, "timed_out": timed_out, "outcome": outcome})
            if code != 0 or timed_out:
                break
    after = snapshot(load(root), task_id)
    receipt = {"key": key, "task": task_id, "command": argv, "exit_code": code, "timed_out": timed_out,
               "planned_repeat": repeat, "attempts": attempts, "outcome": outcome, "timeout_seconds": timeout,
               "duration_seconds": round(time.monotonic() - start, 3), "at": now(), "snapshot": before,
               "unchanged": before["content"] == after["content"], "log": name, "log_hash": hashlib.sha256(Path(name).read_bytes()).hexdigest(),
               "after": {"content": after["content"], "base_sha": after["base_sha"], "path": owner["path"]}}
    receipt["passed"] = code == 0 and not timed_out and receipt["unchanged"] and len(attempts) == repeat
    def admit(current):
        require(current["state"] not in TERMINAL, "terminal_run", "This run has ended.")
        require(current["plan_hash"] == run["plan_hash"], "plan_drift", "Plan changed while gate executed.")
    # The job's finishing checkpoint appends the receipt; see gate_jobs.defer_receipt.
    gate_jobs.defer_receipt(receipt, admit)
    return receipt


def assert_rich_checks(run: dict, *, for_execution=False) -> dict:
    if run["plan"]["classification"]["level"] == "small":
        return {}
    binding = run.get("rich_snapshot") or {}
    require(binding and evidence_current(binding, snapshot(run)) and binding.get("check_root") == run["check_root"], "stale_check_binding", "Capture current integration snapshots before authorizing rich checks.")
    path = Path(run["check_root"]) / "actual-diff.json"
    require(path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == binding.get("check_diff_hash"), "check_diff_drift", "The check inventory no longer matches the authoritative integration snapshot.")
    status = spec_command(run["root"], ["status"], check=True, _run=run)
    if not for_execution:
        require(status.get("state") == "CHECK_DONE", "unfinished_rich_checks", "Finish every rich check and its evidence report before release readiness.")
    if status.get("state") == "CHECK_DONE" and not for_execution:
        validation = spec_command(run["root"], ["check-validate"], check=True, _run=run)
        require(status.get("authorization") and validation.get("readiness", {}).get("ready_to_execute"), "stale_check_authority", "Completed check authority must still match its artifacts.")
    else:
        spec_command(run["root"], ["check-guard"], check=True, _run=run)
    required = "deep" if run["plan"]["classification"]["level"] == "large" else "standard"
    require(status.get("check_mode") in ({"deep"} if required == "deep" else {"standard", "deep"}), "check_depth", "The rich check depth cannot be lower than the run's risk classification.")
    check_plan = json.loads((Path(run["check_root"]) / "check.json").read_text())
    cases = check_plan["cases"]
    mapping = {case["id"]: case for case in cases}
    order = check_plan["execution_plan"]
    require(all(type(entry.get("order")) is int and entry["order"] > 0 and type(entry.get("stop_on_fail", True)) is bool for entry in order), "invalid_case_order", "Rich execution order and stop-on-fail flags must be explicit and valid.")
    require(len({entry["order"] for entry in order}) == len(order) and len({entry["case"] for entry in order}) == len(order), "invalid_case_order", "Execute each rich case once in a unique position; use repeat for repeated observations.")
    planned = [case["check_case"] for case in run["plan"]["verification"]]
    obligations = {case["id"] for case in cases if case.get("review", {}).get("verdict") == "accepted"}
    obligations.update(entry["case"] for entry in check_plan["execution_plan"])
    require(len(planned) == len(set(planned)) and set(planned) == obligations == {entry["case"] for entry in order}, "unmapped_rich_case", "Every accepted/planned rich case must map exactly once to an ordered actual delivery execution.")
    safety = {"safe": "safe", "external": "needs-approval", "destructive": "destructive"}
    for case in run["plan"]["verification"]:
        rich = mapping.get(case["check_case"], {})
        require(rich.get("delivery_command") == case["command"] and rich.get("safety") == safety[case["risk"]], "check_command_mismatch", "Each rich case must bind the exact delivery_command and declared side-effect risk that will execute.")
    return mapping


def evidence_current(receipt: dict, snap: dict) -> bool:
    if receipt.get("snapshot", {}).get("content") != snap["content"] or receipt["snapshot"].get("base_sha") != snap["base_sha"]:
        return False
    if "log" in receipt:
        path = Path(receipt["log"])
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == receipt.get("log_hash")
    return True


def gates_current(run: dict, task_id: str | None = None) -> bool:
    snap = snapshot(run, task_id)
    keys = [f"{task_id}:{i}" for i in range(len(task_plan(run, task_id)["gates"]))] if task_id else [c["id"] for c in run["plan"]["verification"]]
    for key in keys:
        receipts = [g for g in run["gates"] if g["key"] == key and g["task"] == task_id]
        if not receipts or not receipts[-1]["passed"] or not evidence_current(receipts[-1], snap):
            return False
    return True


def assert_gates_idle(run: dict, *, task_id: str | None = None) -> None:
    resources = {r.strip() for r in task_plan(run, task_id)["resources"]} if task_id else set()
    for job in run.get("gate_jobs", {}).values():
        if job["status"] == "RUNNING":
            require(task_id is not None and job["task"] != task_id and not resources.intersection(job["resources"]),
                    "gate_job_active", "Wait for the active gate's actual result or recover its confirmed-ended job before changing its lifecycle or worktree.")


def triage(decision: Any, finding_keys: Any) -> tuple[str, list[str]]:
    decision = "code-fix" if decision is None else decision
    require(decision in DECISIONS, "invalid_decision", "A finding decision is code-fix, test-plan, environment, requirements or human.")
    keys = [] if finding_keys is None else finding_keys
    require(isinstance(keys, (list, tuple)) and all(isinstance(key, str) and key.strip() and len(key) <= 1000 for key in keys), "invalid_finding_key", "Finding keys are short non-empty strings such as path|kind.")
    return decision, sorted({key.strip().lower() for key in keys})


def review_evidence(verdict: str, evidence: Any) -> str:
    value = text(evidence, "Original authorization or host-result reference")
    if verdict == "PASS":
        lines = [line for line in value.splitlines() if line.strip()]
        require(len(value) >= 160 and len(lines) >= 2, "review_evidence_insufficient", "A PASS needs concrete evidence per acceptance item and the gates the reviewer reran, not a one-line verdict.")
    return value


def _record_review(run: dict, root, actor: str, verdict: str, evidence: str, *, task_id: str | None, decision: str, finding_keys: list[str], extra: dict | None = None) -> None:
    """The single recording path for direct and journal-imported reviews."""
    assert_plan(run)
    assert_gates_idle(run, task_id=task_id)
    if task_id is not None:
        _implementation_state(run)
        require(task_id in run["tasks"], "missing_worktree", "Prepare the owned task worktree first.")
        _require_current_dependencies(run, task_id)
    else:
        require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "review_state", "Integrated review requires returned implementation and an active verification or PR state.")
        fix = run.get("integration_fix")
        if fix:
            require(fix.get("status") != "DISPATCHED", "fix_report_required", "Collect the registered integration fix's actual return before review.")
            require(fix.get("report") and evidence_current(fix["report"], snapshot(run)), "stale_fix_report", "The integration fix report must match the current integration content.")
    require(gates_current(run, task_id), "gate_required", "Run all planned gates on the current content before review.")
    require(actor not in _writer_actors(run), "reviewer_not_independent", "The reviewer must not be a current or previous implementation actor.")
    current = snapshot(run, task_id)
    if verdict == "PASS":
        require(not _unoverridden_failures(run, task_id, current), "review_reroll",
                "This exact content and base already received a FAIL review; a later PASS does not replace it. Change the content "
                "(rework, or an integration fix), or record the user's authorize --scope decision --code review_override for this content after that FAIL.")
    receipt = {**attestation(actor, evidence), "task": task_id, "verdict": verdict, "snapshot": current,
               "plan_hash": run["plan_hash"], "spec_version": run["spec_version"], **(extra or {})}
    if task_id is not None:
        receipt["attempt"] = run["tasks"][task_id]["attempt"]
    if verdict == "FAIL":
        receipt.update(decision=decision, finding_keys=finding_keys)
    run["reviews"].append(receipt)
    if task_id is None and verdict == "FAIL":
        _integrated_finding(run, receipt, evidence)
    if task_id:
        owner = run["tasks"][task_id]
        require(owner["status"] in {"REPORTED", "VERIFIED"}, "task_state", "Review requires a returned task report.")
        if verdict == "PASS":
            report = owner["report"]
            require(evidence_current(report, snapshot(run, task_id)), "stale_report", "Collect an updated implementer report after changes.")
            patches = Path(root) / "patches"
            patches.mkdir(exist_ok=True)
            snap = snapshot(run, task_id)
            require(all(covered(p, task_plan(run, task_id)["paths"]) for p in snap["files"]), "scope_violation", "The reviewed changes left the task's owned scope.")
            patch = git_ops.export_patch(owner["path"], str(patches / f"v{run['spec_version']}-{task_id}-r{run['revision']}.patch"), snap["files"])
            owner.update(status="VERIFIED", patch=patch)
        else:
            _mark_rework(run, task_id, evidence, decision, finding_keys)


def _failed_reviews(run: dict, task_id: str | None, snap: dict) -> list[dict]:
    """Every FAIL review of this target on exactly this content and base.

    Any decision, attempt or plan version counts, including harvested late FAILs: only changed
    content or a review_override recorded after the FAIL lets a later PASS through.
    """
    reviews = [review for version in run.get("previous_versions", []) for review in version.get("reviews", [])]
    reviews += [review for entry in run.get("verification_history", []) for review in entry.get("reviews", [])] + run["reviews"] + run.get("harvested_reviews", [])
    return [review for review in reviews if review.get("task") == task_id and review.get("verdict") == "FAIL"
            and (review.get("snapshot") or {}).get("content") == snap["content"] and review["snapshot"].get("base_sha") == snap["base_sha"]]


def _unoverridden_failures(run: dict, task_id: str | None, snap: dict) -> list[dict]:
    """FAILs on this content and base that no review_override recorded after them covers."""
    covered = {key for grant in run.get("decision_grants", []) if grant.get("code") == "review_override" and grant.get("task") == task_id
               and grant.get("content") == snap["content"] and grant.get("base_sha") == snap["base_sha"] for key in grant.get("failed_reviews", [])}
    return [review for review in _failed_reviews(run, task_id, snap) if _review_key(review) not in covered]


def _review_key(review: dict) -> str:
    return review.get("id") or review["at"]


def _integrated_finding(run: dict, receipt: dict, reason: str) -> None:
    """Apply the triage decision of an integrated FAIL review; code-fix keeps the same-scope fix path."""
    if receipt["decision"] == "requirements":
        _block_task(run, "requirements_finding", f"A requirements finding needs revise and renewed approval, not an integration fix: {reason}")
    elif receipt["decision"] == "human":
        _block_task(run, "human_decision", f"A human decision is required: {reason}")
    elif receipt["decision"] == "code-fix" and receipt["finding_keys"]:
        earlier = [review for entry in run.get("verification_history", []) for review in entry.get("reviews", [])] + run["reviews"][:-1]
        previous = next((review for review in reversed(earlier) if review.get("task") is None and review.get("verdict") == "FAIL"
                         and review.get("plan_hash") == run["plan_hash"] and review.get("spec_version") == run["spec_version"]), None)
        repeated = sorted(set(receipt["finding_keys"]) & set((previous or {}).get("finding_keys") or []))
        if repeated:
            _block_task(run, "non_converging", f"The same integrated finding survived a fix round ({', '.join(repeated)}); revise the plan, or record authorize --scope rework-budget --count N and resume.")


def review(root, actor: str, verdict: str, evidence: str, *, task_id: str | None = None, expected_revision=None, decision="code-fix", finding_keys=None) -> dict:
    require(verdict in {"PASS", "FAIL"}, "invalid_verdict", "Review verdict is PASS or FAIL.")
    actor = _typed_actor(actor, "Reviewer identity")
    decision, finding_keys = triage(decision, finding_keys)
    review_evidence(verdict, evidence)
    with transaction(root, "independent_review_recorded", expected_revision) as run:
        owner = run["tasks"].get(task_id) if task_id is not None else None
        require(not ((owner or {}).get("agent") or {}).get("via_workflow"), "workflow_review_required",
                "A task implemented through a workflow dispatch is reviewed only through review-token and review-import.")
        require(not _pending_request(run, task_id), "workflow_review_required",
                "Review tokens are open for this target; import their verdicts with review-import instead of typing a review.")
        _record_review(run, root, actor, verdict, evidence, task_id=task_id, decision=decision, finding_keys=finding_keys)
    return load(root)


def _review_target(task_id: str | None) -> str:
    return f"task:{task_id}" if task_id is not None else "integration"


def _review_identity(run: dict, task_id: str | None) -> dict:
    """What a review request is bound to: exact content, plan version and the result under review."""
    require(run.get("plan"), "missing_plan", "Record a plan before requesting review.")
    if task_id is not None:
        task_plan(run, task_id)
        report = (run["tasks"].get(task_id) or {}).get("report") or {}
        subject = {"dispatch_id": report.get("dispatch_id"), "reported_at": report.get("at")}
    else:
        fix = run.get("integration_fix") or {}
        subject = {"fix_dispatch_id": fix.get("dispatch_id"), "fix_status": fix.get("status")}
    snap = snapshot(run, task_id)
    return {"task": task_id, "content": snap["content"], "base_sha": snap["base_sha"], "plan_hash": run["plan_hash"], "spec_version": run["spec_version"], "subject": subject}


def _token(run: dict, request: dict, lens: str) -> str:
    return "review-" + digest({"run": run["root"], "lens": lens, "request": request["request"], **request["identity"]})[:32]


def _pending_request(run: dict, task_id: str | None) -> dict | None:
    """An open review request of the current plan version for this target, whatever content it was issued for."""
    request = run.get("review_requests", {}).get(_review_target(task_id))
    if request and request["status"] == "open" and request["identity"].get("plan_hash") == run.get("plan_hash") \
            and request["identity"].get("spec_version") == run.get("spec_version"):
        return request
    return None


def _open_request(run: dict, task_id: str | None) -> dict | None:
    request = _pending_request(run, task_id)
    if request and request["identity"] == _review_identity(run, task_id):
        return request
    return None


def review_token(root, *, task_id: str | None = None, lens: str = "default", expected_revision=None) -> dict:
    """Issue and record the token a reviewer returns, binding its verdict to this exact content and lens.

    Requests are recorded per target: every lens issued for the same content and reviewed result
    joins one open request, and review-import must import all of them together. A changed content,
    report or fix, or an import of the request, starts a new request with new tokens; a new request
    that supersedes an open one keeps all of its lenses. Tokens are issued only where their verdicts
    can be imported (a returned task in active implementation; the integration during verification
    or review with no fix in flight), and an open request whose tokens already have unimported,
    countable verdicts is not superseded: import them first.
    """
    lens = identifier(lens)
    with transaction(root, "review_token_issued", expected_revision) as run:
        assert_plan(run)
        _review_state(run, task_id)
        request = _open_request(run, task_id)
        if request is None:
            superseded = _pending_request(run, task_id)
            previous = run.setdefault("review_requests", {}).get(_review_target(task_id))
            # Superseding an open request of this plan version never drops a lens: each one must be reviewed again and imported.
            carried = sorted(superseded["lenses"]) if superseded else []
            if previous:
                run.setdefault("review_request_history", []).append({**previous, "status": "superseded" if previous["status"] == "open" else previous["status"]})
            request = {"target": task_id, "identity": _review_identity(run, task_id), "request": run["revision"] + 1, "status": "open", "lenses": {}, "at": now()}
            for kept in carried:
                request["lenses"][kept] = _token(run, request, kept)
            run["review_requests"][_review_target(task_id)] = request
        request["lenses"].setdefault(lens, _token(run, request, lens))
        issued = {"token": request["lenses"][lens], "task": task_id, "lens": lens, "content": request["identity"]["content"],
                  "request": request["request"], "lenses": sorted(request["lenses"]), "revision": run["revision"] + 1}
    return issued


def _review_state(run: dict, task_id: str | None) -> None:
    """Where a review can be imported, and therefore where review tokens may be issued."""
    if task_id is not None:
        task_plan(run, task_id)
        require(run["state"] == "IMPLEMENTING" and not run.get("integration") and (run["tasks"].get(task_id) or {}).get("status") in {"REPORTED", "VERIFIED"},
                "review_state", "Request task review tokens for a returned task during active, unintegrated implementation.")
    else:
        require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"} and run.get("integration") and (run.get("integration_fix") or {}).get("status") != "DISPATCHED",
                "review_state", "Request integrated review tokens during verification or review, with no integration fix in flight.")


def _writer_agents(run: dict) -> tuple[set, set]:
    writers = _writer_actors(run)
    return writers, {actor.split("/", 1)[1] for actor in writers if actor.startswith("workflow-agent:") and "/" in actor}


def _usable_defects(defects: Any) -> bool:
    return isinstance(defects, list) and all(isinstance(d, dict) and all(isinstance(d[k], str) for k in ("file", "kind") if k in d) for d in defects)


def _storable(verdict: dict) -> dict:
    """Keep a harvested verdict storable: evidence as text and defects as flat scalar fields."""
    evidence = verdict.get("evidence")
    if not isinstance(evidence, str):
        try:
            evidence = json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False)
        except (ValueError, RecursionError, TypeError):
            evidence = "(the reviewer's evidence could not be stored)"
    defects = [{key: defect[key] for key in ("file", "line", "kind", "severity", "summary", "scenario")
                if isinstance(defect.get(key), (str, int)) and not isinstance(defect.get(key), bool)}
               for defect in verdict.get("defects") or [] if isinstance(defect, dict)]
    return {**verdict, "evidence": evidence[:20_000], "defects": defects}


def _unreadable_verdict(result: dict, item: dict) -> dict:
    verdict = result.get("verdict")
    if isinstance(verdict, str) and verdict.strip().upper() == "FAIL":
        return {**item, **_normalized_fail({"evidence": "(the reviewer's FAIL could not be read in full)", "defects": None})}
    return {**item, "verdict": verdict if isinstance(verdict, str) else None, "refused": "The verdict could not be read in full.", "refused_code": "unreadable_verdict"}


def _normalized_fail(result: dict) -> dict:
    evidence, defects, normalized = result.get("evidence"), result.get("defects"), []
    if not ((isinstance(evidence, str) and evidence.strip()) or (isinstance(evidence, dict) and evidence)):
        evidence, normalized = "(the reviewer returned FAIL without usable evidence)", normalized + ["evidence"]
    if not _usable_defects(defects):
        defects, normalized = [{"file": "(review)", "kind": "other", "summary": "The reviewer returned FAIL with defects in an unusable shape."}], normalized + ["defects"]
    return {"verdict": "FAIL", "evidence": evidence, "defects": defects, **({"normalized": normalized} if normalized else {})}


def _finding_keys(defects: list) -> list[str]:
    return sorted({f"{str(d.get('file', '')).strip().lower()}|{str(d.get('kind', '')).strip().lower()}" for d in defects} - {"|"})


def _tracked_tokens(run: dict) -> dict:
    """Every issued review token, whatever became of its request, with the content it was issued for."""
    tokens = {}
    for request in list(run.get("review_request_history", [])) + list(run.get("review_requests", {}).values()):
        identity = request.get("identity") or {}
        for lens, token in request.get("lenses", {}).items():
            tokens[token] = {"target": request.get("target"), "lens": lens, "request": request.get("request"), "content": identity.get("content"),
                             "base_sha": identity.get("base_sha"), "plan_hash": identity.get("plan_hash"), "spec_version": identity.get("spec_version")}
    return tokens


def harvest_reviews(run: dict) -> dict | None:
    """Record every countable verdict returned for any issued review token, then apply late FAILs.

    Runs at the start of every mutating command. Each new verdict becomes a receipt in
    ``harvested_reviews`` bound to the content and base its token was issued for. A result with
    incomplete provenance counts when it is a FAIL and is refused when it is a PASS (fail closed);
    verdicts from implementation actors and invalid PASS results are refused.

    The outcome is kept in ``run["harvest"]``: whether the harvest was complete, the reason when it
    was not, the journals or directories that could not be read, and the workflow paths skipped
    because they are symlinks (not refused: forging one needs write access to the host root). It
    never raises; a failed harvest leaves the verdicts as they were, and release commands refuse
    while the record is incomplete. Returns the history entry to persist, or None.
    """
    try:
        if not _tracked_tokens(run):
            return None
    except Exception as exc:  # noqa: BLE001 - a harvest problem must never stop the command
        return _harvest_record(run, None, [], [], exc)
    candidate = copy.deepcopy(run)
    problems, skipped = [], []
    try:
        changed = _harvest(candidate, problems, skipped)
        changed = _apply_late_failures(candidate) or changed
    except Exception as exc:  # noqa: BLE001 - a harvest problem must never stop the command
        return _harvest_record(run, None, problems, skipped, exc)
    return _harvest_record(run, candidate if changed else None, problems, skipped, None)


def _harvest_record(run: dict, harvested: dict | None, problems: list, skipped: list, error: Exception | None) -> dict | None:
    if harvested is not None:
        run.clear()
        run.update(harvested)
    reason = f"{type(error).__name__}: {error}"[:2000] if error else ("Some workflow journals or directories under the host root could not be read." if problems else None)
    record = {"complete": reason is None, "reason": reason, "unreadable": problems, "symlinks_skipped": sorted(set(skipped))}
    previous = dict(run.get("harvest") or {})
    unchanged = {key: value for key, value in previous.items() if key != "at"} == record
    run["harvest"] = {**record, "at": previous.get("at") if unchanged else now()}
    if not record["complete"] and not unchanged:
        return {"event": "harvest_incomplete", "reason": reason, "paths": [item["path"] for item in problems]}
    if harvested is not None or not unchanged:
        return {"event": "reviews_harvested"}
    return None


def require_complete_harvest(run: dict) -> None:
    """Release and review import read every verdict: an incomplete harvest could hide a late FAIL."""
    if not _tracked_tokens(run):
        return
    record = run.get("harvest") or {}
    paths = [item["path"] for item in record.get("unreadable", [])]
    require(record.get("complete"), "harvest_incomplete",
            f"The review harvest is incomplete ({record.get('reason') or 'no complete harvest recorded'}"
            + (f"; unreadable: {', '.join(paths)}" if paths else "") + "); restore read access to the host root and retry.")


def _harvest(run: dict, problems: list, skipped: list) -> bool:
    import workflow_journal
    tokens = _tracked_tokens(run)
    host = run.get("workflow_host_root") or str(workflow_journal.host_root())
    if not Path(host).is_dir():
        return False
    accepted = run.setdefault("accepted_review_events", {})
    refused = run.setdefault("refused_review_events", {})
    refused_before = len(refused)
    rejected = []
    found = workflow_journal.find_results(lambda result: isinstance(result.get("review_token"), str) and result["review_token"] in tokens,
                                          host=host, strict=False, rejected=rejected, problems=problems, skipped=skipped)
    writers = _writer_agents(run)
    receipts = []
    for item in found:
        line = item["line_sha256"]
        if line in accepted or line in refused:
            continue
        info = tokens[item["result"]["review_token"]]
        try:
            verdict = _classify_verdict(info["lens"], item, writers)
        except workflow_journal.LINE_ERRORS:
            # A result too odd to read in full still counts when it is a FAIL.
            verdict = _unreadable_verdict(item["result"], {"lens": info["lens"], "actor": f"workflow-agent:{item['run_id']}/{item['agent_id']}", "journal": _journal_evidence(item)})
        if "refused" in verdict:
            refused[line] = {"token": item["result"]["review_token"], "task": info["target"], "lens": info["lens"], "actor": verdict["actor"],
                             "verdict": verdict["verdict"], "reason": verdict["refused"], "code": verdict["refused_code"], "line_sha256": line, "at": now()}
            continue
        receipts.append((line, item["result"]["review_token"], info, {**verdict, "agent_id": item["agent_id"], "unverified": False}))
    for item in rejected:
        line, result = item["line_sha256"], item["result"]
        if line in accepted or line in refused:
            continue
        info = tokens[result["review_token"]]
        agent = item.get("agent_id") if isinstance(item.get("agent_id"), str) and workflow_journal.AGENT_ID.fullmatch(item["agent_id"]) else None
        actor = f"unverified:{Path(item['journal']).parent.name}/{agent or 'unknown-agent'}"
        base = {"lens": info["lens"], "actor": actor, "agent_id": agent or actor, "unverified": True,
                "journal": {"path": item["journal"], "line_sha256": line, "code": item["code"], "message": item["message"]}}
        is_writer = agent is not None and agent in writers[1]
        if isinstance(result.get("verdict"), str) and result["verdict"].strip().upper() == "FAIL" and not is_writer:
            receipts.append((line, result["review_token"], info, {**base, **_normalized_fail(result)}))
            continue
        refused[line] = {"token": result["review_token"], "task": info["target"], "lens": info["lens"], "actor": actor, "verdict": result.get("verdict") if isinstance(result.get("verdict"), str) else None,
                         "reason": "The reviewer is a current or previous implementation actor." if is_writer else f"A PASS with incomplete provenance does not count: {item['message']}",
                         "code": "writer_verdict" if is_writer else "unverified_pass", "line_sha256": line, "at": now()}
    for line, token, info, verdict in receipts:
        verdict = _storable(verdict)
        receipt = {"id": line, "source": "harvested", "token": token, "request": info["request"],
                   "task": info["target"], "at": now(), "snapshot": {"content": info["content"], "base_sha": info["base_sha"]},
                   "plan_hash": info["plan_hash"], "spec_version": info["spec_version"], **verdict}
        if verdict["verdict"] == "FAIL":
            receipt.update(decision="code-fix", finding_keys=_finding_keys(verdict["defects"]))
        run.setdefault("harvested_reviews", []).append(receipt)
        accepted[line] = {"token": receipt["token"], "task": info["target"], "lens": info["lens"], "actor": verdict["actor"], "harvested": True, "at": receipt["at"]}
    return bool(receipts) or len(refused) != refused_before


def _apply_late_failures(run: dict) -> bool:
    """A harvested FAIL on a target's current content supersedes an earlier PASS on it.

    A verified, not yet integrated task returns to REWORK (a counted code-fix round with the FAIL's
    finding keys); if that is not possible yet the FAIL stays pending and is retried. On an integrated
    task or the integration, release is refused instead (see assert_no_late_failures). A FAIL on stale
    content stays history, where the re-roll guard applies.
    """
    changed = False
    for receipt in run.get("harvested_reviews", []):
        if receipt["verdict"] != "FAIL" or receipt.get("effect") not in (None, "pending"):
            continue
        effect = _late_failure_effect(run, receipt)
        if effect != receipt.get("effect"):
            receipt["effect"] = effect
            changed = True
    return changed


def _late_failure_effect(run: dict, receipt: dict) -> str:
    if receipt.get("plan_hash") != run.get("plan_hash") or receipt.get("spec_version") != run.get("spec_version"):
        return "history"
    target = receipt["task"]
    if target is None:
        return "guard"
    owner = run["tasks"].get(target)
    if not owner or owner["status"] not in {"REPORTED", "VERIFIED"}:
        return "history"
    try:
        snap = snapshot(run, target)
    except (RunError, git_ops.GitError):
        return "pending"
    if receipt["snapshot"] != {"content": snap["content"], "base_sha": snap["base_sha"]}:
        return "history"
    if receipt["id"] not in {_review_key(review) for review in _unoverridden_failures(run, target, snap)}:
        return "overridden"
    if run.get("integration"):
        integrated = snapshot(run)
        receipt["integration_snapshot"] = {"content": integrated["content"], "base_sha": integrated["base_sha"]}
        return "guard"
    if owner["status"] == "REPORTED":
        return "guard"
    try:
        _mark_rework(run, target, f"A late review FAIL from {receipt['actor']} was harvested for the verified content.", "code-fix", receipt["finding_keys"])
    except (RunError, git_ops.GitError):
        return "pending"
    return "rework"


def assert_no_late_failures(run: dict) -> None:
    """Release waits for a complete harvest, every unoverridden FAIL on the current integration content, and late FAILs on integrated tasks."""
    require_complete_harvest(run)
    if not run.get("integration"):
        return
    snap = snapshot(run)
    failures = _unoverridden_failures(run, None, snap)
    require(not failures, "late_review_fail", f"The integrated content has an unanswered FAIL review from {failures[0]['actor'] if failures else ''}; "
            "change the content through an integration fix, or record authorize --scope decision --code review_override after that FAIL.")
    current = {"content": snap["content"], "base_sha": snap["base_sha"]}
    for receipt in run.get("harvested_reviews", []):
        if receipt["verdict"] == "FAIL" and receipt.get("effect") == "guard" and receipt["task"] is not None and receipt.get("integration_snapshot") == current:
            task_snap = snapshot(run, receipt["task"])
            require(receipt["id"] not in {_review_key(review) for review in _unoverridden_failures(run, receipt["task"], task_snap)}, "late_review_fail",
                    f"Integrated task {receipt['task']} received a late FAIL from {receipt['actor']}; change the integrated content through an integration fix, "
                    f"or record authorize --scope decision --code review_override --task {receipt['task']} after that FAIL.")


def _classify_verdict(lens: str, found: dict, writers: tuple[set, set]) -> dict:
    """A FAIL for an issued token always counts; a PASS counts only with usable evidence and defects.

    A FAIL whose evidence or defects are unusable is normalized (its defects become the single
    finding key (review)|other); a PASS that fails validation is kept as refused, with its reason.
    A verdict from a current or previous implementation actor never counts and is kept as refused
    (writer_verdict); its lens still needs an independent verdict.
    """
    result = found["result"]
    item = {"lens": lens, "actor": f"workflow-agent:{found['run_id']}/{found['agent_id']}", "journal": _journal_evidence(found)}
    verdict = result.get("verdict").strip().upper() if isinstance(result.get("verdict"), str) else ""
    if item["actor"] in writers[0] or found["agent_id"] in writers[1]:
        return {**item, "verdict": result.get("verdict"), "refused": "The reviewer is a current or previous implementation actor.", "refused_code": "writer_verdict"}
    if verdict == "FAIL":
        return {**item, **_normalized_fail(result)}
    if verdict != "PASS":
        return {**item, "verdict": result.get("verdict"), "refused": "The verdict is neither PASS nor FAIL.", "refused_code": "invalid_verdict"}
    try:
        return _lens_result(lens, {**found, "result": {**result, "verdict": "PASS"}})
    except RunError as exc:
        return {**item, "verdict": "PASS", "refused": exc.message, "refused_code": exc.code}


def _lens_result(lens: str, found: dict) -> dict:
    result = found["result"]
    require(result.get("verdict") in {"PASS", "FAIL"}, "invalid_verdict", f"Review lens {lens} must return verdict PASS or FAIL.")
    evidence = result.get("evidence")
    require((isinstance(evidence, str) and evidence.strip()) or (isinstance(evidence, dict) and evidence), "missing_text", f"Review lens {lens} must return concrete evidence.")
    rendered = evidence if isinstance(evidence, str) else json.dumps(evidence, indent=2, sort_keys=True, ensure_ascii=False)
    review_evidence(result["verdict"], rendered)
    defects = result.get("defects")
    require(isinstance(defects, list) and all(isinstance(d, dict) and all(isinstance(d[k], str) for k in ("file", "kind") if k in d) for d in defects),
            "invalid_defects", f"Review lens {lens} must return defects as a list of objects whose file and kind are strings.")
    return {"lens": lens, "actor": f"workflow-agent:{found['run_id']}/{found['agent_id']}", "verdict": result["verdict"],
            "evidence": evidence, "defects": defects, "journal": _journal_evidence(found)}


def import_review(root, *, lenses, task_id: str | None = None, journal=None, search_root=None, decision=None, finding_keys=None, expected_revision=None) -> dict:
    """Record one review from the verdicts harvested for the open request, bound to the current content.

    The transaction harvests first, so every verdict returned for the request's tokens is already a
    receipt: all of them count and the worst decides. A lens with only refused results (an invalid
    or unverifiable PASS, a writer's verdict) is incomplete until a correct rerun returns. A named
    journal or search root is still checked against the host root; the harvest always reads all of it.
    """
    import workflow_journal
    require(isinstance(lenses, (list, tuple)) and lenses and len(set(lenses)) == len(lenses), "invalid_lens", "Name one or more distinct review lenses.")
    decision, finding_keys = triage(decision, finding_keys)
    host = workflow_host(load(root))
    workflow_journal.check_location(journal, search_root, host)
    with transaction(root, "independent_review_imported", expected_revision) as run:
        require(workflow_host(run) == host, "host_root_changed", "The run's workflow host root changed while the command started.")
        run.setdefault("workflow_host_root", host)
        require_complete_harvest(run)
        request = _open_request(run, task_id)
        require(request, "review_not_requested", "No review-token request is open for the current content; request tokens with review-token first.")
        extra = sorted(set(lenses) - set(request["lenses"]))
        require(not extra, "review_not_requested", "These lenses were never issued for the current content: " + ", ".join(extra))
        missing = sorted(set(request["lenses"]) - set(lenses))
        require(not missing, "review_lenses_incomplete", "Import every lens issued for the current content in one call; missing: " + ", ".join(missing))
        tokens = {request["lenses"][lens]: lens for lens in lenses}
        results = [receipt for receipt in run.get("harvested_reviews", []) if receipt.get("token") in tokens and not receipt.get("imported_by")]
        refused = [entry for entry in run.get("refused_review_events", {}).values() if entry.get("token") in tokens]
        for lens in lenses:
            require(any(item["lens"] == lens for item in results + refused), "journal_result_missing",
                    f"No workflow journal result carries the current review token for lens {lens}; request review of the current content.")
        empty = [lens for lens in lenses if not any(item["lens"] == lens for item in results)]
        require(not empty, "review_lenses_incomplete",
                f"No usable verdict remains for lens {', '.join(empty)}; refused: " + "; ".join(f"{item['lens']} from {item['actor']}: {item['reason']}" for item in refused)
                + ". Rerun that review with the same token, then import again.")
        verdict = "FAIL" if any(item["verdict"] == "FAIL" for item in results) else "PASS"
        finding_keys = sorted(set(_finding_keys([d for item in results if item["verdict"] == "FAIL" for d in item["defects"]])) | set(finding_keys))
        shown = [{key: item[key] for key in ("lens", "actor", "verdict", "evidence", "defects", "journal", "unverified", "normalized") if key in item} for item in results]
        evidence = json.dumps({"lenses": shown, "refused": refused}, indent=2, sort_keys=True, ensure_ascii=False)
        review_evidence(verdict, evidence)
        actors = list(dict.fromkeys(item["actor"] for item in results))
        writers, writer_agents = _writer_agents(run)
        agents = [agent for lens in lenses for agent in sorted({item["agent_id"] for item in results if item["lens"] == lens})]
        require(len(set(agents)) == len(agents), "reviewer_not_independent", "Each review lens needs its own reviewer agents; one agent returned verdicts for two lenses.")
        require(not writers.intersection(actors) and not writer_agents.intersection(agents), "reviewer_not_independent", "A reviewer must not be a current or previous implementation actor.")
        _record_review(run, root, "+".join(actors), verdict, evidence, task_id=task_id, decision=decision, finding_keys=finding_keys,
                       extra={"reviewers": actors, "lenses": list(lenses), "source": "workflow-journal", "refused": refused, "harvested": [item["id"] for item in results],
                              "verdicts": [{"lens": item["lens"], "actor": item["actor"], "verdict": item["verdict"], "unverified": item["unverified"],
                                            **({"normalized": item["normalized"]} if "normalized" in item else {})} for item in results]})
        for item in results:
            item["imported_by"] = run["revision"] + 1
        run["review_requests"][_review_target(task_id)] = {**request, "status": "imported", "imported_at": now()}
    return load(root)


def _verify_task_current(run: dict, task_id: str, seen: set[str], late: bool = True) -> None:
    if task_id in seen:
        return
    owner = run["tasks"].get(task_id)
    require(owner and owner["status"] == "VERIFIED", "dependency_not_ready", f"Task {task_id} is not verified.")
    for dep in task_plan(run, task_id)["depends_on"]:
        _verify_task_current(run, dep, seen, late)
    require(owner.get("dependencies", {}) == _dependency_bindings(run, task_id), "stale_dependency", f"Task {task_id} was verified against an older dependency attempt or patch.")
    owned_worktree_record(run, owner, task_id=task_id)
    snap = snapshot(run, task_id)
    require(owner.get("report") and owner["report"].get("dispatch_id") == owner.get("agent", {}).get("dispatch_id") and evidence_current(owner["report"], snap), "stale_report", f"Task {task_id} changed after verification or lacks its current dispatch report.")
    reviews = [r for r in run["reviews"] if r["task"] == task_id]
    require(reviews and reviews[-1]["verdict"] == "PASS" and evidence_current(reviews[-1], snap) and gates_current(run, task_id), "stale_task", f"Task {task_id} changed after verification.")
    if late and not run.get("integration"):
        failures = _unoverridden_failures(run, task_id, snap)
        require(not failures, "late_review_fail", f"Task {task_id} has a FAIL review on its verified content from {failures[0]['actor'] if failures else ''}; rework it, "
                f"or record authorize --scope decision --code review_override --task {task_id} after that FAIL.")
    patch = owner["patch"]
    require(Path(patch["path"]).is_file() and hashlib.sha256(Path(patch["path"]).read_bytes()).hexdigest() == patch["sha256"], "patch_drift", "Verified patch changed after review.")
    seen.add(task_id)


def verify_task_current(run: dict, task_id: str, *, late: bool = True) -> None:
    _verify_task_current(run, task_id, set(), late)


def integrate(root, expected_revision=None) -> dict:
    with transaction(root, "tasks_integrated", expected_revision) as run:
        assert_gates_idle(run)
        assert_plan(run)
        assert_spec(run)
        require(run["state"] == "IMPLEMENTING" and not run["integration"], "integration_state", "Integrate the verified wave once; resume its worktree on retry.")
        for task in run["plan"]["tasks"]:
            verify_task_current(run, task["id"])
        ordered = _task_order(run)
        patches = [run["tasks"][tid]["patch"] for tid in ordered]
        inputs = {"base_sha": run["base_sha"], "patches": [{key: patch[key] for key in ("path", "sha256", "allowed_paths")} for patch in patches]}
        receipt = _owned_preparation(run, "integration", inputs)
        if receipt["phase"] == "CREATED":
            git_ops.integrate_patches(receipt["path"], patches, run["base_sha"])
            receipt = _save_worktree_checkpoint(receipt, "INTEGRATED")
        require(receipt["phase"] == "INTEGRATED", "worktree_checkpoint_phase", "The integration worktree has an unexpected preparation phase.")
        result = git_ops.inventory(receipt["path"], run["base_sha"])
        scopes = [p for task in run["plan"]["tasks"] for p in task["paths"]]
        require(all(covered(p, scopes) for p in result["files"]), "unexpected_surface", "Integrated diff includes unplanned files.")
        run["integration"] = {"path": receipt["path"], "branch": receipt["branch"], "base_sha": run["base_sha"],
                              "attempt": receipt["attempt"], "ownership_receipt": receipt["ownership_receipt"],
                              "previous_attempts": _previous_worktree_attempts(run, "integration", receipt["attempt"])}
        run["state"] = "VERIFYING"
    return load(root)


def ready(root, expected_revision=None) -> dict:
    with transaction(root, "release_readiness_verified", expected_revision) as run:
        assert_gates_idle(run)
        assert_plan(run)
        assert_spec(run)
        require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "readiness_state", "Complete integrated verification first.")
        if run["plan"]["classification"]["level"] != "small":
            require(run.get("rich_snapshot") and evidence_current(run["rich_snapshot"], snapshot(run)), "stale_spec_verification", "Capture the current integration tree and repeat actual-diff verification after changes or rebase.")
            require(spec_command(root, ["status"])["state"] == "DONE", "spec_verification_incomplete", "Complete rich actual-diff verification before publication.")
        for task in run["plan"]["tasks"]:
            verify_task_current(run, task["id"])
        require(gates_current(run), "verification_incomplete", "Every integrated case must pass on the current diff.")
        snap = snapshot(run)
        scopes = [p for task in run["plan"]["tasks"] for p in task["paths"]]
        require(snap["files"] and all(covered(p, scopes) for p in snap["files"]), "unexpected_surface", "An integrated review cannot widen the approved implementation scope.")
        assert_rich_checks(run)
        reviews = [r for r in run["reviews"] if r["task"] is None]
        require(reviews and reviews[-1]["verdict"] == "PASS" and evidence_current(reviews[-1], snap), "review_required", "An independent review of the integrated diff is required.")
        assert_no_late_failures(run)
        run["validated"] = {**snap, "plan_hash": run["plan_hash"], "at": now()}
        run["state"] = "PR_OPEN" if run.get("pr") else "READY_TO_PUBLISH"
    return load(root)


def assert_gitlinks_unchanged(changes: list) -> None:
    """Snapshots carry gitlinks without content, so a moved submodule is refused, not reviewed blind."""
    require(not changes, "submodule_changed",
            "The integration changes submodule commits, which the actual-diff review cannot inspect: "
            + ", ".join(f"{c['path']} {c['before'] or 'absent'} -> {c['after'] or 'absent'}" for c in changes)
            + ". Restore the base gitlinks or verify the submodule change in a separately scoped run.")


def capture_verification(root, reason: str, expected_revision=None) -> dict:
    """Bind rich actual-diff review to exact current base and staged Git trees."""
    import snapshots
    with transaction(root, "verification_snapshots_captured", expected_revision) as run:
        assert_gates_idle(run)
        assert_plan(run)
        require(run["state"] == "VERIFYING" and run.get("integration"), "verification_state", "Capture the integrated worktree during verification.")
        require(run["plan"]["classification"]["level"] != "small", "small_verification", "Small work uses direct content-bound verification without the rich engine.")
        text(reason, "Verification capture reason")
        owner = run["integration"]
        assert_gitlinks_unchanged(snapshots.staged_gitlink_changes(owner["path"], owner["base_sha"]))
        inventory = git_ops.inventory(owner["path"], owner["base_sha"])
        require(not inventory["unstaged"] and not inventory["untracked"] and not inventory["in_progress"], "unstaged_snapshot", "Stage only the exact intended integration files before snapshotting; finish existing Git operations.")
        snapshot_home = Path(root) / "spec-snapshots"
        snapshot_home.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"r{run['revision']}-", dir=snapshot_home))
        before = snapshots.capture_tree(owner["path"], owner["base_sha"], str(folder / "before"))
        after = snapshots.capture_tree(owner["path"], inventory["tree"], str(folder / "after"))
        assert_gitlinks_unchanged(snapshots.gitlink_changes(before, after))
        snap = snapshot(run)
        require(snap["content"] == inventory["content_fingerprint"], "worktree_changed", "The integration tree changed while it was captured.")
        state = spec_command(root, ["status"])["state"]
        command = "begin-verify" if state == "IMPLEMENTING" else "reopen-verify"
        arguments = [command, "--before", before["path"], "--after", after["path"]]
        if command == "reopen-verify":
            arguments += ["--reason", reason]
        spec_command(root, arguments)
        run.setdefault("previous_checks", []).append(run["check_root"])
        run["check_root"] = str(Path(root) / "checks" / folder.name)
        mode = "deep" if run["plan"]["classification"]["level"] == "large" else "standard"
        spec_command(root, ["check-init", "--title", run["plan"]["goal"][:256], "--mode", mode], check=True, _run=run)
        spec_command(root, ["check-diff", "--before", before["path"], "--after", after["path"]], check=True, _run=run)
        check_hash = hashlib.sha256((Path(run["check_root"]) / "actual-diff.json").read_bytes()).hexdigest()
        run["rich_snapshot"] = {"snapshot": snap, "before": before, "after": after, "check_root": run["check_root"], "check_diff_hash": check_hash, "at": now(), "reason": reason}
        run["spec_refresh_required"] = False
    return load(root)


def invalidate_integration(run: dict, reason: str) -> None:
    old_gates = [g for g in run["gates"] if g["task"] is None]
    old_reviews = [r for r in run["reviews"] if r["task"] is None]
    run.setdefault("verification_history", []).append({"reason": reason, "at": now(), "validated": run.get("validated"), "gates": old_gates, "reviews": old_reviews, "rich_snapshot": run.get("rich_snapshot")})
    run["gates"] = [g for g in run["gates"] if g["task"] is not None]
    run["reviews"] = [r for r in run["reviews"] if r["task"] is not None]
    run["validated"] = None
    run["spec_refresh_required"] = run["plan"]["classification"]["level"] != "small"


def fix_budget(run: dict) -> dict:
    """Integration-fix rounds used by the current integration and the explicit limit.

    The base limit is two rounds. Each fix-budget grant bound to the current plan
    hash and specification version adds its count; grants recorded for another
    plan, or for an identical plan approved again after a revision, do not count.
    A rework-budget grant for an integrated non_converging block adds its count as well.
    """
    used = (run.get("integration_fix") or {}).get("attempt", 0)
    current = run.get("plan_hash")
    extra = sum(grant["count"] for grant in run.get("fix_budget_grants", [])
                if current and grant.get("plan_hash") == current and grant.get("spec_version") == run.get("spec_version"))
    return {"used": used, "limit": extra + rework_limits(run, None)["integration"]}


def register_fix(root, actor: str, host: str, handle: str, reason: str, expected_revision=None) -> dict:
    """Record a real same-scope integration/PR correction dispatch."""
    with transaction(root, "integration_fix_dispatched", expected_revision) as run:
        assert_gates_idle(run)
        assert_plan(run)
        need_authority(run, "implement")
        require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "fix_state", "Fix only an existing unmerged integration within the approved scope.")
        require((run.get("local_finish") or {}).get("phase", "prepared") == "prepared", "local_finish_started", "finish-local already started changing main or retiring worktrees; retry finish-local instead of reopening the integration.")
        require(run.get("integration"), "missing_worktree", "No integration branch exists.")
        owned_worktree_record(run, run["integration"])
        previous = run.get("integration_fix")
        require(not previous or previous["status"] == "REPORTED", "fix_dispatch_active", "Query and collect the current integration dispatch before starting another.")
        fields = {"actor": _typed_actor(actor, "Implementation actor"), "host": text(host, "Host"), "handle": text(handle, "Actual spawn handle"), "registered_at": now()}
        require(not any(t.get("status") == "DISPATCHED" for t in run["tasks"].values()), "active_task", "Collect active task workers before editing their integrated result.")
        attempt = 1 if not previous else previous["attempt"] + 1
        limit = fix_budget(run)["limit"]
        require(attempt <= limit, "rework_budget", f"The integration-fix budget of {limit} rounds is exhausted; reassess the plan or record an explicit extension with authorize --scope fix-budget --count N.")
        dispatch_id = digest({**fields, "revision": run["revision"], "run": run["id"]})
        if previous:
            run.setdefault("integration_fix_history", []).append(previous)
        run["integration_fix"] = {**fields, "attempt": attempt, "dispatch_id": dispatch_id, "status": "DISPATCHED", "reason": text(reason, "Concrete review finding"), "before": snapshot(run), "report": None}
        run.setdefault("writer_history", []).append({**fields, "dispatch_id": dispatch_id, "task": None, "attempt": attempt})
        invalidate_integration(run, reason)
        run["state"] = "VERIFYING"
    return load(root)


def report_fix(root, actor: str, result: dict, expected_revision=None) -> dict:
    with transaction(root, "integration_fix_reported", expected_revision) as run:
        assert_plan(run)
        require(run["state"] == "VERIFYING", "fix_state", "An integration fix reports during verification.")
        owner = run.get("integration_fix") or {}
        require(owner.get("status") == "DISPATCHED" and owner.get("actor") == actor and result.get("dispatch_id") == owner.get("dispatch_id"), "dispatch_mismatch", "The actual integration-fix result must match this dispatch.")
        validate_report(result)
        check_source_event(result["source_event"], owner)
        snap = snapshot(run)
        scopes = [p for task in run["plan"]["tasks"] for p in task["paths"]]
        require(snap["files"] and all(covered(p, scopes) for p in snap["files"]), "scope_violation", "A same-scope PR correction cannot expand the approved surface.")
        require(snap["head"] == owner["before"]["head"], "fix_commit_forbidden", "Return a staged integration correction without committing or rewriting its branch.")
        owner["report"] = {**result, "snapshot": snap, "at": now()}
        owner["status"] = "REPORTED"
    return load(root)


def block(root, reason: str, expected_revision=None) -> dict:
    with transaction(root, "blocked", expected_revision) as run:
        require(run["state"] != "BLOCKED", "already_blocked", "The blocker is already recorded.")
        run["blocker"] = {"reason": text(reason, "Blocker"), "from": run["state"], "at": now()}
        run["state"] = "BLOCKED"
    return load(root)


def _budget_grant(run: dict, blocker: dict) -> dict | None:
    return next((grant for grant in run.get("rework_budget_grants", []) if grant.get("task") == blocker.get("task")
                 and grant.get("blocker_at") == blocker.get("at") and grant.get("blocker_code") == blocker.get("code")
                 and grant.get("plan_hash") == run.get("plan_hash") and grant.get("spec_version") == run.get("spec_version")), None)


def _decision_grant(run: dict, blocker: dict) -> dict | None:
    return next((grant for grant in run.get("decision_grants", []) if grant.get("task") == blocker.get("task")
                 and grant.get("blocker_at") == blocker.get("at") and grant.get("code") == blocker.get("code")
                 and grant.get("plan_hash") == run.get("plan_hash") and grant.get("spec_version") == run.get("spec_version")), None)


def resume(root, resolution: str, expected_revision=None) -> dict:
    with transaction(root, "resumed", expected_revision) as run:
        require(run["state"] == "BLOCKED" and run["blocker"], "not_blocked", "No blocker is recorded.")
        blocker = run["blocker"]
        require(blocker.get("code") not in DECISION_BLOCKERS or _decision_grant(run, blocker), "decision_required",
                "A requirements or human-decision block resumes only after authorize --scope decision --code <blocker code> records the user's decision for this block; otherwise revise.")
        require(blocker.get("code") not in BUDGET_BLOCKERS or _budget_grant(run, blocker), "rework_budget_required",
                "An exhausted or non-converging rework budget resumes only after authorize --scope rework-budget recorded for this block; otherwise revise.")
        text(resolution, "Observed blocker resolution")
        if blocker.get("previous"):
            run["blocker"] = blocker["previous"]
        else:
            run["state"] = blocker["from"]
            run["blocker"] = None
    return load(root)


def revise(root, reason: str, expected_revision=None) -> dict:
    with transaction(root, "semantic_revision_started", expected_revision) as run:
        assert_gates_idle(run)
        open_workflow = sorted(task_id for task_id, owner in run["tasks"].items() if owner.get("status") == "DISPATCHED" and (owner.get("agent") or {}).get("via_workflow"))
        require(not any(owner.get("status") == "DISPATCHED" and not (owner.get("agent") or {}).get("via_workflow") for owner in run["tasks"].values())
                and (run.get("integration_fix") or {}).get("status") != "DISPATCHED",
                "active_task", "Collect or abandon active manual implementation dispatches before replacing their semantic plan.")
        text(reason, "Material revision reason")
        require(not run.get("pr"), "open_pr_revision", "Keep the existing PR and reconcile its reviewed scope explicitly before replacing this run plan.")
        if (Path(run["spec_root"]) / "spec-session.json").exists():
            spec_command(root, ["revise", "--reason", reason])
        run.setdefault("previous_versions", []).append({"version": run["spec_version"], "plan": run["plan"], "tasks": run["tasks"], "integration": run["integration"],
                                                        "integration_fix": run.get("integration_fix"), "integration_fix_history": run.get("integration_fix_history", []),
                                                        "gates": run["gates"], "reviews": run["reviews"], "reason": reason, "open_workflow_dispatches": open_workflow})
        run.update(state="DISCOVERY", plan=None, plan_hash=None, approval=None, authorizations={}, tasks={}, integration=None,
                   integration_fix=None, integration_fix_history=[], gates=[], reviews=[], blocker=None)
    return load(root)


def _block_task(run: dict, code: str, reason: str, task_id: str | None = None) -> None:
    """Block with a code; a block raised while already blocked stacks on the earlier one, which resume restores."""
    blocker = {"reason": reason, "code": code, "task": task_id, "from": run["state"], "at": now()}
    if run["state"] == "BLOCKED" and run.get("blocker"):
        blocker.update({"from": run["blocker"]["from"], "previous": run["blocker"]})
    run["blocker"] = blocker
    run["state"] = "BLOCKED"


def _rounds_word(limit: int) -> str:
    return {2: "Two", 3: "Three", 4: "Four"}.get(limit, str(limit))


def _count_support_round(run: dict, task_id: str, owner: dict) -> None:
    owner["support_rounds"] = owner.get("support_rounds", 0) + 1
    limit = rework_limits(run, task_id)["support"]
    if owner["support_rounds"] > limit:
        _block_task(run, "support_budget", f"{_rounds_word(limit)} test-plan or environment rounds are exhausted; fix the plan or the environment explicitly.", task_id)


def _mark_rework(run: dict, task_id: str, reason: str, decision: str = "code-fix", finding_keys=()) -> None:
    decision, finding_keys = triage(decision, list(finding_keys))
    owner = run["tasks"].get(task_id)
    require(owner and owner["status"] in {"REPORTED", "VERIFIED"}, "task_state", "Only returned work can enter rework.")
    reason = text(reason, "Concrete failure or review finding")
    require(not run.get("integration"), "integrated_rework", "Reconcile the integrated branch rather than silently replacing a saved patch.")
    descendants = [key for key in run["tasks"] if task_id in _dependency_ids(run, key)]
    active = [key for key in descendants if run["tasks"][key]["status"] == "DISPATCHED"]
    require(not active, "active_descendant", "Collect the actual return of active dependent tasks before reworking their dependency: " + ", ".join(active))
    active_gates = [job["id"] for job in run.get("gate_jobs", {}).values() if job["status"] == "RUNNING" and job["task"] in descendants]
    require(not active_gates, "active_descendant", "Collect or explicitly recover the running dependent gate jobs before reworking their dependency: " + ", ".join(active_gates))
    _exclusive_dispatch_available(run, task_id)
    for key in descendants:
        run["tasks"][key]["status"] = "INVALIDATED"
        run["tasks"][key]["invalidated_by"] = {"task": task_id, "reason": reason, "at": now()}
    history = owner.setdefault("rework_history", [])
    previous = next((entry for entry in reversed(history) if entry.get("decision", "code-fix") == "code-fix"), None)
    history.append({"reason": reason, "report": owner.get("report"), "patch": owner.get("patch"), "at": now(), "decision": decision, "finding_keys": finding_keys})
    owner.update(status="REWORK", patch=None, report=None)
    if decision == "code-fix":
        owner["rework_rounds"] = owner.get("rework_rounds", 0) + 1
        limit = rework_limits(run, task_id)["code-fix"]
        repeated = sorted(set(finding_keys) & set((previous or {}).get("finding_keys") or []))
        if owner["rework_rounds"] > limit:
            _block_task(run, "rework_budget", f"{_rounds_word(limit)} rework rounds exhausted; revise, or record authorize --scope rework-budget --task {task_id} --count N and resume.", task_id)
        elif repeated:
            _block_task(run, "non_converging", f"The same finding survived a rework round ({', '.join(repeated)}); revise the plan or escalate explicitly.", task_id)
    elif decision in {"test-plan", "environment"}:
        _count_support_round(run, task_id, owner)
    elif decision == "requirements":
        _block_task(run, "requirements_finding", f"A requirements finding needs revise and renewed approval, not implementer rework: {reason}", task_id)
    else:
        _block_task(run, "human_decision", f"A human decision is required: {reason}", task_id)


def rework(root, task_id: str, reason: str, expected_revision=None, *, decision="code-fix", finding_keys=None) -> dict:
    decision, finding_keys = triage(decision, finding_keys)
    with transaction(root, "task_rework_requested", expected_revision) as run:
        _implementation_state(run)
        _mark_rework(run, task_id, reason, decision, finding_keys)
    return load(root)


def abandon_task(root, task_id: str, reason: str, expected_revision=None) -> dict:
    """End a dispatch that will not return a usable result, preserving its worktree.

    For a workflow dispatch the run's recorded host journals are read first: failed agents under
    the task's implement label are recorded (and kept as possible writers), and so is whether any
    result names the dispatch. The round counts toward the task's test-plan/environment budget.
    Normally the task returns to REWORK with its current content as the new rework baseline. If
    the worker committed or switched branches, the observed HEAD and branch are recorded without
    reading its content; if the worktree no longer exists, its absence is recorded and the ownership
    receipt and branch are kept. In both cases the task is INVALIDATED so task-prepare creates a new
    attempt.
    """
    import workflow_journal
    reason = text(reason, "Reason for abandoning the dispatch")
    run = load(root)
    owner = run["tasks"].get(task_id)
    agent = (owner or {}).get("agent") or {}
    require(owner and owner["status"] == "DISPATCHED", "task_state", "Only a dispatched task can be abandoned.")
    observed = None
    if agent.get("via_workflow"):
        pinned = run.get("workflow_host_root") or str(workflow_journal.host_root())
        since = parse_instant(agent["registered_at"], "Dispatch registration time").timestamp() - 1
        observed = workflow_journal.observe_dispatch(workflow_journal.implementer_labels(task_id), agent["dispatch_id"], since=since, host=pinned)
        observed["effective_host_root"] = str(workflow_journal.host_root())
    with transaction(root, "task_dispatch_abandoned", expected_revision) as run:
        _collection_state(run)
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] == "DISPATCHED" and owner["agent"]["dispatch_id"] == agent["dispatch_id"], "task_state", "The dispatch changed while the journals were read; inspect the task again.")
        receipt = _load_worktree_receipt(run, "task-" + task_id, owner.get("attempt", 0))
        require(all(owner.get(key) == receipt[key] for key in ("path", "branch", "ownership_receipt")), "worktree_identity", "The current worktree owner differs from its durable creation record.")
        record = {"reason": reason, "at": now(), "journal": observed}
        if not Path(owner["path"]).exists() and not Path(owner["path"]).is_symlink():
            record["missing_worktree"] = {"path": owner["path"], "branch": owner["branch"], "ownership_receipt": owner["ownership_receipt"],
                                          "branch_head": git_ops._ref_sha(Path(run["primary"]), "refs/heads/" + owner["branch"])}
            moved = False
        else:
            identity = git_ops.inspect_repo(owner["path"])
            require(identity["primary"] == run["primary"] and identity["worktree"] == owner["path"], "worktree_identity", "The recorded worktree repository or exact path changed.")
            moved = identity["branch"] != owner["branch"] or identity["head"] != owner["base_sha"]
        if moved:
            record["moved"] = {"head": identity["head"], "branch": identity["branch"], "expected_head": owner["base_sha"], "expected_branch": owner["branch"]}
        owner["agent"].update(liveness="abandoned", abandoned=copy.deepcopy(record))
        if owner.get("dispatches"):
            owner["dispatches"][-1].update(liveness="abandoned", abandoned=copy.deepcopy(record))
        for failed in (observed or {}).get("failed", []):
            if failed.get("agent_id") and workflow_journal.AGENT_ID.fullmatch(str(failed["agent_id"])):
                run.setdefault("writer_history", []).append({"actor": f"workflow-agent:{failed['run_id']}/{failed['agent_id']}", "task": task_id, "attempt": owner["attempt"],
                                                             "dispatch_id": agent["dispatch_id"], "abandoned": True, "journal": failed})
        entry = {"reason": reason, "report": None, "patch": None, "at": record["at"], "decision": "abandon",
                 "finding_keys": [], "abandoned_dispatch": agent["dispatch_id"], "journal": observed}
        if moved or "missing_worktree" in record:
            # A moved worktree keeps the worker's commits; a missing one keeps its receipt and branch. The next attempt starts fresh.
            detail = {key: record[key] for key in ("moved", "missing_worktree") if key in record}
            entry.update(detail)
            owner.setdefault("rework_history", []).append(entry)
            owner["status"] = "INVALIDATED"
            owner["invalidated_by"] = {"task": task_id, "reason": reason, "at": record["at"], "abandoned": True, **detail}
        else:
            entry["baseline"] = snapshot(run, task_id)
            owner.setdefault("rework_history", []).append(entry)
            owner["status"] = "REWORK"
        _count_support_round(run, task_id, owner)
    return load(root)


def status(root) -> dict:
    run = load(root)
    result = {k: run[k] for k in ("id", "root", "primary", "revision", "state", "spec_version", "plan_hash", "blocker", "pr")}
    result["classification"] = run["plan"]["classification"] if run.get("plan") else None
    result["tasks"] = {tid: {"status": t["status"], "path": t["path"], "agent": t.get("agent"), "via_workflow": bool((t.get("agent") or {}).get("via_workflow")),
                             "rework_rounds": t.get("rework_rounds", 0), "support_rounds": t.get("support_rounds", 0),
                             "limits": {kind: limit for kind, limit in rework_limits(run, tid).items() if kind != "integration"}} for tid, t in run["tasks"].items()}
    result["blocker_code"] = (run.get("blocker") or {}).get("code")
    grant = run.get("standing_approval")
    result["standing_approval"] = {key: grant[key] for key in ("actor", "until", "max_level", "triggers")} if grant else None
    result["approvals"] = [{"version": item.get("version"), "actor": item.get("actor"), "kind": item.get("kind", "individual")} for item in run.get("approval_history", [])]
    result["approval"] = {"version": run["approval"].get("version"), "actor": run["approval"].get("actor"), "kind": run["approval"].get("kind", "individual")} if run.get("approval") else None
    owner = run.get("integration")
    result["integration"] = {key: owner.get(key) for key in ("path", "branch", "base_sha", "status")} if owner else None
    result["spec_root"] = run["spec_root"]
    result["check_root"] = run["check_root"]
    result["authority_scopes"] = {scope: {"actor": grant["actor"], "targets": list(grant.get("targets", {})), "pr": grant.get("pr")} for scope, grant in run["authorizations"].items()}
    result["retained_versions"] = len(run.get("previous_versions", []))
    result["gate_jobs"] = {key: {"status": job["status"], "task": job.get("task"), "case": job.get("case"), "resources": job.get("resources", [])} for key, job in run.get("gate_jobs", {}).items()}
    result["delivery_mode"] = run.get("delivery_mode", "github")
    result["workflow_host_root"] = run.get("workflow_host_root")
    import workflow_journal
    result["workflow_host_root_default"] = (run.get("workflow_host_root") == str(Path(workflow_journal.DEFAULT_SEARCH_ROOT).expanduser().resolve())
                                            if run.get("workflow_host_root") else None)
    result["local_outcome"] = run.get("local_outcome")
    result["fix_budget"] = fix_budget(run)
    result["next_action"] = {
        "DISCOVERY": "Load explicit product/repository context, classify risk and write the plan.",
        "READY_FOR_APPROVAL": "Present material decisions and obtain semantic approval; Small work may use scoped request authority.",
        "APPROVED": "Record implementation authority, then start; approval alone does not execute code.",
        "IMPLEMENTING": "Dispatch ready tasks; re-query registered host handles before claiming work is running.",
        "VERIFYING": "Run all integrated cases and collect an independent actual-diff review.",
        "READY_TO_PUBLISH": "Obtain publication authority and publish the tested integration branch.",
        "PR_OPEN": "Resolve review findings, obtain merge authority, refresh main and rerun invalidated evidence.",
        "MERGED": "Confirm primary main contains the merge; preserve artifacts and clean only owned worktrees.",
        "COMPLETE": "Use the durable product note and linked evidence to resume future work.",
        "BLOCKED": "Resolve the recorded blocker without bypassing it.",
    }.get(run["state"], "Inspect the run history.")
    if result["delivery_mode"] == "local" and run["state"] == "READY_TO_PUBLISH":
        result["next_action"] = "Local-only run: commit, then finish-local (add --fast-forward-main with local-merge authority to update main)."
    try:
        if run.get("plan"):
            assert_plan(run)
        result["plan_current"] = True
    except RunError:
        result["plan_current"] = False
        result["next_action"] = "The plan artifact drifted. Reconcile it through revision before any action."
    return result
