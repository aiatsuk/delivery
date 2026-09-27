"""Execution-control guards with real local Git and synthetic host workflow journals.

Every actor, journal line and transcript below is a synthetic unit-test artifact under a
temporary directory. None of it proves that a host workflow, an agent or a person acted.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
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


def worker_result(dispatch_id: str) -> dict:
    return {"dispatch_id": dispatch_id, "summary": "Changed only the fixture value.",
            "tests": [{"command": ["git", "diff", "--cached", "--check"], "exit_code": 0, "outcome": "Synthetic fixture observation; no whitespace errors."}],
            "limitations": ["Synthetic journal for unit testing only."]}


def lens(token: str, verdict="PASS", evidence=PASS_EVIDENCE, defects=None) -> dict:
    return {"review_token": token, "verdict": verdict, "evidence": evidence, "defects": defects or []}


class ControlFixture(RunFixture):
    def setUp(self):
        super().setUp()
        self.search = self.home / "journals"
        self.search.mkdir()

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(e.RunError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code, caught.exception.message)
        return caught.exception

    def write_value(self, task="value", content="after\n") -> Path:
        path = Path(e.load(self.root)["tasks"][task]["path"])
        (path / "value.txt").write_text(content)
        git(path, "add", "--", "value.txt")
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
        accepted = e.report_task(self.root, "value", "fixture-writer-value", {**result, "source_event": "observation: x 2026-09-13T20:52:10Z"})
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
        self.assertEqual(before, e.load(self.root)["revision"])
        Journal(self.search, "wf_fixture-0002").agent("a0fixturerev01", lens(token["token"]))
        verified = e.import_review(self.root, lenses=["correctness"], task_id="value", search_root=self.search)
        self.assertEqual("VERIFIED", verified["tasks"]["value"]["status"])
        receipt = verified["reviews"][-1]
        self.assertEqual(("PASS", "workflow-agent:wf_fixture-0002/a0fixturerev01"), (receipt["verdict"], receipt["actor"]))
        self.assertEqual("correctness", json.loads(receipt["evidence"])["lenses"][0]["lens"])

    def test_journal_refusals_leave_the_dispatch_pending(self):
        self.begin()
        dispatch_id = self.workflow_dispatch()
        result = worker_result(dispatch_id)
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        cases = {
            "journal_result_missing": lambda j: j.agent(IMPLEMENTER, worker_result("another-dispatch")),
            "journal_result_ambiguous": lambda j: (j.agent(IMPLEMENTER, result), j.agent("a0fixtureimpl02", result)),
            "journal_order": lambda j: j.agent(IMPLEMENTER, result, result_first=True),
            "journal_transcript_missing": lambda j: j.agent(IMPLEMENTER, result, transcript=False),
        }
        for code, write in cases.items():
            with self.subTest(code=code):
                root = self.home / ("journals-" + code)
                write(Journal(root))
                self.assert_code(code, e.import_task_report, self.root, "value", search_root=root)
        with self.subTest(code="journal_symlink"):
            root = self.home / "journals-symlink"
            linked = Journal(root)
            Journal(elsewhere).agent(IMPLEMENTER, result)
            linked.path.unlink()
            linked.path.symlink_to(Journal(elsewhere).path)
            (linked.dir / f"agent-{IMPLEMENTER}.jsonl").write_text("{}\n")
            self.assert_code("journal_symlink", e.import_task_report, self.root, "value", search_root=root)
            self.assert_code("journal_symlink", e.import_task_report, self.root, "value", journal=linked.path)
        current = e.load(self.root)["tasks"]["value"]
        self.assertEqual(("DISPATCHED", "workflow-pending"), (current["status"], current["agent"]["actor"]))

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
        other_run = Journal(self.search, "wf_fixture-0002")
        other_run.agent(IMPLEMENTER, lens(token))
        self.assert_code("reviewer_not_independent", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=other_run.path)
        independent = Journal(self.search, "wf_fixture-0003")
        independent.agent("a0fixturerev01", lens(token))
        self.assertEqual("VERIFIED", e.import_review(self.root, lenses=["correctness"], task_id="value", journal=independent.path)["tasks"]["value"]["status"])

    def test_two_lenses_from_one_reviewer_agent_are_refused(self):
        self.reviewed_ready()
        tokens = [e.review_token(self.root, task_id="value", lens=name)["token"] for name in ("correctness", "security")]
        shared = Journal(self.search, "wf_fixture-0002")
        for token in tokens:
            shared.agent("a0fixturerev01", lens(token))
        self.assert_code("reviewer_not_independent", e.import_review, self.root, lenses=["correctness", "security"], task_id="value", journal=shared.path)
        distinct = Journal(self.search, "wf_fixture-0003")
        distinct.agent("a0fixturerev01", lens(tokens[0]))
        distinct.agent("a0fixturerev02", lens(tokens[1]))
        receipt = e.import_review(self.root, lenses=["correctness", "security"], task_id="value", journal=distinct.path)["reviews"][-1]
        self.assertEqual("workflow-agent:wf_fixture-0003/a0fixturerev01+workflow-agent:wf_fixture-0003/a0fixturerev02", receipt["actor"])

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
        owner = run["tasks"]["value"]
        self.assertEqual(("REWORK", 1, keys), (owner["status"], owner["rework_rounds"], owner["rework_history"][-1]["finding_keys"]))

    def test_review_import_needs_exactly_one_result_per_current_token(self):
        self.reviewed_ready()
        token = e.review_token(self.root, task_id="value", lens="correctness")["token"]
        journal = Journal(self.search, "wf_fixture-0002")
        journal.agent("a0fixturerev01", lens("review-" + "0" * 32))
        self.assert_code("journal_result_missing", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
        journal.agent("a0fixturerev01", lens(token))
        journal.agent("a0fixturerev02", lens(token))
        self.assert_code("journal_result_ambiguous", e.import_review, self.root, lenses=["correctness"], task_id="value", journal=journal.path)
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

    def test_requirements_and_human_decisions_block(self):
        self.begin()
        self.implement()
        self.assert_code("invalid_decision", e.rework, self.root, "value", "Synthetic finding.", decision="guess")
        blocked = e.rework(self.root, "value", "The acceptance text contradicts the requirement.", decision="requirements")
        self.assertEqual(("BLOCKED", "requirements_finding", 0), (blocked["state"], blocked["blocker"]["code"], blocked["tasks"]["value"].get("rework_rounds", 0)))
        self.assertEqual("A requirements finding needs revise and renewed approval, not implementer rework: The acceptance text contradicts the requirement.", blocked["blocker"]["reason"])
        self.assertEqual("requirements", blocked["tasks"]["value"]["rework_history"][-1]["decision"])
        e.resume(self.root, "Synthetic: the fixture user answered the requirement question.")
        self.redispatch("synthetic:after-requirements")
        blocked = e.rework(self.root, "value", "Keep or drop the legacy value.", decision="human")
        self.assertEqual(("BLOCKED", "human_decision", "A human decision is required: Keep or drop the legacy value."),
                         (blocked["state"], blocked["blocker"]["code"], blocked["blocker"]["reason"]))
        self.assertEqual("human_decision", e.status(self.root)["blocker_code"])

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
        weak = [("requirements", "Exit 0."), ("gate", "passes"), ("gate", "File looks right."), ("verification", "All tests pass ."), ("verification", "Tests are  green .")]
        for where, value in weak:
            with self.subTest(where=where, oracle=value):
                candidate = plan()
                target = {"requirements": candidate["requirements"][0], "gate": candidate["tasks"][0]["gates"][0], "verification": candidate["verification"][0]}[where]
                target["oracle"] = value
                with self.assertRaises(e.RunError) as caught:
                    e.validate_plan(candidate)
                self.assertEqual("weak_oracle", caught.exception.code)
        candidate = plan()
        candidate["verification"][0]["oracle"] = "value.txt reads after plus newline."
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
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home / "linked")
        self.assertEqual("journal_symlink", caught.exception.code)
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, journal=workflows / RUN_ID / "journal.jsonl")
        self.assertEqual("journal_symlink", caught.exception.code)
        bad = Journal(self.home / "bad", "wf_bad.name")
        bad.agent(IMPLEMENTER, {"dispatch_id": "d1"})
        with self.assertRaises(e.RunError) as caught:
            wj.find_results(lambda result: True, search_root=self.home / "bad")
        self.assertEqual("journal_identity", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
