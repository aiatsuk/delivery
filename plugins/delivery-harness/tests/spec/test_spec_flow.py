from __future__ import annotations

import copy
import importlib.util
import json
import os
import random
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).resolve().parents[2] / "skills/delivery/internal/spec/scripts/spec_flow.py"
SPEC = importlib.util.spec_from_file_location("spec_flow", MODULE_PATH)
assert SPEC and SPEC.loader
spec_flow = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = spec_flow
SPEC.loader.exec_module(spec_flow)


def args(**values):
    return SimpleNamespace(**values)


class SpecFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "change"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self, intent_file: str | None = None) -> dict:
        return spec_flow.cmd_init(
            args(
                root=str(self.root),
                session_id="checkout-recovery",
                title="Checkout recovery",
                rigor="standard",
                intent_file=intent_file,
            )
        )

    def write_ready_package(self) -> None:
        contents = {
            "intent.md": "# Intent\n\n## Raw input\nKeep offline mode.\n\n## Fidelity map\noffline -> REQ-001\n",
            "feature-model.md": "# Feature model\n\nL0 L1 L2 L3 L4 L5 L6 L7\n",
            "proposal.md": "# Proposal\n\nOutcome and non-goals.\n",
            "decisions.md": "# Decisions\n\nNo blocking decisions.\n",
            "design.md": "# Design\n\nState, contracts, recovery.\n",
            "impact-analysis.md": "# Impact\n\nRISK-001 with protected invariant.\n",
            "verification-cases.md": "# Verification\n\nVER-001 authoritative oracle.\n",
            "test-plan.md": "# Test plan\n\nREQ-001 -> TEST-001.\n",
            "tasks.md": "# Tasks\n\n- [ ] TASK-001 implement and verify.\n",
            "review.md": "# Review\n\nReadiness: READY\nCritical findings: 0\n",
            "review-brief.md": "# Review Brief\n\nSpec version: 1\nReadiness: READY\nStop for approval.\n",
            "specs/example/spec.md": "# Capability\n\nREQ-001 MUST preserve offline mode.\n",
        }
        for relative, content in contents.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def set_state(self, state: str, revision: int = 0) -> dict:
        session = spec_flow.read_session(self.root)
        session["state"] = state
        session["revision"] = revision
        spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
        return session

    def approve(self) -> dict:
        current = spec_flow.read_session(self.root)
        return spec_flow.cmd_approve(
            args(root=str(self.root), actor="test-reviewer", note="Test fixture: explicit approval after review", expected_revision=current["revision"])
        )

    def test_init_preserves_raw_intent_and_scaffolds_package(self) -> None:
        transcript = self.base / "intent.txt"
        transcript.write_text("Need offline mode; do not upload customer data.", encoding="utf-8")
        status = self.initialize(str(transcript))
        self.assertEqual("INTENT_CAPTURED", status["state"])
        self.assertIn(transcript.read_text(encoding="utf-8"), (self.root / "intent.md").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "specs/example/spec.md").is_file())
        self.assertFalse((self.root / "implementation-evidence.md").exists())

    def test_initialized_templates_are_not_a_ready_package(self) -> None:
        self.initialize()
        result = spec_flow.cmd_validate(args(root=str(self.root)))
        self.assertFalse(result["ok"])
        self.assertTrue(any("unfilled template placeholders" in error for error in result["errors"]))

    def test_waiting_approval_requires_filled_artifacts_and_ready_review(self) -> None:
        self.initialize()
        self.set_state("REVIEWING")
        with self.assertRaises(spec_flow.FlowError) as incomplete:
            spec_flow.cmd_advance(args(root=str(self.root), to="WAITING_APPROVAL", expected_revision=0))
        self.assertEqual("package_not_ready", incomplete.exception.code)

        self.write_ready_package()
        waiting = spec_flow.cmd_advance(
            args(root=str(self.root), to="WAITING_APPROVAL", expected_revision=0)
        )
        self.assertEqual("WAITING_APPROVAL", waiting["state"])

    def test_illegal_and_privileged_transitions_are_rejected(self) -> None:
        self.initialize()
        with self.assertRaises(spec_flow.FlowError) as illegal:
            spec_flow.cmd_advance(args(root=str(self.root), to="VERIFYING", expected_revision=0))
        self.assertEqual("illegal_transition", illegal.exception.code)
        self.set_state("WAITING_APPROVAL")
        with self.assertRaises(spec_flow.FlowError) as privileged:
            spec_flow.cmd_advance(args(root=str(self.root), to="IMPLEMENTING", expected_revision=0))
        self.assertEqual("privileged_transition", privileged.exception.code)

    def test_apply_without_approval_is_blocked(self) -> None:
        self.initialize()
        self.set_state("WAITING_APPROVAL")
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_apply(args(root=str(self.root), expected_revision=0))
        self.assertEqual("apply_not_allowed", error.exception.code)
        self.assertEqual("WAITING_APPROVAL", spec_flow.read_session(self.root)["state"])

    def test_incomplete_package_cannot_be_approved(self) -> None:
        self.initialize()
        self.set_state("WAITING_APPROVAL")
        with self.assertRaises(spec_flow.FlowError) as error:
            self.approve()
        self.assertIn(error.exception.code, {"review_not_ready", "package_not_ready"})

    def test_approve_then_apply_matching_baseline(self) -> None:
        self.initialize()
        self.write_ready_package()
        self.set_state("WAITING_APPROVAL")
        approved = self.approve()
        self.assertEqual("APPROVED", approved["state"])
        applied = spec_flow.cmd_apply(
            args(root=str(self.root), expected_revision=approved["revision"])
        )
        self.assertEqual("IMPLEMENTING", applied["state"])
        self.assertTrue((self.root / "implementation-evidence.md").is_file())

    def test_semantic_drift_blocks_apply(self) -> None:
        self.initialize()
        self.write_ready_package()
        self.set_state("WAITING_APPROVAL")
        approved = self.approve()
        with (self.root / "design.md").open("a", encoding="utf-8") as stream:
            stream.write("\nNew behavior after approval.\n")
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("baseline_drift", error.exception.code)
        self.assertIn("design.md", error.exception.details["delta"]["modified"])

    def test_task_checkbox_progress_is_not_semantic_drift(self) -> None:
        self.initialize()
        self.write_ready_package()
        self.set_state("WAITING_APPROVAL")
        approved = self.approve()
        tasks = self.root / "tasks.md"
        tasks.write_text(tasks.read_text(encoding="utf-8").replace("[ ]", "[x]"), encoding="utf-8")
        applied = spec_flow.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))
        self.assertEqual("IMPLEMENTING", applied["state"])

    def test_revise_invalidates_approval_and_increments_version(self) -> None:
        self.initialize()
        self.write_ready_package()
        self.set_state("WAITING_APPROVAL")
        approved = self.approve()
        revised = spec_flow.cmd_revise(
            args(root=str(self.root), reason="Contract changed", expected_revision=approved["revision"])
        )
        self.assertEqual("REVISING", revised["state"])
        self.assertEqual(2, revised["spec_version"])
        self.assertIsNone(revised["approval"])

    def test_block_and_resume_use_stable_decision(self) -> None:
        self.initialize()
        self.set_state("CONTEXT_DISCOVERY")
        blocked = spec_flow.cmd_block(
            args(root=str(self.root), decision_id="DEC-007", summary="Choose data retention", expected_revision=0)
        )
        self.assertEqual("BLOCKED", blocked["state"])
        resolved = spec_flow.cmd_resolve(
            args(root=str(self.root), decision_id="DEC-007", resolution="Retain metadata for 7 days", expected_revision=1)
        )
        self.assertEqual("CONTEXT_DISCOVERY", resolved["state"])
        self.assertEqual([], resolved["blocking_decisions"])

    def test_concurrent_expected_revision_allows_only_one_writer(self) -> None:
        self.initialize()
        outcomes: list[str] = []

        def worker() -> None:
            try:
                spec_flow.cmd_advance(args(root=str(self.root), to="INTENT_CAPTURED", expected_revision=0))
                outcomes.append("ok")
            except spec_flow.FlowError as error:
                outcomes.append(error.code)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(1, outcomes.count("ok"))
        self.assertEqual(1, outcomes.count("stale_revision"))
        json.loads((self.root / spec_flow.SESSION_NAME).read_text(encoding="utf-8"))

    def test_actual_diff_finds_unplanned_surface_and_completion_gate(self) -> None:
        self.initialize()
        self.write_ready_package()
        session = self.set_state("WAITING_APPROVAL")
        session["expected_change_surface"] = ["src"]
        spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
        approved = self.approve()
        applied = spec_flow.cmd_apply(args(root=str(self.root), expected_revision=approved["revision"]))

        before = self.base / "before"
        after = self.base / "after"
        (before / "src").mkdir(parents=True)
        (after / "src").mkdir(parents=True)
        (after / "config").mkdir(parents=True)
        (before / "src/a.txt").write_text("old", encoding="utf-8")
        (after / "src/a.txt").write_text("new", encoding="utf-8")
        (after / "config/app.toml").write_text("enabled=true", encoding="utf-8")

        reviewed = spec_flow.cmd_begin_verify(
            args(
                root=str(self.root),
                before=str(before),
                after=str(after),
                planned=None,
                expected_revision=applied["revision"],
            )
        )
        self.assertEqual("POST_IMPLEMENTATION_REVIEW", reviewed["state"])
        self.assertEqual(["config/app.toml"], [item["path"] for item in reviewed["material_findings"]])
        self.assertTrue((self.root / "actual-diff.json").is_file())

        dispositioned = spec_flow.cmd_disposition(
            args(
                root=str(self.root),
                path="config/app.toml",
                disposition="verified-non-material",
                material=False,
                evidence="configuration contract test",
                expected_revision=reviewed["revision"],
            )
        )
        verifying = spec_flow.cmd_advance(
            args(root=str(self.root), to="VERIFYING", expected_revision=dispositioned["revision"])
        )
        (self.root / "verification-report.md").write_text("Verdict: PASS\nAll mapped checks passed.\n", encoding="utf-8")
        completed = spec_flow.cmd_complete(
            args(
                root=str(self.root),
                verdict="PASS",
                report="verification-report.md",
                expected_revision=verifying["revision"],
            )
        )
        self.assertEqual("DONE", completed["state"])

    def test_baseline_revision_disposition_blocks_verifying(self) -> None:
        self.initialize()
        self.set_state("POST_IMPLEMENTATION_REVIEW")
        session = spec_flow.read_session(self.root)
        session["material_findings"] = [
            {
                "id": "DIFF-001",
                "path": "shared/config.toml",
                "status": "dispositioned",
                "material": True,
                "disposition": "baseline-revision-required",
            }
        ]
        spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_advance(args(root=str(self.root), to="VERIFYING", expected_revision=0))
        self.assertEqual("post_review_incomplete", error.exception.code)


def minimal_case(case_id: str = "CASE-001", risk_id: str = "RISK-001", safety: str = "safe") -> dict:
    return {
        "id": case_id,
        "title": "Lost response does not double charge",
        "risks": [risk_id],
        "requirements": [],
        "references": ["REF-1"],
        "objective": "Does a retry after a lost response create a second charge?",
        "why_it_catches_the_risk": "It reproduces the retry path that regenerates the key.",
        "preconditions": ["Sandbox merchant with an empty ledger"],
        "environment": {"build": "PR head", "clock": "fixed"},
        "fixtures": ["Order ORD-1 for 1000"],
        "fault_injection": ["Drop the provider response with the proxy"],
        "steps": [
            {
                "n": 1,
                "action": "POST /payments for ORD-1",
                "expected": "Provider receives one charge",
                "how_to_observe": "provider sandbox ledger for ORD-1",
            }
        ],
        "intermediate_checks": ["Row stays PENDING after the drop"],
        "expected_final_state": ["Exactly one provider charge"],
        "forbidden_outcomes": ["Two charges in the ledger"],
        "oracle": {
            "kind": "ledger",
            "source": "provider sandbox ledger",
            "query": "GET /sandbox/ledger?merchant_ref=ORD-1",
            "expected": "exactly one entry",
        },
        "side_effect_count": "1 charge",
        "evidence": ["artifacts/logs/case-001.log"],
        "cleanup": ["Refund the sandbox charge"],
        "repeat": 1,
        "timeout": "5 min",
        "flake_policy": "An inconsistent result is a defect.",
        "safety": safety,
        "review": {"verdict": "accepted", "findings": []},
    }


def minimal_risk(risk_id: str = "RISK-001", severity: str = "critical") -> dict:
    return {
        "id": risk_id,
        "title": "Double charge after retry",
        "mechanism": "Charge succeeds, response is lost, retry charges again.",
        "invariant": "One order produces at most one charge.",
        "evidence": ["src/payments/retry.py:42", "REF-1"],
        "reality": "likely",
        "severity": severity,
        "detectability": "only in ledger reconciliation",
        "blast_radius": "every retried payment",
        "existing_protection": "none",
        "disposition": "verify",
        "cases": ["CASE-001"],
        "found_in_pass": 1,
    }


def minimal_check() -> dict:
    return {
        "schema_version": 1,
        "title": "Payment retry",
        "mode": "deep",
        "mode_reason": "touches payments",
        "sources": [{"kind": "pr", "ref": "#67", "location": "https://example.test/pr/67", "authority": "implementation"}],
        "references": [
            {
                "id": "REF-1",
                "kind": "ticket",
                "title": "PAY-142",
                "location": "PAY-142",
                "relevance": "defines retry behavior",
            }
        ],
        "intent": {"stated": "Retry safely", "reconstructed": "Adds a retry loop", "divergences": []},
        "changed_surface": [{"path": "src/payments/retry.py", "status": "modified", "summary": "retry loop"}],
        "affected_surface": [{"area": "payment creation", "why": "retry reaches the provider twice", "evidence": ["REF-1"]}],
        "risks": [minimal_risk()],
        "cases": [minimal_case()],
        "environment": {"available": [], "required": ["network proxy"], "missing": [], "setup": []},
        "execution_plan": [{"order": 1, "case": "CASE-001", "rationale": "highest severity", "stop_on_fail": True}],
        "open_questions": [],
        "waivers": [],
    }


class CheckFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / "check"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def initialize(self, mode: str = "auto", paths: list[str] | None = None, source_file: str | None = None) -> dict:
        return spec_flow.cmd_check_init(
            args(
                root=str(self.root),
                session_id="payment-retry",
                title="Payment retry",
                rigor="standard",
                mode=mode,
                source_file=source_file,
                config=None,
                path=paths if paths is not None else ["src/payments/retry.py"],
                hint=None,
            )
        )

    def fill_authored_artifacts(self) -> None:
        for name in spec_flow.CHECK_AUTHORED_FILES:
            path = self.root / name
            text = spec_flow.PLACEHOLDER_RE.sub("reviewed content", path.read_text(encoding="utf-8"))
            path.write_text(text, encoding="utf-8")

    def write_check(self, check: dict | None = None) -> None:
        payload = check if check is not None else minimal_check()
        spec_flow.atomic_write_json(self.root / "check.json", payload)

    def record_passes(self, count: int, material_first: int = 1) -> None:
        for index in range(count):
            spec_flow.cmd_check_pass(
                args(
                    root=str(self.root),
                    kind="broad" if index == 0 else "adversarial",
                    summary=f"pass {index + 1}",
                    new_critical=1 if index < material_first else 0,
                    new_high=0,
                    new_medium=0,
                    uncovered_invariants=0,
                    surface_changed=False,
                    expected_revision=None,
                )
            )

    def reach_brief_ready(self) -> dict:
        self.initialize()
        self.fill_authored_artifacts()
        self.write_check()
        for state in ("CHECK_SOURCE_RESOLVED", "CHECK_CHANGE_ANALYZED", "CHECK_SURFACE_MAPPED", "CHECK_RISK_ITERATING"):
            spec_flow.cmd_advance(args(root=str(self.root), to=state, expected_revision=None))
        self.record_passes(4)
        for state in (
            "CHECK_CASES_DRAFTED",
            "CHECK_CASES_REVIEWED",
            "CHECK_ENVIRONMENT_PLANNED",
            "CHECK_BRIEF_READY",
        ):
            status = spec_flow.cmd_advance(args(root=str(self.root), to=state, expected_revision=None))
        return status

    def test_depth_mode_is_derived_from_blast_radius(self) -> None:
        status = self.initialize(paths=["src/payments/retry.py"])
        self.assertEqual("deep", status["check_mode"])
        self.assertEqual(["payments"], status["mode_suggestion"]["matched_critical_areas"])

    def test_local_presentation_change_selects_lite_mode(self) -> None:
        status = self.initialize(paths=["lib/ui/button.dart", "docs/readme.md"])
        self.assertEqual("lite", status["check_mode"])

    def test_check_diff_records_the_actual_surface_and_suggests_the_mode(self) -> None:
        self.initialize()
        before = self.base / "before"
        after = self.base / "after"
        (before / "src/payments").mkdir(parents=True)
        (after / "src/payments").mkdir(parents=True)
        (before / "src/payments/retry.py").write_text("old", encoding="utf-8")
        (after / "src/payments/retry.py").write_text("new", encoding="utf-8")
        (after / "src/payments/events.py").write_text("added", encoding="utf-8")
        result = spec_flow.cmd_check_diff(
            args(root=str(self.root), before=str(before), after=str(after), config=None)
        )
        self.assertEqual({"added": 1, "modified": 1, "deleted": 0, "total": 2}, result["counts"])
        self.assertEqual("deep", result["mode_suggestion"]["mode"])
        inventory = json.loads((self.root / "actual-diff.json").read_text(encoding="utf-8"))
        self.assertEqual(2, inventory["counts"]["total"])

    def test_check_diff_is_rejected_for_planning_sessions(self) -> None:
        spec_flow.cmd_init(
            args(root=str(self.root), session_id="plan", title="Plan", rigor="standard", intent_file=None)
        )
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_check_diff(
                args(root=str(self.root), before=str(self.base), after=str(self.base), config=None)
            )
        self.assertEqual("check_only", error.exception.code)

    def test_narrow_authorization_does_not_cover_a_destructive_case(self) -> None:
        self.initialize()
        check = minimal_check()
        check["cases"].append({**minimal_case("CASE-002", "RISK-001", "destructive")})
        check["risks"][0]["cases"] = ["CASE-001", "CASE-002"]
        check["execution_plan"].append({"order": 2, "case": "CASE-002", "rationale": "settles the contract", "stop_on_fail": True})
        self.write_check(check)
        session = spec_flow.read_session(self.root)
        session["authorization"] = {"actor": "test-operator", "at": "2026-01-01T00:00:00+00:00", "scope": "safe", "note": "Test fixture"}
        spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertEqual(["CASE-002"], result["readiness"]["cases_needing_authorization"])
        self.assertFalse(result["readiness"]["ready_to_execute"])

    def test_completion_cannot_skip_execution(self) -> None:
        self.reach_brief_ready()
        (self.root / "verification-report.md").write_text("Verdict: PASS\n", encoding="utf-8")
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_complete(
                args(root=str(self.root), verdict="PASS", report="verification-report.md", expected_revision=None)
            )
        self.assertEqual("completion_not_allowed", error.exception.code)

    def test_risk_table_never_truncates_mid_word(self) -> None:
        self.reach_brief_ready()
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        brief = (self.root / "verification-brief.md").read_text(encoding="utf-8")
        self.assertIn("Check approach", brief)
        for line in brief.splitlines():
            if line.startswith("| RISK-"):
                self.assertFalse(line.rstrip().endswith("|") and " …" in line and line.count("…") > 1)
        self.assertEqual("a b …", spec_flow.clip("a b cdefgh", limit=5))

    def test_a_finished_or_running_check_cannot_be_turned_into_a_blocker(self) -> None:
        self.initialize()
        for state in ("CHECK_EXECUTING", "CHECK_DONE"):
            session = spec_flow.read_session(self.root)
            session["state"] = state
            spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
            with self.assertRaises(spec_flow.FlowError) as error:
                spec_flow.cmd_block(
                    args(root=str(self.root), decision_id="DEC-1", summary="late question", expected_revision=None)
                )
            self.assertEqual("block_not_allowed", error.exception.code)

    def test_blocking_a_check_returns_to_its_own_lifecycle(self) -> None:
        self.initialize()
        self.fill_authored_artifacts()
        spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_SOURCE_RESOLVED", expected_revision=None))
        spec_flow.cmd_block(
            args(root=str(self.root), decision_id="DEC-1", summary="is a real charge allowed?", expected_revision=None)
        )
        resolved = spec_flow.cmd_resolve(
            args(root=str(self.root), decision_id="DEC-1", resolution="sandbox only", expected_revision=None)
        )
        self.assertIn(resolved["state"], spec_flow.CHECK_STATES)
        self.assertEqual("CHECK_SOURCE_RESOLVED", spec_flow.cmd_status(args(root=str(self.root)))["state"])

    def test_execution_plan_cannot_reference_an_unknown_case(self) -> None:
        self.initialize()
        check = minimal_check()
        check["execution_plan"] = [{"order": 1, "case": "CASE-999", "rationale": "typo", "stop_on_fail": True}]
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["ok"])
        self.assertTrue(any("unknown case CASE-999" in error for error in result["errors"]))

    def test_rendered_tables_survive_pipes_and_non_ascii_content(self) -> None:
        self.initialize()
        check = minimal_check()
        check["risks"][0]["title"] = "Duplicate charge | after retry"
        check["cases"][0]["fault_injection"] = ["drop provider response | toxiproxy"]
        self.write_check(check)
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        row = next(
            line
            for line in (self.root / "verification-brief.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("| RISK-001")
        )
        self.assertEqual(6, len(re.findall(r"(?<!\\)\|", row)))
        self.assertIn("Duplicate charge \\| after retry", row)

    def test_mode_drift_between_session_and_check_json_is_reported(self) -> None:
        self.initialize()
        check = minimal_check()
        check["mode"] = "lite"
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["ok"])
        self.assertTrue(any("the session enforced" in error for error in result["errors"]))

    def test_missing_change_inventory_gives_an_actionable_error(self) -> None:
        self.initialize()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_check_mode(
                args(root=str(self.root), path=None, hint=None, from_diff="actual-diff.json", config=None)
            )
        self.assertEqual("diff_input_missing", error.exception.code)

    def test_wrong_container_types_fail_validation_without_crashing(self) -> None:
        for field, bad in (
            ("environment", "not-a-mapping"),
            ("intent", "just a string"),
            ("cases", {"x": 1}),
            ("risks", "none"),
            ("execution_plan", {"a": 1}),
            ("open_questions", "none"),
        ):
            with self.subTest(field=field):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                self.root = Path(temp.name) / "check"
                self.initialize()
                check = minimal_check()
                check[field] = bad
                self.write_check(check)
                result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
                self.assertFalse(result["ok"])
                self.assertTrue(any(field in error for error in result["errors"]), result["errors"])
                spec_flow.cmd_check_render(args(root=str(self.root), config=None))
                self.assertTrue((self.root / "verification-brief.md").is_file())

    def test_a_report_outside_the_package_cannot_satisfy_the_gate(self) -> None:
        self.initialize()
        outside = self.base / "outside.md"
        outside.write_text("an unrelated non-empty file", encoding="utf-8")
        session = spec_flow.read_session(self.root)
        session["state"] = "CHECK_EXECUTING"
        spec_flow.atomic_write_json(self.root / spec_flow.SESSION_NAME, session)
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_complete(
                args(root=str(self.root), verdict="PASS", report="../outside.md", expected_revision=None)
            )
        self.assertEqual("report_outside_package", error.exception.code)

    def test_pass_counters_and_summary_are_validated(self) -> None:
        self.initialize()
        with self.assertRaises(spec_flow.FlowError) as negative:
            spec_flow.cmd_check_pass(
                args(root=str(self.root), kind="broad", summary="x", new_critical=-1, new_high=0,
                     new_medium=0, uncovered_invariants=0, surface_changed=False, expected_revision=None)
            )
        self.assertEqual("invalid_pass_counts", negative.exception.code)
        with self.assertRaises(spec_flow.FlowError) as empty:
            spec_flow.cmd_check_pass(
                args(root=str(self.root), kind="broad", summary="  ", new_critical=1, new_high=0,
                     new_medium=0, uncovered_invariants=0, surface_changed=False, expected_revision=None)
            )
        self.assertEqual("pass_summary_required", empty.exception.code)

    def test_authorization_must_name_a_human(self) -> None:
        status = self.reach_brief_ready()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_check_authorize(
                args(root=str(self.root), actor="   ", scope="safe", note="", acknowledge="",
                     expected_revision=status["revision"])
            )
        self.assertEqual("actor_required", error.exception.code)

    def test_a_hand_edited_session_fails_structurally_not_with_a_crash(self) -> None:
        self.initialize()
        for field, bad in (("iterations", "oops"), ("blocking_decisions", {}), ("authorization", "granted")):
            with self.subTest(field=field):
                session = json.loads((self.root / spec_flow.SESSION_NAME).read_text(encoding="utf-8"))
                session[field] = bad
                (self.root / spec_flow.SESSION_NAME).write_text(json.dumps(session), encoding="utf-8")
                with self.assertRaises(spec_flow.FlowError) as error:
                    spec_flow.cmd_status(args(root=str(self.root)))
                self.assertEqual("session_invalid", error.exception.code)
                session[field] = [] if field != "authorization" else None
                (self.root / spec_flow.SESSION_NAME).write_text(json.dumps(session), encoding="utf-8")

    def test_identity_collisions_and_dangling_governance_are_reported(self) -> None:
        self.initialize()
        check = minimal_check()
        check["references"].append(
            {"id": "REF-1", "kind": "design", "title": "Another doc", "location": "docs/other.md", "relevance": "other"}
        )
        check["open_questions"] = [
            {"id": "Q-1", "question": "a", "why": "x", "blocking": True, "owner": "human"},
            {"id": "Q-1", "question": "b", "why": "y", "blocking": False, "owner": "human"},
        ]
        check["waivers"] = [{"risk": "RISK-404", "reason": "accepted", "owner": "lead", "revisit": "next release"}]
        self.write_check(check)
        joined = " | ".join(spec_flow.cmd_check_validate(args(root=str(self.root), config=None))["errors"])
        self.assertIn("duplicate reference id: REF-1", joined)
        self.assertIn("duplicate open question id: Q-1", joined)
        self.assertIn("waiver references unknown risk RISK-404", joined)

    def test_depth_classification_is_platform_independent(self) -> None:
        config = spec_flow.resolve_config(self.base)
        for windows, posix in ((r"lib\ui\badge.dart", "lib/ui/badge.dart"), (r"src\payments\retry.py", "src/payments/retry.py")):
            self.assertEqual(
                spec_flow.suggest_mode([posix], config)["mode"],
                spec_flow.suggest_mode([windows], config)["mode"],
                windows,
            )

    def test_rendering_is_idempotent_and_tables_stay_rectangular(self) -> None:
        self.initialize()
        check = minimal_check()
        check["open_questions"] = [
            {"id": "Q-1", "question": "multi\nline question", "why": "x", "blocking": True, "owner": "human"}
        ]
        check["environment"]["missing"] = [
            {"capability": "replay | hook", "needed_for": ["CASE-001"], "request": "add it"}
        ]
        self.write_check(check)
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        first = [(self.root / name).read_bytes() for name in spec_flow.CHECK_GENERATED_FILES]
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        self.assertEqual(first, [(self.root / name).read_bytes() for name in spec_flow.CHECK_GENERATED_FILES])
        for name in spec_flow.CHECK_GENERATED_FILES:
            lines = (self.root / name).read_text(encoding="utf-8").splitlines()
            for index, line in enumerate(lines):
                divider = lines[index + 1] if index + 1 < len(lines) else ""
                if not line.startswith("|") or not set(divider.replace("|", "").strip()) <= set("- ") or not divider:
                    continue
                width = len(re.findall(r"(?<!\\)\|", line))
                for row in lines[index + 2:]:
                    if not row.startswith("|"):
                        break
                    self.assertEqual(width, len(re.findall(r"(?<!\\)\|", row)), f"{name}: {row}")

    def test_every_promised_case_field_is_enforced(self) -> None:
        for field in spec_flow.REQUIRED_CASE_FIELDS:
            with self.subTest(field=field):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                self.root = Path(temp.name) / "check"
                self.initialize()
                check = minimal_check()
                check["cases"][0].pop(field, None)
                self.write_check(check)
                result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
                self.assertTrue(
                    any(field in error for error in result["errors"]),
                    f"{field} is documented as required but not enforced: {result['errors']}",
                )

    def test_optional_case_fields_may_be_empty(self) -> None:
        self.initialize()
        check = minimal_check()
        for field in spec_flow.OPTIONAL_CASE_FIELDS:
            check["cases"][0][field] = []
        check["cases"][0].pop("repeat", None)
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertTrue(result["ok"], result["errors"])

    def test_repeat_must_be_a_positive_whole_number(self) -> None:
        for bad in (0, -1, 1.5, True, "three"):
            with self.subTest(repeat=bad):
                temp = tempfile.TemporaryDirectory()
                self.addCleanup(temp.cleanup)
                self.root = Path(temp.name) / "check"
                self.initialize()
                check = minimal_check()
                check["cases"][0]["repeat"] = bad
                self.write_check(check)
                result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
                self.assertTrue(any("repeat" in error for error in result["errors"]), result["errors"])

    def test_the_golden_real_world_package_still_validates(self) -> None:
        """A real package produced by an actual run must keep validating as the rules tighten."""
        archive = Path(__file__).resolve().parent / "fixtures/payment-retry"
        self.initialize()
        for name in ("check.json", "source.md", "impact-map.md", "case-review.md", "spec-session.json"):
            (self.root / name).write_text((archive / name).read_text(encoding="utf-8"), encoding="utf-8")
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertTrue(result["ok"], result["errors"])
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))

    def test_reopening_the_brief_requires_fresh_analysis(self) -> None:
        status = self.reach_brief_ready()
        spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_RISK_ITERATING", expected_revision=status["revision"]))
        iteration = spec_flow.cmd_status(args(root=str(self.root)))["iteration"]
        self.assertFalse(iteration["may_stop"])
        self.assertEqual(0, iteration["dry_tail"])
        self.assertTrue(any("reopened" in reason for reason in iteration["blocking_reasons"]))
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_CASES_DRAFTED", expected_revision=None))
        self.assertEqual("iteration_incomplete", error.exception.code)
        self.record_passes(2, material_first=0)
        resumed = spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_CASES_DRAFTED", expected_revision=None))
        self.assertEqual("CHECK_CASES_DRAFTED", resumed["state"])
        self.assertEqual(2, spec_flow.cmd_status(args(root=str(self.root)))["iteration"]["passes_since_reopen"])

    def test_check_session_uses_its_own_lifecycle(self) -> None:
        self.initialize()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_advance(args(root=str(self.root), to="SPECIFYING", expected_revision=None))
        self.assertEqual("illegal_transition", error.exception.code)

    def test_unfilled_source_blocks_the_first_transition(self) -> None:
        self.initialize()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_SOURCE_RESOLVED", expected_revision=None))
        self.assertEqual("check_artifact_incomplete", error.exception.code)

    def test_iteration_stop_rule_gates_case_drafting(self) -> None:
        self.initialize()
        self.fill_authored_artifacts()
        self.write_check()
        for state in ("CHECK_SOURCE_RESOLVED", "CHECK_CHANGE_ANALYZED", "CHECK_SURFACE_MAPPED", "CHECK_RISK_ITERATING"):
            spec_flow.cmd_advance(args(root=str(self.root), to=state, expected_revision=None))
        self.record_passes(2)
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_CASES_DRAFTED", expected_revision=None))
        self.assertEqual("iteration_incomplete", error.exception.code)
        self.record_passes(2, material_first=0)
        status = spec_flow.cmd_advance(args(root=str(self.root), to="CHECK_CASES_DRAFTED", expected_revision=None))
        self.assertEqual("CHECK_CASES_DRAFTED", status["state"])

    def test_uncovered_critical_risk_blocks_brief_ready(self) -> None:
        self.initialize()
        self.fill_authored_artifacts()
        check = minimal_check()
        check["risks"].append(minimal_risk("RISK-002"))
        check["risks"][1]["cases"] = []
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["ok"])
        self.assertTrue(any("disposition verify requires at least one case" in error for error in result["errors"]))
        self.assertTrue(any("critical risk coverage" in error for error in result["errors"]))

    def test_weak_case_fields_are_validation_errors(self) -> None:
        self.initialize()
        check = minimal_check()
        check["cases"][0]["oracle"] = {"kind": "http-status", "source": "", "expected": ""}
        check["cases"][0]["cleanup"] = []
        check["cases"][0]["references"] = []
        check["cases"][0]["steps"][0]["how_to_observe"] = ""
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        joined = " | ".join(result["errors"])
        self.assertIn("oracle.kind", joined)
        self.assertIn("cleanup is empty", joined)
        self.assertIn("cite at least one reference", joined)
        self.assertIn("empty how_to_observe", joined)

    def test_template_placeholders_in_check_json_are_rejected(self) -> None:
        self.initialize()
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["ok"])
        self.assertTrue(any("unfilled template placeholder" in error for error in result["errors"]))

    def test_render_produces_detailed_cases_and_brief(self) -> None:
        self.reach_brief_ready()
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        cases = (self.root / "verification-cases.md").read_text(encoding="utf-8")
        brief = (self.root / "verification-brief.md").read_text(encoding="utf-8")
        self.assertIn("How to observe", cases)
        self.assertIn("Authoritative oracle", cases)
        self.assertIn("REF-1", cases)
        self.assertIn("Ready to execute: YES", brief)
        self.assertIn("RISK-001", brief)

    def test_unsafe_case_keeps_execution_unready(self) -> None:
        self.initialize()
        check = minimal_check()
        check["cases"][0]["safety"] = "destructive"
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["readiness"]["ready_to_execute"])
        self.assertEqual(["CASE-001"], result["readiness"]["cases_needing_authorization"])

    def test_empty_risk_register_is_a_finding_not_a_pass(self) -> None:
        self.initialize()
        check = minimal_check()
        check["risks"] = []
        check["cases"] = []
        check["execution_plan"] = []
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertFalse(result["ok"])
        self.assertFalse(result["readiness"]["ready_to_execute"])
        joined = " | ".join(result["errors"])
        self.assertIn("risk register is empty", joined)
        self.assertIn("no_material_risk_rationale", joined)

    def test_a_genuinely_riskless_change_needs_an_explicit_rationale(self) -> None:
        self.initialize()
        check = minimal_check()
        check["risks"][0]["disposition"] = "dropped"
        check["cases"] = []
        check["execution_plan"] = []
        check["no_material_risk_rationale"] = (
            "Only the button label changed; no contract, persistence, or permission surface is reachable."
        )
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertTrue(result["ok"], result["errors"])

    def test_coverage_threshold_boundary_is_inclusive(self) -> None:
        self.initialize()
        check = minimal_check()
        check["risks"] = [minimal_risk(f"RISK-{index:03d}", "medium") for index in range(1, 11)]
        for index, risk in enumerate(check["risks"]):
            risk["cases"] = ["CASE-001"] if index < 7 else []
        check["cases"][0]["risks"] = ["RISK-001"]
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertEqual(70.0, result["coverage"]["medium"]["percent"])
        self.assertFalse([error for error in result["errors"] if "coverage" in error])

    def test_depth_mode_changes_the_required_pass_count(self) -> None:
        config = spec_flow.resolve_config(self.base)
        self.assertEqual([2, 3, 4], [spec_flow.minimum_passes(mode, config) for mode in ("lite", "standard", "deep")])
        lowered = spec_flow.deep_merge(config, {"verification": {"minimum_iterations": 2}})
        self.assertEqual([1, 2, 3], [spec_flow.minimum_passes(mode, lowered) for mode in ("lite", "standard", "deep")])

    def test_a_valid_package_is_not_ready_while_iteration_is_unfinished(self) -> None:
        self.initialize()
        self.fill_authored_artifacts()
        self.write_check()
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertTrue(result["ok"], result["errors"])
        self.assertFalse(result["readiness"]["iteration_finished"])
        self.assertFalse(result["readiness"]["ready_to_execute"])
        self.assertTrue(any("risk iteration is not finished" in reason for reason in result["readiness"]["reasons"]))
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        self.assertIn("Ready to execute: NO", (self.root / "verification-brief.md").read_text(encoding="utf-8"))

    def test_blocking_question_keeps_execution_unready(self) -> None:
        self.initialize()
        check = minimal_check()
        check["open_questions"] = [{"id": "Q-1", "question": "Is a real charge allowed?", "why": "cost", "blocking": True, "owner": "human"}]
        self.write_check(check)
        result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
        self.assertEqual(["Q-1"], result["readiness"]["blocking_questions"])
        self.assertFalse(result["readiness"]["ready_to_execute"])

    def test_authorization_is_required_before_execution(self) -> None:
        status = self.reach_brief_ready()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_check_authorize(
                args(root=str(self.root), actor="test-operator", scope="all", note="Test fixture", acknowledge="", expected_revision=None)
            )
        self.assertEqual("acknowledgement_required", error.exception.code)
        authorized = spec_flow.cmd_check_authorize(
            args(
                root=str(self.root),
                actor="test-operator",
                scope="safe",
                note="sandbox only",
                acknowledge="",
                expected_revision=status["revision"],
            )
        )
        self.assertEqual("CHECK_EXECUTING", authorized["state"])
        self.assertEqual("test-operator", authorized["authorization"]["actor"])

    def test_check_completion_accepts_a_failing_verdict(self) -> None:
        self.reach_brief_ready()
        spec_flow.cmd_check_authorize(
            args(root=str(self.root), actor="test-operator", scope="safe", note="Test fixture", acknowledge="", expected_revision=None)
        )
        (self.root / "verification-report.md").write_text("Verdict: FAIL\nCASE-001 reproduced RISK-001.\n", encoding="utf-8")
        completed = spec_flow.cmd_complete(
            args(root=str(self.root), verdict="FAIL", report="verification-report.md", expected_revision=None)
        )
        self.assertEqual("CHECK_DONE", completed["state"])
        self.assertEqual("FAIL", spec_flow.read_session(self.root)["verification_verdict"])

    def test_execution_cannot_start_without_the_brief_gate(self) -> None:
        self.initialize()
        self.fill_authored_artifacts()
        self.write_check()
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.cmd_check_authorize(
                args(root=str(self.root), actor="test-operator", scope="safe", note="Test fixture", acknowledge="", expected_revision=None)
            )
        self.assertEqual("authorization_not_allowed", error.exception.code)


class RobustnessTests(unittest.TestCase):
    """A hand-edited or truncated artifact must fail structurally, never with a stack trace."""

    MUTANTS = (None, "", 0, -1, [], {}, "TBD", True, 3.14, "🙂", "نص عربي", "x" * 500,
               {"nested": {"deep": [1, 2]}}, [{"a": 1}])

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "check"
        spec_flow.cmd_check_init(
            args(root=str(self.root), session_id="f", title="f", rigor="standard", mode="deep",
                 source_file=None, config=None, path=None, hint=None)
        )
        for artifact in spec_flow.CHECK_AUTHORED_FILES:
            path = self.root / artifact
            path.write_text(spec_flow.PLACEHOLDER_RE.sub("reviewed", path.read_text(encoding="utf-8")), encoding="utf-8")

    def mutate(self, obj: object, rng: random.Random) -> object:
        mutant = copy.deepcopy(obj)
        for _ in range(rng.randint(1, 4)):
            target, trail = mutant, []
            for _ in range(rng.randint(0, 3)):
                if isinstance(target, dict) and target:
                    key = rng.choice(list(target))
                    trail.append(key)
                    target = target[key]
                elif isinstance(target, list) and target:
                    index = rng.randrange(len(target))
                    trail.append(index)
                    target = target[index]
                else:
                    break
            cursor = mutant
            for step in trail[:-1]:
                cursor = cursor[step]
            if trail:
                try:
                    cursor[trail[-1]] = rng.choice(self.MUTANTS)
                except (TypeError, IndexError, KeyError):
                    pass
            elif isinstance(mutant, dict) and mutant:
                mutant[rng.choice(list(mutant))] = rng.choice(self.MUTANTS)
        return mutant

    def test_mutated_check_files_never_crash_validate_or_render(self) -> None:
        rng = random.Random(20260803)
        for iteration in range(250):
            payload = self.mutate(minimal_check(), rng)
            (self.root / "check.json").write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            try:
                spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
                spec_flow.cmd_check_render(args(root=str(self.root), config=None))
            except spec_flow.FlowError:
                pass
            except Exception as error:  # noqa: BLE001 - the point of the test
                self.fail(f"iteration {iteration} crashed with {type(error).__name__}: {error}")

    def test_mutated_sessions_raise_structured_errors(self) -> None:
        rng = random.Random(4242)
        original = json.loads((self.root / spec_flow.SESSION_NAME).read_text(encoding="utf-8"))
        for iteration in range(250):
            payload = self.mutate(original, rng)
            (self.root / spec_flow.SESSION_NAME).write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
            try:
                spec_flow.cmd_status(args(root=str(self.root)))
            except spec_flow.FlowError:
                pass
            except Exception as error:  # noqa: BLE001 - the point of the test
                self.fail(f"iteration {iteration} crashed with {type(error).__name__}: {error}")

    def test_session_list_entries_must_be_records(self) -> None:
        original = json.loads((self.root / spec_flow.SESSION_NAME).read_text(encoding="utf-8"))
        for field in ("blocking_decisions", "iterations", "material_findings"):
            with self.subTest(field=field):
                broken = dict(original, **{field: ["not-a-record"]})
                (self.root / spec_flow.SESSION_NAME).write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(spec_flow.FlowError) as error:
                    spec_flow.cmd_status(args(root=str(self.root)))
                self.assertEqual("session_invalid", error.exception.code)

    def test_as_lines_accepts_scalars(self) -> None:
        self.assertEqual(["True"], spec_flow.as_lines(True))
        self.assertEqual(["7"], spec_flow.as_lines(7))
        self.assertEqual([], spec_flow.as_lines(None))
        self.assertEqual(["a", "b"], spec_flow.as_lines(["a", "b"]))

    def test_a_missing_planning_file_is_reported_not_crashed_on(self) -> None:
        planning = Path(self.temp.name) / "plan"
        spec_flow.cmd_init(
            args(root=str(planning), session_id="p", title="P", rigor="standard", intent_file=None)
        )
        (planning / "decisions.md").unlink()
        result = spec_flow.cmd_validate(args(root=str(planning)))
        self.assertFalse(result["ok"])
        self.assertIn("missing or empty: decisions.md", result["errors"])


class ArtifactStoreTests(unittest.TestCase):
    """Packages belong to the machine, never to the project's diff."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store = self.base / "store"
        self.project = self.base / "myproject"
        (self.project / "src").mkdir(parents=True)
        (self.project / ".git").mkdir()

    def check_init(self, project: Path | None = None, session_id: str = "payment-retry", **overrides) -> dict:
        payload = dict(
            root=None, project=str(project or self.project), store=str(self.store),
            session_id=session_id, title="t", rigor="standard", mode="deep",
            source_file=None, config=None, path=None, hint=None,
        )
        payload.update(overrides)
        return spec_flow.cmd_check_init(args(**payload))

    def test_a_package_lands_outside_the_project(self) -> None:
        status = self.check_init()
        root = Path(status["root"])
        self.assertTrue(root.is_relative_to(self.store), root)
        self.assertFalse(root.is_relative_to(self.project), root)
        self.assertEqual([], [path.name for path in self.project.iterdir() if path.name not in {".git", "src"}])
        self.assertTrue((root / "check.json").is_file())

    def test_the_store_is_found_from_a_subdirectory(self) -> None:
        deep = self.project / "src/deep/nested"
        deep.mkdir(parents=True)
        self.assertEqual(self.project.resolve(), spec_flow.find_project_root(deep))

    def test_same_named_projects_in_different_paths_stay_separate(self) -> None:
        twin = self.base / "elsewhere/myproject"
        (twin / ".git").mkdir(parents=True)
        self.assertNotEqual(spec_flow.project_key(self.project), spec_flow.project_key(twin))
        self.assertTrue(spec_flow.project_key(twin).startswith("myproject-"))

    def test_history_lists_every_package_with_its_project(self) -> None:
        self.check_init(session_id="payment-retry")
        spec_flow.cmd_init(
            args(root=None, project=str(self.project), store=str(self.store), session_id="offline-mode",
                 title="Offline", rigor="standard", intent_file=None, config=None)
        )
        other = self.base / "other"
        (other / ".git").mkdir(parents=True)
        self.check_init(project=other, session_id="auth-refresh")

        everything = spec_flow.cmd_list(args(store=str(self.store), project=None, kind=None, state=None, limit=None))
        self.assertEqual(3, everything["count"])
        self.assertEqual({"payment-retry", "offline-mode", "auth-refresh"},
                         {item["session_id"] for item in everything["packages"]})
        self.assertEqual({str(self.project.resolve()), str(other.resolve())},
                         {item["project"] for item in everything["packages"]})

        mine = spec_flow.cmd_list(args(store=str(self.store), project=str(self.project), kind="checks",
                                       state=None, limit=None))
        self.assertEqual(["payment-retry"], [item["session_id"] for item in mine["packages"]])

    def test_where_resolves_a_path_without_the_user_remembering_it(self) -> None:
        created = Path(self.check_init()["root"])
        located = spec_flow.cmd_where(
            args(project=str(self.project), session_id="payment-retry", kind="checks", store=str(self.store))
        )
        self.assertEqual(str(created), located["path"])
        self.assertTrue(located["exists"])
        missing = spec_flow.cmd_where(
            args(project=str(self.project), session_id="never-created", kind="checks", store=str(self.store))
        )
        self.assertFalse(missing["exists"])

    def test_a_repository_may_opt_into_versioned_packages(self) -> None:
        (self.project / ".spec").mkdir()
        (self.project / ".spec/verification.yml").write_text(
            "verification:\n  artifact_store: repo\n", encoding="utf-8"
        )
        root = Path(self.check_init(session_id="in-repo")["root"])
        self.assertTrue(root.is_relative_to(self.project), root)
        self.assertEqual(self.project / "spec/checks/in-repo", root)

    def test_an_explicit_root_always_wins(self) -> None:
        explicit = self.base / "somewhere/else"
        root = Path(self.check_init(root=str(explicit))["root"])
        self.assertEqual(explicit.resolve(), root)

    def test_the_store_honours_its_environment_overrides(self) -> None:
        original = {key: os.environ.get(key) for key in (spec_flow.STORE_ENV, "XDG_STATE_HOME")}

        def restore() -> None:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.pop(spec_flow.STORE_ENV, None)
        os.environ["XDG_STATE_HOME"] = str(self.base / "xdg")
        self.assertEqual((self.base / "xdg" / spec_flow.STORE_DIR).resolve(), spec_flow.store_root())
        os.environ[spec_flow.STORE_ENV] = str(self.base / "explicit")
        self.assertEqual((self.base / "explicit").resolve(), spec_flow.store_root())

    def test_a_session_id_that_would_escape_the_store_is_refused(self) -> None:
        for hostile in ("../escape", "/etc/passwd", "..", "   "):
            with self.subTest(session_id=hostile):
                path = None
                try:
                    path = spec_flow.package_path(self.project, "checks", hostile, str(self.store))
                except spec_flow.FlowError as error:
                    self.assertEqual("invalid_session_id", error.code)
                    continue
                self.assertTrue(path.resolve().is_relative_to(self.store.resolve()), f"{hostile} -> {path}")


class PropertyTests(unittest.TestCase):
    """Invariants that must hold for any package, not only the hand-written examples."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "check"
        spec_flow.cmd_check_init(
            args(root=str(self.root), session_id="p", title="p", rigor="standard", mode="deep",
                 source_file=None, config=None, path=None, hint=None)
        )
        for artifact in spec_flow.CHECK_AUTHORED_FILES:
            path = self.root / artifact
            path.write_text(spec_flow.PLACEHOLDER_RE.sub("reviewed", path.read_text(encoding="utf-8")), encoding="utf-8")

    def packages(self) -> dict:
        """A spread of valid and invalid shapes, all of which must respect the invariants."""
        base = minimal_check()
        waived = copy.deepcopy(base)
        waived["risks"][0]["disposition"] = "waived"
        waived["risks"][0]["cases"] = []
        waived["cases"] = []
        waived["execution_plan"] = []
        waived["waivers"] = [{"risk": "RISK-001", "reason": "accepted", "owner": "lead", "revisit": "next release"}]
        many = copy.deepcopy(base)
        many["risks"] = [
            {**minimal_risk(f"RISK-{index:03d}", severity), "cases": ["CASE-001"]}
            for index, severity in enumerate(("critical", "high", "medium", "low"), start=1)
        ]
        broken = copy.deepcopy(base)
        broken["cases"][0]["oracle"] = {"kind": "http-status", "source": "", "expected": ""}
        empty = copy.deepcopy(base)
        empty["risks"] = []
        empty["cases"] = []
        empty["execution_plan"] = []
        return {"base": base, "waived": waived, "many": many, "broken": broken, "empty": empty}

    def test_readiness_never_outruns_validation(self) -> None:
        for name, payload in self.packages().items():
            with self.subTest(package=name):
                spec_flow.atomic_write_json(self.root / "check.json", payload)
                result = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))
                if result["readiness"]["ready_to_execute"]:
                    self.assertTrue(result["ok"], f"{name} is ready to execute while validation fails")
                    self.assertEqual([], result["readiness"]["blocking_questions"])
                    self.assertEqual([], result["readiness"]["cases_needing_authorization"])

    def test_any_package_renders_whatever_its_verdict(self) -> None:
        for name, payload in self.packages().items():
            with self.subTest(package=name):
                spec_flow.atomic_write_json(self.root / "check.json", payload)
                spec_flow.cmd_check_render(args(root=str(self.root), config=None))
                for generated in spec_flow.CHECK_GENERATED_FILES:
                    self.assertTrue((self.root / generated).read_text(encoding="utf-8").strip(), f"{name}/{generated}")

    def test_coverage_arithmetic_is_consistent(self) -> None:
        for name, payload in self.packages().items():
            with self.subTest(package=name):
                spec_flow.atomic_write_json(self.root / "check.json", payload)
                coverage = spec_flow.cmd_check_validate(args(root=str(self.root), config=None))["coverage"]
                for severity, bucket in coverage.items():
                    self.assertLessEqual(bucket["covered"], bucket["total"], severity)
                    self.assertEqual(bucket["total"] - bucket["covered"], len(bucket["uncovered"]), severity)
                    expected = 100.0 if bucket["total"] == 0 else round(100.0 * bucket["covered"] / bucket["total"], 1)
                    self.assertEqual(expected, bucket["percent"], severity)

    def test_every_case_and_risk_reaches_the_rendered_output(self) -> None:
        payload = self.packages()["many"]
        spec_flow.atomic_write_json(self.root / "check.json", payload)
        spec_flow.cmd_check_render(args(root=str(self.root), config=None))
        cases = (self.root / "verification-cases.md").read_text(encoding="utf-8")
        brief = (self.root / "verification-brief.md").read_text(encoding="utf-8")
        for case in payload["cases"]:
            self.assertIn(case["id"], cases)
        for risk in payload["risks"]:
            self.assertIn(risk["id"], brief)

    def test_parallel_passes_are_all_recorded(self) -> None:
        errors: list[str] = []

        def worker(index: int) -> None:
            try:
                spec_flow.cmd_check_pass(
                    args(root=str(self.root), kind="broad", summary=f"pass {index}", new_critical=0,
                         new_high=0, new_medium=0, uncovered_invariants=0, surface_changed=False,
                         expected_revision=None)
                )
            except spec_flow.FlowError as error:
                errors.append(error.code)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        recorded = spec_flow.read_session(self.root)["iterations"]
        self.assertEqual(8, len(recorded), "a concurrent writer lost an update")
        self.assertEqual(list(range(1, 9)), sorted(item["pass"] for item in recorded))


class ContractTests(unittest.TestCase):
    """Keep the code, the shipped templates, the reference docs, and the CLI telling one story."""

    skill_root = Path(spec_flow.SKILL_ROOT)

    def test_the_shipped_template_satisfies_its_own_validator(self) -> None:
        template = json.loads((self.skill_root / "assets/check.template.json").read_text(encoding="utf-8"))
        self.assertEqual([], [f for f in spec_flow.REQUIRED_CASE_FIELDS if f not in template["cases"][0]])
        self.assertEqual([], [f for f in spec_flow.REQUIRED_RISK_FIELDS if f not in template["risks"][0]])
        self.assertEqual([], [f for f in spec_flow.REQUIRED_STEP_FIELDS if f not in template["cases"][0]["steps"][0]])

    def test_every_enforced_field_is_documented(self) -> None:
        doc = (self.skill_root / "references/test-case-quality.md").read_text(encoding="utf-8")
        undocumented = [field for field in spec_flow.REQUIRED_CASE_FIELDS if f"`{field}`" not in doc]
        self.assertEqual([], undocumented)

    def test_the_brief_renders_exactly_the_documented_sections(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "check"
            spec_flow.cmd_check_init(
                args(root=str(root), session_id="c", title="c", rigor="standard", mode="deep",
                     source_file=None, config=None, path=None, hint=None)
            )
            for artifact in spec_flow.CHECK_AUTHORED_FILES:
                path = root / artifact
                path.write_text(spec_flow.PLACEHOLDER_RE.sub("reviewed", path.read_text(encoding="utf-8")), encoding="utf-8")
            spec_flow.atomic_write_json(root / "check.json", minimal_check())
            spec_flow.cmd_check_render(args(root=str(root), config=None))
            rendered = {
                line.strip()
                for line in (root / "verification-brief.md").read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")
            }
        documented = {
            line.strip()
            for line in (self.skill_root / "assets/verification-brief.template.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        }
        self.assertEqual(documented, rendered)

    def test_a_negative_verdict_fails_the_shell(self) -> None:
        import subprocess

        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "check"
            spec_flow.cmd_check_init(
                args(root=str(root), session_id="c", title="c", rigor="standard", mode="deep",
                     source_file=None, config=None, path=None, hint=None)
            )
            result = subprocess.run(
                [sys.executable, str(self.skill_root / "scripts/spec_flow.py"), "check-validate", "--root", str(root)],
                capture_output=True, text=True,
            )
        self.assertEqual(1, result.returncode)
        self.assertFalse(json.loads(result.stdout)["ok"])


class ConfigTests(unittest.TestCase):
    def test_restricted_yaml_subset_parses_both_list_forms(self) -> None:
        indented = spec_flow.parse_simple_yaml(
            "verification:\n  critical_areas:\n    - payments\n    - auth\n  minimum_iterations: 2\n"
        )
        compact = spec_flow.parse_simple_yaml(
            "verification:\n  critical_areas:\n  - payments\n  - auth\n  minimum_iterations: 2\n"
        )
        self.assertEqual(indented, compact)
        self.assertEqual(["payments", "auth"], indented["verification"]["critical_areas"])
        self.assertEqual(2, indented["verification"]["minimum_iterations"])

    def test_project_config_overrides_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / ".spec").mkdir()
            (base / ".spec/verification.yml").write_text(
                "verification:\n  default_mode: lite\n  minimum_iterations: 1\n", encoding="utf-8"
            )
            root = base / "checks/one"
            root.mkdir(parents=True)
            config = spec_flow.resolve_config(root)
            self.assertEqual("lite", spec_flow.verification_setting(config, "default_mode"))
            self.assertEqual(1, spec_flow.verification_setting(config, "minimum_iterations"))
            self.assertEqual(100, spec_flow.verification_setting(config, "required_risk_coverage")["critical"])
            self.assertEqual("lite", spec_flow.suggest_mode(["docs/readme.md"], config)["mode"])
            self.assertEqual("deep", spec_flow.suggest_mode(["src/payments/x.py"], config)["mode"])

    def test_unreadable_config_surfaces_as_an_error_not_a_silent_default(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / ".spec").mkdir()
            (base / ".spec/verification.json").write_text("{not json", encoding="utf-8")
            config = spec_flow.resolve_config(base)
            self.assertIn("cannot read verification config", config["_error"])

    def test_broken_yaml_never_crashes_a_lifecycle_command(self) -> None:
        for body in ("verification:\n\tminimum_iterations: 2\n", "verification:\n  - [oops\n", "\x00binary"):
            with tempfile.TemporaryDirectory() as name:
                base = Path(name)
                (base / ".spec").mkdir()
                (base / ".spec/verification.yml").write_text(body, encoding="utf-8")
                config = spec_flow.resolve_config(base)
                self.assertIn("_error", config, body)
                self.assertEqual(3, spec_flow.verification_setting(config, "minimum_iterations"))
                self.assertEqual("deep", spec_flow.suggest_mode(["src/payments/x.py"], config)["mode"])

    def test_tab_indentation_is_rejected_by_the_fallback_parser(self) -> None:
        with self.assertRaises(spec_flow.FlowError) as error:
            spec_flow.parse_simple_yaml("verification:\n\tminimum_iterations: 2\n")
        self.assertEqual("config_invalid", error.exception.code)

    def test_unusable_config_values_fall_back_and_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            (base / ".spec").mkdir()
            (base / ".spec/verification.yml").write_text(
                "verification:\n"
                "  default_mode: turbo\n"
                "  minimum_iterations: not-a-number\n"
                "  maximum_iterations: 0\n"
                "  require_cleanup: sometimes\n"
                "  required_risk_coverage:\n"
                "    critical: 100\n"
                "    catastrophic: 100\n"
                "    high: 900\n"
                "  unknown_setting: 1\n",
                encoding="utf-8",
            )
            config = spec_flow.resolve_config(base)
            self.assertEqual(3, spec_flow.verification_setting(config, "minimum_iterations"))
            self.assertEqual(6, spec_flow.verification_setting(config, "maximum_iterations"))
            self.assertEqual("auto", spec_flow.verification_setting(config, "default_mode"))
            self.assertIs(True, spec_flow.verification_setting(config, "require_cleanup"))
            coverage = spec_flow.verification_setting(config, "required_risk_coverage")
            self.assertEqual(100, coverage["critical"])
            self.assertEqual(100, coverage["high"])
            self.assertNotIn("catastrophic", coverage)
            self.assertEqual(3, spec_flow.minimum_passes("standard", config))
            joined = " | ".join(config["_warnings"])
            for fragment in ("default_mode", "minimum_iterations", "maximum_iterations", "require_cleanup",
                             "catastrophic", "high", "unknown_setting"):
                self.assertIn(fragment, joined)

    def test_carriage_returns_and_inline_comments_parse(self) -> None:
        parsed = spec_flow.parse_simple_yaml("verification:\r\n  minimum_iterations: 2 # keep it low\r\n")
        self.assertEqual(2, parsed["verification"]["minimum_iterations"])


if __name__ == "__main__":
    unittest.main()
