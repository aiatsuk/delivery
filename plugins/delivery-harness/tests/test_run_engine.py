"""Real local Git fixtures; synthetic actors test guards, not agent liveness."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delivery/scripts"
sys.path.insert(0, str(SCRIPTS))
import run_engine as e


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True).stdout.strip()


def gate(command=None, risk="safe"):
    return {"command": command or [sys.executable, "-B", "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'after\\n'"],
            "risk": risk, "oracle": "The fixture value is exactly the expected text.", "cleanup": "No resources or files are created."}


def plan():
    return {"goal": "Update the documented fixture value.", "non_goals": ["No external service or deployment."],
            "scores": {key: 0 for key in e.AXES}, "triggers": [], "decisions": [],
            "requirements": [{"id": "updated-value", "behavior": "The value reads after.", "oracle": "Exact file content equals after followed by a newline."}],
            "tasks": [{"id": "value", "title": "Update value", "acceptance": "Exact value and no other file changes.", "paths": ["value.txt"], "depends_on": [], "requirements": ["updated-value"], "gates": [gate()]}],
            "verification": [{"id": "value-check", "check_case": "CASE-001", **gate(), "requirements": ["updated-value"]}]}


class RunFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery-run-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.remote, self.repo = self.home / "origin.git", self.home / "project"
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.remote), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "config", "user.name", "Fixture Maintainer")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "value.txt").write_text("before\n")
        git(self.repo, "add", "value.txt")
        git(self.repo, "commit", "-m", "Create fixture")
        git(self.repo, "push", "origin", "main")
        self.original = git(self.repo, "rev-parse", "HEAD")
        created = e.create(str(self.repo), "fixture", "Update the fixture value and verify it.", store=str(self.home / "runs"))
        self.root = Path(created["root"])

    def begin(self, value=None):
        e.set_plan(self.root, value or plan())
        e.authorize(self.root, "implement", "fixture-user", "Synthetic unit fixture authority, not a real user's approval.")
        e.start(self.root, within_request=True)

    def implement(self, task="value", changes=None):
        e.prepare_task(self.root, task)
        e.register_agent(self.root, task, "fixture-writer-" + task, "unit-test", "synthetic:" + task)
        owner = e.load(self.root)["tasks"][task]
        path = Path(owner["path"])
        for name, content in (changes or {"value.txt": "after\n"}).items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(content)
        git(path, "add", "--", *(changes or {"value.txt": "after\n"}))
        self.report(task)
        return path

    def report(self, task="value"):
        dispatch_id = e.load(self.root)["tasks"][task]["agent"]["dispatch_id"]
        e.report_task(self.root, task, "fixture-writer-" + task, {"summary": "Changed only the fixture value.", "tests": "Host gates pending.", "limitations": "Synthetic actor for unit testing only.", "dispatch_id": dispatch_id, "source_event": "unit-test fixture event " + dispatch_id})

    def verify(self, task="value"):
        self.assertTrue(e.execute_gate(self.root, task_id=task)["passed"])
        e.review(self.root, "fixture-reviewer", "PASS", "Synthetic independent fixture review; exact scope and oracle checked.", task_id=task)

    def integrated(self):
        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Synthetic integrated review.")
        return e.ready(self.root)


class PlanTests(unittest.TestCase):
    def test_hard_triggers_override_zero_scores(self):
        self.assertEqual(e.classify({key: 0 for key in e.AXES}, ["auth"])["level"], "large")
        self.assertEqual(e.classify({key: 0 for key in e.AXES}, ["runtime_qa"])["level"], "medium")

    def test_boundaries(self):
        for total, expected in [(5, "small"), (6, "medium"), (10, "medium"), (11, "large")]:
            values = {key: min(2, max(0, total - i * 2)) for i, key in enumerate(e.AXES)}
            self.assertEqual(e.classify(values, [])["level"], expected)

    def test_unknown_trigger_rejected(self):
        with self.assertRaisesRegex(e.RunError, "Unknown risk"):
            e.classify({key: 0 for key in e.AXES}, ["unclassified"])

    def test_boolean_score_rejected(self):
        scores = {key: 0 for key in e.AXES}
        scores["risk"] = True
        with self.assertRaises(e.RunError): e.classify(scores, [])

    def test_bare_command_is_not_a_risk_declaration(self):
        value = plan()
        value["tasks"][0]["gates"] = [["true"]]
        with self.assertRaises(e.RunError): e.validate_plan(value)

    def test_scope_traversal_and_metadata_rejected(self):
        for name in ["../secret", "/absolute", "src/../secret", ".git/config", "src/*", "src//file"]:
            with self.subTest(name=name), self.assertRaises(e.RunError): e.safe_path(name)

    def test_requirement_coverage_required(self):
        value = plan()
        value["requirements"].append({"id": "uncovered", "behavior": "Extra behavior.", "oracle": "Extra observation."})
        with self.assertRaisesRegex(e.RunError, "Every requirement"):
            e.validate_plan(value)

    def test_material_decisions_block(self):
        value = plan()
        value["decisions"] = [{"status": "open", "reason": "Need a data retention decision."}]
        with self.assertRaisesRegex(e.RunError, "Resolve material"):
            e.validate_plan(value)

    def test_cycles_and_overlap_rejected(self):
        value = plan()
        other = copy.deepcopy(value["tasks"][0])
        other["id"] = "other"
        value["tasks"].append(other)
        with self.assertRaisesRegex(e.RunError, "Serialize"):
            e.validate_plan(value)
        value["tasks"][0]["depends_on"] = ["other"]
        other["depends_on"] = ["value"]
        with self.assertRaisesRegex(e.RunError, "cycle"):
            e.validate_plan(value)

    def test_dependency_allows_shared_paths(self):
        value = plan()
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", depends_on=["value"])
        value["tasks"].append(other)
        self.assertEqual(len(e.validate_plan(value)["tasks"]), 2)

    def test_shared_resources_need_ordering(self):
        value = plan()
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", paths=["other.txt"], resources=["simulator:one"])
        value["tasks"][0]["resources"] = ["simulator:one"]
        value["tasks"].append(other)
        with self.assertRaisesRegex(e.RunError, "Serialize"):
            e.validate_plan(value)


class RunTests(RunFixture):
    def test_initial_context_is_read_only_and_external(self):
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(e.status(self.root)["state"], "DISCOVERY")
        self.assertIsNone(e.load(self.root)["product"])
        self.assertNotIn(self.repo, self.root.parents)

    def test_artifact_store_inside_repo_rejected(self):
        with self.assertRaises(e.RunError):
            e.create(str(self.repo), "bad", "A bounded request.", store=str(self.repo / "artifacts"))

    def test_run_id_reuse_requires_resume(self):
        with self.assertRaisesRegex(e.RunError, "resume"):
            e.create(str(self.repo), "fixture", "Different request.", store=str(self.home / "runs"))

    def test_start_requires_separate_implementation_authority(self):
        e.set_plan(self.root, plan())
        e.approve(self.root, "fixture-user", "Synthetic semantic approval.")
        with self.assertRaisesRegex(e.RunError, "implement authority"):
            e.start(self.root)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.original)

    def test_medium_cannot_use_small_fast_path(self):
        value = plan()
        value["triggers"] = ["runtime_qa"]
        e.set_plan(self.root, value)
        e.authorize(self.root, "implement", "fixture-user", "Synthetic authority.")
        with self.assertRaisesRegex(e.RunError, "Approve"):
            e.start(self.root, within_request=True)

    def test_stale_revision_does_not_change_state(self):
        e.set_plan(self.root, plan(), 0)
        with self.assertRaisesRegex(e.RunError, "another writer"):
            e.authorize(self.root, "implement", "fixture-user", "Synthetic authority.", 0)
        self.assertEqual(e.load(self.root)["revision"], 1)

    def test_disk_plan_drift_refused(self):
        self.begin()
        value = json.loads((self.root / "plan.json").read_text())
        value["goal"] = "Unapproved replacement."
        (self.root / "plan.json").write_text(json.dumps(value))
        with self.assertRaisesRegex(e.RunError, "Plan artifact changed"):
            e.prepare_task(self.root, "value")
        self.assertFalse(e.status(self.root)["plan_current"])

    def test_actor_registration_is_not_liveness_claim(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", "fixture-writer", "fixture", "synthetic")
        actor = e.status(self.root)["tasks"]["value"]["agent"]
        self.assertEqual(actor["liveness"], "must-query-host")

    def test_wrong_actor_report_rejected(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.register_agent(self.root, "value", "fixture-writer", "fixture", "synthetic")
        with self.assertRaisesRegex(e.RunError, "registered"):
            e.report_task(self.root, "value", "wrong", {})

    def test_scope_violation_blocks_report(self):
        self.begin()
        with self.assertRaisesRegex(e.RunError, "confined"):
            self.implement(changes={"value.txt": "after\n", "secret.txt": "unexpected\n"})

    def test_directory_ownership_exports_exact_files(self):
        value = plan()
        value["tasks"][0]["paths"] = ["docs"]
        value["tasks"][0]["gates"] = [gate([sys.executable, "-B", "-c", "from pathlib import Path; assert Path('docs/note.md').read_text() == 'note\\n'"])]
        self.begin(value)
        self.implement(changes={"docs/note.md": "note\n"})
        self.verify()
        self.assertEqual(e.load(self.root)["tasks"]["value"]["patch"]["allowed_paths"], ["docs/note.md"])

    def test_reviewer_cannot_be_implementer(self):
        self.begin(); self.implement()
        e.execute_gate(self.root, task_id="value")
        with self.assertRaisesRegex(e.RunError, "reviewer"):
            e.review(self.root, "fixture-writer-value", "PASS", "Synthetic self review.", task_id="value")

    def test_failed_gate_blocks_review(self):
        self.begin(); self.implement(changes={"value.txt": "wrong\n"})
        self.assertFalse(e.execute_gate(self.root, task_id="value")["passed"])
        with self.assertRaisesRegex(e.RunError, "planned gates"):
            e.review(self.root, "fixture-reviewer", "PASS", "Invalid synthetic pass.", task_id="value")

    def test_test_that_mutates_source_cannot_pass(self):
        value = plan()
        value["tasks"][0]["gates"] = [gate([sys.executable, "-B", "-c", "from pathlib import Path; Path('value.txt').write_text('mutated')"])]
        self.begin(value); self.implement()
        result = e.execute_gate(self.root, task_id="value")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["unchanged"])
        self.assertFalse(result["passed"])

    def test_task_external_gate_needs_explicit_authority(self):
        value = plan(); value["tasks"][0]["gates"][0]["risk"] = "external"
        self.begin(value); self.implement()
        with self.assertRaisesRegex(e.RunError, "test-external"):
            e.execute_gate(self.root, task_id="value")
        e.authorize(self.root, "test-external", "fixture-user", "Synthetic permission; fixture command has no network.", targets=["value:0"])
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])

    def test_log_tampering_invalidates_gate(self):
        self.begin(); self.implement()
        receipt = e.execute_gate(self.root, task_id="value")
        Path(receipt["log"]).write_text("forged output")
        self.assertFalse(e.gates_current(e.load(self.root), "value"))

    def test_code_drift_invalidates_review(self):
        self.begin(); worktree = self.implement(); self.verify()
        (worktree / "value.txt").write_text("later\n")
        with self.assertRaisesRegex(e.RunError, "changed after verification"):
            e.integrate(self.root)

    def test_patch_tampering_invalidates_task(self):
        self.begin(); self.implement(); self.verify()
        path = Path(e.load(self.root)["tasks"]["value"]["patch"]["path"])
        path.write_text(path.read_text() + "tampered")
        with self.assertRaisesRegex(e.RunError, "patch changed"):
            e.integrate(self.root)

    def test_unapproved_task_commit_rejected(self):
        self.begin(); worktree = self.implement()
        git(worktree, "commit", "-m", "Unexpected task commit")
        with self.assertRaisesRegex(e.RunError, "do not commit"):
            e.execute_gate(self.root, task_id="value")

    def test_real_git_integration_reaches_readiness_without_main_edits(self):
        value = self.integrated()
        self.assertEqual(value["state"], "READY_TO_PUBLISH")
        self.assertEqual(git(self.repo, "branch", "--show-current"), "main")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), self.original)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(value["validated"]["files"], ["value.txt"])
        self.assertIsNone(value["pr"])

    def test_returned_rework_creates_new_patch_receipt(self):
        self.begin(); worktree = self.implement(); self.verify()
        old = e.load(self.root)["tasks"]["value"]["patch"]["path"]
        e.rework(self.root, "value", "Synthetic review requests a fresh confirmation.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:rework")
        self.report(); self.verify()
        new = e.load(self.root)["tasks"]["value"]["patch"]["path"]
        self.assertNotEqual(old, new)
        self.assertTrue(Path(old).is_file())

    def test_rework_budget_is_persisted_blocker(self):
        self.begin(); self.implement(); self.verify()
        for i in range(3):
            e.rework(self.root, "value", "Synthetic bounded rework.")
            if i < 2:
                e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", f"synthetic:{i}")
                self.report(); self.verify()
        result = e.load(self.root)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertIn("Two rework", result["blocker"]["reason"])

    def test_revision_preserves_old_worktree_and_invalidates_authority(self):
        self.begin(); path = self.implement()
        e.revise(self.root, "Synthetic material change in scope.")
        value = e.load(self.root)
        self.assertTrue(path.is_dir())
        self.assertEqual(value["authorizations"], {})
        self.assertEqual(value["previous_versions"][0]["tasks"]["value"]["path"], str(path))
        e.set_plan(self.root, plan())
        e.authorize(self.root, "implement", "fixture-user", "Fresh synthetic request.")
        e.start(self.root, within_request=True)
        e.prepare_task(self.root, "value")
        self.assertNotEqual(str(path), e.load(self.root)["tasks"]["value"]["path"])

    def test_block_resume_restores_state(self):
        self.begin()
        e.block(self.root, "Synthetic unavailable capability.")
        self.assertEqual(e.status(self.root)["state"], "BLOCKED")
        e.resume(self.root, "Synthetic capability restored and checked.")
        self.assertEqual(e.status(self.root)["state"], "IMPLEMENTING")

    def test_worktree_lookup_has_canonical_primary_identity(self):
        self.begin(); e.prepare_task(self.root, "value")
        path = e.load(self.root)["tasks"]["value"]["path"]
        self.assertEqual(str(e.git_ops.canonical_repo(path)), str(self.repo))
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), "list", "--repo", path, "--store", str(self.home / "runs")], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)["result"][0]["root"], str(self.root))

    def test_dependency_integration_is_ordered_and_preserves_reviewed_content(self):
        value = plan()
        second = copy.deepcopy(value["tasks"][0])
        second.update(id="second", depends_on=["value"])
        second["gates"] = [gate([sys.executable, "-B", "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'final\\n'"])]
        value["tasks"].append(second)
        value["verification"][0]["command"] = second["gates"][0]["command"]
        self.begin(value)
        with self.assertRaisesRegex(e.RunError, "not verified"):
            e.prepare_task(self.root, "second")
        self.implement(); self.verify()
        self.implement("second", {"value.txt": "final\n"}); self.verify("second")
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Synthetic dependency integration review.")
        self.assertEqual(e.ready(self.root)["state"], "READY_TO_PUBLISH")


if __name__ == "__main__":
    unittest.main()
