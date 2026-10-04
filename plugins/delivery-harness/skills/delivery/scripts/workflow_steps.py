#!/usr/bin/env python3
"""The delivery engine as the authority of the shared execution loop.

The loop is ``workflows/orchestrate-execute.js``, a verbatim copy of the pinned orchestrate
workflow. It calls this helper once per mechanical step through a relay agent and reads exactly one
JSON object from stdout (contract: the orchestrate reference ``authority.md``):

  workflow_steps.py --run ROOT args [--task T ...] [--lens L ...] [--model M] [--effort E] [--integration]
  workflow_steps.py --run ROOT prepare --task T
  workflow_steps.py --run ROOT dispatch --task T
  workflow_steps.py --run ROOT collect --task T
  workflow_steps.py --run ROOT gate --task T --label L
  workflow_steps.py --run ROOT rework --task T --reason R [--key K ...]
  workflow_steps.py --run ROOT review-open (--task T | --integration) --lens L [--lens L ...]
  workflow_steps.py --run ROOT review-close (--task T | --integration) --lens L [--lens L ...]
  workflow_steps.py --run ROOT finish --task T

Every step runs the ordinary ``delivery.py`` command, so the engine's guards, gate jobs, journal
import and budgets apply unchanged; this file only translates. A refusal that the loop cannot
repair by another round (the run is blocked, a budget is spent, an attempt was invalidated, an
unauthorized gate) comes back as ``blocked`` with the engine's reason.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

SCRIPTS = Path(__file__).resolve().parent
SKILL = SCRIPTS.parent
LOOP = SKILL / "workflows" / "orchestrate-execute.js"
TAIL_LINES = 40

ADVERSARY_VARIATIONS = [
    "two events in the same turn against a collaborator that completes synchronously or immediately",
    "duplicate delivery of the same request, message or event",
    "restart or crash between a side effect and its commit",
    "reordering of events, responses or callbacks",
    "retry after a partial failure, and cancellation while work is in flight",
    "boundary values, empty inputs and data persisted by an older version",
]

TEXT = {"type": "string", "minLength": 1}
# Mirrors validate_report and the import's tree check, so a result the engine refuses fails at the host.
REPORT = {
    "type": "object",
    "properties": {
        "dispatch_id": TEXT, "summary": TEXT,
        "tests": {"type": "array", "items": {"type": "object", "properties": {
            "command": {"type": "array", "items": TEXT, "minItems": 1}, "exit_code": {"type": "integer"}, "outcome": TEXT},
            "required": ["command", "exit_code", "outcome"]}},
        "limitations": {"type": "array", "items": TEXT},
        "tree": TEXT,
    },
    "required": ["dispatch_id", "summary", "tests", "limitations", "tree"],
}
# Mirrors review-import: a PASS needs evidence of at least 160 characters on two or more non-empty lines.
VERDICT = {
    "type": "object",
    "properties": {
        "review_token": TEXT,
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "evidence": {"type": "string", "minLength": 160, "pattern": "\\S[^\\n]*\\n+[^\\n]*\\S"},
        "defects": {"type": "array", "items": {"type": "object", "properties": {
            "file": TEXT, "line": {"type": "integer"},
            "kind": {"type": "string", "enum": ["behavior", "regression", "test-gap", "scope", "spec-violation", "build", "docs", "security", "other"]},
            "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
            "summary": TEXT, "scenario": TEXT},
            "required": ["file", "kind", "severity", "summary", "scenario"]}},
    },
    "required": ["review_token", "verdict", "evidence", "defects"],
}
PATHS = {"type": "array", "items": {"type": "string"}}
GATE = {
    "type": "object",
    "properties": {
        "exit_code": {"type": "integer"}, "tail": {"type": "string"}, "tree": {"type": "string"}, "head": {"type": "string"},
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "command": {"type": "string"}, "rc": {"type": "integer"}, "outcome": {"type": "string"}, "log": {"type": "string"}}}},
        "unstaged": PATHS, "untracked": PATHS, "outside_scope": PATHS,
        "blocked": {"type": "string"},
    },
    "required": ["exit_code", "tail", "tree", "head", "results", "unstaged", "untracked", "outside_scope"],
}
FINISH = {
    "type": "object",
    "properties": {"patch": {"type": "string"}, "sha256": {"type": "string"}, "files": PATHS, "tree": {"type": "string"},
                   "verify_clean_exit": {"type": "integer"}, "output": {"type": "string"}, "blocked": {"type": "string"}},
    "required": ["patch", "sha256", "files", "tree", "verify_clean_exit", "output"],
}
# One shape for every recorded step; a relay returns structured output against it and drops any field
# it does not list, so it names every field the loop reads off a step (prepare, dispatch, collect,
# review-open, review-close). tests/test_workflows.py checks it against the shipped loop. prepare sets
# resume_at "gate" only for a task the engine holds as REPORTED (step_prepare); otherwise it is absent.
STEP = {
    "type": "object",
    "properties": {"exit_code": {"type": "integer"}, "output": {"type": "string"}, "blocked": {"type": "string"},
                   "worktree": {"type": "string"}, "branch": {"type": "string"}, "head": {"type": "string"},
                   "worktree_id": {"type": "string"}, "spec_sha256": {"type": "string"}, "dispatch": {"type": "string"},
                   "accepted": {"type": "boolean"}, "tokens": {"type": "object", "additionalProperties": {"type": "string"}},
                   "verdict": {"type": "string"}, "resume_at": {"type": "string", "enum": ["gate"]}},
    "required": ["exit_code", "output"],
}

REPORT_FORMAT = ("Return the report as the structured output. dispatch_id: the dispatch named below. summary: what actually "
                 "changed and why. tests: every command you ran as an argument array, its exit code and the observed outcome "
                 "(an empty list means no tests ran). limitations: specific behaviour you did not check. tree: the output of "
                 "`git write-tree` in the worktree after your final staging.")
VERDICT_FORMAT = ("Return the verdict as the structured output: review_token, verdict, evidence (one line per acceptance item with "
                  "what you observed in the code, and one line per gate you reran with its exit code; a PASS needs at least two "
                  "such lines and 160 characters), defects (only real ones, each with file, line, kind, severity, a summary and a "
                  "concrete failing scenario).")
BRIEF = ("You are not alone in this repository. Work only in your assigned worktree and owned paths. Do not revert others' "
         "changes. Preserve any unexpected work and report it. Stage exact intended files. Do not commit, push, open a PR, "
         "merge, deploy, or mutate product/session records. Run only the safe planned gates; gates with external or "
         "destructive effects run only under the coordinator's authority. Report actual tests and limitations.")


class StepError(Exception):
    pass


def emit(value: dict) -> int:
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def delivery(*arguments: str) -> tuple[bool, dict]:
    """Run one delivery.py command; return (ok, result) or (False, {code, message})."""
    process = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), *arguments], capture_output=True, text=True)
    stream = process.stdout if process.returncode == 0 else (process.stderr or process.stdout)
    try:
        data = json.loads(stream)
    except ValueError:
        return False, {"code": "unreadable_output", "message": (stream or f"exit {process.returncode}").strip()[-2000:]}
    if process.returncode == 0 and data.get("ok"):
        return True, data["result"]
    error = data.get("error") or {}
    return False, {"code": error.get("code", "error"), "message": error.get("message", stream.strip()[-2000:])}


def status(root: str) -> dict:
    ok, result = delivery("status", "--run", root)
    if not ok:
        raise StepError(f"{result['code']}: {result['message']}")
    return result


def run_state(root: str) -> dict:
    return json.loads((Path(root) / "run.json").read_text(encoding="utf-8"))


def blocker(root: str) -> str:
    run = run_state(root)
    if run.get("state") != "BLOCKED":
        return ""
    block = run.get("blocker") or {}
    return f"{block.get('code', 'blocked')}: {block.get('reason', 'the run is blocked')}"


def refusal(result: dict) -> str:
    return f"{result['code']}: {result['message']}"


def git(worktree: str, *arguments: str) -> tuple[int, str]:
    process = subprocess.run(["git", "-C", worktree, *arguments], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
    return process.returncode, process.stdout


def paths(worktree: str, *arguments: str, pathspecs=()) -> list[str]:
    code, out = git(worktree, *arguments, "-z", *(("--", *pathspecs) if pathspecs else ()))
    return sorted(path for path in out.split("\0") if path) if code == 0 else []


def task_owner(root: str, task: str) -> dict:
    owner = run_state(root)["tasks"].get(task)
    if not owner:
        raise StepError(f"task {task} has no worktree in this run")
    return owner


def worktree_state(worktree: str, scopes: list[str]) -> dict:
    staged = set(paths(worktree, "diff", "--cached", "--no-renames", "--name-only"))
    allowed = set(paths(worktree, "diff", "--cached", "--no-renames", "--name-only", pathspecs=scopes)) if scopes else staged
    return {"unstaged": paths(worktree, "diff", "--no-renames", "--name-only"),
            "untracked": paths(worktree, "ls-files", "--others", "--exclude-standard"),
            "outside_scope": sorted(staged - allowed)}


def plan_task(run: dict, task: str) -> dict:
    for item in run["plan"]["tasks"]:
        if item["id"] == task:
            return item
    raise StepError(f"unknown task {task}")


def argv_text(command: list[str]) -> str:
    return " ".join(json.dumps(part) if re.search(r"[^\w./=:@%+,-]", part) else part for part in command)


# ---------------------------------------------------------------- steps

def step_prepare(a) -> dict:
    run = run_state(a.run)
    owner = run["tasks"].get(a.task)
    if not owner or owner["status"] == "INVALIDATED":
        ok, result = delivery("task-prepare", "--run", a.run, "--task", a.task)
        if not ok:
            return {"exit_code": 1, "output": refusal(result), **({"blocked": blocker(a.run)} if blocker(a.run) else {})}
        owner = result["tasks"][a.task]
    elif owner["status"] == "REPORTED":
        return resume_reported(a, owner)
    elif owner["status"] not in {"PREPARED", "REWORK"}:
        # A task the engine already holds in another state belongs to the coordinator, not to a fresh loop.
        return {"exit_code": 1, "output": NOT_RESUMABLE.get(owner["status"], f"task {a.task} is {owner['status']}; continue it from status instead of a new loop").format(task=a.task)}
    return prepared(a, owner)


def prepared(a, owner: dict, **extra) -> dict:
    contract = Path(a.run) / "tasks" / f"{a.task}.json"
    return {"exit_code": 0, "output": f"prepared attempt {owner['attempt']}", "worktree": owner["path"], "branch": owner["branch"],
            "head": owner["base_sha"], "spec_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(), **extra}


NOT_RESUMABLE = {
    "DISPATCHED": ("task {task} is DISPATCHED: its implementer may still be running. Wait for it, then collect its result with "
                   "task-import (the task becomes REPORTED and a new loop resumes it at the gate) or end the dispatch with "
                   "task-abandon; a new loop never starts a second implementer on the same task"),
    "VERIFIED": "task {task} is already VERIFIED with a reviewed patch; there is nothing to resume (status shows the next step)",
}


def resume_reported(a, owner: dict) -> dict:
    """A REPORTED task (its implementer's result was imported, then the loop stopped) resumes at the gate.

    Read-only: the gates rerun on the current content and the review follows as in a normal round. A
    task whose content no longer matches its report, or whose current content already carries an
    unanswered FAIL, would only fail later, so it is refused here with the way out.
    """
    reason = blocker(a.run)
    if reason:
        return {"exit_code": 1, "output": f"task {a.task} is REPORTED but the run is blocked", "blocked": reason}
    import run_engine
    import git_ops
    try:
        run = run_engine.load(a.run)
        if run["state"] != "IMPLEMENTING" or run.get("integration"):
            return {"exit_code": 1, "output": f"task {a.task} is REPORTED but the run is {run['state']}"
                    f"{' and integrated' if run.get('integration') else ''}; a loop resumes only active, unintegrated implementation"}
        held = run["tasks"][a.task]
        snap = run_engine.snapshot(run, a.task)
        if not held.get("report") or not run_engine.evidence_current(held["report"], snap):
            return {"exit_code": 1, "output": f"task {a.task} is REPORTED but its worktree changed since its report; send it back with "
                    "task-rework (a new dispatch reports the current content) instead of resuming at the gate"}
        failures = run_engine._unoverridden_failures(run, a.task, snap)
        if failures:
            return {"exit_code": 1, "output": f"task {a.task} is REPORTED but its current content has an unanswered FAIL review from "
                    f"{failures[0].get('actor', 'a reviewer')}; send it back with task-rework, or record authorize --scope decision "
                    f"--code review_override --task {a.task} after that FAIL"}
    except (run_engine.RunError, git_ops.GitError) as exc:
        return {"exit_code": 1, "output": f"task {a.task} is REPORTED but cannot resume: {getattr(exc, 'code', 'error')}: {exc}"}
    return prepared(a, owner, output=f"resuming attempt {owner['attempt']} at the gate with its imported report", resume_at="gate")


def step_dispatch(a) -> dict:
    ok, result = delivery("task-register", "--run", a.run, "--task", a.task, "--via-workflow")
    if not ok:
        return {"exit_code": 1, "output": refusal(result), **({"blocked": blocker(a.run)} if blocker(a.run) else {})}
    # Named `dispatch`, not dispatch_id: the journal import accepts exactly one result carrying the ID.
    return {"exit_code": 0, "output": "registered", "dispatch": result["tasks"][a.task]["agent"]["dispatch_id"]}


# Refusals that condemn the returned result itself: the dispatch is abandoned and the task reworked.
# Any other refusal (the journal, the host root, the run or the engine) blocks, since abandoning would
# throw away a result that may be valid and spend a round on a problem no implementer can fix.
REFUSED_RESULTS = {"journal_result_missing", "invalid_report", "report_limit", "missing_text", "invalid_command",
                   "scope_violation", "import_tree_mismatch"}


def step_collect(a) -> dict:
    ok, result = delivery("task-import", "--run", a.run, "--task", a.task)
    if ok:
        return {"exit_code": 0, "output": "imported from the host journal", "accepted": True}
    reason = refusal(result)
    if result["code"] not in REFUSED_RESULTS:
        return {"exit_code": 1, "output": reason, "accepted": False, "blocked": f"the result cannot be imported: {reason}"}
    ok, abandoned = delivery("task-abandon", "--run", a.run, "--task", a.task, "--reason", f"workflow result not importable: {reason}")
    if not ok:
        return {"exit_code": 1, "output": reason, "accepted": False, "blocked": f"the dispatch could not be abandoned: {refusal(abandoned)}"}
    owner = abandoned["tasks"][a.task]
    if blocker(a.run):
        return {"exit_code": 1, "output": reason, "accepted": False, "blocked": blocker(a.run)}
    if owner["status"] == "INVALIDATED":
        return {"exit_code": 1, "output": reason, "accepted": False,
                "blocked": "the attempt was invalidated (the worker committed, switched branches or the worktree is gone); prepare a new attempt"}
    return {"exit_code": 1, "output": reason, "accepted": False}


def step_gate(a) -> dict:
    run = run_state(a.run)
    task = plan_task(run, a.task)
    owner = task_owner(a.run, a.task)
    worktree = owner["path"]
    results, tail, failed = [], [], False
    for index, gate in enumerate(task["gates"]):
        # A planned timeout lets a long but legitimate gate (a full test suite) run past the 300 s default.
        timeout = ["--timeout", str(gate["timeout"])] if "timeout" in gate else []
        ok, receipt = delivery("gate", "--run", a.run, "--task", a.task, "--index", str(index), *timeout)
        if not ok:
            # A planned command that ran and failed still returns a receipt; a refusal means the gate
            # could not run at all (authority, a held resource, the run's state), which no rework fixes.
            reason = refusal(receipt)
            return {"exit_code": 1, "tail": reason, "tree": "", "head": "", "results": results, "unstaged": [], "untracked": [],
                    "outside_scope": [], "blocked": f"gate {index} cannot run: {reason}"}
        outcome = receipt["outcome"] if receipt["unchanged"] else f"{receipt['outcome']}; the command changed the worktree"
        results.append({"command": argv_text(gate["command"]), "rc": 0 if receipt["passed"] else (receipt["exit_code"] or 1),
                        "outcome": outcome, "log": receipt["log"]})
        if not receipt["passed"]:
            tail.append(f"$ {argv_text(gate['command'])}  ({outcome})")
            try:
                tail.extend(Path(receipt["log"]).read_text(errors="replace").splitlines()[-TAIL_LINES:])
            except OSError:
                pass
            failed = True
            break
    code, tree = git(worktree, "write-tree")
    head_code, head = git(worktree, "rev-parse", "HEAD")
    return {"exit_code": 1 if failed else 0, "label": a.label, "tail": "\n".join(tail), "tree": tree.strip() if code == 0 else "",
            "head": head.strip() if head_code == 0 else "", "results": results, **worktree_state(worktree, task["paths"])}


def step_rework(a) -> dict:
    keys = [part for key in (a.key or []) for part in ("--finding-key", key)]
    ok, result = delivery("task-rework", "--run", a.run, "--task", a.task, "--reason", a.reason, "--decision", "code-fix", *keys)
    if blocker(a.run):
        return {"exit_code": 1, "output": refusal(result) if not ok else "rework recorded", "blocked": blocker(a.run)}
    if not ok:
        return {"exit_code": 1, "output": refusal(result)}
    return {"exit_code": 0, "output": f"rework round {result['tasks'][a.task].get('rework_rounds', 0)} recorded"}


def target(a) -> list[str]:
    return ["--task", a.task] if a.task else []


def step_review_open(a) -> dict:
    tokens = {}
    for lens in a.lens:
        ok, result = delivery("review-token", "--run", a.run, *target(a), "--lens", lens)
        if not ok:
            return {"exit_code": 1, "output": refusal(result), **({"blocked": blocker(a.run)} if blocker(a.run) else {})}
        tokens[lens] = result["token"]
    return {"exit_code": 0, "output": f"issued {len(tokens)} token(s)", "tokens": tokens}


def step_review_close(a) -> dict:
    ok, result = delivery("review-import", "--run", a.run, *target(a), *[part for lens in a.lens for part in ("--lens", lens)])
    if blocker(a.run):
        return {"exit_code": 1, "output": refusal(result) if not ok else "review recorded", "blocked": blocker(a.run)}
    if not ok:
        return {"exit_code": 1, "output": refusal(result)}
    review = [item for item in result["reviews"] if item.get("task") == a.task][-1]
    return {"exit_code": 0, "output": f"{review['verdict']} from {len(review.get('reviewers', []))} reviewer(s)", "verdict": review["verdict"]}


def step_finish(a) -> dict:
    owner = task_owner(a.run, a.task)
    worktree = owner["path"]
    code, tree = git(worktree, "write-tree")
    files = paths(worktree, "diff", "--cached", "--name-only")
    patch = owner.get("patch") or {}
    if owner["status"] != "VERIFIED" or not patch:
        return {"patch": "", "sha256": "", "files": files, "tree": tree.strip(), "verify_clean_exit": 1,
                "output": f"task {a.task} is {owner['status']}, not VERIFIED with a saved patch"}
    path = Path(patch["path"])
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    clean = actual == patch["sha256"] and not worktree_state(worktree, [])["unstaged"]
    return {"patch": str(path), "sha256": actual, "files": files, "tree": tree.strip() if code == 0 else "",
            "verify_clean_exit": 0 if clean else 1,
            "output": "the engine exported the reviewed patch" if clean else "the saved patch or the worktree changed after review"}


# ---------------------------------------------------------------- arguments for the loop

def loop_version() -> str:
    match = re.search(r"^const SCRIPT_VERSION = '([^']+)'$", LOOP.read_text(encoding="utf-8"), re.M)
    if not match:
        raise StepError(f"{LOOP} carries no SCRIPT_VERSION")
    return match.group(1)


def build_args(a) -> dict:
    run = run_state(a.run)
    if not run.get("plan"):
        raise StepError("the run has no plan")
    order = [task["id"] for task in run["plan"]["tasks"]]
    selected = a.task or [tid for tid in order if (run["tasks"].get(tid) or {}).get("status") != "VERIFIED"]
    unknown = sorted(set(selected) - set(order))
    if unknown:
        raise StepError(f"unknown task(s): {', '.join(unknown)}")
    step = {"tier": 0, "model": a.model, "effort": a.effort}
    lenses = [{"key": lens, "model": a.model, "effort": a.effort} for lens in (a.lens or ["conformance"])]
    tasks = []
    for tid in order:
        if tid not in selected:
            continue
        task = plan_task(run, tid)
        safe = [argv_text(g["command"]) for g in task["gates"] if g.get("risk", "safe") == "safe"]
        tasks.append({
            "id": tid, "title": task.get("title", tid), "spec": str(Path(a.run) / "tasks" / f"{tid}.json"), "spec_sha256": None,
            "worktree": None, "branch": None, "start_head": None, "worktree_id": None,
            # Unselected dependencies must already be verified; the engine checks that when it prepares.
            "scope": task["paths"], "depends_on": [dep for dep in task["depends_on"] if dep in selected], "scaffold_from": [],
            "gate": safe, "risk": [], "gate_label_prefix": f"task-{tid}", "chain": [step], "lenses": lenses,
        })
    integration = None
    if a.integration:
        owner = run.get("integration")
        if not owner:
            raise StepError("the run has no integration worktree; run integrate first")
        plan = run["plan"]
        integration = {
            # refresh rebases the integration and records the new base on it, not on the run.
            "worktree": owner["path"], "base_sha": owner["base_sha"], "acceptance": plan.get("goal", ""),
            "requirements": [f"{r.get('id', '')}: {r.get('oracle', r.get('text', ''))}".strip(": ") for r in plan.get("requirements", [])],
            "gate": [argv_text(c["command"]) for c in plan.get("verification", []) if c.get("risk", "safe") == "safe"],
            "brief": None, "lenses": lenses,
        }
        tasks = []
    helper = [sys.executable, str(Path(__file__).resolve()), "--run", str(Path(a.run).resolve())]
    return {
        "schema": "orchestrate-execute-args/v1", "version": loop_version(), "harness": "claude", "run_dir": str(Path(a.run).resolve()),
        "authority": {"name": "delivery", "helper": helper},
        "reviewer_brief": f"{SKILL / 'references' / 'host-execution.md'} (section Independent review brief)",
        "limits": {"rework_rounds": 5}, "utility": None,
        "agent_types": {"implementer": None, "reviewer": None}, "role_text": {},
        "formats": {"report": REPORT_FORMAT, "verdict": VERDICT_FORMAT, "brief": BRIEF},
        "schemas": {"report": REPORT, "verdict": VERDICT, "gate": GATE, "finish": FINISH, "step": STEP},
        "adversary_variations": ADVERSARY_VARIATIONS,
        "tasks": tasks, **({"integration": integration} if integration else {}),
    }


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", required=True)
    sub = p.add_subparsers(dest="step", required=True)
    args = sub.add_parser("args")
    args.add_argument("--task", action="append")
    args.add_argument("--lens", action="append")
    args.add_argument("--model")
    args.add_argument("--effort")
    args.add_argument("--integration", action="store_true")
    for name in ("prepare", "dispatch", "collect", "finish"):
        sub.add_parser(name).add_argument("--task", required=True)
    gate = sub.add_parser("gate")
    gate.add_argument("--task", required=True)
    gate.add_argument("--label", default="")
    rework = sub.add_parser("rework")
    rework.add_argument("--task", required=True)
    rework.add_argument("--reason", required=True)
    rework.add_argument("--key", action="append")
    for name in ("review-open", "review-close"):
        review = sub.add_parser(name)
        which = review.add_mutually_exclusive_group(required=True)
        which.add_argument("--task")
        which.add_argument("--integration", action="store_true")
        review.add_argument("--lens", action="append", required=True)
    return p


STEPS = {"prepare": step_prepare, "dispatch": step_dispatch, "collect": step_collect, "gate": step_gate, "rework": step_rework,
         "review-open": step_review_open, "review-close": step_review_close, "finish": step_finish}


def main(argv=None) -> int:
    a = parser().parse_args(argv)
    try:
        if a.step == "args":
            return emit(build_args(a))
        return emit(STEPS[a.step](a))
    except (StepError, OSError, ValueError, KeyError) as exc:
        # Unexpected state is never guessed around: the loop blocks the task with this reason. The object
        # also fills the gate and finish shapes, so a relay can return it unchanged from any step.
        print(json.dumps({"exit_code": 2, "output": str(exc), "blocked": f"{a.step} failed: {exc}", "tail": str(exc), "tree": "", "head": "",
                          "results": [], "unstaged": [], "untracked": [], "outside_scope": [], "patch": "", "sha256": "", "files": [],
                          "verify_clean_exit": 1}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
