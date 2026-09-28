#!/usr/bin/env python3
"""Portable entry point for a single evidence-bound delivery run."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

import run_engine as engine


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_text(path):
    return Path(path).read_text(encoding="utf-8").strip()


def parser():
    p = argparse.ArgumentParser(description="One product-to-PR delivery workflow. No implicit merge or deploy.")
    sub = p.add_subparsers(dest="command", required=True)
    new = sub.add_parser("new", help="Create an external run; repository state is only inspected.")
    new.add_argument("--repo", required=True)
    new.add_argument("--id", required=True)
    new.add_argument("--request-file", required=True)
    new.add_argument("--store")
    new.add_argument("--product")
    new.add_argument("--work-item")
    new.add_argument("--local-only", action="store_true", help="Deliver to a committed local branch; no remote, PR, or provider.")
    listing = sub.add_parser("list", help="Find runs by canonical repository identity.")
    listing.add_argument("--repo", required=True)
    listing.add_argument("--store")
    sub.add_parser("doctor", help="Check local capabilities without network or installation changes.")
    for name in ("plan", "approve", "authorize", "start", "status", "task-prepare", "task-register", "task-report", "task-import", "task-abandon", "task-rework", "fix-register", "fix-report", "gate", "gate-recover", "review", "review-token", "review-import", "integrate", "ready", "capture-verification", "commit", "publish", "refresh", "merge", "cleanup", "finish-local", "block", "resume", "revise"):
        q = sub.add_parser(name)
        q.add_argument("--run", required=True)
        if name != "status":
            q.add_argument("--expected-revision", type=int)
        if name in {"plan", "task-report", "fix-report"}:
            q.add_argument("--file", required=True)
        if name in {"approve", "authorize", "review"}:
            q.add_argument("--actor", required=True)
            q.add_argument("--evidence-file", required=True)
        if name == "authorize":
            q.add_argument("--scope", required=True, choices=["implement", "publish", "merge", "test-external", "test-destructive", "local-merge", "fix-budget", "rework-budget", "decision", "standing-approval"])
            q.add_argument("--target", action="append", dest="targets", help="Exact task:index or case ID; repeat for each approved side-effect gate.")
            q.add_argument("--count", type=int, help="Extra rounds: 1-3 integration-fix rounds for --scope fix-budget, 1-2 rounds for --scope rework-budget.")
            q.add_argument("--task", help="Task named by the blocker, for --scope rework-budget or decision; omit it for an integrated block.")
            q.add_argument("--code", choices=sorted(engine.DECISION_CODES), help="For --scope decision: the blocker code the user's decision answers, or review_override for a FAIL the user judges wrong.")
            q.add_argument("--until", help="Standing approval end: ISO-8601 with an offset or Z, at most 30 days ahead.")
            q.add_argument("--max-level", choices=["small", "medium", "large"], help="Highest plan level a standing approval covers.")
            q.add_argument("--trigger", action="append", dest="triggers", help="Risk trigger a standing approval covers; repeat for each.")
        if name == "approve":
            q.add_argument("--standing", action="store_true", help="Approve under the recorded standing approval; the evidence file is the materiality note.")
        if name == "start":
            q.add_argument("--within-request", action="store_true")
        if name.startswith("task-"):
            q.add_argument("--task", required=True)
        if name in {"task-register", "task-report", "fix-register", "fix-report"}:
            q.add_argument("--actor", required=name != "task-register")
        if name in {"task-register", "fix-register"}:
            q.add_argument("--host", required=name != "task-register")
            q.add_argument("--handle", required=name != "task-register")
        if name == "task-register":
            q.add_argument("--via-workflow", action="store_true", help="The host workflow journal supplies the agent identity; omit --actor, --host and --handle.")
        if name in {"task-import", "review-import"}:
            q.add_argument("--journal")
            q.add_argument("--search-root")
        if name in {"task-rework", "review", "review-import"}:
            q.add_argument("--decision", choices=list(engine.DECISIONS))
            q.add_argument("--finding-key", action="append", dest="finding_keys")
        if name == "review-token":
            q.add_argument("--task")
            q.add_argument("--lens", default="default")
        if name == "review-import":
            q.add_argument("--task")
            q.add_argument("--lens", action="append", dest="lenses", required=True)
        if name in {"task-rework", "task-abandon", "block", "resume", "revise", "capture-verification", "fix-register"}:
            q.add_argument("--reason", required=True)
        if name == "commit":
            q.add_argument("--message-file", required=True)
        if name == "finish-local":
            q.add_argument("--fast-forward-main", action="store_true", help="Fast-forward local main to the committed head; needs local-merge authority.")
        if name == "publish":
            q.add_argument("--title", required=True)
            q.add_argument("--body-file", required=True)
        if name == "review":
            q.add_argument("--task")
            q.add_argument("--verdict", required=True, choices=["PASS", "FAIL"])
        if name == "gate":
            q.add_argument("--task")
            q.add_argument("--case")
            q.add_argument("--index", type=int, default=0)
            q.add_argument("--timeout", type=int, default=300)
        if name == "gate-recover":
            q.add_argument("--job", required=True)
            q.add_argument("--evidence-file", required=True)
    spec = sub.add_parser("spec", help="Use the bundled rich specification/check engine with the run-owned root.")
    spec.add_argument("--run", required=True)
    spec.add_argument("--check", action="store_true")
    spec.add_argument("arguments", nargs=argparse.REMAINDER)
    product = sub.add_parser("product", help="Use Markdown product memory.")
    product.add_argument("arguments", nargs=argparse.REMAINDER)
    return p


def dispatch(a):
    if a.command == "doctor":
        result = {"python": sys.version.split()[0], "git": shutil.which("git"), "gh": shutil.which("gh"), "platform": sys.platform,
                  "spec_engine": (Path(__file__).parent.parent / "internal/spec/scripts/spec_flow.py").is_file(),
                  "product_engine": (Path(__file__).parent / "product.py").is_file()}
        result["ready_for_local_work"] = bool(result["git"] and result["spec_engine"] and result["product_engine"] and sys.version_info >= (3, 10))
        result["publication_available"] = bool(result["gh"])
        return result
    if a.command == "product":
        arguments = a.arguments[1:] if a.arguments[:1] == ["--"] else a.arguments
        process = subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("product.py")), *arguments], capture_output=True, text=True)
        if process.returncode:
            raise engine.RunError("product_command", process.stderr or process.stdout)
        return json.loads(process.stdout)
    if a.command == "new":
        return engine.create(a.repo, a.id, read_text(a.request_file), store=a.store, product_root=a.product, work_item=a.work_item,
                             delivery_mode="local" if a.local_only else "github")
    if a.command == "list":
        import hashlib
        import os
        primary = Path(engine.git_ops.canonical_repo(a.repo)).resolve()
        home = Path(a.store or os.environ.get("DELIVERY_HOME", "~/.local/state/delivery-harness")).expanduser().resolve()
        key = primary.name + "-" + hashlib.sha256(str(primary).encode()).hexdigest()[:10]
        return [engine.status(path.parent) for path in sorted((home / key / "runs").glob("*/run.json"))]
    if a.command == "spec":
        arguments = a.arguments[1:] if a.arguments[:1] == ["--"] else a.arguments
        return engine.spec_command(a.run, arguments, check=a.check)
    revision = getattr(a, "expected_revision", None)
    if a.command == "status": return engine.status(a.run)
    if a.command == "plan": return engine.set_plan(a.run, read_json(a.file), revision)
    if a.command == "authorize": return engine.authorize(a.run, a.scope, a.actor, read_text(a.evidence_file), revision, targets=a.targets, count=a.count, until=a.until, max_level=a.max_level, triggers=a.triggers, task=a.task, code=a.code)
    if a.command == "approve": return engine.approve(a.run, a.actor, read_text(a.evidence_file), revision, standing=a.standing)
    if a.command == "start": return engine.start(a.run, within_request=a.within_request, expected_revision=revision)
    if a.command == "task-prepare": return engine.prepare_task(a.run, a.task, revision)
    if a.command == "task-register": return engine.register_agent(a.run, a.task, a.actor, a.host, a.handle, revision, via_workflow=a.via_workflow)
    if a.command == "task-report": return engine.report_task(a.run, a.task, a.actor, read_json(a.file), revision)
    if a.command == "task-import": return engine.import_task_report(a.run, a.task, journal=a.journal, search_root=a.search_root, expected_revision=revision)
    if a.command == "task-abandon": return engine.abandon_task(a.run, a.task, a.reason, revision)
    if a.command == "task-rework": return engine.rework(a.run, a.task, a.reason, revision, decision=a.decision, finding_keys=a.finding_keys)
    if a.command == "fix-register": return engine.register_fix(a.run, a.actor, a.host, a.handle, a.reason, revision)
    if a.command == "fix-report": return engine.report_fix(a.run, a.actor, read_json(a.file), revision)
    if a.command == "gate": return engine.execute_gate(a.run, task_id=a.task, case_id=a.case, gate_index=a.index, timeout=a.timeout, expected_revision=revision)
    if a.command == "gate-recover": return engine.gate_jobs.recover(a.run, a.job, read_text(a.evidence_file), expected_revision=revision)
    if a.command == "review": return engine.review(a.run, a.actor, a.verdict, read_text(a.evidence_file), task_id=a.task, expected_revision=revision, decision=a.decision, finding_keys=a.finding_keys)
    if a.command == "review-token": return engine.review_token(a.run, task_id=a.task, lens=a.lens, expected_revision=revision)
    if a.command == "review-import": return engine.import_review(a.run, lenses=a.lenses, task_id=a.task, journal=a.journal, search_root=a.search_root, decision=a.decision, finding_keys=a.finding_keys, expected_revision=revision)
    if a.command == "integrate": return engine.integrate(a.run, revision)
    if a.command == "ready": return engine.ready(a.run, revision)
    if a.command == "capture-verification": return engine.capture_verification(a.run, a.reason, revision)
    if a.command in {"commit", "publish", "refresh", "merge", "cleanup", "finish-local"}:
        import publisher
        if a.command == "commit": return publisher.commit(a.run, read_text(a.message_file), revision)
        if a.command == "finish-local": return publisher.finish_local(a.run, revision, fast_forward=a.fast_forward_main)
        if a.command == "publish": return publisher.publish(a.run, a.title, read_text(a.body_file), revision)
        return getattr(publisher, a.command)(a.run, revision)
    if a.command == "block": return engine.block(a.run, a.reason, revision)
    if a.command == "resume": return engine.resume(a.run, a.reason, revision)
    if a.command == "revise": return engine.revise(a.run, a.reason, revision)
    raise engine.RunError("unknown_command", "Unknown delivery command.")


def checked_arguments(argv=None):
    p = parser()
    a = p.parse_args(argv)
    if a.command == "task-register":
        supplied = [flag for flag, value in (("--actor", a.actor), ("--host", a.host), ("--handle", a.handle)) if value is not None]
        if a.via_workflow and supplied:
            p.error("task-register --via-workflow takes the agent identity from the host journal; remove " + ", ".join(supplied))
        if not a.via_workflow and len(supplied) != 3:
            p.error("task-register requires --actor, --host and --handle unless --via-workflow is given")
    return a


def main(argv=None):
    try:
        result = dispatch(checked_arguments(argv))
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False, indent=2))
        return 0
    except (engine.RunError, engine.git_ops.GitError) as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": {"code": "invalid_input", "message": str(exc)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
