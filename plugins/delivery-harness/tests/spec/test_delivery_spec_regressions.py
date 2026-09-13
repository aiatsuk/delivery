"""Adversarial regressions for the bundled engine; all actors are explicit test actors."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from . import test_spec_flow as fixtures
except ImportError:
    import test_spec_flow as fixtures

engine = fixtures.spec_flow
args = fixtures.args


class FallbackYamlRegressionTests(unittest.TestCase):
    def test_malformed_flow_yaml_fails_closed_without_pyyaml(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(sys.modules, {"yaml": None}):
            root = Path(directory)
            config_path = root / ".spec/verification.yml"
            config_path.parent.mkdir()
            config_path.write_text("verification:\n  - [oops\n", encoding="utf-8")
            config = engine.resolve_config(root)
            self.assertIn("_error", config)
            self.assertTrue(engine.validate_check(fixtures.minimal_check(), config))
            self.assertEqual("deep", engine.suggest_mode(["src/payments/service.py"], config)["mode"])

    def test_unsupported_yaml_is_an_error_instead_of_a_silent_scalar(self):
        for content in (
            "verification:\n  critical_areas: [payments, auth]\n",
            "verification:\n  critical_areas: {payments: true}\n",
            "verification:\n  default_mode: &choice lite\n",
            "verification:\n  default_mode: *choice\n",
            "verification:\n  default_mode: !!str lite\n",
            "verification:\n  default_mode: |\n    lite\n",
            "verification:\n  default_mode: \"unterminated\n",
            "verification:\n  default_mode: 'unterminated\n",
            "verification:\n  value: \x00invalid\n",
        ):
            with self.subTest(content=content), self.assertRaises(engine.FlowError) as error:
                engine.parse_simple_yaml(content)
            self.assertEqual("config_invalid", error.exception.code)

    def test_duplicate_keys_and_invalid_indentation_are_rejected(self):
        for content in (
            "verification:\n  minimum_iterations: 3\n  minimum_iterations: 1\n",
            "verification:\n  minimum_iterations: 3\n    default_mode: lite\n",
            "verification:\n  critical_areas:\n    - payments\n     - auth\n",
            "  verification:\n    minimum_iterations: 1\n",
            "verification:\n  critical_areas:\n    -\n      nested: mapping\n",
        ):
            with self.subTest(content=content), self.assertRaises(engine.FlowError):
                engine.parse_simple_yaml(content)

    def test_quoted_hashes_and_single_quote_escaping_preserve_values(self):
        parsed = engine.parse_simple_yaml(
            "evidence:\n  logs: \"proof # 1\" # outside comment\n"
            "  reports: 'it''s # proof' # outside comment\n"
            "  query: \"https://example.invalid/a#section\"\n"
        )
        self.assertEqual("proof # 1", parsed["evidence"]["logs"])
        self.assertEqual("it's # proof", parsed["evidence"]["reports"])
        self.assertEqual("https://example.invalid/a#section", parsed["evidence"]["query"])

    def test_the_shipped_configuration_works_without_optional_dependencies(self):
        template = engine.ASSET_ROOT / "verification-config.template.yaml"
        with mock.patch.dict(sys.modules, {"yaml": None}):
            parsed = engine.load_config_file(template)
        self.assertEqual(3, parsed["verification"]["minimum_iterations"])
        self.assertIn("payments", parsed["verification"]["critical_areas"])
        self.assertEqual("docker compose up -d", parsed["test_environment"]["start_services"])

    def test_non_mapping_json_is_not_silently_replaced_by_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verification.json"
            for value in ([], None, False, 42, "not a mapping"):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.subTest(value=value):
                    self.assertIn("_error", engine.resolve_config(Path(directory), str(path)))

    def test_critical_area_cannot_be_downgraded_by_config_or_explicit_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "verification.yml"
            config.write_text("verification:\n  default_mode: lite\n", encoding="utf-8")
            initialized = engine.cmd_check_init(args(
                root=str(root / "check"), session_id="critical", title="Critical path",
                rigor="standard", mode="lite", source_file=None, config=str(config),
                path=["src/authentication/session.py"], hint=[],
            ))
            self.assertEqual("deep", initialized["check_mode"])


class ApprovalAndDiffRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SpecFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.base = self.fixture.root, self.fixture.base

    def approved(self, planned=("src",)):
        self.fixture.initialize()
        self.fixture.write_ready_package()
        self.fixture.set_state("WAITING_APPROVAL")
        return engine.cmd_approve(args(
            root=str(self.root), actor="test-reviewer", note="Test-only explicit baseline approval",
            planned=list(planned), expected_revision=0,
        ))

    def implementing(self, planned=("src",)):
        approved = self.approved(planned)
        return engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))

    def snapshots(self):
        before, after = self.base / "before", self.base / "after"
        (before / "src").mkdir(parents=True)
        (after / "src").mkdir(parents=True)
        (before / "src/code.txt").write_text("before", encoding="utf-8")
        (after / "src/code.txt").write_text("after", encoding="utf-8")
        return before, after

    def reviewed(self):
        state = self.implementing()
        before, after = self.snapshots()
        return engine.cmd_begin_verify(args(root=str(self.root), before=str(before), after=str(after), planned=None, expected_revision=state["revision"]))

    def verifying(self):
        state = self.reviewed()
        return engine.cmd_advance(args(root=str(self.root), to="VERIFYING", expected_revision=state["revision"]))

    def complete(self):
        return engine.cmd_complete(args(root=str(self.root), verdict="PASS", report="verification-report.md", expected_revision=None))

    def test_approval_remains_separate_from_apply(self):
        result = self.approved()
        self.assertEqual("APPROVED", result["state"])
        self.assertFalse((self.root / "implementation-evidence.md").exists())
        self.assertEqual(["src"], result["approval"]["expected_change_surface"])

    def test_empty_actor_cannot_record_approval(self):
        self.fixture.initialize()
        self.fixture.write_ready_package()
        self.fixture.set_state("WAITING_APPROVAL")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_approve(args(root=str(self.root), actor="  ", note="", expected_revision=0))
        self.assertEqual("actor_required", error.exception.code)
        self.assertEqual(0, engine.read_session(self.root)["revision"])

    def test_replacing_baseline_does_not_rebind_existing_approval(self):
        approved = self.approved()
        (self.root / "design.md").write_text("# Changed design\nDifferent contract.\n", encoding="utf-8")
        session = engine.read_session(self.root)
        session["baseline"] = {**engine.semantic_fingerprint(self.root), "expected_change_surface": ["src"]}
        engine.atomic_write_json(self.root / engine.SESSION_NAME, session)
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("approval_stale", error.exception.code)

    def test_both_test_and_evaluation_plans_are_bound(self):
        approved = self.approved()
        (self.root / "evaluation-plan.md").write_text("# Evaluation\nNew oracle.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("baseline_drift", error.exception.code)
        self.assertIn("evaluation-plan.md", error.exception.details["delta"]["added"])

    def test_non_task_checkboxes_remain_semantic(self):
        self.fixture.initialize()
        self.fixture.write_ready_package()
        self.fixture.set_state("WAITING_APPROVAL")
        path = self.root / "design.md"
        path.write_text("# Design\n- [ ] Allow paid calls.\n", encoding="utf-8")
        approved = self.fixture.approve()
        path.write_text("# Design\n- [x] Allow paid calls.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("baseline_drift", error.exception.code)

    def test_new_capability_spec_invalidates_approval(self):
        approved = self.approved()
        extra = self.root / "specs/extra/spec.md"
        extra.parent.mkdir()
        extra.write_text("# Extra\nREQ-002 MUST charge a fee.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertIn("specs/extra/spec.md", error.exception.details["delta"]["added"])

    def test_planned_surface_cannot_change_after_approval(self):
        approved = self.approved()
        session = engine.read_session(self.root)
        session["expected_change_surface"].append("config")
        engine.atomic_write_json(self.root / engine.SESSION_NAME, session)
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("planned_surface_drift", error.exception.code)

    def test_begin_verify_cannot_expand_the_approved_surface(self):
        state = self.implementing()
        before, after = self.snapshots()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_begin_verify(args(root=str(self.root), before=str(before), after=str(after), planned=["config"], expected_revision=state["revision"]))
        self.assertEqual("planned_surface_drift", error.exception.code)
        self.assertFalse((self.root / "actual-diff.json").exists())

    def test_begin_verify_cannot_skip_baseline_approval(self):
        self.fixture.initialize()
        self.fixture.write_ready_package()
        self.fixture.set_state("IMPLEMENTING")
        before, after = self.snapshots()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_begin_verify(args(root=str(self.root), before=str(before), after=str(after), planned=None, expected_revision=0))
        self.assertEqual("approval_missing", error.exception.code)

    def test_advance_cannot_bypass_actual_diff_capture(self):
        state = self.implementing()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_advance(args(root=str(self.root), to="POST_IMPLEMENTATION_REVIEW", expected_revision=state["revision"]))
        self.assertEqual("actual_diff_required", error.exception.code)

    def test_changes_after_inventory_block_verification(self):
        state = self.reviewed()
        (self.base / "after/unreviewed.txt").write_text("unexpected", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_advance(args(root=str(self.root), to="VERIFYING", expected_revision=state["revision"]))
        self.assertEqual("actual_diff_drift", error.exception.code)

    def test_changes_after_verification_block_completion(self):
        self.verifying()
        (self.base / "after/src/code.txt").write_text("new untested behavior", encoding="utf-8")
        (self.root / "verification-report.md").write_text("Verdict: PASS\nObserved fixture checks passed.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            self.complete()
        self.assertEqual("actual_diff_drift", error.exception.code)

    def test_diff_file_tampering_is_not_review_evidence(self):
        self.verifying()
        inventory = json.loads((self.root / "actual-diff.json").read_text(encoding="utf-8"))
        inventory["files"] = []
        engine.atomic_write_json(self.root / "actual-diff.json", inventory)
        (self.root / "verification-report.md").write_text("Verdict: PASS\nObserved fixture checks passed.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            self.complete()
        self.assertEqual("actual_diff_drift", error.exception.code)

    def test_unfilled_report_is_not_completion_evidence(self):
        self.verifying()
        with self.assertRaises(engine.FlowError) as error:
            self.complete()
        self.assertEqual("verification_report_incomplete", error.exception.code)

    def test_baseline_drift_after_apply_blocks_completion(self):
        self.verifying()
        (self.root / "design.md").write_text("# Changed\nNew behavior after implementation.\n", encoding="utf-8")
        (self.root / "verification-report.md").write_text("Verdict: PASS\nObserved fixture checks passed.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            self.complete()
        self.assertEqual("baseline_drift", error.exception.code)

    def test_reopen_preserves_semantic_approval_and_reviews_new_surface(self):
        self.verifying()
        previous_approval = engine.read_session(self.root)["approval"]
        (self.root / "verification-report.md").write_text("Verdict: PASS\nResults for the first integration.\n", encoding="utf-8")
        self.complete()
        (self.base / "after/config").mkdir()
        (self.base / "after/config/service.toml").write_text("setting = 1", encoding="utf-8")
        reopened = engine.cmd_reopen_verify(args(root=str(self.root), before=str(self.base / "before"), after=str(self.base / "after"), reason="Test integration rebased onto a new base", expected_revision=None))
        self.assertEqual("POST_IMPLEMENTATION_REVIEW", reopened["state"])
        self.assertEqual("matching", reopened["baseline_status"])
        self.assertEqual(previous_approval, reopened["approval"])
        self.assertEqual(2, reopened["verification_generation"])
        self.assertEqual(["config/service.toml"], [item["path"] for item in reopened["material_findings"]])
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_advance(args(root=str(self.root), to="VERIFYING", expected_revision=None))
        self.assertEqual("post_review_incomplete", error.exception.code)

    def test_reopen_refuses_semantic_drift_without_reapproval(self):
        self.verifying()
        (self.root / "design.md").write_text("# New semantics\nA changed contract.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_reopen_verify(args(root=str(self.root), before=str(self.base / "before"), after=str(self.base / "after"), reason="Test rebase", expected_revision=None))
        self.assertEqual("baseline_drift", error.exception.code)
        self.assertEqual(1, engine.read_session(self.root)["verification_generation"])

    def test_reopen_does_not_erase_a_required_baseline_revision(self):
        self.reviewed()
        session = engine.read_session(self.root)
        session["material_findings"] = [{"id": "DIFF-001", "path": "src/code.txt", "status": "dispositioned", "disposition": "baseline-revision-required"}]
        engine.atomic_write_json(self.root / engine.SESSION_NAME, session)
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_reopen_verify(args(root=str(self.root), before=str(self.base / "before"), after=str(self.base / "after"), reason="Test rebase", expected_revision=None))
        self.assertEqual("post_review_incomplete", error.exception.code)

    def test_reopen_requires_a_reason(self):
        self.verifying()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_reopen_verify(args(root=str(self.root), before=str(self.base / "before"), after=str(self.base / "after"), reason=" ", expected_revision=None))
        self.assertEqual("reason_required", error.exception.code)

    def test_snapshot_tracks_executable_and_directory_symlink_changes(self):
        before, after = self.snapshots()
        (before / "tool").write_text("run", encoding="utf-8")
        (after / "tool").write_text("run", encoding="utf-8")
        (before / "tool").chmod(0o644)
        (after / "tool").chmod(0o755)
        (before / "linked").symlink_to("src", target_is_directory=True)
        (after / "linked").symlink_to("elsewhere", target_is_directory=True)
        (before / ".git").mkdir()
        (after / ".git").write_text("gitdir: /unused/metadata", encoding="utf-8")
        (after / "__pycache__").mkdir()
        (after / "__pycache__/material.txt").write_text("included", encoding="utf-8")
        inventory = engine.tree_diff(before, after)
        paths = {item["path"] for item in inventory["files"]}
        self.assertEqual({"src/code.txt", "tool", "linked", "__pycache__/material.txt"}, paths)


class ExecutionAuthorizationRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CheckFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.base = self.fixture.root, self.fixture.base

    def authorize(self, scope="safe", acknowledge=""):
        return engine.cmd_check_authorize(args(root=str(self.root), actor="test-operator", scope=scope, note="Test-only execution authority", acknowledge=acknowledge, expected_revision=None))

    def test_scope_cannot_authorize_a_more_dangerous_case(self):
        self.fixture.reach_brief_ready()
        check = engine.load_check(self.root)
        check["cases"][0]["safety"] = "destructive"
        self.fixture.write_check(check)
        with self.assertRaises(engine.FlowError) as error:
            self.authorize("approved", "Test fixture permits shared staging only.")
        self.assertEqual("check_execution_blocked", error.exception.code)
        self.assertEqual("CHECK_BRIEF_READY", engine.read_session(self.root)["state"])
        self.assertIsNone(engine.read_session(self.root)["authorization"])

    def test_whitespace_is_not_an_acknowledgement(self):
        self.fixture.reach_brief_ready()
        with self.assertRaises(engine.FlowError) as error:
            self.authorize("all", "   ")
        self.assertEqual("acknowledgement_required", error.exception.code)

    def test_explicit_scope_and_acknowledgement_allow_the_test_plan(self):
        self.fixture.reach_brief_ready()
        check = engine.load_check(self.root)
        check["cases"][0]["safety"] = "destructive"
        self.fixture.write_check(check)
        state = self.authorize("all", "Test fixture: accepts the described destructive sandbox effects.")
        self.assertEqual("CHECK_EXECUTING", state["state"])
        guarded = engine.cmd_check_guard(args(root=str(self.root)))
        self.assertTrue(guarded["ok"])
        self.assertEqual("test-operator", guarded["authorization"]["actor"])

    def test_execution_guard_refuses_a_brief_without_authorization(self):
        self.fixture.reach_brief_ready()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_guard(args(root=str(self.root)))
        self.assertEqual("execution_not_allowed", error.exception.code)

    def test_new_blocking_question_prevents_authorization(self):
        self.fixture.reach_brief_ready()
        check = engine.load_check(self.root)
        check["open_questions"] = [{"id": "Q-001", "question": "Which account?", "why": "Scope is unresolved", "blocking": True, "owner": "test-operator"}]
        self.fixture.write_check(check)
        with self.assertRaises(engine.FlowError) as error:
            self.authorize()
        self.assertEqual("check_execution_blocked", error.exception.code)

    def test_missing_capability_prevents_authorization(self):
        self.fixture.reach_brief_ready()
        check = engine.load_check(self.root)
        check["environment"]["missing"] = [{"capability": "ledger read access", "blocks": ["CASE-001"], "request": "Provide sandbox access"}]
        self.fixture.write_check(check)
        self.assertFalse(engine.evaluate_check(self.root)["readiness"]["ready_to_execute"])
        with self.assertRaises(engine.FlowError) as error:
            self.authorize()
        self.assertEqual("check_execution_blocked", error.exception.code)

    def test_check_edits_after_authorization_block_execution_and_completion(self):
        self.fixture.reach_brief_ready()
        self.authorize()
        check = engine.load_check(self.root)
        check["cases"][0]["steps"][0]["action"] = "Use a different account."
        self.fixture.write_check(check)
        self.assertFalse(engine.evaluate_check(self.root)["readiness"]["ready_to_execute"])
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_guard(args(root=str(self.root)))
        self.assertEqual("authorization_drift", error.exception.code)
        (self.root / "verification-report.md").write_text("Verdict: PASS\nTest-only results.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_complete(args(root=str(self.root), verdict="PASS", report="verification-report.md", expected_revision=None))
        self.assertEqual("authorization_drift", error.exception.code)

    def test_authored_source_edits_invalidate_execution_authority(self):
        self.fixture.reach_brief_ready()
        self.authorize()
        (self.root / "source.md").write_text("# Changed\nDifferent requested scope.\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_guard(args(root=str(self.root)))
        self.assertEqual("authorization_drift", error.exception.code)

    def test_explicit_config_binding_survives_external_package_and_detects_drift(self):
        config = self.base / "project/verification.yml"
        config.parent.mkdir()
        config.write_text("verification:\n  minimum_iterations: 3\n", encoding="utf-8")
        self.fixture.reach_brief_ready()
        session = engine.read_session(self.root)
        session["verification_config_path"] = str(config)
        engine.atomic_write_json(self.root / engine.SESSION_NAME, session)
        self.authorize()
        config.write_text("verification:\n  minimum_iterations: 5\n", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_guard(args(root=str(self.root)))
        self.assertEqual("authorization_drift", error.exception.code)

    def test_check_diff_rejects_stale_revision_and_running_session(self):
        state = self.fixture.reach_brief_ready()
        before, after = self.base / "before", self.base / "after"
        before.mkdir()
        after.mkdir()
        (after / "changed.txt").write_text("new", encoding="utf-8")
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_diff(args(root=str(self.root), before=str(before), after=str(after), config=None, expected_revision=state["revision"] - 1))
        self.assertEqual("stale_revision", error.exception.code)
        self.assertFalse((self.root / "actual-diff.json").exists())
        self.authorize()
        with self.assertRaises(engine.FlowError) as error:
            engine.cmd_check_diff(args(root=str(self.root), before=str(before), after=str(after), config=None, expected_revision=None))
        self.assertEqual("diff_not_allowed", error.exception.code)


@unittest.skipUnless(shutil.which("git"), "Git is required for linked-worktree integration tests")
class WorktreeIdentityRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.primary = self.base / "primary"
        self.linked = self.base / "differently-named-task"
        self.store = self.base / "store"
        self.git("init", "-q", "--initial-branch=main", str(self.primary))
        self.git("-C", str(self.primary), "-c", "user.name=Test Fixture", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "Initialize local test fixture")
        self.git("-C", str(self.primary), "worktree", "add", "-q", "-b", "fixture-task", str(self.linked))

    def git(self, *arguments):
        result = subprocess.run(["git", "-c", "user.name=Test Fixture", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", *arguments], text=True, capture_output=True, env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def initialize(self, project=None, **overrides):
        payload = dict(root=None, project=str(project or self.primary), store=str(self.store), session_id="worktree-check", title="Worktree test", rigor="standard", mode="deep", source_file=None, config=None, path=[], hint=[])
        payload.update(overrides)
        return engine.cmd_check_init(args(**payload))

    def test_primary_linked_and_nested_paths_have_one_identity(self):
        nested = self.linked / "src/deep"
        nested.mkdir(parents=True)
        keys = {engine.project_key(path) for path in (self.primary, self.linked, nested)}
        self.assertEqual(1, len(keys))
        self.assertEqual(str(self.primary / ".git"), engine.project_identity(self.linked)["identity"])

    def test_where_and_history_resume_from_another_worktree(self):
        created = self.initialize()
        located = engine.cmd_where(args(project=str(self.linked), session_id="worktree-check", kind="checks", store=str(self.store)))
        self.assertEqual(created["root"], located["path"])
        history = engine.cmd_list(args(project=str(self.linked), store=str(self.store), kind=None, state=None, limit=None))
        self.assertEqual(1, history["count"])
        self.assertEqual(created["root"], history["packages"][0]["path"])
        listed = engine.cmd_where(args(project=str(self.linked), session_id=None, kind="checks", store=str(self.store)))
        self.assertEqual(1, len(listed["packages"]))

    def test_explicit_root_wins_over_repository_store_config(self):
        (self.linked / ".spec").mkdir()
        (self.linked / ".spec/verification.yml").write_text("verification:\n  artifact_store: repo\n", encoding="utf-8")
        explicit = self.base / "run/spec"
        created = self.initialize(project=self.linked, root=str(explicit))
        self.assertEqual(str(explicit), created["root"])
        self.assertFalse(self.store.exists())
        self.assertFalse((self.linked / "spec").exists())
        self.assertEqual(str(self.linked / ".spec/verification.yml"), engine.resolve_config(explicit)["_source"])

    def test_git_environment_cannot_redirect_explicit_repository_identity(self):
        with mock.patch.dict(os.environ, {"GIT_DIR": str(self.base / "unrelated"), "GIT_WORK_TREE": str(self.base)}):
            self.assertEqual(engine.project_key(self.primary), engine.project_key(self.linked))
            self.assertEqual(str(self.primary / ".git"), engine.project_identity(self.linked)["identity"])

    def test_identity_can_resolve_worktree_metadata_without_a_git_executable(self):
        with mock.patch.object(engine.subprocess, "run", side_effect=FileNotFoundError("test: git unavailable")):
            self.assertEqual(engine.project_key(self.primary), engine.project_key(self.linked))

    def test_separate_repositories_do_not_share_identity(self):
        other = self.base / "other/primary"
        self.git("init", "-q", "--initial-branch=main", str(other))
        self.assertNotEqual(engine.project_key(self.primary), engine.project_key(other))

    def test_repo_store_slug_cannot_escape_its_package_directory(self):
        (self.primary / ".spec").mkdir()
        (self.primary / ".spec/verification.yml").write_text("verification:\n  artifact_store: repo\n", encoding="utf-8")
        created = self.initialize(session_id="../../../escape")
        self.assertTrue(Path(created["root"]).is_relative_to(self.primary / "spec/checks"))

    def test_full_cli_medium_flow_and_rebase_reverification(self):
        """No state mocks: real Git worktrees and every lifecycle change goes through the CLI."""
        (self.primary / "src").mkdir()
        (self.primary / "src/value.txt").write_text("one\n", encoding="utf-8")
        self.git("-C", str(self.primary), "add", "src/value.txt")
        self.git("-C", str(self.primary), "commit", "-q", "-m", "Add test baseline")
        self.git("-C", str(self.linked), "rebase", "main")
        package = self.base / "run/spec"

        def cli(command, *arguments, exit_code=0):
            result = subprocess.run([sys.executable, "-S", str(fixtures.MODULE_PATH), command, "--root", str(package), *arguments], cwd=self.base, capture_output=True, text=True)
            self.assertEqual(exit_code, result.returncode, result.stdout + result.stderr)
            return json.loads(result.stdout)

        cli("init", "--session-id", "medium-fixture", "--project", str(self.primary))
        writer = fixtures.SpecFlowTests()
        writer.root = package
        writer.write_ready_package()
        self.assertTrue(cli("validate")["ok"])
        for state in engine.PLANNING_STATES[1:]:
            cli("advance", "--to", state)
        approval = cli("approve", "--actor", "test-reviewer", "--note", "Test-only explicit semantic approval", "--planned", "src")
        self.assertEqual("APPROVED", approval["state"])
        self.assertFalse((package / "implementation-evidence.md").exists())
        cli("apply")
        (self.linked / "src/value.txt").write_text("two\n", encoding="utf-8")
        self.git("-C", str(self.linked), "add", "src/value.txt")
        self.git("-C", str(self.linked), "commit", "-q", "-m", "Update test value")
        review = cli("begin-verify", "--before", str(self.primary), "--after", str(self.linked))
        self.assertEqual(["src/value.txt"], [item["path"] for item in review["actual_change_surface"]])
        self.assertEqual([], review["material_findings"])
        cli("advance", "--to", "VERIFYING")
        report = package / "verification-report.md"
        report.write_text("Verdict: PASS\nObserved test value two in the first integration.\n", encoding="utf-8")
        self.assertEqual("DONE", cli("complete", "--verdict", "PASS")["state"])

        (self.primary / "readme.md").write_text("Updated base for the test rebase.\n", encoding="utf-8")
        self.git("-C", str(self.primary), "add", "readme.md")
        self.git("-C", str(self.primary), "commit", "-q", "-m", "Advance test base")
        self.git("-C", str(self.linked), "rebase", "main")
        reopened = cli("reopen-verify", "--before", str(self.primary), "--after", str(self.linked), "--reason", "Test integration rebased onto the new main revision")
        self.assertEqual(approval["approval"], reopened["approval"])
        self.assertEqual(2, reopened["verification_generation"])
        self.assertEqual("POST_IMPLEMENTATION_REVIEW", reopened["state"])
        cli("advance", "--to", "VERIFYING")
        stale = cli("complete", "--verdict", "PASS", exit_code=1)
        self.assertEqual("verification_report_stale", stale["error"])
        report.write_text("Verdict: PASS\nObserved test value two after the new base was integrated.\n", encoding="utf-8")
        self.assertEqual("DONE", cli("complete", "--verdict", "PASS")["state"])


class StandaloneBundleRegressionTests(unittest.TestCase):
    def test_relocated_engine_uses_only_its_bundled_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            copied = base / "independent-engine"
            shutil.copytree(engine.SKILL_ROOT, copied)
            root = base / "run/spec"
            result = subprocess.run([sys.executable, "-S", str(copied / "scripts/spec_flow.py"), "init", "--root", str(root), "--session-id", "relocated"], cwd=base, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(str(root.resolve()), json.loads(result.stdout)["root"])
            self.assertTrue((root / "specs/example/spec.md").is_file())
            self.assertFalse(list(copied.rglob("SKILL.md")))

    def test_malformed_yaml_makes_the_dependency_free_cli_gate_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "run/spec"
            (base / ".spec").mkdir()
            (base / ".spec/verification.yml").write_text("verification:\n  - [oops\n", encoding="utf-8")
            initialized = subprocess.run([sys.executable, "-S", str(fixtures.MODULE_PATH), "check-init", "--root", str(root), "--session-id", "bad-config"], cwd=base, capture_output=True, text=True)
            self.assertEqual(0, initialized.returncode, initialized.stdout + initialized.stderr)
            result = subprocess.run([sys.executable, "-S", str(fixtures.MODULE_PATH), "check-validate", "--root", str(root)], cwd=base, capture_output=True, text=True)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertTrue(any("verification config" in error for error in json.loads(result.stdout)["errors"]))


if __name__ == "__main__":
    unittest.main()
