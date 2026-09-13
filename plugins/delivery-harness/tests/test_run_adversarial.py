"""Adversarial lifecycle guards, using temporary repositories and synthetic events.

These fixtures do not represent live host jobs or actual human authorization.
Any simulated side effect stays under the test's temporary directory.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
from unittest import mock

from tests.test_run_engine import RunFixture, e, gate, git, plan


def dependent_plan(*, shared_resource=False, shared_file=False):
    value = plan()
    first = value["tasks"][0]
    first["gates"] = [gate([sys.executable, "-B", "-c",
                            "from pathlib import Path; assert Path('value.txt').read_text().startswith('after')"])]
    second = copy.deepcopy(first)
    second.update(id="second", title="Use the verified dependency", depends_on=["value"],
                  paths=["value.txt"] if shared_file else ["second.txt"])
    second_name = "value.txt" if shared_file else "second.txt"
    second["gates"] = [gate([sys.executable, "-B", "-c",
                             f"from pathlib import Path; assert Path({second_name!r}).read_text() == 'final\\n'"])]
    if shared_resource:
        first["resources"] = second["resources"] = ["fixture-exclusive-device"]
    value["tasks"].append(second)
    value["verification"][0]["command"] = second["gates"][0]["command"]
    return value


class AdversarialRunTests(RunFixture):
    def test_readiness_rejects_new_unplanned_integration_files(self):
        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        integration = Path(e.load(self.root)["integration"]["path"])
        (integration / "unplanned.txt").write_text("Outside the approved file scope.\n")
        git(integration, "add", "--", "unplanned.txt")
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Synthetic review of the current files.")
        with self.assertRaises(e.RunError, msg="An integrated review cannot widen the approved implementation scope."):
            e.ready(self.root)

    def test_old_dispatch_report_cannot_complete_replacement_handle(self):
        self.begin()
        self.implement()
        old_report = copy.deepcopy(e.load(self.root)["tasks"]["value"]["report"])
        e.rework(self.root, "value", "Collect a fresh result from a replacement dispatch.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:replacement-handle")
        with self.assertRaises(e.RunError, msg="The prior dispatch's source event must not complete a replacement handle."):
            e.report_task(self.root, "value", "fixture-writer-value", old_report)

    def test_blocked_run_cannot_register_an_implementation_dispatch(self):
        self.begin()
        e.prepare_task(self.root, "value")
        e.block(self.root, "Synthetic capability is unavailable; no dispatch may proceed.")
        with self.assertRaises(e.RunError, msg="BLOCKED must guard dispatch registration, not only gate execution."):
            e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:blocked-dispatch")

    def test_dependency_rework_invalidates_previously_verified_descendant(self):
        self.begin(dependent_plan())
        self.implement()
        self.verify()
        self.implement("second", {"second.txt": "final\n"})
        self.verify("second")
        e.rework(self.root, "value", "The dependency's reviewed behavior has changed.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:dependency-rework")
        first = Path(e.load(self.root)["tasks"]["value"]["path"])
        (first / "value.txt").write_text("after revised\n")
        git(first, "add", "--", "value.txt")
        self.report()
        self.verify()
        with self.assertRaises(e.RunError, msg="A descendant tested against the previous dependency patch must be invalidated."):
            e.verify_task_current(e.load(self.root), "second")

    def test_rework_cannot_reclaim_a_live_descendants_exclusive_resource(self):
        self.begin(dependent_plan(shared_resource=True))
        self.implement()
        self.verify()
        e.prepare_task(self.root, "second")
        e.register_agent(self.root, "second", "fixture-writer-second", "unit-test", "synthetic:exclusive-resource-holder")
        refused = False
        try:
            e.rework(self.root, "value", "Recheck the dependency while the descendant owns the fixture device.")
            e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:conflicting-resource-claim")
        except e.RunError:
            refused = True
        self.assertTrue(refused, "Rework or redispatch must refuse concurrent ownership of the same exclusive resource.")

    def test_previous_implementer_cannot_become_independent_reviewer_after_replacement(self):
        self.begin()
        self.implement()
        e.rework(self.root, "value", "A different implementation actor must return the next result.")
        e.register_agent(self.root, "value", "replacement-writer", "unit-test", "synthetic:replacement-writer")
        e.report_task(self.root, "value", "replacement-writer", {
            "summary": "Synthetic replacement confirms the existing fixture edit.",
            "tests": "Host gate remains pending.", "limitations": "Unit fixture only.",
            "source_event": "synthetic replacement result event",
            "dispatch_id": e.load(self.root)["tasks"]["value"]["agent"]["dispatch_id"],
        })
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        with self.assertRaises(e.RunError, msg="Independent review must exclude every actor who implemented this task, not only its latest actor."):
            e.review(self.root, "fixture-writer-value", "PASS", "Synthetic prior implementer review.", task_id="value")

    def test_reviewer_whitespace_cannot_disguise_the_implementation_identity(self):
        self.begin()
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])
        with self.assertRaises(e.RunError) as refused:
            e.review(self.root, " fixture-writer-value ", "PASS", "Synthetic whitespace alias of the writer.", task_id="value")
        self.assertEqual(refused.exception.code, "reviewer_not_independent")

    def test_failed_integration_preserves_owned_worktree_for_a_safe_retry(self):
        self.begin()
        self.implement()
        self.verify()
        failure = e.git_ops.GitError("INJECTED_TEST_FAILURE", "Synthetic failure after worktree creation.")
        with mock.patch.object(e.git_ops, "integrate_patches", side_effect=failure):
            with self.assertRaises(e.git_ops.GitError):
                e.integrate(self.root)
        try:
            result = e.integrate(self.root)
        except (e.RunError, e.git_ops.GitError) as exc:
            self.fail(f"An integration retry must resume its already-owned clean worktree; got {exc.code}: {exc}")
        self.assertEqual(result["state"], "VERIFYING")

    def test_gate_authority_for_one_case_does_not_authorize_another_side_effect(self):
        value = plan()
        value["tasks"][0]["gates"][0]["risk"] = "external"
        sentinel = self.home / "unapproved-side-effect.txt"
        value["tasks"][0]["gates"].append(gate([
            sys.executable, "-B", "-c",
            f"from pathlib import Path; Path({str(sentinel)!r}).write_text('synthetic side effect')",
        ], risk="external"))
        self.begin(value)
        self.implement()
        e.authorize(self.root, "test-external", "fixture-user",
                    "Synthetic narrowly bounded approval for the first fixture gate.", targets=["value:0"])
        refused = False
        try:
            e.execute_gate(self.root, task_id="value", gate_index=1)
        except e.RunError:
            refused = True
        self.assertTrue(refused and not sentinel.exists(),
                        "A narrow test approval needs a structural gate binding; the other side effect ran.")

    def test_test_authority_requires_explicit_targets_and_all_is_opt_in(self):
        value = plan()
        value["tasks"][0]["gates"][0]["risk"] = "external"
        self.begin(value)
        with self.assertRaises(e.RunError) as missing:
            e.authorize(self.root, "test-external", "fixture-user", "Synthetic approval without a structural scope.")
        self.assertEqual(missing.exception.code, "authorization_targets_required")
        with self.assertRaises(e.RunError) as wrong_risk:
            e.authorize(self.root, "test-external", "fixture-user", "Synthetic wrong-risk target.", targets=["value-check"])
        self.assertEqual(wrong_risk.exception.code, "invalid_authorization_target")
        granted = e.authorize(self.root, "test-external", "fixture-user", "Synthetic explicit approval of all matching-risk fixture gates.", targets=["*"])
        self.assertEqual(set(granted["authorizations"]["test-external"]["targets"]), {"value:0"})
        self.assertTrue(granted["authorizations"]["test-external"]["all_matching"])
        self.implement()
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])

    def test_report_requires_dispatch_binding_and_cannot_reuse_a_host_event(self):
        self.begin()
        self.implement()
        previous = copy.deepcopy(e.load(self.root)["tasks"]["value"]["report"])
        e.rework(self.root, "value", "Synthetic new dispatch to the existing host handle.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:value")
        missing = {key: value for key, value in previous.items() if key != "dispatch_id"}
        with self.assertRaises(e.RunError) as absent:
            e.report_task(self.root, "value", "fixture-writer-value", missing)
        self.assertEqual(absent.exception.code, "dispatch_mismatch")
        previous["dispatch_id"] = e.load(self.root)["tasks"]["value"]["agent"]["dispatch_id"]
        with self.assertRaises(e.RunError) as replay:
            e.report_task(self.root, "value", "fixture-writer-value", previous)
        self.assertEqual(replay.exception.code, "source_event_reused")

    def test_invalidated_descendant_can_complete_a_fresh_attempt_without_losing_old_work(self):
        self.begin(dependent_plan())
        first = self.implement()
        self.verify()
        old_second = self.implement("second", {"second.txt": "final\n"})
        self.verify("second")
        e.rework(self.root, "value", "Synthetic dependency behavior revision.")
        e.register_agent(self.root, "value", "fixture-writer-value", "unit-test", "synthetic:dependency-rework")
        (first / "value.txt").write_text("after revised\n")
        git(first, "add", "--", "value.txt")
        self.report()
        self.verify()
        current = e.prepare_task(self.root, "second")["tasks"]["second"]
        new_second = Path(current["path"])
        self.assertEqual(current["attempt"], 2)
        self.assertNotEqual(old_second, new_second)
        self.assertEqual((new_second / "value.txt").read_text(), "after revised\n")
        self.assertEqual((old_second / "value.txt").read_text(), "after\n")
        self.assertEqual((old_second / "second.txt").read_text(), "final\n")
        self.assertIn(str(old_second), {attempt["path"] for attempt in current["previous_attempts"]})
        self.assertFalse((new_second / "second.txt").exists())
        e.register_agent(self.root, "second", "fixture-writer-second", "unit-test", "synthetic:dependent-attempt-2")
        (new_second / "second.txt").write_text("final\n")
        git(new_second, "add", "--", "second.txt")
        self.report("second")
        self.verify("second")
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-final-reviewer", "PASS", "Synthetic current dependency attempt review.")
        self.assertEqual(e.ready(self.root)["state"], "READY_TO_PUBLISH")

    def test_dependency_preparation_resumes_after_a_recorded_patch_failure(self):
        self.begin(dependent_plan())
        self.implement()
        self.verify()
        failure = e.git_ops.GitError("INJECTED_TEST_FAILURE", "Synthetic dependency preflight failure.")
        with mock.patch.object(e.git_ops, "integrate_patches", side_effect=failure):
            with self.assertRaises(e.git_ops.GitError):
                e.prepare_task(self.root, "second")
        record = e._load_worktree_receipt(e.load(self.root), "task-second", 1)
        self.assertEqual(record["phase"], "CREATED")
        self.assertTrue(Path(record["path"]).is_dir())
        resumed = e.prepare_task(self.root, "second")["tasks"]["second"]
        self.assertEqual(resumed["path"], record["path"])
        self.assertEqual(resumed["status"], "PREPARED")
        self.assertEqual((Path(resumed["path"]) / "value.txt").read_text(), "after\n")

    def test_dependency_preparation_resumes_after_a_recorded_commit_failure(self):
        self.begin(dependent_plan())
        self.implement()
        self.verify()
        failure = e.git_ops.GitError("INJECTED_TEST_FAILURE", "Synthetic commit failure after staged dependencies.")
        with mock.patch.object(e.git_ops, "commit_changes", side_effect=failure):
            with self.assertRaises(e.git_ops.GitError):
                e.prepare_task(self.root, "second")
        record = e._load_worktree_receipt(e.load(self.root), "task-second", 1)
        self.assertEqual(record["phase"], "DEPENDENCIES_APPLIED")
        self.assertEqual(record["checkpoint"]["staged"], ["value.txt"])
        resumed = e.prepare_task(self.root, "second")["tasks"]["second"]
        self.assertEqual(resumed["path"], record["path"])
        self.assertEqual(resumed["status"], "PREPARED")
        self.assertEqual(git(Path(resumed["path"]), "status", "--porcelain"), "")

    def test_worktree_without_creation_receipt_is_never_adopted(self):
        self.begin()
        run = e.load(self.root)
        path, branch, _ = e._worktree_location(run, "task-value", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        e.git_ops.create_worktree(run["primary"], str(path), branch, run["base_sha"])
        with self.assertRaises(e.git_ops.GitError) as refused:
            e.prepare_task(self.root, "value")
        self.assertEqual(refused.exception.code, "WORKTREE_PATH_IN_USE")
        self.assertTrue(path.is_dir())
        self.assertNotIn("value", e.load(self.root)["tasks"])

    def test_changed_checkpoint_is_preserved_and_refuses_integration_retry(self):
        self.begin()
        self.implement()
        self.verify()
        failure = e.git_ops.GitError("INJECTED_TEST_FAILURE", "Synthetic integration preflight failure.")
        with mock.patch.object(e.git_ops, "integrate_patches", side_effect=failure):
            with self.assertRaises(e.git_ops.GitError):
                e.integrate(self.root)
        record = e._load_worktree_receipt(e.load(self.root), "integration", 1)
        sentinel = Path(record["path"]) / "personal.txt"
        sentinel.write_text("Preserve this untracked file.\n")
        with self.assertRaises(e.RunError) as refused:
            e.integrate(self.root)
        self.assertEqual(refused.exception.code, "worktree_checkpoint_drift")
        self.assertEqual(sentinel.read_text(), "Preserve this untracked file.\n")

    def test_run_evidence_store_cannot_be_inside_a_linked_worktree(self):
        self.begin()
        e.prepare_task(self.root, "value")
        linked = Path(e.load(self.root)["tasks"]["value"]["path"])
        with self.assertRaises(e.RunError, msg="External evidence must stay outside every checkout of the repository."):
            e.create(str(linked), "nested-evidence", "Synthetic linked-checkout discovery.",
                     store=str(linked / "delivery-evidence"))
