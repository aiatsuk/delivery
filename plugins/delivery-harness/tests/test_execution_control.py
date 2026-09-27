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
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_run_engine import PASS_EVIDENCE, SCRIPTS, RunFixture, e, gate, git, plan
import workflow_journal as wj

RUN_ID = "wf_fixture-0001"
IMPLEMENTER = "a0fixtureimpl01"


def later(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(**delta)).isoformat()


class Journal:
    """Writes host-format journal lines and transcripts for one synthetic workflow run."""

    def __init__(self, search_root: Path, run_id: str = RUN_ID):
        self.dir = search_root / "fixture-project" / "fixture-session" / "subagents" / "workflows" / run_id
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
        self.search = self.home / "journals"
        self.search.mkdir()
        host = mock.patch.dict(os.environ, {"DELIVERY_WORKFLOW_HOST_ROOT": str(self.search)})
        host.start()
        self.addCleanup(host.stop)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(e.RunError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code, caught.exception.message)
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
        elsewhere = self.search / "cases" / "symlink-target"
        elsewhere.mkdir(parents=True)
        cases = {
            "journal_result_missing": lambda j: j.agent(IMPLEMENTER, worker_result("another-dispatch")),
            "journal_result_ambiguous": lambda j: (j.agent(IMPLEMENTER, result), j.agent("a0fixtureimpl02", result)),
            "journal_order": lambda j: j.agent(IMPLEMENTER, result, result_first=True),
            "journal_transcript_missing": lambda j: j.agent(IMPLEMENTER, result, transcript=False),
        }
        for code, write in cases.items():
            with self.subTest(code=code):
                root = self.search / "cases" / code
                write(Journal(root))
                self.assert_code(code, e.import_task_report, self.root, "value", search_root=root)
        with self.subTest(code="journal_symlink"):
            root = self.search / "cases" / "symlink"
            linked = Journal(root)
            Journal(elsewhere).agent(IMPLEMENTER, result)
            linked.path.unlink()
            linked.path.symlink_to(Journal(elsewhere).path)
            (linked.dir / f"agent-{IMPLEMENTER}.jsonl").write_text("{}\n")
            self.assert_code("journal_result_missing", e.import_task_report, self.root, "value", search_root=root)
            self.assert_code("journal_symlink", e.import_task_report, self.root, "value", journal=linked.path)
        current = e.load(self.root)["tasks"]["value"]
        self.assertEqual(("DISPATCHED", "workflow-pending"), (current["status"], current["agent"]["actor"]))

    def test_journals_outside_the_host_root_or_behind_a_symlink_are_refused(self):
        self.begin()
        result = worker_result(self.workflow_dispatch())
        outside = Journal(self.home / "outside")
        outside.agent(IMPLEMENTER, result)
        self.assert_code("journal_outside_host", e.import_task_report, self.root, "value", search_root=self.home / "outside")
        self.assert_code("journal_outside_host", e.import_task_report, self.root, "value", journal=outside.path)
        real = Journal(self.search / "cases" / "real")
        real.agent(IMPLEMENTER, result)
        (self.search / "linked-project").symlink_to(real.dir.parents[3], target_is_directory=True)
        aliased = self.search / "linked-project" / "fixture-session" / "subagents" / "workflows" / RUN_ID / "journal.jsonl"
        self.assert_code("journal_symlink", e.import_task_report, self.root, "value", journal=aliased)
        run = e.import_task_report(self.root, "value", journal=real.path)
        self.assertEqual((str(self.search), str(real.path)), (run["tasks"]["value"]["agent"]["journal"]["host_root"], run["tasks"]["value"]["agent"]["journal"]["path"]))

    def test_imported_tree_must_match_the_staged_worktree(self):
        self.begin()
        dispatch_id = self.workflow_dispatch()
        tree = git(Path(e.load(self.root)["tasks"]["value"]["path"]), "write-tree")
        Journal(self.search / "cases" / "stale").agent(IMPLEMENTER, {**worker_result(dispatch_id), "tree": "0" * 40})
        self.assert_code("import_tree_mismatch", e.import_task_report, self.root, "value", search_root=self.search / "cases" / "stale")
        Journal(self.search / "cases" / "current").agent(IMPLEMENTER, {**worker_result(dispatch_id), "tree": tree})
        run = e.import_task_report(self.root, "value", search_root=self.search / "cases" / "current")
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
        self.assert_code("reviewer_not_independent", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=same_run.path)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])

    def test_implementer_agent_id_under_another_workflow_run_cannot_review(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        other_run = Journal(self.search, "wf_fixture-0002")
        other_run.agent(IMPLEMENTER, lens(token))
        self.assert_code("reviewer_not_independent", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=other_run.path)

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

    def test_review_import_needs_exactly_one_result_per_current_token(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens("review-" + "0" * 32))
        self.assert_code("journal_result_missing", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        journal.agent("a0fixturerev01", lens(token))
        Journal(self.search, "wf_fixture-0003").agent("a0fixturerev02", lens(token))
        self.assert_code("journal_result_ambiguous", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])

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

    def test_lens_pass_with_one_line_evidence_is_refused(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens(token, evidence="PASS: looks correct."))
        self.assert_code("review_evidence_insufficient", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        self.assertEqual("REPORTED", e.load(self.root)["tasks"]["value"]["status"])


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
        self.assert_code("revision_required", e.resume, self.root, "Synthetic: the fixture user answered the requirement question.")
        self.assertEqual("BLOCKED", e.load(self.root)["state"])

    def test_human_decision_blocks_and_resume_continues(self):
        self.begin()
        self.implement()
        blocked = e.rework(self.root, "value", "Keep or drop the legacy value.", decision="human")
        self.assertEqual(("BLOCKED", "human_decision", "A human decision is required: Keep or drop the legacy value."),
                         (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["reason"]))
        self.assertEqual("human_decision", e.status(self.root)["blocker_code"])
        e.resume(self.root, "Synthetic: the fixture user decided to keep the legacy value.")
        self.redispatch("synthetic:after-decision")

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
        self.assertEqual("READY_TO_PUBLISH", e.resume(self.root, "Synthetic: the fixture user decided.")["state"])
        requirements = e.review(self.root, "fixture-integration-reviewer-3", "FAIL", "The requirement contradicts the verification case.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding"), (requirements["state"], requirements["blocker"]["code"]))
        self.assert_code("revision_required", e.resume, self.root, "Synthetic: resume is not the way out.")

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
        real = Journal(self.home / "real")
        real.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        workflows = self.home / "linked" / "fixture-project" / "fixture-session" / "subagents" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / RUN_ID).symlink_to(real.dir, target_is_directory=True)
        self.assertEqual([], wj.find_results(lambda result: True, search_root=self.home / "linked"))
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=workflows / RUN_ID / "journal.jsonl")
        self.assertEqual("journal_symlink", caught.exception.code)
        bad = Journal(self.home / "bad", "wf_bad.name")
        bad.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home / "bad")
        self.assertEqual("journal_identity", caught.exception.code)

    def test_host_root_confines_search_roots_and_named_journals(self):
        outside = Journal(self.home.parent / "outside")
        outside.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        for kwargs in ({"search_root": self.home.parent / "outside"}, {"journal": outside.path}, {"search_root": self.home.parent}):
            with self.subTest(**{key: str(value) for key, value in kwargs.items()}), self.assertRaises(e.RunError) as caught:
                wj.find_results(lambda result: True, **kwargs)
            self.assertEqual("journal_outside_host", caught.exception.code)
        (self.home / "escape").symlink_to(outside.dir.parents[3], target_is_directory=True)
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=self.home / "escape" / "fixture-session" / "subagents" / "workflows" / RUN_ID / "journal.jsonl")
        self.assertEqual("journal_outside_host", caught.exception.code)
        inside = Journal(self.home)
        inside.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        self.assertEqual([str(self.home)], [item["host_root"] for item in wj.find_results(lambda result: True, journal=inside.path)])


if __name__ == "__main__":
    unittest.main()
