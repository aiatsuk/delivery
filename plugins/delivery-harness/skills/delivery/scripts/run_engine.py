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
        yield value
        value["revision"] += 1
        value["updated_at"] = now()
        value["history"].append({"revision": value["revision"], "at": value["updated_at"], "event": event, "state": value["state"]})
        atomic_json(root / "run.json", value)


def create(repo: str, run_id: str, request: str, *, store: str | None = None, product_root: str | None = None, work_item: str | None = None) -> dict:
    run_id, request = identifier(run_id), text(request, "Original user request")
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
             "spec_root": str(root / "spec"), "check_root": str(root / "check")}
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
        text(req.get("oracle"), "Requirement oracle")
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
            text(gate.get("oracle"), "Task gate oracle")
            text(gate.get("cleanup"), "Task gate cleanup")
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
        text(case.get("oracle"), "Observable verification oracle")
        text(case.get("cleanup"), "Verification cleanup")
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


def authorize(root, scope: str, actor: str, evidence: str, expected_revision=None, *, targets=None) -> dict:
    require(scope in {"implement", "publish", "merge", "test-external", "test-destructive"}, "invalid_scope", "Unknown authorization scope; deploy is deliberately separate.")
    with transaction(root, "authority_recorded", expected_revision) as run:
        assert_plan(run)
        if scope == "merge":
            require(run.get("pr"), "missing_pr", "Merge authority must name an existing run PR.")
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
        run["authorizations"][scope] = grant
    return load(root)


def need_authority(run: dict, scope: str, *, target: str | None = None) -> None:
    grant = run["authorizations"].get(scope)
    require(grant and grant.get("plan_hash") == run["plan_hash"], "authorization_required", f"Obtain explicit, current {scope} authority first.")
    if scope.startswith("test-"):
        available = _test_targets(run, scope)
        require(isinstance(target, str) and target in available and grant.get("targets", {}).get(target) == available[target], "authorization_required", f"Obtain explicit, current {scope} authority for gate {target or '<unspecified>'} first.")


def approve(root, actor: str, evidence: str, expected_revision=None) -> dict:
    with transaction(root, "semantic_baseline_approved", expected_revision) as run:
        require(run["state"] == "READY_FOR_APPROVAL", "approval_state", "Review the current plan before approving it.")
        assert_plan(run)
        if run["plan"]["classification"]["level"] != "small":
            scope = sorted({p for task in run["plan"]["tasks"] for p in task["paths"]})
            spec_command(root, ["approve", "--actor", actor, "--note", evidence, *[arg for path in scope for arg in ("--planned", path)]])
        run["approval"] = {**attestation(actor, evidence), "plan_hash": run["plan_hash"], "version": run["spec_version"]}
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


def _require_current_dependencies(run: dict, task_id: str) -> None:
    for dep in task_plan(run, task_id)["depends_on"]:
        verify_task_current(run, dep)
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


def register_agent(root, task_id: str, actor: str, host: str, handle: str, expected_revision=None) -> dict:
    with transaction(root, "agent_registered", expected_revision) as run:
        _implementation_state(run)
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] in {"PREPARED", "REWORK"}, "task_state", "Task is not ready for dispatch.")
        owned_worktree_record(run, owner, task_id=task_id)
        snapshot(run, task_id)
        _require_current_dependencies(run, task_id)
        _exclusive_dispatch_available(run, task_id)
        actor, host, handle = text(actor, "Agent identity"), text(host, "Host"), text(handle, "Successful spawn handle")
        require(not any(other_id != task_id and other["status"] == "DISPATCHED" and other.get("agent", {}).get("host") == host and other.get("agent", {}).get("handle") == handle for other_id, other in run["tasks"].items()), "handle_in_use", "A live host handle cannot be assigned to two task dispatches.")
        dispatch_id = digest({"run": run["root"], "plan": run["plan_hash"], "task": task_id,
                              "attempt": owner["attempt"], "sequence": len(owner.get("dispatches", [])) + 1,
                              "revision": run["revision"] + 1, "actor": actor, "host": host, "handle": handle})
        agent = {"actor": actor, "host": host, "handle": handle, "dispatch_id": dispatch_id,
                 "registered_at": now(), "liveness": "must-query-host"}
        if owner.get("agent") and not owner.get("dispatches"):
            owner.setdefault("dispatches", []).append(copy.deepcopy(owner["agent"]))
        owner.setdefault("dispatches", []).append(agent)
        run.setdefault("writer_history", []).append({**agent, "task": task_id, "attempt": owner["attempt"]})
        owner["agent"] = agent
        owner["status"] = "DISPATCHED"
    return load(root)


def validate_report(result: dict) -> None:
    require(isinstance(result, dict), "invalid_report", "A report is a JSON object.")
    require(len(json.dumps(result, ensure_ascii=False)) <= 100_000, "report_limit", "Keep the report bounded; link detailed external evidence.")
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


def report_task(root, task_id: str, actor: str, result: dict, expected_revision=None) -> dict:
    with transaction(root, "task_report_received", expected_revision) as run:
        _implementation_state(run)
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] == "DISPATCHED" and owner["agent"]["actor"] == actor, "agent_mismatch", "Only the registered task actor can supply its report.")
        require(isinstance(result, dict), "invalid_report", "A task report must be a structured result.")
        require(result.get("dispatch_id") == owner["agent"]["dispatch_id"], "dispatch_mismatch", "The report must name the current registered dispatch_id; a prior handle's result cannot complete replacement work.")
        validate_report(result)
        _require_current_dependencies(run, task_id)
        owned_worktree_record(run, owner, task_id=task_id)
        event = digest({"host": owner["agent"]["host"], "handle": owner["agent"]["handle"], "source_event": result["source_event"]})
        require(event not in run.get("accepted_source_events", {}), "source_event_reused", "This host result event was already accepted; collect the current dispatch's actual return.")
        snap = snapshot(run, task_id)
        paths = task_plan(run, task_id)["paths"]
        require(snap["files"] and all(covered(p, paths) for p in snap["files"]), "scope_violation", "Task changes must be nonempty and confined to approved paths.")
        report = {**result, "actor": actor, "host": owner["agent"]["host"], "handle": owner["agent"]["handle"], "snapshot": snap, "at": now()}
        owner.update(status="REPORTED", report=report)
        owner.setdefault("reports", []).append(report)
        run.setdefault("accepted_source_events", {})[event] = {"task": task_id, "dispatch_id": owner["agent"]["dispatch_id"], "source_event": result["source_event"]}
    return load(root)


@gate_jobs.guarded
def execute_gate(root, *, task_id: str | None = None, case_id: str | None = None, gate_index: int = 0, timeout: int = 300, expected_revision=None) -> dict:
    require(1 <= timeout <= 3600, "invalid_timeout", "Gate timeout must be between 1 and 3600 seconds.")
    run = load(root)
    require(expected_revision is None or run["revision"] == expected_revision, "stale_revision", "Reload the run before executing a gate.")
    assert_plan(run)
    require(run["state"] in {"IMPLEMENTING", "VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "gate_state", "Gates are not executable in this state.")
    if task_id:
        task = task_plan(run, task_id)
        require(0 <= gate_index < len(task["gates"]), "unknown_gate", "Unknown planned task gate.")
        gate = task["gates"][gate_index]
        argv, key, risk = gate["command"], f"{task_id}:{gate_index}", gate["risk"]
        owner = run["tasks"].get(task_id)
        require(owner and owner["status"] in {"REPORTED", "VERIFIED"}, "task_state", "Collect the real task report first.")
    else:
        case = next((c for c in run["plan"]["verification"] if c["id"] == case_id), None)
        require(case, "unknown_case", "Select a planned integrated verification case.")
        argv, key, risk = case["command"], case["id"], case["risk"]
        owner = run.get("integration")
        fix = run.get("integration_fix")
        require(not fix or fix.get("status") == "REPORTED", "integration_report_required", "Wait for the current integration-fix dispatch's real report before verification.")
    require(owner, "missing_worktree", "No owned worktree for the gate.")
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
                process = subprocess.Popen(argv, cwd=owner["path"], stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    gate_jobs.set_process(process.pid)
                    remaining = max(0.01, timeout - (time.monotonic() - start))
                    code = process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
                    code = process.returncode
                except BaseException:
                    # This process group belongs to this invocation. A failed
                    # identity checkpoint must not leave its child running.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
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
               "planned_repeat": repeat, "attempts": attempts, "outcome": outcome,
               "duration_seconds": round(time.monotonic() - start, 3), "at": now(), "snapshot": before,
               "unchanged": before["content"] == after["content"], "log": name, "log_hash": hashlib.sha256(Path(name).read_bytes()).hexdigest()}
    receipt["passed"] = code == 0 and not timed_out and receipt["unchanged"] and len(attempts) == repeat
    with transaction(root, "gate_executed") as current:
        require(current["plan_hash"] == run["plan_hash"], "plan_drift", "Plan changed while gate executed.")
        current["gates"].append(receipt)
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


def review(root, actor: str, verdict: str, evidence: str, *, task_id: str | None = None, expected_revision=None) -> dict:
    require(verdict in {"PASS", "FAIL"}, "invalid_verdict", "Review verdict is PASS or FAIL.")
    actor = text(actor, "Reviewer identity")
    with transaction(root, "independent_review_recorded", expected_revision) as run:
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
        receipt = {**attestation(actor, evidence), "task": task_id, "verdict": verdict, "snapshot": snapshot(run, task_id)}
        run["reviews"].append(receipt)
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
                _mark_rework(run, task_id, evidence)
    return load(root)


def _verify_task_current(run: dict, task_id: str, seen: set[str]) -> None:
    if task_id in seen:
        return
    owner = run["tasks"].get(task_id)
    require(owner and owner["status"] == "VERIFIED", "dependency_not_ready", f"Task {task_id} is not verified.")
    for dep in task_plan(run, task_id)["depends_on"]:
        _verify_task_current(run, dep, seen)
    require(owner.get("dependencies", {}) == _dependency_bindings(run, task_id), "stale_dependency", f"Task {task_id} was verified against an older dependency attempt or patch.")
    owned_worktree_record(run, owner, task_id=task_id)
    snap = snapshot(run, task_id)
    require(owner.get("report") and owner["report"].get("dispatch_id") == owner.get("agent", {}).get("dispatch_id") and evidence_current(owner["report"], snap), "stale_report", f"Task {task_id} changed after verification or lacks its current dispatch report.")
    reviews = [r for r in run["reviews"] if r["task"] == task_id]
    require(reviews and reviews[-1]["verdict"] == "PASS" and evidence_current(reviews[-1], snap) and gates_current(run, task_id), "stale_task", f"Task {task_id} changed after verification.")
    patch = owner["patch"]
    require(Path(patch["path"]).is_file() and hashlib.sha256(Path(patch["path"]).read_bytes()).hexdigest() == patch["sha256"], "patch_drift", "Verified patch changed after review.")
    seen.add(task_id)


def verify_task_current(run: dict, task_id: str) -> None:
    _verify_task_current(run, task_id, set())


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
        run["validated"] = {**snap, "plan_hash": run["plan_hash"], "at": now()}
        run["state"] = "PR_OPEN" if run.get("pr") else "READY_TO_PUBLISH"
    return load(root)


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
        inventory = git_ops.inventory(owner["path"], owner["base_sha"])
        require(not inventory["unstaged"] and not inventory["untracked"] and not inventory["in_progress"], "unstaged_snapshot", "Stage only the exact intended integration files before snapshotting; finish existing Git operations.")
        snapshot_home = Path(root) / "spec-snapshots"
        snapshot_home.mkdir(exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix=f"r{run['revision']}-", dir=snapshot_home))
        before = snapshots.capture_tree(owner["path"], owner["base_sha"], str(folder / "before"))
        after = snapshots.capture_tree(owner["path"], inventory["tree"], str(folder / "after"))
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


def register_fix(root, actor: str, host: str, handle: str, reason: str, expected_revision=None) -> dict:
    """Record a real same-scope integration/PR correction dispatch."""
    with transaction(root, "integration_fix_dispatched", expected_revision) as run:
        assert_gates_idle(run)
        assert_plan(run)
        need_authority(run, "implement")
        require(run["state"] in {"VERIFYING", "READY_TO_PUBLISH", "PR_OPEN"}, "fix_state", "Fix only an existing unmerged integration within the approved scope.")
        require(run.get("integration"), "missing_worktree", "No integration branch exists.")
        owned_worktree_record(run, run["integration"])
        previous = run.get("integration_fix")
        require(not previous or previous["status"] == "REPORTED", "fix_dispatch_active", "Query and collect the current integration dispatch before starting another.")
        fields = {"actor": text(actor, "Implementation actor"), "host": text(host, "Host"), "handle": text(handle, "Actual spawn handle"), "registered_at": now()}
        require(not any(t.get("status") == "DISPATCHED" for t in run["tasks"].values()), "active_task", "Collect active task workers before editing their integrated result.")
        attempt = 1 if not previous else previous["attempt"] + 1
        require(attempt <= 2, "rework_budget", "Two integrated rework rounds are exhausted; reassess the plan or request explicit escalation.")
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


def resume(root, resolution: str, expected_revision=None) -> dict:
    with transaction(root, "resumed", expected_revision) as run:
        require(run["state"] == "BLOCKED" and run["blocker"], "not_blocked", "No blocker is recorded.")
        text(resolution, "Observed blocker resolution")
        run["state"] = run["blocker"]["from"]
        run["blocker"] = None
    return load(root)


def revise(root, reason: str, expected_revision=None) -> dict:
    with transaction(root, "semantic_revision_started", expected_revision) as run:
        assert_gates_idle(run)
        require(not any(owner.get("status") == "DISPATCHED" for owner in run["tasks"].values())
                and (run.get("integration_fix") or {}).get("status") != "DISPATCHED",
                "active_task", "Collect active implementation results before replacing their semantic plan.")
        text(reason, "Material revision reason")
        require(not run.get("pr"), "open_pr_revision", "Keep the existing PR and reconcile its reviewed scope explicitly before replacing this run plan.")
        if (Path(run["spec_root"]) / "spec-session.json").exists():
            spec_command(root, ["revise", "--reason", reason])
        run.setdefault("previous_versions", []).append({"version": run["spec_version"], "plan": run["plan"], "tasks": run["tasks"], "integration": run["integration"],
                                                        "integration_fix": run.get("integration_fix"), "integration_fix_history": run.get("integration_fix_history", []),
                                                        "gates": run["gates"], "reviews": run["reviews"], "reason": reason})
        run.update(state="DISCOVERY", plan=None, plan_hash=None, approval=None, authorizations={}, tasks={}, integration=None,
                   integration_fix=None, integration_fix_history=[], gates=[], reviews=[], blocker=None)
    return load(root)


def _mark_rework(run: dict, task_id: str, reason: str) -> None:
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
    owner.setdefault("rework_history", []).append({"reason": reason, "report": owner.get("report"), "patch": owner.get("patch"), "at": now()})
    owner.update(status="REWORK", patch=None, report=None)
    owner["rework_rounds"] = owner.get("rework_rounds", 0) + 1
    if owner["rework_rounds"] > 2:
        run["blocker"] = {"reason": "Two rework rounds exhausted; revise or obtain explicit escalation.", "from": run["state"], "at": now()}
        run["state"] = "BLOCKED"


def rework(root, task_id: str, reason: str, expected_revision=None) -> dict:
    with transaction(root, "task_rework_requested", expected_revision) as run:
        _implementation_state(run)
        _mark_rework(run, task_id, reason)
    return load(root)


def status(root) -> dict:
    run = load(root)
    result = {k: run[k] for k in ("id", "root", "primary", "revision", "state", "spec_version", "plan_hash", "blocker", "pr")}
    result["classification"] = run["plan"]["classification"] if run.get("plan") else None
    result["tasks"] = {tid: {"status": t["status"], "path": t["path"], "agent": t.get("agent")} for tid, t in run["tasks"].items()}
    owner = run.get("integration")
    result["integration"] = {key: owner.get(key) for key in ("path", "branch", "base_sha", "status")} if owner else None
    result["spec_root"] = run["spec_root"]
    result["check_root"] = run["check_root"]
    result["authority_scopes"] = {scope: {"actor": grant["actor"], "targets": list(grant.get("targets", {})), "pr": grant.get("pr")} for scope, grant in run["authorizations"].items()}
    result["retained_versions"] = len(run.get("previous_versions", []))
    result["gate_jobs"] = {key: {"status": job["status"], "task": job.get("task"), "case": job.get("case"), "resources": job.get("resources", [])} for key, job in run.get("gate_jobs", {}).items()}
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
    try:
        if run.get("plan"):
            assert_plan(run)
        result["plan_current"] = True
    except RunError:
        result["plan_current"] = False
        result["next_action"] = "The plan artifact drifted. Reconcile it through revision before any action."
    return result
