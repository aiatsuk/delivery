"""Execution-control guards with real local Git and synthetic host workflow journals.

Every actor, journal line and transcript below is a synthetic unit-test artifact under a
temporary directory. None of it proves that a host workflow, an agent or a person acted.
"""
from __future__ import annotations

import datetime as dt
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_run_engine import PASS_EVIDENCE, SCRIPTS, RunFixture, e, gate, git, plan
from tests.test_local_mode import LocalFixture
from tests.test_publisher import PublisherFixture
from tests.test_run_adversarial import dependent_plan
import publisher
import workflow_journal as wj

RUN_ID = "wf_fixture-0001"
IMPLEMENTER = "a0fixtureimpl01"


def later(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**delta)).isoformat()


class Journal:
    """Writes host-format journal lines and transcripts for one synthetic workflow run."""

    def __init__(self, search_root: Path, run_id: str = RUN_ID, project: str = "fixture-project"):
        self.project = search_root / project
        self.dir = self.project / "fixture-session" / "subagents" / "workflows" / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "journal.jsonl"
        if not self.path.exists():
            self.line({"type": "launched"})

    def line(self, value) -> None:
        with self.path.open("a", encoding="utf-8") as out:
            out.write((value if isinstance(value, str) else json.dumps(value)) + "\n")

    def agent(self, agent_id: str, result: dict, *, started=True, result_first=False, transcript=True) -> None:
        key = "v2:" + hashlib.sha256(json.dumps([agent_id, result], sort_keys=True).encode()).hexdigest()[:16]
        begin = {"type": "started", "key": key, "agentId": agent_id, "label": "fixture " + agent_id, "phase": "fixture"}
        end = {"type": "result", "key": key, "agentId": agent_id, "result": result}
        for item in ([end, begin] if result_first else [begin, end] if started else [end]):
            self.line(item)
        if transcript:
            (self.dir / f"agent-{agent_id}.jsonl").write_text(json.dumps({"type": "fixture transcript", "agent": agent_id}) + "\n")

    def failed(self, agent_id: str, label: str) -> None:
        key = "v2:" + hashlib.sha256(json.dumps([agent_id, label]).encode()).hexdigest()[:16]
        self.line({"type": "started", "key": key, "agentId": agent_id, "label": label, "phase": "Implement"})
        self.line({"type": "failed", "key": key, "agentId": agent_id})
        (self.dir / f"agent-{agent_id}.jsonl").write_text(json.dumps({"type": "fixture transcript", "agent": agent_id}) + "\n")


def worker_result(dispatch_id: str) -> dict:
    return {"dispatch_id": dispatch_id, "summary": "Changed only the fixture value.",
            "tests": [{"command": ["git", "diff", "--cached", "--check"], "exit_code": 0, "outcome": "Synthetic fixture observation; no whitespace errors."}],
            "limitations": ["Synthetic journal for unit testing only."]}


def lens(token: str, verdict="PASS", evidence=PASS_EVIDENCE, defects=None) -> dict:
    return {"review_token": token, "verdict": verdict, "evidence": evidence, "defects": defects or []}


def multi_plan(*names):
    """The fixture plan plus independent tasks that each own one file named after the task."""
    value = plan()
    for name in names:
        extra = copy.deepcopy(value["tasks"][0])
        extra.update(id=name, title=f"Write {name}", paths=[f"{name}.txt"],
                     gates=[gate([sys.executable, "-B", "-c", f"from pathlib import Path; assert Path('{name}.txt').read_text() == '{name}\\n'"])])
        value["tasks"].append(extra)
    return value


class ControlFixture(RunFixture):
    def setUp(self):
        super().setUp()
        self.search = self.host_root

    def assert_code(self, expected, function, *args, **kwargs):
        with self.assertRaises(e.RunError) as caught:
            function(*args, **kwargs)
        self.assertEqual(expected, caught.exception.code, caught.exception.message)
        return caught.exception

    def write_value(self, task="value", content="after\n", name="value.txt") -> Path:
        path = Path(e.load(self.root)["tasks"][task]["path"])
        (path / name).write_text(content)
        git(path, "add", "--", name)
        return path

    def workflow_dispatch(self, task="value") -> str:
        e.prepare_task(self.root, task)
        dispatch_id = e.register_agent(self.root, task, via_workflow=True)["tasks"][task]["agent"]["dispatch_id"]
        self.write_value(task)
        return dispatch_id

    def imported(self) -> dict:
        self.begin()
        dispatch_id = self.workflow_dispatch()
        Journal(self.search).agent(IMPLEMENTER, worker_result(dispatch_id))
        return e.import_task_report(self.root, "value", search_root=self.search)

    def redispatch(self, handle: str) -> None:
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", handle)
        self.report()

    def cli(self, *arguments, expected=0):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), *map(str, arguments)], cwd=self.home, capture_output=True, text=True)
        self.assertEqual(expected, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)["result"] if expected == 0 else result.stderr


class PreDispatchTests(ControlFixture):
    def test_prepared_worktree_written_before_dispatch_is_refused(self):
        self.begin()
        e.prepare_task(self.root, "value")
        path = self.write_value()
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", "fixture-writer-value", "unit-test", "synthetic:value")
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", via_workflow=True)
        self.assertEqual("PREPARED", e.load(self.root)["tasks"]["value"]["status"])
        self.assertEqual("after\n", (path / "value.txt").read_text())

    def test_rework_worktree_changed_after_failing_report_is_refused(self):
        self.begin()
        path = self.implement()
        e.rework(self.root, "value", "Synthetic finding: confirm the value again.")
        (path / "value.txt").write_text("written by the coordinator\n")
        git(path, "add", "--", "value.txt")
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", "fixture-writer-value", "unit-test", "synthetic:rework")
        (path / "value.txt").write_text("after\n")
        git(path, "add", "--", "value.txt")
        self.assertEqual("DISPATCHED", e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:rework")["tasks"]["value"]["status"])

    def test_gate_after_snapshot_is_the_rework_baseline_but_later_writes_are_refused(self):
        value = plan()
        value["tasks"][0]["gates"] = [gate([sys.executable, "-B", "-c", "from pathlib import Path; p = Path('value.txt'); p.write_text(p.read_text() + 'gate output\\n')"])]
        self.begin(value)
        path = self.implement()
        receipt = e.execute_gate(self.root, task_id="value")
        self.assertFalse(receipt["passed"])
        self.assertNotEqual(receipt["snapshot"]["content"], receipt["after"]["content"])
        self.assertEqual(str(path), receipt["after"]["path"])
        e.rework(self.root, "value", "Synthetic finding: the gate itself rewrote the value file.", decision="test-plan")
        observed = (path / "value.txt").read_text()
        (path / "value.txt").write_text("written by the coordinator\n")
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", "fixture-writer-value", "unit-test", "synthetic:after-gate")
        (path / "value.txt").write_text(observed)
        self.assertEqual("DISPATCHED", e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:after-gate")["tasks"]["value"]["status"])


    def test_a_gate_run_after_an_outside_write_does_not_adopt_that_write(self):
        self.begin()
        path = self.implement()
        (path / "value.txt").write_text("written by the coordinator after the report\n")
        git(path, "add", "--", "value.txt")
        receipt = e.execute_gate(self.root, task_id="value")
        self.assertEqual(receipt["snapshot"]["content"], receipt["after"]["content"])
        e.rework(self.root, "value", "Synthetic finding: the gate saw a different value.")
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", "fixture-writer-value", "unit-test", "synthetic:laundered")


class SourceEventTests(ControlFixture):
    def test_placeholder_source_events_are_refused(self):
        self.begin()
        e.prepare_task(self.root, "value")
        dispatch_id = e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:value")["tasks"]["value"]["agent"]["dispatch_id"]
        self.write_value()
        result = worker_result(dispatch_id)
        for event in ("agent_registered", " Dispatched ", "N/A", "todo", dispatch_id, "synthetic:value"):
            with self.subTest(event=event):
                self.assert_code("placeholder_source_event", e.report_task, self.root, "value", "fixture-writer-value", {**result, "source_event": event})
        accepted = e.report_task(self.root, "value", "fixture-writer-value", {**result, "source_event": "unit-test observation of the final message for " + dispatch_id})
        self.assertEqual("REPORTED", accepted["tasks"]["value"]["status"])

    def test_result_observed_before_its_dispatch_is_refused(self):
        self.begin()
        e.prepare_task(self.root, "value")
        with mock.patch.object(e, "now", return_value="2026-09-13T20:48:03.000000+00:00"):
            dispatch_id = e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:value")["tasks"]["value"]["agent"]["dispatch_id"]
        self.write_value()
        result = worker_result(dispatch_id)
        self.assert_code("source_event_predates_dispatch", e.report_task, self.root, "value", "fixture-writer-value", {**result, "source_event": "observation: x 2026-09-13T20:47:50Z"})
        self.assert_code("source_event_predates_dispatch", e.report_task, self.root, "value", "fixture-writer-value", {**result, "source_event": "observed 2026-09-13T22:47:50+02:00"})
        self.assert_code("source_event_predates_dispatch", e.report_task, self.root, "value", "fixture-writer-value", {**result, "source_event": "started 2026-09-13T20:40:00Z, observed 2026-09-13T20:48:02"})
        # A zone-less time is UTC: 20:52:10 is after the 20:48:03Z registration, and the earlier quoted time does not refuse it.
        accepted = e.report_task(self.root, "value", "fixture-writer-value", {**result, "source_event": "dispatch requested 2026-09-13T20:40:00Z; final message observed 2026-09-13T20:52:10"})
        self.assertEqual("REPORTED", accepted["tasks"]["value"]["status"])

    def test_integration_fix_report_checks_its_source_event(self):
        self.integrated()
        with mock.patch.object(e, "now", return_value="2026-09-13T20:48:03.000000+00:00"):
            fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix", "Synthetic same-scope correction.")["integration_fix"]
        result = worker_result(fix["dispatch_id"])
        self.assert_code("placeholder_source_event", e.report_fix, self.root, "fixture-fix-writer", {**result, "source_event": "registered"})
        self.assert_code("source_event_predates_dispatch", e.report_fix, self.root, "fixture-fix-writer", {**result, "source_event": "observation: x 2026-09-13T20:47:50Z"})
        self.assertEqual("REPORTED", e.report_fix(self.root, "fixture-fix-writer", {**result, "source_event": "observation: x 2026-09-13T20:50:00Z"})["integration_fix"]["status"])


class JournalImportTests(ControlFixture):
    def test_workflow_dispatch_imports_journal_result_and_reaches_verified(self):
        run = self.imported()
        agent = run["tasks"]["value"]["agent"]
        self.assertEqual("REPORTED", run["tasks"]["value"]["status"])
        self.assertEqual((f"workflow-agent:{RUN_ID}/{IMPLEMENTER}", f"{RUN_ID}/{IMPLEMENTER}", "claude-code-workflow", "returned"),
                         (agent["actor"], agent["handle"], agent["host"], agent["liveness"]))
        self.assertEqual(agent["journal"], run["tasks"]["value"]["dispatches"][-1]["journal"])
        self.assertEqual(IMPLEMENTER, agent["journal"]["agent_id"])
        self.assertTrue(run["tasks"]["value"]["report"]["source_event"].startswith(f"workflow-journal:{RUN_ID}/{IMPLEMENTER}#"))
        self.assertIn(agent["actor"], {entry["actor"] for entry in run["writer_history"]})
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        before = e.load(self.root)["revision"]
        token = e.review_token(self.root, task_id="value", lens="correctness")
        self.assertEqual((before + 1, before + 1, ["correctness"]), (e.load(self.root)["revision"], token["revision"], token["lenses"]))
        self.assertEqual(token["token"], e.review_token(self.root, task_id="value", lens="correctness")["token"])
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token["token"]))
        verified = e.import_review(self.root, lenses=["correctness"], task_id="value", search_root=self.search)
        self.assertEqual("VERIFIED", verified["tasks"]["value"]["status"])
        receipt = verified["reviews"][-1]
        self.assertEqual(("PASS", "workflow-agent:wf_fixture-0002/a0fixturerev01"), (receipt["verdict"], receipt["actor"]))
        self.assertEqual("correctness", json.loads(receipt["evidence"])["lenses"][0]["lens"])
        self.assertEqual("imported", verified["review_requests"]["task:value"]["status"])
        renewed = e.review_token(self.root, task_id="value", lens="correctness")
        self.assertNotEqual(token["request"], renewed["request"])
        self.assertNotEqual(token["token"], renewed["token"])

    def test_journal_refusals_leave_the_dispatch_pending(self):
        self.begin()
        dispatch_id = self.workflow_dispatch()
        result = worker_result(dispatch_id)
        # Each case is a separate project in the host root and is removed afterwards: every
        # search covers the whole root, so leftover cases would change the next outcome.
        cases = {
            "journal_result_missing": lambda j: j.agent(IMPLEMENTER, worker_result("another-dispatch")),
            "journal_result_ambiguous": lambda j: (j.agent(IMPLEMENTER, result), j.agent("a0fixtureimpl02", result)),
            "journal_order": lambda j: j.agent(IMPLEMENTER, result, result_first=True),
            "journal_transcript_missing": lambda j: j.agent(IMPLEMENTER, result, transcript=False),
        }
        for code, write in cases.items():
            with self.subTest(code=code):
                journal = Journal(self.search, project="case-" + code)
                write(journal)
                self.assert_code(code, e.import_task_report, self.root, "value")
                shutil.rmtree(journal.project)
        with self.subTest(code="journal_symlink"):
            target = Journal(self.search / "deeper" / "symlink-target")
            target.agent(IMPLEMENTER, result)
            linked = Journal(self.search, project="case-symlink")
            linked.path.unlink()
            linked.path.symlink_to(target.path)
            (linked.dir / f"agent-{IMPLEMENTER}.jsonl").write_text("{}\n")
            self.assert_code("journal_result_missing", e.import_task_report, self.root, "value", search_root=self.search)
            self.assert_code("journal_symlink", e.import_task_report, self.root, "value", journal=linked.path)
            shutil.rmtree(linked.project)
        with self.subTest(code="named_journal_is_not_the_only_result"):
            named = Journal(self.search, project="case-named")
            named.agent(IMPLEMENTER, result)
            Journal(self.search, project="case-other").agent("a0fixtureimpl03", result)
            self.assert_code("journal_result_ambiguous", e.import_task_report, self.root, "value", journal=named.path)
        current = e.load(self.root)["tasks"]["value"]
        self.assertEqual(("DISPATCHED", "workflow-pending"), (current["status"], current["agent"]["actor"]))

    def test_journals_outside_the_host_root_or_behind_a_symlink_are_refused(self):
        self.begin()
        result = worker_result(self.workflow_dispatch())
        outside = Journal(self.home / "outside")
        outside.agent(IMPLEMENTER, result)
        self.assert_code("journal_outside_host", e.import_task_report, self.root, "value", search_root=self.home / "outside")
        self.assert_code("journal_outside_host", e.import_task_report, self.root, "value", journal=outside.path)
        self.assert_code("search_root_mismatch", e.import_task_report, self.root, "value", search_root=self.search / "cases")
        deep = Journal(self.search / "cases" / "deep")
        deep.agent(IMPLEMENTER, result)
        self.assert_code("journal_layout", e.import_task_report, self.root, "value", journal=deep.path)
        real = Journal(self.search, project="real-project")
        real.agent(IMPLEMENTER, result)
        (self.search / "linked-project").symlink_to(real.dir.parents[3], target_is_directory=True)
        aliased = self.search / "linked-project" / "fixture-session" / "subagents" / "workflows" / RUN_ID / "journal.jsonl"
        self.assert_code("journal_symlink", e.import_task_report, self.root, "value", journal=aliased)
        run = e.import_task_report(self.root, "value", journal=real.path)
        self.assertEqual((str(self.search), str(real.path)), (run["tasks"]["value"]["agent"]["journal"]["host_root"], run["tasks"]["value"]["agent"]["journal"]["path"]))

    def test_run_records_its_host_root_and_imports_refuse_a_changed_one(self):
        self.assertEqual((str(self.search), False), tuple(e.status(self.root)[key] for key in ("workflow_host_root", "workflow_host_root_default")))
        with mock.patch.dict(os.environ):
            os.environ.pop("DELIVERY_WORKFLOW_HOST_ROOT")
            other = e.create(str(self.repo), "default-host", "A run created with the default host root.", store=str(self.home / "runs"))
        self.assertEqual(str(Path("~/.claude/projects").expanduser().resolve()), other["workflow_host_root"])
        self.assertIs(True, e.status(other["root"])["workflow_host_root_default"])
        self.begin()
        dispatch_id = self.workflow_dispatch()
        Journal(self.search).agent(IMPLEMENTER, worker_result(dispatch_id))
        moved = self.home / "another-host"
        moved.mkdir()
        with mock.patch.dict(os.environ, {"DELIVERY_WORKFLOW_HOST_ROOT": str(moved)}):
            self.assert_code("host_root_changed", e.import_task_report, self.root, "value")
            self.assert_code("host_root_changed", e.import_review, self.root, lenses=["correctness"], task_id="value")
        self.assertEqual("REPORTED", e.import_task_report(self.root, "value")["tasks"]["value"]["status"])

    def test_imported_tree_must_match_the_staged_worktree(self):
        self.begin()
        dispatch_id = self.workflow_dispatch()
        tree = git(Path(e.load(self.root)["tasks"]["value"]["path"]), "write-tree")
        stale = Journal(self.search, project="stale-project")
        stale.agent(IMPLEMENTER, {**worker_result(dispatch_id), "tree": "0" * 40})
        self.assert_code("import_tree_mismatch", e.import_task_report, self.root, "value", journal=stale.path)
        shutil.rmtree(stale.project)
        current = Journal(self.search, project="current-project")
        current.agent(IMPLEMENTER, {**worker_result(dispatch_id), "tree": tree})
        run = e.import_task_report(self.root, "value", journal=current.path)
        self.assertEqual(("REPORTED", tree), (run["tasks"]["value"]["status"], run["tasks"]["value"]["report"]["tree"]))

    def test_imported_journal_event_cannot_be_reused_by_a_manual_dispatch(self):
        imported = self.imported()["tasks"]["value"]["report"]
        e.rework(self.root, "value", "Synthetic finding: collect a fresh result.")
        e.register_agent(self.root, "value", "fixture-manual-writer", e.WORKFLOW_HOST, f"{RUN_ID}/{IMPLEMENTER}")
        dispatch_id = e.load(self.root)["tasks"]["value"]["agent"]["dispatch_id"]
        self.assert_code("source_event_reused", e.report_task, self.root, "value", "fixture-manual-writer",
                         {**worker_result(dispatch_id), "source_event": imported["source_event"]})

    def test_workflow_dispatch_takes_identity_only_from_the_journal(self):
        self.begin()
        e.prepare_task(self.root, "value")
        self.assert_code("workflow_identity_supplied", e.register_agent, self.root, "value", "fixture-writer", None, None, via_workflow=True)
        dispatch_id = e.register_agent(self.root, "value", via_workflow=True)["tasks"]["value"]["agent"]["dispatch_id"]
        self.write_value()
        self.assert_code("workflow_report_required", e.report_task, self.root, "value", "workflow-pending",
                         {**worker_result(dispatch_id), "source_event": "coordinator-supplied observation"})
        self.assertIn("--via-workflow", self.cli("task-register", "--run", self.root, "--task", "value", "--via-workflow", "--actor", "x", expected=2))
        self.assertIn("--actor", self.cli("task-register", "--run", self.root, "--task", "value", "--host", "h", "--handle", "x", expected=2))


class ReviewImportTests(ControlFixture):
    def reviewed_ready(self):
        self.imported()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])

    def test_implementer_agent_cannot_review_its_own_task(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        same_run = Journal(self.search)
        same_run.agent(IMPLEMENTER, lens(token))
        refused = self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=same_run.path)
        self.assertIn("implementation actor", refused.message)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])

    def test_implementer_agent_id_under_another_workflow_run_cannot_review(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        other_run = Journal(self.search, "wf_fixture-0002")
        other_run.agent(IMPLEMENTER, lens(token))
        refused = self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=other_run.path)
        self.assertIn("implementation actor", refused.message)

    def test_two_lenses_from_one_reviewer_agent_are_refused(self):
        self.reviewed_ready()
        tokens = [e.review_token(self.root, task_id="value", lens=name)["token"] for name in ("correctness", "security")]
        shared = Journal(self.search, "wf_fixture-0002")
        for token in tokens:
            shared.agent("a0fixturerev01", lens(token))
        self.assert_code("reviewer_not_independent", e.import_review, self.root, lenses=["correctness", "security"], task_id="value", journal=shared.path)

    def test_one_failing_lens_fails_the_review_and_records_finding_keys(self):
        self.reviewed_ready()
        tokens = {name: e.review_token(self.root, task_id="value", lens=name)["token"] for name in ("correctness", "security")}
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens(tokens["correctness"]))
        defects = [{"file": " Value.txt", "line": 1, "kind": "Wrong-Value", "severity": "high", "summary": "Synthetic defect.", "scenario": "Read the file."},
                   {"file": "value.txt", "kind": "wrong-value"}, {"kind": "missing-test"}]
        journal.agent("a0fixturerev02", lens(tokens["security"], "FAIL", "The value file ends without the planned newline.", defects))
        run = e.import_review(self.root, lenses=["correctness", "security"], task_id="value", search_root=self.search)
        receipt = run["reviews"][-1]
        keys = sorted(["value.txt|wrong-value", "|missing-test"])
        self.assertEqual(("FAIL", "code-fix", keys), (receipt["verdict"], receipt["decision"], receipt["finding_keys"]))
        self.assertEqual(["PASS", "FAIL"], [item["verdict"] for item in json.loads(receipt["evidence"])["lenses"]])
        self.assertEqual("workflow-agent:wf_fixture-0002/a0fixturerev01+workflow-agent:wf_fixture-0002/a0fixturerev02", receipt["actor"])
        owner = run["tasks"]["value"]
        self.assertEqual(("REWORK", 1, keys), (owner["status"], owner["rework_rounds"], owner["rework_history"][-1]["finding_keys"]))

    def test_review_import_needs_a_result_and_counts_every_verdict_of_one_agent(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens("review-" + "0" * 32))
        self.assert_code("journal_result_missing", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        journal.agent("a0fixturerev01", lens(token))
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev01", lens(token, "FAIL", "A second return of the same agent found the newline missing."))
        run = e.import_review(self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        self.assertEqual(("FAIL", 2, "REWORK"), (run["reviews"][-1]["verdict"], len(run["reviews"][-1]["verdicts"]), run["tasks"]["value"]["status"]))

    def test_duplicate_verdicts_from_distinct_agents_are_combined_to_the_worst(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token))
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, "FAIL", "A rerun of the same lens found the newline missing.",
                                                                              [{"file": "value.txt", "kind": "behavior"}]))
        run = e.import_review(self.root, lenses=["correctness"], task_id="value")
        receipt = run["reviews"][-1]
        self.assertEqual(("FAIL", ["value.txt|behavior"], "REWORK"), (receipt["verdict"], receipt["finding_keys"], run["tasks"]["value"]["status"]))
        self.assertEqual({("correctness", "PASS"), ("correctness", "FAIL")}, {(item["lens"], item["verdict"]) for item in receipt["verdicts"]})
        self.assertEqual(["workflow-agent:wf_fixture-0002/a0fixturerev01", "workflow-agent:wf_fixture-0003/a0fixturerev02"], sorted(receipt["reviewers"]))

    def test_a_writer_verdict_is_refused_and_the_independent_one_counts(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token))
        Journal(self.search, "wf_fixture-0003").agent(IMPLEMENTER, lens(token, "FAIL", "The implementer judging its own change."))
        run = e.import_review(self.root, lenses=["correctness"], task_id="value")
        receipt = run["reviews"][-1]
        self.assertEqual(("PASS", "VERIFIED", ["workflow-agent:wf_fixture-0002/a0fixturerev01"]), (receipt["verdict"], run["tasks"]["value"]["status"], receipt["reviewers"]))
        self.assertEqual([(f"workflow-agent:wf_fixture-0003/{IMPLEMENTER}", "writer_verdict")], [(item["actor"], item["code"]) for item in receipt["refused"]])

    def test_superseding_an_open_request_keeps_every_lens(self):
        self.reviewed_ready()
        path = Path(e.load(self.root)["tasks"]["value"]["path"])
        first = e.review_token(self.root, task_id="value", lens="conformance")
        (path / "scratch.txt").write_text("changes the content only while the next token is issued\n")
        during = e.review_token(self.root, task_id="value", lens="adversary")
        (path / "scratch.txt").unlink()
        after = e.review_token(self.root, task_id="value", lens="adversary")
        self.assertEqual((["adversary", "conformance"], ["adversary", "conformance"]), (during["lenses"], after["lenses"]))
        self.assertEqual(first["content"], after["content"])
        self.assertNotEqual(first["request"], after["request"])
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(after["token"]))
        self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["adversary"], task_id="value")

    def test_typed_review_is_refused_while_review_tokens_are_open(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        e.review_token(self.root, task_id="value", lens="conformance")
        self.assert_code("workflow_review_required", e.review, self.root, "fixture-reviewer", "PASS", PASS_EVIDENCE, task_id="value")
        e.revise(self.root, "Synthetic revision: start over to test the integrated target.")
        self.integrated()
        e.review_token(self.root, lens="integrated")
        self.assert_code("workflow_review_required", e.review, self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic typed finding.")

    def test_review_import_requires_every_issued_lens_of_the_open_request(self):
        self.reviewed_ready()
        self.assert_code("review_not_requested", e.import_review, self.root, lenses=["correctness"], task_id="value", search_root=self.search)
        first = e.review_token(self.root, task_id="value", lens="correctness")
        second = e.review_token(self.root, task_id="value", lens="security")
        self.assertEqual((first["request"], ["correctness", "security"]), (second["request"], second["lenses"]))
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens(first["token"]))
        journal.agent("a0fixturerev02", lens(second["token"], "FAIL", "The staged value misses the planned newline."))
        self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["correctness"], task_id="value", search_root=self.search)
        self.assert_code("review_not_requested", e.import_review, self.root, lenses=["correctness", "security", "style"], task_id="value", search_root=self.search)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])
        run = e.import_review(self.root, lenses=["security", "correctness"], task_id="value", search_root=self.search)
        self.assertEqual(("FAIL", "REWORK", "imported"), (run["reviews"][-1]["verdict"], run["tasks"]["value"]["status"], run["review_requests"]["task:value"]["status"]))

    def test_manual_review_of_a_workflow_task_is_refused(self):
        self.reviewed_ready()
        for verdict, evidence in (("PASS", PASS_EVIDENCE), ("FAIL", "Synthetic manual finding about the value.")):
            with self.subTest(verdict=verdict):
                self.assert_code("workflow_review_required", e.review, self.root, "fixture-reviewer", verdict, evidence, task_id="value")
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])

    def test_lens_pass_with_one_line_evidence_is_refused_and_a_correct_rerun_imports(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens(token, evidence="PASS: looks correct."))
        refused = self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        self.assertIn("A PASS needs concrete evidence", refused.message)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token))
        run = e.import_review(self.root, lenses=["correctness"], task_id="value")
        self.assertEqual("VERIFIED", run["tasks"]["value"]["status"])
        self.assertEqual([("correctness", "workflow-agent:wf_fixture-0002/a0fixturerev01", "PASS")],
                         [(item["lens"], item["actor"], item["verdict"]) for item in run["reviews"][-1]["refused"]])

    def test_a_malformed_fail_always_counts_and_a_malformed_pass_is_refused(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", {"review_token": token, "verdict": "PASS", "evidence": PASS_EVIDENCE, "defects": "none"})
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", {"review_token": token, "verdict": "fail", "evidence": "", "defects": [{"file": 3}]})
        run = e.import_review(self.root, lenses=["correctness"], task_id="value")
        receipt = run["reviews"][-1]
        self.assertEqual(("FAIL", ["(review)|other"], "REWORK"), (receipt["verdict"], receipt["finding_keys"], run["tasks"]["value"]["status"]))
        self.assertEqual([["evidence", "defects"]], [item.get("normalized") for item in receipt["verdicts"]])
        self.assertEqual(["workflow-agent:wf_fixture-0002/a0fixturerev01"], [item["actor"] for item in receipt["refused"]])

    def test_supersession_proceeds_after_harvesting_and_the_harvested_fail_still_counts(self):
        self.reviewed_ready()
        path = Path(e.load(self.root)["tasks"]["value"]["path"])
        first = e.review_token(self.root, task_id="value", lens="conformance")
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(first["token"], "FAIL", "The staged value misses its newline."))
        (path / "scratch.txt").write_text("changes the content so a new request supersedes the open one\n")
        during = e.review_token(self.root, task_id="value", lens="adversary")
        self.assertEqual(["adversary", "conformance"], during["lenses"])
        harvested = [item for item in e.load(self.root)["harvested_reviews"] if item["token"] == first["token"]]
        self.assertEqual([("FAIL", first["content"])], [(item["verdict"], item["snapshot"]["content"]) for item in harvested])
        (path / "scratch.txt").unlink()
        after = {name: e.review_token(self.root, task_id="value", lens=name)["token"] for name in ("conformance", "adversary")}
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(after["conformance"]))
        Journal(self.search, "wf_fixture-0004").agent("a0fixturerev03", lens(after["adversary"]))
        self.assert_code("review_reroll", e.import_review, self.root, lenses=["conformance", "adversary"], task_id="value")

    def test_refused_and_writer_verdicts_are_recorded_as_refused_when_harvested(self):
        self.reviewed_ready()
        path = Path(e.load(self.root)["tasks"]["value"]["path"])
        token = e.review_token(self.root, task_id="value", lens="conformance")["token"]
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token, evidence="PASS: fine."))
        Journal(self.search, "wf_fixture-0003").agent(IMPLEMENTER, lens(token, "FAIL", "The implementer reviewing itself."))
        (path / "scratch.txt").write_text("changes the content\n")
        issued = e.review_token(self.root, task_id="value", lens="adversary")
        self.assertEqual(["adversary", "conformance"], issued["lenses"])
        run = e.load(self.root)
        self.assertEqual([], [item for item in run.get("harvested_reviews", []) if item["token"] == token])
        self.assertEqual({("workflow-agent:wf_fixture-0002/a0fixturerev01", "review_evidence_insufficient"), (f"workflow-agent:wf_fixture-0003/{IMPLEMENTER}", "writer_verdict")},
                         {(item["actor"], item["code"]) for item in run["refused_review_events"].values()})



class TriageTests(ControlFixture):
    def test_test_plan_and_environment_rounds_do_not_consume_rework_budget(self):
        self.begin()
        self.implement()
        e.rework(self.root, "value", "Synthetic finding: the gate read a missing fixture file.", decision="test-plan")
        self.redispatch("synthetic:1")
        e.rework(self.root, "value", "Synthetic finding: the fixture device was offline.", decision="environment")
        owner = e.load(self.root)["tasks"]["value"]
        self.assertEqual((0, 2, "IMPLEMENTING"), (owner.get("rework_rounds", 0), owner["support_rounds"], e.load(self.root)["state"]))
        for index in range(2):
            self.redispatch(f"synthetic:code-{index}")
            e.rework(self.root, "value", f"Synthetic code finding {index}.")
        self.assertEqual(("IMPLEMENTING", 2, 2), (e.status(self.root)["state"], e.status(self.root)["tasks"]["value"]["rework_rounds"], e.status(self.root)["tasks"]["value"]["support_rounds"]))
        self.redispatch("synthetic:3")
        blocked = e.rework(self.root, "value", "Synthetic third environment problem.", decision="environment")
        self.assertEqual(("BLOCKED", "support_budget"), (blocked["state"], blocked["blocker"]["code"]))
        self.assertIn("Two test-plan or environment rounds are exhausted", blocked["blocker"]["reason"])
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: the environment looks fine now.")

    def test_budget_blocks_resume_only_after_rework_budget_authority(self):
        self.begin()
        self.implement()
        for index in range(3):
            if index:
                self.redispatch(f"synthetic:{index}")
            e.rework(self.root, "value", f"Synthetic code finding {index}.")
        blocked = e.load(self.root)
        self.assertEqual(("BLOCKED", "rework_budget", "value"), (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["task"]))
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: try once more.")
        evidence = "Synthetic explicit authority for one more code-fix round of this fixture task."
        self.assert_code("invalid_rework_budget", e.authorize, self.root, "rework-budget", "fixture-user", evidence, task="value", count=3)
        self.assert_code("rework_budget_state", e.authorize, self.root, "rework-budget", "fixture-user", evidence, count=1)
        (self.home / "budget.md").write_text(evidence + "\n")
        granted = self.cli("authorize", "--run", self.root, "--scope", "rework-budget", "--task", "value", "--count", "1", "--actor", "fixture-user", "--evidence-file", self.home / "budget.md")
        self.assertEqual(("code-fix", 1, blocked["blocker"]["at"]), tuple(granted["rework_budget_grants"][-1][key] for key in ("kind", "count", "blocker_at")))
        self.assertEqual("IMPLEMENTING", e.resume(self.root, "Synthetic: explicit authority for one more round.")["state"])
        self.assertEqual(3, e.rework_limits(e.load(self.root), "value")["code-fix"])
        self.redispatch("synthetic:3")
        again = e.rework(self.root, "value", "Synthetic code finding 3.")
        self.assertEqual(("BLOCKED", "rework_budget"), (again["state"], again["blocker"]["code"]))
        self.assertTrue(again["blocker"]["reason"].startswith("Three rework rounds exhausted"))
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: the earlier grant does not cover this block.")

    def test_requirements_and_human_decisions_block(self):
        self.begin()
        self.implement()
        self.assert_code("invalid_decision", e.rework, self.root, "value", "Synthetic finding.", decision="guess")
        blocked = e.rework(self.root, "value", "The acceptance text contradicts the requirement.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding", 0), (blocked["state"], blocked["blocker"]["code"], blocked["tasks"]["value"].get("rework_rounds", 0)))
        self.assertEqual("A requirements finding needs revise and renewed approval, not implementer rework: The acceptance text contradicts the requirement.", blocked["blocker"]["reason"])
        self.assertEqual("requirements", blocked["tasks"]["value"]["rework_history"][-1]["decision"])
        self.assert_code("decision_required", e.resume, self.root, "Synthetic: the fixture user answered the requirement question.")
        self.assertEqual("BLOCKED", e.load(self.root)["state"])
        decision = "Synthetic user decision: keep the requirement as written and continue within the reviewed scope."
        self.assert_code("decision_state", e.authorize, self.root, "decision", "fixture-user", decision, code="human_decision", task="value")
        self.assert_code("decision_state", e.authorize, self.root, "decision", "fixture-user", decision, code="requirements_finding")
        self.assert_code("invalid_decision_grant", e.authorize, self.root, "decision", "fixture-user", decision, task="value")
        (self.home / "decision.md").write_text(decision + "\n")
        granted = self.cli("authorize", "--run", self.root, "--scope", "decision", "--code", "requirements_finding", "--task", "value", "--actor", "fixture-user", "--evidence-file", self.home / "decision.md")
        self.assertEqual(("requirements_finding", "value", blocked["blocker"]["at"], decision), tuple(granted["decision_grants"][-1][key] for key in ("code", "task", "blocker_at", "evidence")))
        self.assertEqual("IMPLEMENTING", e.resume(self.root, "Synthetic: the recorded decision keeps the requirement.")["state"])

    def test_human_decision_resumes_only_with_a_decision_grant_for_that_block(self):
        self.begin()
        self.implement()
        blocked = e.rework(self.root, "value", "Keep or drop the legacy value.", decision="human")
        self.assertEqual(("BLOCKED", "human_decision", "A human decision is required: Keep or drop the legacy value."),
                         (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["reason"]))
        self.assertEqual("human_decision", e.status(self.root)["blocker_code"])
        self.assert_code("decision_required", e.resume, self.root, "Synthetic: any text used to be enough.")
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: keep the legacy value.", code="human_decision", task="value")
        e.resume(self.root, "Synthetic: the fixture user decided to keep the legacy value.")
        self.redispatch("synthetic:after-decision")
        e.rework(self.root, "value", "Keep the legacy value in the new format too?", decision="human")
        self.assert_code("decision_required", e.resume, self.root, "Synthetic: the earlier decision does not answer this block.")

    def test_journal_identity_prefixes_are_reserved_for_imports(self):
        self.begin()
        e.prepare_task(self.root, "value")
        for actor in ("workflow-agent:wf_x-1/a0typed", "workflow-pending"):
            with self.subTest(actor=actor):
                self.assert_code("reserved_actor", e.register_agent, self.root, "value", actor, "unit-test", "synthetic:typed")
        self.assertEqual("PREPARED", e.load(self.root)["tasks"]["value"]["status"])
        self.assert_code("reserved_actor", e.review, self.root, "workflow-agent:wf_x-1/a0typed", "PASS", PASS_EVIDENCE, task_id="value")

    def test_repeated_finding_key_blocks_as_non_converging(self):
        self.begin()
        self.implement()
        first = e.rework(self.root, "value", "Synthetic finding: wrong value.", finding_keys=["value.txt|wrong-value"])
        self.assertEqual("IMPLEMENTING", first["state"])
        self.redispatch("synthetic:second")
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        blocked = e.review(self.root, "fixture-reviewer", "FAIL", "Synthetic finding: the value is still wrong.", task_id="value",
                           finding_keys=[" Value.txt|Wrong-Value ", "value.txt|missing-newline"])
        self.assertEqual(("BLOCKED", "non_converging", 2), (blocked["state"], blocked["blocker"]["code"], blocked["tasks"]["value"]["rework_rounds"]))
        self.assertIn("(value.txt|wrong-value)", blocked["blocker"]["reason"])
        self.assertEqual(["value.txt|missing-newline", "value.txt|wrong-value"], blocked["reviews"][-1]["finding_keys"])
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: try the same fix again.")


class CollectionTests(ControlFixture):
    def test_dispatches_are_collected_while_blocked_and_revise_waits_for_manual_ones(self):
        self.begin(multi_plan("other", "third"))
        self.implement()
        e.prepare_task(self.root, "other")
        e.register_agent(self.root, "other", "fixture-writer-other", "unit-test", "synthetic:other")
        self.write_value("other", "other\n", "other.txt")
        e.prepare_task(self.root, "third")
        third = e.register_agent(self.root, "third", via_workflow=True)["tasks"]["third"]["agent"]["dispatch_id"]
        self.write_value("third", "third\n", "third.txt")
        blocked = e.rework(self.root, "value", "The acceptance text contradicts the requirement.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding"), (blocked["state"], blocked["blocker"]["code"]))
        self.assert_code("wrong_state", e.register_agent, self.root, "value", "fixture-writer-value", "unit-test", "synthetic:while-blocked")
        self.assert_code("active_task", e.revise, self.root, "Synthetic revision while a manual dispatch is live.")
        self.report("other")
        Journal(self.search).agent("a0fixturethird1", worker_result(third))
        collected = e.import_task_report(self.root, "third", search_root=self.search)
        self.assertEqual(("BLOCKED", "REPORTED", "REPORTED"), (collected["state"], collected["tasks"]["other"]["status"], collected["tasks"]["third"]["status"]))
        revised = e.revise(self.root, "The requirement is rewritten before any further work.")
        self.assertEqual(("DISCOVERY", None, []), (revised["state"], revised["blocker"], revised["previous_versions"][-1]["open_workflow_dispatches"]))


class AbandonTests(ControlFixture):
    def test_abandoned_workflow_dispatch_records_the_journal_and_becomes_the_rework_baseline(self):
        self.begin()
        old = Journal(self.search, "wf_fixture-old")
        old.failed("a0fixtureold01", "implement:value")
        os.utime(old.path, (1_000_000_000, 1_000_000_000))
        dispatch_id = self.workflow_dispatch()
        Journal(self.search).failed("a0fixturefail01", "implement:value")
        Journal(self.search).failed("a0fixtureother1", "implement:other")
        run = e.abandon_task(self.root, "value", "The workflow agent failed and returned no result.")
        owner = run["tasks"]["value"]
        entry = owner["rework_history"][-1]
        self.assertEqual(("REWORK", 1, "abandoned", "abandon", dispatch_id), (owner["status"], owner["support_rounds"], owner["agent"]["liveness"], entry["decision"], entry["abandoned_dispatch"]))
        self.assertEqual((["a0fixturefail01"], False, str(self.search)), ([item["agent_id"] for item in entry["journal"]["failed"]], entry["journal"]["result_present"], entry["journal"]["host_root"]))
        self.assertIn(f"workflow-agent:{RUN_ID}/a0fixturefail01", {item["actor"] for item in run["writer_history"]})
        path = Path(owner["path"])
        self.assertEqual("after\n", (path / "value.txt").read_text())
        self.assertEqual(entry["baseline"]["content"], e.snapshot(run, "value")["content"])
        (path / "value.txt").write_text("written after the abandonment\n")
        self.assert_code("pre_dispatch_changes", e.register_agent, self.root, "value", via_workflow=True)
        (path / "value.txt").write_text("after\n")
        self.assertEqual("DISPATCHED", e.register_agent(self.root, "value", via_workflow=True)["tasks"]["value"]["status"])

    def test_abandon_counts_support_rounds_works_while_blocked_and_stacks_blocks(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:0")
        first = self.cli("task-abandon", "--run", self.root, "--task", "value", "--reason", "The worker session ended without a result.")
        self.assertEqual(("REWORK", 1, None), (first["tasks"]["value"]["status"], first["tasks"]["value"]["support_rounds"], first["tasks"]["value"]["rework_history"][-1]["journal"]))
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:1")
        e.abandon_task(self.root, "value", "The worker lost its sandbox.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:2")
        e.block(self.root, "Synthetic outage of the fixture host.")
        stacked = e.abandon_task(self.root, "value", "The worker did not survive the outage.")
        self.assertEqual(("BLOCKED", "support_budget", 3, None), (stacked["state"], stacked["blocker"]["code"], stacked["tasks"]["value"]["support_rounds"], stacked["blocker"]["previous"].get("code")))
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: try again.")
        e.authorize(self.root, "rework-budget", "fixture-user", "Synthetic authority for one more support round of this task.", task="value", count=1)
        self.assertEqual(("BLOCKED", None), (lambda run: (run["state"], run["blocker"].get("code")))(e.resume(self.root, "Synthetic: one more round is authorized.")))
        self.assertEqual("IMPLEMENTING", e.resume(self.root, "Synthetic: the outage is over.")["state"])
        self.assert_code("task_state", e.abandon_task, self.root, "value", "Nothing is dispatched now.")

    def test_abandoning_a_committed_dispatch_invalidates_the_attempt_and_a_new_attempt_finishes(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:committer")
        old = self.write_value()
        git(old, "commit", "-m", "A worker commit the contract forbids")
        committed = git(old, "rev-parse", "HEAD")
        run = e.abandon_task(self.root, "value", "The worker committed instead of returning a staged patch.")
        owner = run["tasks"]["value"]
        self.assertEqual(("INVALIDATED", 1, committed, owner["branch"]), (owner["status"], owner["support_rounds"], owner["invalidated_by"]["moved"]["head"], owner["invalidated_by"]["moved"]["branch"]))
        self.assertEqual(committed, git(old, "rev-parse", "HEAD"))
        fresh = e.prepare_task(self.root, "value")["tasks"]["value"]
        self.assertEqual((2, 1, "PREPARED"), (fresh["attempt"], fresh["support_rounds"], fresh["status"]))
        self.assertIn(str(old), {attempt["path"] for attempt in fresh["previous_attempts"]})
        self.assertEqual("before\n", (Path(fresh["path"]) / "value.txt").read_text())
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:second-attempt")
        self.write_value()
        self.report()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", PASS_EVIDENCE)
        self.assertEqual("READY_TO_PUBLISH", e.ready(self.root)["state"])

    def test_abandoning_a_dispatch_that_switched_branches_records_the_branch(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", via_workflow=True)
        path = Path(e.load(self.root)["tasks"]["value"]["path"])
        git(path, "switch", "-c", "worker-own-branch")
        moved = e.abandon_task(self.root, "value", "The worker switched to its own branch.")["tasks"]["value"]["invalidated_by"]["moved"]
        self.assertEqual(("worker-own-branch", git(path, "rev-parse", "HEAD")), (moved["branch"], moved["head"]))

    def test_abandoning_a_dispatch_whose_worktree_is_gone_starts_a_new_attempt(self):
        self.begin()
        e.prepare_task(self.root, "value")
        owner = e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:vanished")["tasks"]["value"]
        shutil.rmtree(owner["path"])
        run = e.abandon_task(self.root, "value", "The worktree was deleted while the worker ran.")
        gone = run["tasks"]["value"]["invalidated_by"]["missing_worktree"]
        self.assertEqual(("INVALIDATED", 1, owner["path"], owner["branch"], owner["base_sha"]),
                         (run["tasks"]["value"]["status"], run["tasks"]["value"]["support_rounds"], gone["path"], gone["branch"], gone["branch_head"]))
        self.assertTrue(Path(owner["ownership_receipt"]).is_file())
        self.assertEqual(owner["base_sha"], git(self.repo, "rev-parse", "refs/heads/" + owner["branch"]))
        self.assertEqual((2, 1), (lambda fresh: (fresh["attempt"], fresh["support_rounds"]))(e.prepare_task(self.root, "value")["tasks"]["value"]))
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:after-vanished")
        self.write_value()
        self.report()
        self.verify()
        self.assertEqual("VERIFIED", e.load(self.root)["tasks"]["value"]["status"])

    def test_abandoning_a_workflow_dispatch_whose_worktree_is_gone_invalidates_it(self):
        self.begin()
        e.prepare_task(self.root, "value")
        path = e.register_agent(self.root, "value", via_workflow=True)["tasks"]["value"]["path"]
        shutil.rmtree(path)
        run = e.abandon_task(self.root, "value", "The workflow worktree disappeared.")
        self.assertEqual(("INVALIDATED", path, False), (run["tasks"]["value"]["status"], run["tasks"]["value"]["invalidated_by"]["missing_worktree"]["path"],
                                                        run["tasks"]["value"]["rework_history"][-1]["journal"]["result_present"]))

    def test_revise_archives_open_workflow_dispatches_but_not_manual_ones(self):
        self.begin(multi_plan("other"))
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", via_workflow=True)
        e.prepare_task(self.root, "other")
        e.register_agent(self.root, "other", "fixture-writer-other", "unit-test", "synthetic:other")
        self.assert_code("active_task", e.revise, self.root, "Synthetic revision with a live manual worker.")
        e.abandon_task(self.root, "other", "Synthetic: the manual worker was stopped before the revision.")
        revised = e.revise(self.root, "Synthetic revision; the workflow dispatch is archived.")
        self.assertEqual(("DISCOVERY", ["value"], "DISPATCHED"), (revised["state"], revised["previous_versions"][-1]["open_workflow_dispatches"], revised["previous_versions"][-1]["tasks"]["value"]["status"]))


class IntegratedDecisionTests(ControlFixture):
    def test_integrated_requirements_and_human_decisions_block(self):
        self.integrated()
        human = e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "The integrated value needs a product decision.", decision="human")
        self.assertEqual(("BLOCKED", "human_decision", None, "READY_TO_PUBLISH"), (human["state"], human["blocker"]["code"], human["blocker"]["task"], human["blocker"]["from"]))
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision about the integrated value.", code="human_decision")
        self.assertEqual("READY_TO_PUBLISH", e.resume(self.root, "Synthetic: the fixture user decided.")["state"])
        requirements = e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "The requirement contradicts the verification case.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding"), (requirements["state"], requirements["blocker"]["code"]))
        self.assert_code("decision_required", e.resume, self.root, "Synthetic: resume needs the user's decision.")

    def test_repeated_integrated_finding_blocks_as_non_converging(self):
        self.integrated()
        first = e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding: the release marker is missing.", finding_keys=["value.txt|missing-marker"])
        self.assertEqual(("READY_TO_PUBLISH", first["spec_version"]), (first["state"], first["reviews"][-1]["spec_version"]))
        fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-1", "Add the release marker.")["integration_fix"]
        e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": "synthetic fix result for " + fix["handle"] + " observed"})
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        blocked = e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "Synthetic: the release marker is still missing.", finding_keys=["value.txt|missing-marker", "value.txt|other"])
        self.assertEqual(("BLOCKED", "non_converging", None), (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["task"]))
        self.assertIn("(value.txt|missing-marker)", blocked["blocker"]["reason"])
        self.assert_code("rework_budget_required", e.resume, self.root, "Synthetic: try the same fix again.")
        self.assert_code("rework_budget_state", e.authorize, self.root, "rework-budget", "fixture-user", "Synthetic authority naming the wrong target.", task="value", count=1)
        e.authorize(self.root, "rework-budget", "fixture-user", "Synthetic authority for one more integration-fix round.", count=1)
        self.assertEqual(("VERIFYING", {"used": 1, "limit": 3}), (lambda run: (run["state"], e.fix_budget(run)))(e.resume(self.root, "Synthetic: one more fix round is authorized.")))

    def test_integrated_finding_of_an_earlier_plan_version_does_not_count(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding: the release marker is missing.", finding_keys=["value.txt|missing-marker"])
        fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-1", "Add the release marker.")["integration_fix"]
        e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": "synthetic fix result for " + fix["handle"] + " observed"})
        e.revise(self.root, "Synthetic revision: the marker requirement moves into the plan.")
        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        again = e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "Synthetic: the marker is missing in the new version too.", finding_keys=["value.txt|missing-marker"])
        self.assertEqual(("VERIFYING", 2), (again["state"], again["reviews"][-1]["spec_version"]))


class RerollTests(ControlFixture):
    def test_integrated_pass_after_a_fail_on_the_same_content_needs_an_override(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding: the release marker is missing.", finding_keys=["value.txt|missing-marker"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)
        self.assert_code("decision_state", e.authorize, self.root, "decision", "fixture-user", "Synthetic override for a task without a FAIL.", code="review_override", task="value")
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the marker is not part of this change, so the FAIL is wrong.", code="review_override")
        self.assertEqual("PASS", e.review(self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)["reviews"][-1]["verdict"])

    def test_integrated_pass_needs_content_changed_by_an_integration_fix(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding: the file mode is wrong.", finding_keys=["value.txt|mode"])
        path = Path(e.load(self.root)["integration"]["path"])
        for attempt, change in ((1, False), (2, True)):
            fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", f"synthetic:fix-{attempt}", "Correct the file mode.")["integration_fix"]
            if change:
                (path / "value.txt").chmod(0o755)
                git(path, "add", "--", "value.txt")
            e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": f"synthetic fix result {attempt} for {fix['handle']}"})
            self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
            if not change:
                self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)
        self.assertEqual("PASS", e.review(self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)["reviews"][-1]["verdict"])

    def test_task_pass_after_a_fail_on_the_same_content_in_the_attempt_needs_an_override(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        e.review(self.root, "fixture-reviewer", "FAIL", "Synthetic finding: the value lacks a trailing marker.", task_id="value")
        self.redispatch("synthetic:same-content")
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-reviewer-2", "PASS", PASS_EVIDENCE, task_id="value")
        (self.home / "override.md").write_text("Synthetic user decision: the marker is out of scope, so that FAIL is wrong.\n")
        grant = self.cli("authorize", "--run", self.root, "--scope", "decision", "--code", "review_override", "--task", "value",
                         "--actor", "fixture-user", "--evidence-file", self.home / "override.md")["decision_grants"][-1]
        self.assertEqual(("review_override", "value"), (grant["code"], grant["task"]))
        self.assertEqual("VERIFIED", e.review(self.root, "fixture-reviewer-2", "PASS", PASS_EVIDENCE, task_id="value")["tasks"]["value"]["status"])


    def test_an_override_covers_only_the_content_it_names(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding on the first content.", finding_keys=["value.txt|mode"])
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision about the first content only.", code="review_override")
        fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-mode", "Correct the file mode.")["integration_fix"]
        path = Path(e.load(self.root)["integration"]["path"])
        (path / "value.txt").chmod(0o755)
        git(path, "add", "--", "value.txt")
        e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": "synthetic fix result for " + fix["handle"]})
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "Synthetic integrated finding on the changed content.", finding_keys=["value.txt|other"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-4", "PASS", PASS_EVIDENCE)

    def test_a_new_attempt_with_identical_content_is_still_guarded(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        e.review(self.root, "fixture-reviewer", "FAIL", "Synthetic finding in the first attempt.", task_id="value")
        vanished = e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:vanishing")["tasks"]["value"]["path"]
        shutil.rmtree(vanished)
        e.abandon_task(self.root, "value", "The first attempt's worktree was deleted.")
        self.assertEqual(2, e.prepare_task(self.root, "value")["tasks"]["value"]["attempt"])
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:second-attempt")
        self.write_value()
        self.report()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-reviewer-2", "PASS", PASS_EVIDENCE, task_id="value")

    def test_a_fail_of_an_earlier_plan_version_guards_identical_content(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic integrated finding in the first plan version.", finding_keys=["value.txt|mode"])
        e.revise(self.root, "Synthetic revision after the integrated FAIL.")
        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)

    def test_an_override_covers_only_fails_recorded_before_it(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic first integrated finding.", finding_keys=["value.txt|mode"])
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the first FAIL is wrong.", code="review_override")
        e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "Synthetic second integrated finding on the same content.", finding_keys=["value.txt|other"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-4", "PASS", PASS_EVIDENCE)
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the second FAIL is wrong too.", code="review_override")
        self.assertEqual("PASS", e.review(self.root, "fixture-integration-reviewer-4", "PASS", PASS_EVIDENCE)["reviews"][-1]["verdict"])

    def test_an_environment_labelled_integrated_fail_guards_a_pass_on_the_same_content(self):
        self.integrated()
        e.review(self.root, "fixture-integration-reviewer-2", "FAIL", "Synthetic finding: the fixture device was offline.", decision="environment")
        self.assert_code("review_reroll", e.review, self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the device outage caused that FAIL, not the change.", code="review_override")
        self.assertEqual("PASS", e.review(self.root, "fixture-integration-reviewer-3", "PASS", PASS_EVIDENCE)["reviews"][-1]["verdict"])

    def test_a_test_plan_labelled_task_fail_guards_a_pass_on_the_same_content(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        e.review(self.root, "fixture-reviewer", "FAIL", "Synthetic finding: the gate read a missing fixture file.", task_id="value", decision="test-plan")
        self.redispatch("synthetic:after-test-plan")
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-reviewer-2", "PASS", PASS_EVIDENCE, task_id="value")


class HarvestTests(ControlFixture):
    """Every verdict returned for an issued token is harvested at the next command and counted."""

    def returned(self, verdict="PASS", evidence=PASS_EVIDENCE, defects=None, run_id="wf_fixture-0002", agent="a0fixturerev01", transcript=True):
        self.imported()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        token = e.review_token(self.root, task_id="value", lens="conformance")["token"]
        journal = Journal(self.search, run_id)
        journal.agent(agent, lens(token, verdict, evidence, defects), transcript=transcript)
        return token, journal

    def harvested(self, token):
        return [item for item in e.load(self.root).get("harvested_reviews", []) if item["token"] == token]

    def test_transitions_proceed_with_tokens_outstanding_and_harvest_their_verdicts(self):
        token, _ = self.returned()
        self.assertEqual("REWORK", e.rework(self.root, "value", "Synthetic finding raised outside the review.")["tasks"]["value"]["status"])
        self.assertEqual([("PASS", "harvested")], [(item["verdict"], item["source"]) for item in self.harvested(token)])
        self.assert_code("review_not_requested", e.import_review, self.root, lenses=["conformance"], task_id="value")

    def test_g_a_late_verdict_after_a_fix_is_history_and_a_current_fail_blocks_ready_until_overridden(self):
        self.integrated()
        path = Path(e.load(self.root)["integration"]["path"])
        first = e.review_token(self.root, lens="integrated")
        fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-mode", "Correct the file mode.")["integration_fix"]
        (path / "value.txt").chmod(0o755)
        git(path, "add", "--", "value.txt")
        e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": "synthetic fix result for " + fix["handle"]})
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(first["token"], "FAIL", "A late verdict on the first content.", [{"file": "value.txt", "kind": "behavior"}]))
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        second = e.review_token(self.root, lens="integrated")
        late = self.harvested(first["token"])
        self.assertEqual([("FAIL", first["content"])], [(item["verdict"], item["snapshot"]["content"]) for item in late])
        self.assertNotEqual(first["content"], second["content"])
        Journal(self.search, "wf_fixture-0005").agent("a0fixturerev04", lens(first["token"], "FAIL", "Another late verdict for the superseded request."))
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(second["token"]))
        self.assertEqual("PASS", e.import_review(self.root, lenses=["integrated"])["reviews"][-1]["verdict"])
        self.assertEqual(2, len(self.harvested(first["token"])))
        self.assertEqual("READY_TO_PUBLISH", e.ready(self.root)["state"])
        Journal(self.search, "wf_fixture-0004").agent("a0fixturerev03", lens(second["token"], "FAIL", "A second reviewer of the current content found a defect."))
        self.assert_code("late_review_fail", e.ready, self.root)
        self.assertEqual(["FAIL"], [item["verdict"] for item in self.harvested(second["token"]) if item["actor"].endswith("a0fixturerev03")])
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the late FAIL is wrong for this content.", code="review_override")
        self.assertEqual("READY_TO_PUBLISH", e.ready(self.root)["state"])

    def test_h_a_late_fail_after_an_imported_pass_returns_a_verified_task_to_rework(self):
        token, _ = self.returned()
        self.assertEqual("VERIFIED", e.import_review(self.root, lenses=["conformance"], task_id="value")["tasks"]["value"]["status"])
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, "FAIL", "A second reviewer found the newline missing.", [{"file": "value.txt", "kind": "behavior"}]))
        self.assert_code("dependency_not_ready", e.integrate, self.root)
        owner = e.load(self.root)["tasks"]["value"]
        self.assertEqual(("REWORK", 1, ["value.txt|behavior"]), (owner["status"], owner["rework_rounds"], owner["rework_history"][-1]["finding_keys"]))
        self.assertEqual(["rework"], [item["effect"] for item in self.harvested(token) if item["verdict"] == "FAIL"])

    def test_h_a_late_fail_on_an_integrated_task_refuses_ready_until_a_fix(self):
        token, _ = self.returned()
        e.import_review(self.root, lenses=["conformance"], task_id="value")
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", PASS_EVIDENCE)
        self.assertEqual("READY_TO_PUBLISH", e.ready(self.root)["state"])
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, "FAIL", "A second reviewer found the newline missing."))
        self.assert_code("late_review_fail", e.ready, self.root)
        fix = e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-late", "Address the late task finding.")["integration_fix"]
        path = Path(e.load(self.root)["integration"]["path"])
        (path / "value.txt").chmod(0o755)
        git(path, "add", "--", "value.txt")
        e.report_fix(self.root, "fixture-fix-writer", {**worker_result(fix["dispatch_id"]), "source_event": "synthetic fix result for " + fix["handle"]})
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer-2", "PASS", PASS_EVIDENCE)
        self.assertEqual("READY_TO_PUBLISH", e.ready(self.root)["state"])

    def test_a_late_fail_blocked_by_a_running_dependent_stays_pending_and_applies_later(self):
        self.begin(dependent_plan())
        dispatch_id = self.workflow_dispatch()
        Journal(self.search).agent(IMPLEMENTER, worker_result(dispatch_id))
        e.import_task_report(self.root, "value")
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        token = e.review_token(self.root, task_id="value", lens="conformance")["token"]
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token))
        e.import_review(self.root, lenses=["conformance"], task_id="value")
        e.prepare_task(self.root, "second")
        e.register_agent(self.root, "second", "fixture-writer-second", "unit-test", "synthetic:second")
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, "FAIL", "A late FAIL while the dependent task runs."))
        self.write_value("second", "final\n", "second.txt")
        self.report("second")
        self.assertEqual((["pending"], "VERIFIED"), ([item["effect"] for item in self.harvested(token) if item["verdict"] == "FAIL"], e.load(self.root)["tasks"]["value"]["status"]))
        run = e.authorize(self.root, "implement", "fixture-user", "Synthetic renewed authority; every command harvests first.")
        self.assertEqual(("REWORK", "INVALIDATED"), (run["tasks"]["value"]["status"], run["tasks"]["second"]["status"]))
        self.assertEqual(["rework"], [item["effect"] for item in self.harvested(token) if item["verdict"] == "FAIL"])

    def test_an_unverifiable_fail_counts_and_an_unverifiable_pass_is_ignored(self):
        token, _ = self.returned(transcript=False)
        refused = self.assert_code("review_lenses_incomplete", e.import_review, self.root, lenses=["conformance"], task_id="value")
        self.assertIn("incomplete provenance", refused.message)
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, "FAIL", "An unverifiable FAIL still counts."), transcript=False)
        run = e.import_review(self.root, lenses=["conformance"], task_id="value")
        self.assertEqual(("FAIL", [True], "REWORK"), (run["reviews"][-1]["verdict"], [item["unverified"] for item in run["reviews"][-1]["verdicts"]], run["tasks"]["value"]["status"]))

    def test_a_late_pass_changes_nothing(self):
        token, _ = self.returned("FAIL", "The value misses its newline.")
        before = e.import_review(self.root, lenses=["conformance"], task_id="value")
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token))
        after = e.authorize(self.root, "implement", "fixture-user", "Synthetic renewed authority; every command harvests first.")
        self.assertEqual([("FAIL", "workflow-agent:wf_fixture-0002/a0fixturerev01"), ("PASS", "workflow-agent:wf_fixture-0003/a0fixturerev02")],
                         [(item["verdict"], item["actor"]) for item in self.harvested(token)])
        self.assertEqual((before["reviews"], "REWORK", 1), (after["reviews"], after["tasks"]["value"]["status"], after["tasks"]["value"]["rework_rounds"]))

    def test_revise_harvests_first_and_a_fail_stays_a_fail(self):
        token, _ = self.returned("FAIL", "The value misses its newline.")
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token, evidence="PASS: fine."))
        revised = e.revise(self.root, "Synthetic revision while a verdict waits.")
        self.assertEqual([("FAIL", "value")], [(item["verdict"], item["task"]) for item in revised["harvested_reviews"] if item["token"] == token])
        self.assertEqual(["workflow-agent:wf_fixture-0003/a0fixturerev02"], [item["actor"] for item in revised["refused_review_events"].values()])
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        self.assert_code("review_reroll", e.review, self.root, "fixture-reviewer", "PASS", PASS_EVIDENCE, task_id="value")

    def odd_journal(self):
        odd = Journal(self.search, "wf_fixture-0009", project="odd-project")
        odd.line({"type": "started", "key": "k1", "agentId": "a0odd0000001", "label": "odd"})
        odd.line({"type": "result", "key": "k1", "agentId": "a0odd0000001", "result": {"review_token": ["not", "a", "string"], "dispatch_id": {"nested": True}}})
        odd.line("[" * 200000 + "]" * 200000)
        odd.line('{"type": "result", "key": "k1", "agentId": "a0odd0000001", "result": ' + "{\"a\": " * 3000 + "1" + "}" * 3000 + "}")
        with odd.path.open("ab") as out:
            out.write(b"\xff\xfe{ not text\n")
        (odd.dir / "agent-a0odd0000001.jsonl").write_text("{}\n")
        return odd

    def test_odd_lines_elsewhere_under_the_host_root_do_not_break_commands(self):
        self.returned()
        e.import_review(self.root, lenses=["conformance"], task_id="value")
        self.odd_journal()
        e.block(self.root, "Synthetic pause while odd journal lines exist.")
        e.resume(self.root, "Synthetic: the pause is over.")
        self.assertEqual("VERIFYING", e.integrate(self.root)["state"])
        self.assertIs(True, e.load(self.root)["harvest"]["complete"])
        self.assertEqual("DISCOVERY", e.revise(self.root, "Synthetic revision with odd journal lines around.")["state"])

    def test_a_harvest_failure_never_stops_a_command_and_is_recorded(self):
        self.returned()
        with mock.patch.object(e, "_apply_late_failures", side_effect=RuntimeError("synthetic harvest failure")):
            blocked = e.block(self.root, "Synthetic pause during a failing harvest.")
            self.assertEqual("BLOCKED", blocked["state"])
            self.assert_code("harvest_incomplete", e.import_review, self.root, lenses=["conformance"], task_id="value")
        event = [entry for entry in blocked["history"] if entry["event"] == "harvest_incomplete"][-1]
        self.assertIn("synthetic harvest failure", event["reason"])
        e.resume(self.root, "Synthetic: the harvest works again.")
        self.assertIs(True, e.load(self.root)["harvest"]["complete"])

    def locked_fail(self, token):
        locked = Journal(self.search, "wf_fixture-0010", project="locked-project")
        locked.agent("a0fixturerev10", lens(token, "FAIL", "A late FAIL in a journal nobody can read.", [{"file": "value.txt", "kind": "behavior"}]))
        locked.path.chmod(0)
        self.addCleanup(locked.path.chmod, 0o644)
        return locked

    def test_an_unreadable_journal_makes_ready_and_review_import_refuse_until_it_is_readable(self):
        self.integrated()
        token = e.review_token(self.root, lens="integrated")["token"]
        locked = self.locked_fail(token)
        refused = self.assert_code("harvest_incomplete", e.ready, self.root)
        self.assertIn(str(locked.path), refused.message)
        self.assert_code("harvest_incomplete", e.import_review, self.root, lenses=["integrated"])
        e.authorize(self.root, "implement", "fixture-user", "Synthetic renewed authority while a journal is unreadable.")
        e.block(self.root, "Synthetic pause while a journal is unreadable.")
        self.assertEqual("READY_TO_PUBLISH", e.resume(self.root, "Synthetic: the pause is over.")["state"])
        event = [entry for entry in e.load(self.root)["history"] if entry["event"] == "harvest_incomplete"][-1]
        self.assertEqual([str(locked.path)], event["paths"])
        locked.path.chmod(0o644)
        self.assert_code("late_review_fail", e.ready, self.root)
        self.assertEqual(["FAIL"], [item["verdict"] for item in self.harvested(token)])

    def test_symlinked_workflow_paths_are_skipped_and_listed(self):
        self.returned()
        real = Journal(self.search / "deeper", "wf_fixture-0011")
        (self.search / "linked-project").symlink_to(real.project, target_is_directory=True)
        e.authorize(self.root, "implement", "fixture-user", "Synthetic renewed authority; every command harvests first.")
        harvest = e.load(self.root)["harvest"]
        self.assertEqual((True, [str(self.search / "linked-project")]), (harvest["complete"], harvest["symlinks_skipped"]))

    def test_review_tokens_are_issued_only_where_their_verdicts_can_be_imported(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:value")
        self.assert_code("review_state", e.review_token, self.root, task_id="value", lens="conformance")
        self.write_value()
        self.report()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        self.assert_code("review_state", e.review_token, self.root, task_id="value", lens="conformance")
        e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix", "Synthetic correction.")
        self.assert_code("review_state", e.review_token, self.root, lens="integrated")


class PublicationHarvestTests(PublisherFixture):
    def test_merge_refuses_a_late_fail_and_refresh_proceeds(self):
        self.published()
        self.grant_merge()
        token = e.review_token(self.root, lens="integrated")["token"]
        Journal(self.host_root, "wf_fixture-0002").agent("a0fixturerev01", lens(token, "FAIL", "A late FAIL on the published content."))
        self.assert_code("late_review_fail", publisher.publish, self.root, "Update the fixture value", "The value now reads after.", provider=self.provider)
        self.assert_code("late_review_fail", publisher.merge, self.root, provider=self.provider)
        self.remote_commit({"new-main.txt": "A new base for the refresh.\n"})
        self.assertEqual("VERIFYING", publisher.refresh(self.root, provider=self.provider)["state"])


    def test_publish_and_merge_refuse_an_incomplete_harvest(self):
        self.published()
        self.grant_merge()
        token = e.review_token(self.root, lens="integrated")["token"]
        locked = Journal(self.host_root, "wf_fixture-0010", project="locked-project")
        locked.agent("a0fixturerev10", lens(token, "FAIL", "A late FAIL in a journal nobody can read."))
        locked.path.chmod(0)
        self.addCleanup(locked.path.chmod, 0o644)
        self.assert_code("harvest_incomplete", publisher.publish, self.root, "Update the fixture value", "The value now reads after.", provider=self.provider)
        self.assert_code("harvest_incomplete", publisher.merge, self.root, provider=self.provider)
        locked.path.chmod(0o644)
        self.assert_code("late_review_fail", publisher.merge, self.root, provider=self.provider)


class LocalFinishHarvestTests(LocalFixture):
    def setUp(self):
        self.host_dir = tempfile.TemporaryDirectory(prefix="delivery-local-host-")
        self.addCleanup(self.host_dir.cleanup)
        self.host = Path(self.host_dir.name).resolve()
        host = mock.patch.dict(os.environ, {"DELIVERY_WORKFLOW_HOST_ROOT": str(self.host)})
        host.start()
        self.addCleanup(host.stop)
        super().setUp()

    def test_finish_local_refuses_a_late_fail_until_overridden(self):
        self.committed()
        token = e.review_token(self.root, lens="integrated")["token"]
        Journal(self.host, "wf_fixture-0002").agent("a0fixturerev01", lens(token, "FAIL", "A late FAIL on the committed content."))
        with self.assertRaises(e.RunError) as caught:
            publisher.finish_local(self.root)
        self.assertEqual("late_review_fail", caught.exception.code)
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: the late FAIL is wrong.", code="review_override")
        self.assertEqual("COMPLETE", publisher.finish_local(self.root)["state"])

    def test_finish_local_refuses_an_incomplete_harvest(self):
        self.committed()
        token = e.review_token(self.root, lens="integrated")["token"]
        locked = Journal(self.host, "wf_fixture-0010", project="locked-project")
        locked.agent("a0fixturerev10", lens(token, "FAIL", "A late FAIL in a journal nobody can read."))
        locked.path.chmod(0)
        self.addCleanup(locked.path.chmod, 0o644)
        with self.assertRaises(e.RunError) as caught:
            publisher.finish_local(self.root)
        self.assertEqual("harvest_incomplete", caught.exception.code)
        locked.path.chmod(0o644)
        with self.assertRaises(e.RunError) as caught:
            publisher.finish_local(self.root)
        self.assertEqual("late_review_fail", caught.exception.code)


class PullRequestDecisionTests(PublisherFixture):
    def test_requirements_block_on_an_open_pr_resumes_with_the_users_decision(self):
        self.published()
        blocked = e.review(self.root, "fixture-pr-reviewer", "FAIL", "The PR reveals a requirement conflict.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding", "PR_OPEN"), (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["from"]))
        self.assert_code("open_pr_revision", e.revise, self.root, "Synthetic revision of a run with an open PR.")
        self.assert_code("decision_required", e.resume, self.root, "Synthetic resume without the user's decision.")
        e.authorize(self.root, "decision", "fixture-user", "Synthetic user decision: proceed within the reviewed scope of this PR.", code="requirements_finding")
        self.assertEqual("PR_OPEN", e.resume(self.root, "Synthetic: the user decided to proceed within the reviewed scope.")["state"])


class EvidenceTests(ControlFixture):
    def test_one_line_pass_is_refused(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        for evidence in ("PASS: looks good.", "Checked the acceptance item and reran the planned gate; " * 4, "PASS.\nGate rerun: ok."):
            with self.subTest(evidence=evidence[:20]):
                self.assert_code("review_evidence_insufficient", e.review, self.root, "fixture-reviewer", "PASS", evidence, task_id="value")
        self.assertEqual("VERIFIED", e.review(self.root, "fixture-reviewer", "PASS", PASS_EVIDENCE, task_id="value")["tasks"]["value"]["status"])


class OracleTests(unittest.TestCase):
    def test_weak_oracles_are_refused(self):
        weak = [("requirements", "Exit 0."), ("gate", "passes"), ("gate", "File correct."), ("verification", "All tests pass ."), ("verification", "Tests are  green ."),
                ("gate", "Exit code is 0."), ("requirements", "HTTP status is 200"), ("verification", "All the tests pass."), ("gate", "Returns 200."),
                ("gate", "The service returns 200 OK.")]
        for where, value in weak:
            with self.subTest(where=where, oracle=value):
                candidate = plan()
                target = {"requirements": candidate["requirements"][0], "gate": candidate["tasks"][0]["gates"][0], "verification": candidate["verification"][0]}[where]
                target["oracle"] = value
                with self.assertRaises(e.RunError) as caught:
                    e.validate_plan(candidate)
                self.assertEqual("weak_oracle", caught.exception.code)
        for value in ("value.txt reads after plus newline.", "Prints hello world", "The command exits 0 and prints the version."):
            with self.subTest(accepted=value):
                candidate = plan()
                candidate["verification"][0]["oracle"] = value
                self.assertTrue(e.validate_plan(candidate))


class StandingApprovalTests(ControlFixture):
    EVIDENCE = "Synthetic standing instruction for this disposable fixture only, not a real user's grant."
    NOTE = "Materiality: the plan stays within the fixture value and adds no risk trigger."

    def grant(self, max_level="small", triggers=None, **delta):
        return e.authorize(self.root, "standing-approval", "fixture-user", self.EVIDENCE, until=later(**(delta or {"days": 1})), max_level=max_level, triggers=triggers or [])

    def test_standing_approval_within_scope_approves_and_survives_plan_changes(self):
        first = self.grant()["standing_approval"]
        e.set_plan(self.root, plan())
        approved = e.approve(self.root, "fixture-coordinator", self.NOTE, standing=True)["approval"]
        self.assertEqual(("standing:fixture-user", "fixture-coordinator", "standing", first["id"]),
                         (approved["actor"], approved["recorded_by"], approved["kind"], approved["grant_id"]))
        replanned = e.set_plan(self.root, plan())
        self.assertEqual(({}, None, first), (replanned["authorizations"], replanned["approval"], replanned["standing_approval"]))
        second = self.grant(days=2)
        self.assertEqual([first], second["standing_history"])
        e.approve(self.root, "fixture-coordinator", self.NOTE, standing=True)
        status = e.status(self.root)
        self.assertEqual(("fixture-user", "small", []), tuple(status["standing_approval"][key] for key in ("actor", "max_level", "triggers")))
        self.assertEqual(["standing", "standing"], [item["kind"] for item in status["approvals"]])

    def test_standing_approval_refused_when_expired(self):
        for bad in (later(hours=-1), later(days=31), "2026-09-30T12:00:00"):
            with self.subTest(until=bad):
                self.assert_code("invalid_standing_approval" if "+" in bad else "invalid_timestamp", e.authorize, self.root, "standing-approval", "fixture-user", self.EVIDENCE, until=bad, max_level="small")
        self.grant(hours=1)
        e.set_plan(self.root, plan())
        with mock.patch.object(e, "now", return_value=later(hours=2)):
            self.assertIn("expired", self.assert_code("standing_approval_insufficient", e.approve, self.root, "fixture-coordinator", self.NOTE, standing=True).message)
        self.assertEqual("individual", e.approve(self.root, "fixture-user", "Synthetic individual approval.")["approval"]["kind"])

    def test_standing_approval_refused_when_level_too_high(self):
        self.grant(max_level="small", triggers=["runtime_qa"])
        value = plan()
        value["triggers"] = ["runtime_qa"]
        e.set_plan(self.root, value)
        self.assertIn("medium exceeds", self.assert_code("standing_approval_insufficient", e.approve, self.root, "fixture-coordinator", self.NOTE, standing=True).message)
        self.assertEqual("READY_FOR_APPROVAL", e.load(self.root)["state"])

    def test_standing_approval_refused_for_a_new_trigger(self):
        self.grant(max_level="large", triggers=["runtime_qa"])
        value = plan()
        value["triggers"] = ["runtime_qa", "auth"]
        e.set_plan(self.root, value)
        self.assertIn("auth", self.assert_code("standing_approval_insufficient", e.approve, self.root, "fixture-coordinator", self.NOTE, standing=True).message)
        self.assertEqual("READY_FOR_APPROVAL", e.load(self.root)["state"])


class WorkflowCliTests(ControlFixture):
    def test_cli_workflow_dispatch_and_reviews_reach_readiness(self):
        self.begin()
        self.cli("task-prepare", "--run", self.root, "--task", "value")
        dispatch_id = self.cli("task-register", "--run", self.root, "--task", "value", "--via-workflow")["tasks"]["value"]["agent"]["dispatch_id"]
        self.write_value()
        journal = Journal(self.search)
        journal.agent(IMPLEMENTER, worker_result(dispatch_id))
        imported = self.cli("task-import", "--run", self.root, "--task", "value", "--search-root", self.search)
        self.assertEqual(("REPORTED", f"workflow-agent:{RUN_ID}/{IMPLEMENTER}"), (imported["tasks"]["value"]["status"], imported["tasks"]["value"]["agent"]["actor"]))
        self.assertTrue(e.status(self.root)["tasks"]["value"]["via_workflow"])
        self.assertTrue(self.cli("gate", "--run", self.root, "--task", "value")["passed"])
        token = self.cli("review-token", "--run", self.root, "--task", "value", "--lens", "correctness")["token"]
        journal.agent("a0fixturerev01", lens(token))
        self.assertEqual("VERIFIED", self.cli("review-import", "--run", self.root, "--task", "value", "--lens", "correctness", "--search-root", self.search)["tasks"]["value"]["status"])
        self.cli("integrate", "--run", self.root)
        self.assertTrue(self.cli("gate", "--run", self.root, "--case", "value-check")["passed"])
        token = self.cli("review-token", "--run", self.root, "--lens", "integrated")["token"]
        journal.agent("a0fixturerev02", lens(token))
        self.assertEqual("VERIFYING", self.cli("review-import", "--run", self.root, "--lens", "integrated", "--journal", journal.path)["state"])
        self.assertEqual("READY_TO_PUBLISH", self.cli("ready", "--run", self.root)["state"])


class FinishLocalContentTests(LocalFixture):
    def test_untracked_files_that_are_not_ignored_fail_closed(self):
        run = self.committed()
        stray = Path(run["tasks"]["value"]["path"]) / "stray-notes.txt"
        stray.write_text("An untracked, not ignored file is part of the task content.\n")
        with self.assertRaises((e.RunError, e.git_ops.GitError)):
            publisher.finish_local(self.root)
        self.assertEqual(("READY_TO_PUBLISH", True), (e.load(self.root)["state"], stray.is_file()))


class WorkflowJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="workflow-journal-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        host = mock.patch.dict(os.environ, {"DELIVERY_WORKFLOW_HOST_ROOT": str(self.home / "host")})
        host.start()
        self.addCleanup(host.stop)
        self.home = self.home / "host"
        self.home.mkdir()

    def test_invalid_lines_are_skipped_and_hashes_bind_line_and_transcript(self):
        journal = Journal(self.home)
        journal.line("not json")
        journal.line("[1, 2]")
        journal.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        found = wj.find_results(lambda result: result.get("dispatch_id") == "d1", search_root=self.home)
        self.assertEqual(1, len(found))
        raw = journal.path.read_bytes().splitlines()[-1]
        self.assertEqual(hashlib.sha256(raw).hexdigest(), found[0]["line_sha256"])
        self.assertEqual(hashlib.sha256((journal.dir / f"agent-{IMPLEMENTER}.jsonl").read_bytes()).hexdigest(), found[0]["transcript_sha256"])
        self.assertEqual((RUN_ID, IMPLEMENTER, "fixture " + IMPLEMENTER, str(journal.path)), (found[0]["run_id"], found[0]["agent_id"], found[0]["label"], found[0]["journal"]))

    def test_symlinked_run_directory_and_invalid_identities_are_refused(self):
        real = Journal(self.home / "deeper" / "real")
        real.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        workflows = self.home / "linked-project" / "fixture-session" / "subagents" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / RUN_ID).symlink_to(real.dir, target_is_directory=True)
        self.assertEqual([], wj.find_results(lambda result: True, search_root=self.home))
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=workflows / RUN_ID / "journal.jsonl")
        self.assertEqual("journal_symlink", caught.exception.code)
        bad = Journal(self.home, "wf_bad.name", project="bad-project")
        bad.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home)
        self.assertEqual("journal_identity", caught.exception.code)

    def test_a_line_that_breaks_the_match_is_skipped(self):
        journal = Journal(self.home)
        journal.agent(IMPLEMENTER, {"review_token": ["not", "hashable"]})
        journal.agent("a0fixturerev01", {"review_token": "review-plain"})
        found = wj.find_results(lambda result: result.get("review_token") in {"review-plain": 1}, search_root=self.home)
        self.assertEqual(["a0fixturerev01"], [item["agent_id"] for item in found])

    def test_unreadable_directories_and_journals_are_reported_or_refused(self):
        readable = Journal(self.home)
        readable.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        session = Journal(self.home, project="locked-project")
        session.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        locked_dir = session.project / "fixture-session"
        locked_dir.chmod(0)
        self.addCleanup(locked_dir.chmod, 0o755)
        problems = []
        found = wj.find_results(lambda result: True, search_root=self.home, strict=False, problems=problems)
        self.assertEqual(([str(readable.path)], [str(locked_dir)]), ([item["journal"] for item in found], [item["path"] for item in problems]))
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home)
        self.assertEqual("journal_unreadable", caught.exception.code)

    def test_host_root_confines_search_roots_and_named_journals(self):
        outside = Journal(self.home.parent / "outside")
        outside.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        for kwargs in ({"search_root": self.home.parent / "outside"}, {"journal": outside.path}, {"search_root": self.home.parent}):
            with self.subTest(**{key: str(value) for key, value in kwargs.items()}), self.assertRaises(e.RunError) as caught:
                wj.find_results(lambda result: True, **kwargs)
            self.assertEqual("journal_outside_host", caught.exception.code)
        (self.home / "narrower").mkdir()
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home / "narrower")
        self.assertEqual("search_root_mismatch", caught.exception.code)
        (self.home / "escape").symlink_to(outside.dir.parents[3], target_is_directory=True)
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=self.home / "escape" / "fixture-session" / "subagents" / "workflows" / RUN_ID / "journal.jsonl")
        self.assertEqual("journal_outside_host", caught.exception.code)
        deep = Journal(self.home / "deeper")
        deep.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=deep.path)
        self.assertEqual("journal_layout", caught.exception.code)
        inside = Journal(self.home)
        inside.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        self.assertEqual([str(self.home)], [item["host_root"] for item in wj.find_results(lambda result: True, journal=inside.path)])


if __name__ == "__main__":
    unittest.main()
