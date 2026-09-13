"""Combined delivery/spec/check regression with real local Git and executable gates.

All human/agent labels below are synthetic unit-test actors. The publication
provider is an explicit in-memory test transport backed by a local bare remote;
these tests never authenticate a person, launch a real agent, or contact GitHub.
No lifecycle state, approval, task receipt, or gate result is mocked or edited.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from tests import test_run_engine as run_fixtures
from tests.spec import test_spec_flow as spec_fixtures

e = run_fixtures.e
git = run_fixtures.git


class LocalPublicationProvider:
    """The provider seam models only this test's explicit local origin."""

    def __init__(self, remote):
        self.remote = Path(remote).resolve()
        self.rows = {}
        self.calls = []

    def repository(self, origin):
        if Path(origin).resolve() != self.remote:
            raise AssertionError("The rich integration test must only use its local bare origin.")
        return "fixture/rich-delivery"

    def find(self, repo, branch):
        self.calls.append(("find", repo, branch))
        return [self.view(repo, number) for number, row in self.rows.items() if row["headRefName"] == branch]

    def create(self, repo, branch, title, body):
        self.calls.append(("create", repo, branch))
        number = len(self.rows) + 1
        self.rows[number] = {
            "number": number, "url": f"https://github.com/{repo}/pull/{number}",
            "state": "OPEN", "title": title, "body": body,
            "headRefName": branch, "baseRefName": "main", "isCrossRepository": False,
            "isDraft": False, "reviewDecision": "APPROVED", "statusCheckRollup": [],
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "mergedAt": None,
            "mergeCommit": None, "autoMergeRequest": None,
        }
        return self.view(repo, number)

    def view(self, repo, number):
        self.calls.append(("view", repo, number))
        row = copy.deepcopy(self.rows[number])
        row["headRefOid"] = git(self.remote, "rev-parse", "refs/heads/" + row["headRefName"])
        row["baseRefOid"] = git(self.remote, "rev-parse", "refs/heads/main")
        return row

    def update(self, repo, number, title, body):
        self.calls.append(("update", repo, number))
        self.rows[number].update(title=title, body=body)
        return self.view(repo, number)


class RichIntegrationTests(run_fixtures.RunFixture):
    def setUp(self):
        super().setUp()
        self.input_dir = self.root / "fixture-inputs"
        self.input_dir.mkdir()
        self.authority = self.input_dir / "synthetic-authority.md"
        self.authority.write_text(
            "Synthetic unit-test human authority for this disposable fixture only. "
            "This is not a real user's approval, a live agent result, or permission to publish externally.\n",
            encoding="utf-8",
        )

    def cli(self, command, *arguments, expected_code=0):
        result = subprocess.run(
            [sys.executable, "-B", str(run_fixtures.SCRIPTS / "delivery.py"), command,
             "--run", str(self.root), *map(str, arguments)],
            cwd=self.home, capture_output=True, text=True,
        )
        self.assertEqual(expected_code, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout if result.returncode == 0 else result.stderr)

    def rich(self, *arguments, check=False):
        return e.spec_command(self.root, list(arguments), check=check)

    def planning_ready(self):
        value = run_fixtures.plan()
        value["triggers"] = ["runtime_qa"]
        value["verification"][0]["check_case"] = "CASE-001"
        plan_path = self.input_dir / "medium-plan.json"
        plan_path.write_text(json.dumps(value), encoding="utf-8")
        planned = self.cli("plan", "--file", plan_path)["result"]
        self.assertEqual("medium", planned["plan"]["classification"]["level"])
        initialized = self.cli("spec", "--", "init")["result"]
        self.assertEqual("INTENT_CAPTURED", initialized["state"])
        spec_root = Path(e.load(self.root)["spec_root"])
        original = (self.root / "intent.md").read_text(encoding="utf-8")
        self.assertIn(original.rstrip(), (spec_root / "intent.md").read_text(encoding="utf-8"))

        # Reuse the shipped planning fixture's package shape, then write this
        # scenario's actual contract into every semantic artifact.
        fixture = spec_fixtures.SpecFlowTests()
        fixture.root = spec_root
        fixture.write_ready_package()
        artifacts = {
            "intent.md": "# Intent\n\n## Raw input\n" + original + "\n## Fidelity map\nUpdate fixture value -> updated-value.\n",
            "feature-model.md": "# Feature model\n\nL0 fixture request -> L1 exact persisted value -> L2 file update -> L3 read after update -> L4 before to after -> L5 value.txt -> L6 exact text oracle -> L7 value task.\n",
            "proposal.md": "# Proposal\n\nChange value.txt from before to after, each followed by one newline. No other file, service, or product behavior changes.\n",
            "decisions.md": "# Decisions\n\nNo unresolved product decisions. The unit fixture explicitly defines the desired text and excludes external services.\n",
            "design.md": "# Design\n\nReplace the plain-text file value.txt with after followed by one newline. Keep the primary checkout clean and implement through an isolated task worktree.\n",
            "impact-analysis.md": "# Impact\n\nRISK-001: a stale or incorrect staged value could survive integration. Preserve exact scope and verify the persisted integrated file contents.\n",
            "verification-cases.md": "# Verification\n\nCASE-001 reads value.txt in the integrated worktree and requires after followed by one newline. The file is the authoritative effect oracle; the check makes zero writes.\n",
            "test-plan.md": "# Test plan\n\nupdated-value -> CASE-001 -> value-check. Execute the planned Python read-only assertion. Preserve its exit status, output log, content fingerprint, and independent review receipt.\n",
            "tasks.md": "# Tasks\n\n- [ ] value: update only value.txt, stage it, return the exact diff, run the file-content gate, and obtain an independent synthetic fixture review.\n",
            "review.md": "# Review\n\nReadiness: READY\nCritical findings: 0\nThe deterministic unit fixture defines exact behavior, scope, and oracle. All actors are synthetic test actors.\n",
            "review-brief.md": "# Review Brief\n\nSpec version: 1\nReadiness: READY\nScope: value.txt only. Approve the unit-test baseline explicitly, then separately authorize implementation. No external publication is part of ordinary completion.\n",
            "specs/example/spec.md": "# Fixture value\n\nupdated-value MUST persist the exact text after followed by one newline in value.txt. Reading the result MUST NOT change any file.\n",
        }
        for name, content in artifacts.items():
            (spec_root / name).write_text(content, encoding="utf-8")
        self.assertTrue(self.rich("validate")["ok"])
        for state in spec_fixtures.spec_flow.PLANNING_STATES[2:]:
            self.rich("advance", "--to", state)
        self.assertEqual("WAITING_APPROVAL", self.rich("status")["state"])

    def begin_medium(self):
        self.planning_ready()
        self.cli("approve", "--actor", "fixture-user", "--evidence-file", self.authority)
        approved = self.rich("status")
        self.assertEqual("APPROVED", approved["state"])
        self.assertEqual(["value.txt"], approved["approval"]["expected_change_surface"])
        self.assertFalse((Path(e.load(self.root)["spec_root"]) / "implementation-evidence.md").exists())
        self.cli("authorize", "--scope", "implement", "--actor", "fixture-user", "--evidence-file", self.authority)
        self.cli("start")
        self.assertEqual("IMPLEMENTING", self.rich("status")["state"])

    def integrate_medium(self):
        self.begin_medium()
        self.implement()
        self.verify()
        self.cli("integrate")
        current = e.load(self.root)
        task = current["tasks"]["value"]
        evidence = (
            "# Implementation evidence\n\n"
            "Task value changed only value.txt and returned its staged patch through the real run API.\n"
            f"Patch: {task['patch']['path']}\nPatch SHA-256: {task['patch']['sha256']}\n"
            "The real task command passed and the synthetic independent reviewer accepted that exact content.\n"
            "No live agents, external services, or production resources were used by this unit fixture.\n"
        )
        (Path(current["spec_root"]) / "implementation-evidence.md").write_text(evidence, encoding="utf-8")
        return Path(current["integration"]["path"])

    def capture_medium(self, reason="Capture the first exact fixture integration"):
        captured = self.cli("capture-verification", "--reason", reason)["result"]
        binding = captured["rich_snapshot"]
        before, after = Path(binding["before"]["path"]), Path(binding["after"]["path"])
        self.assertEqual("before\n", (before / "value.txt").read_text(encoding="utf-8"))
        self.assertEqual("after\n", (after / "value.txt").read_text(encoding="utf-8"))
        self.assertFalse(list(before.rglob(".git")))
        self.assertFalse(list(after.rglob(".git")))
        self.assertEqual(e.snapshot(captured), binding["snapshot"])
        self.assertEqual("POST_IMPLEMENTATION_REVIEW", self.rich("status")["state"])
        self.assertEqual([], self.rich("status")["material_findings"])
        review = (
            "# Post-implementation review\n\n"
            f"Capture reason: {reason}\n"
            "The actual inventory contains exactly the approved value.txt modification.\n"
            "Unexpected paths: none. Material deviations: none.\n"
            f"Before tree: {binding['before']['tree']}\nAfter tree: {binding['after']['tree']}\n"
            "Exact inventory: " + json.dumps(self.rich("status")["actual_change_surface"], sort_keys=True) + "\n"
        )
        (Path(captured["spec_root"]) / "post-implementation-review.md").write_text(review, encoding="utf-8")
        return captured

    def check_ready(self, *, extra_case=False, command_override=None):
        current = e.load(self.root)
        check_root = Path(current["check_root"])
        initialized = self.rich("status", check=True)
        check = spec_fixtures.minimal_check()
        check.update(
            title="Integrated fixture value", mode=initialized["check_mode"],
            mode_reason="The delivery plan requires runtime verification of the persisted value.",
            sources=[{"kind": "diff", "ref": current["id"], "location": str(check_root / "actual-diff.json"), "authority": "implementation"}],
            references=[{"id": "REF-1", "kind": "spec", "title": "Fixture value contract", "location": str(Path(current["spec_root"]) / "specs/example/spec.md"), "relevance": "Defines exact value and zero check-side effects."}],
            intent={"stated": "Update the fixture value and verify it.", "reconstructed": "The staged file changes from before to after.", "divergences": []},
            changed_surface=[{"path": "value.txt", "status": "modified", "summary": "Exact plain-text value update."}],
            affected_surface=[{"area": "fixture file read", "why": "Readers observe the persisted value", "evidence": ["REF-1"]}],
            environment={"available": ["Python interpreter", "isolated integration worktree"], "required": ["Python interpreter", "isolated integration worktree"], "missing": [], "setup": ["Use the current exact integration tree."]},
        )
        check["risks"][0].update(
            title="Incorrect integrated value", mechanism="A stale or wrong staged value survives integration.",
            invariant="The file reads after followed by exactly one newline.", evidence=["value.txt", "REF-1"],
            severity="medium", detectability="Direct file-content assertion", blast_radius="The one fixture file",
            existing_protection="The task gate checks the same invariant before integration.",
        )
        case = check["cases"][0]
        case.update(
            title="The integrated file has the approved value", requirements=["updated-value"],
            objective="Does the integrated file contain exactly the approved text?",
            why_it_catches_the_risk="Read the persisted file after integration, so wrong or stale staged contents fail.",
            preconditions=["A verified task patch has been integrated into the owned worktree."],
            environment={"build": current["rich_snapshot"]["after"]["tree"], "network": "No network access is needed."},
            fixtures=["value.txt with the proposed after value"], fault_injection=[], intermediate_checks=[],
            steps=[{"n": 1, "action": "Execute the delivery_command array in the integrated worktree.", "expected": "The Python assertion exits zero and changes nothing.", "how_to_observe": "Read value.txt and retain the gate's output log and exit code."}],
            expected_final_state=["value.txt contains after followed by one newline."],
            forbidden_outcomes=["Any other file value", "Any file mutation during verification"],
            oracle={"kind": "filesystem", "source": "value.txt in the owned integration worktree", "query": "Path('value.txt').read_text()", "expected": "after followed by one newline"},
            side_effect_count="0 writes; the check only reads the fixture file.",
            evidence=[str(self.root / "evidence")], cleanup=["No resources are created by the read-only assertion."],
            timeout="30 seconds", safety="safe", delivery_command=current["plan"]["verification"][0]["command"],
        )
        if command_override is not None:
            case["delivery_command"] = command_override
        if extra_case:
            additional = copy.deepcopy(case)
            additional.update(
                id="CASE-002", title="No unexpected companion file was introduced",
                objective="Does the integrated worktree remain free of an unexpected companion file?",
                delivery_command=[sys.executable, "-B", "-c", "from pathlib import Path; assert not Path('unexpected.txt').exists()"],
                steps=[{"n": 1, "action": "Read whether unexpected.txt exists.", "expected": "The file is absent.", "how_to_observe": "Path('unexpected.txt').exists() returns False."}],
                expected_final_state=["unexpected.txt is absent."],
                oracle={"kind": "filesystem", "source": "owned integration worktree", "query": "Path('unexpected.txt').exists()", "expected": "False"},
            )
            check["cases"].append(additional)
            check["risks"][0]["cases"].append("CASE-002")
            check["execution_plan"].append({"order": 2, "case": "CASE-002", "rationale": "Confirm the derived exact-scope invariant.", "stop_on_fail": True})
        check["execution_plan"][0]["rationale"] = "Prove the only changed requirement using the exact persisted file."
        fixture = spec_fixtures.CheckFlowTests()
        fixture.root = check_root
        fixture.write_check(check)
        authored = {
            "source.md": "# Source\n\n" + (self.root / "intent.md").read_text(encoding="utf-8") + "\nImplementation source: the actual-diff.json copied from the captured Git trees.\n",
            "impact-map.md": "# Impact map\n\nvalue.txt is the only changed path. RISK-001 concerns the exact persisted value; CASE-001 reads that value without side effects.\n",
            "case-review.md": "# Case review\n\nVerdict: READY\nCASE-001 accepted: reproducible local setup, exact file oracle, no external effects, and explicit synthetic test actors.\n",
        }
        for name, content in authored.items():
            (check_root / name).write_text(content, encoding="utf-8")
        for state in ("CHECK_SOURCE_RESOLVED", "CHECK_CHANGE_ANALYZED", "CHECK_SURFACE_MAPPED", "CHECK_RISK_ITERATING"):
            self.rich("advance", "--to", state, check=True)
        count = {"lite": 2, "standard": 3, "deep": 4}[initialized["check_mode"]]
        for index in range(count):
            self.rich("check-pass", "--kind", ("broad", "realism", "adversarial")[min(index, 2)], "--summary", f"Synthetic fixture risk pass {index + 1}: exact value and scope examined.", "--new-medium", "1" if index == 0 else "0", check=True)
        for state in ("CHECK_CASES_DRAFTED", "CHECK_CASES_REVIEWED", "CHECK_ENVIRONMENT_PLANNED", "CHECK_BRIEF_READY"):
            self.rich("advance", "--to", state, check=True)
        self.assertTrue(self.rich("check-render", check=True)["ok"])
        return check_root

    def authorize_check(self):
        authorized = self.rich("check-authorize", "--actor", "fixture-check-operator", "--scope", "safe", "--note", self.authority.read_text(encoding="utf-8"), check=True)
        self.assertEqual("CHECK_EXECUTING", authorized["state"])

    def finish_verification(self, *, complete_check=True, mark_ready=True):
        self.rich("advance", "--to", "VERIFYING")
        receipt = self.cli("gate", "--case", "value-check")["result"]
        self.assertTrue(receipt["passed"])
        self.assertEqual(0, receipt["exit_code"])
        self.assertEqual(hashlib.sha256(Path(receipt["log"]).read_bytes()).hexdigest(), receipt["log_hash"])
        current = e.load(self.root)
        report = (
            "# Verification report\n\nVerdict: PASS\n"
            f"Integrated tree: {current['rich_snapshot']['after']['tree']}\n"
            f"Base tree: {current['rich_snapshot']['before']['tree']}\n"
            f"Verification generation: {self.rich('status')['verification_generation']}\n"
            f"Observed command: {json.dumps(current['plan']['verification'][0]['command'])}\n"
            f"Observed exit code: {receipt['exit_code']}\n"
            f"Gate log: {receipt['log']}\nGate log SHA-256: {receipt['log_hash']}\n"
            "Observed final state: value.txt contains after followed by one newline.\n"
            "Observed side effects: zero writes; the gate's content fingerprint stayed unchanged.\n"
            "Scope limit: disposable local fixture only; all authority actors are synthetic.\n"
        )
        for root_key in ("spec_root", "check_root"):
            (Path(current[root_key]) / "verification-report.md").write_text(report, encoding="utf-8")
        if complete_check:
            self.rich("complete", "--verdict", "PASS", check=True)
        self.rich("complete", "--verdict", "PASS")
        self.cli("review", "--actor", "fixture-integration-reviewer", "--verdict", "PASS", "--evidence-file", self.authority)
        return self.cli("ready")["result"] if mark_ready else e.load(self.root)

    def medium_ready(self):
        self.integrate_medium()
        self.capture_medium()
        self.check_ready()
        self.authorize_check()
        return self.finish_verification()

    def test_medium_full_lifecycle_runs_authorized_rich_case_and_reaches_readiness(self):
        ready = self.medium_ready()
        self.assertEqual("READY_TO_PUBLISH", ready["state"])
        self.assertEqual("DONE", self.rich("status")["state"])
        self.assertEqual("CHECK_DONE", self.rich("status", check=True)["state"])
        self.assertEqual("matching", self.rich("status")["baseline_status"])
        self.assertEqual(["value.txt"], ready["validated"]["files"])
        self.assertEqual(self.original, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual("main", git(self.repo, "branch", "--show-current"))
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertIsNone(ready["pr"])

    def test_semantic_drift_blocks_root_start_after_approval(self):
        self.planning_ready()
        self.cli("approve", "--actor", "fixture-user", "--evidence-file", self.authority)
        self.cli("authorize", "--scope", "implement", "--actor", "fixture-user", "--evidence-file", self.authority)
        design = Path(e.load(self.root)["spec_root"]) / "design.md"
        design.write_text(design.read_text(encoding="utf-8") + "\nUnapproved change: also edit another file.\n", encoding="utf-8")
        refused = self.cli("start", expected_code=2)
        self.assertEqual("spec_approval_required", refused["error"]["code"])
        self.assertEqual({}, e.load(self.root)["tasks"])
        self.assertEqual(self.original, git(self.repo, "rev-parse", "HEAD"))

    def test_integrated_content_drift_invalidates_completed_rich_snapshot(self):
        ready = self.medium_ready()
        worktree = Path(ready["integration"]["path"])
        (worktree / "value.txt").write_text("unapproved replacement\n", encoding="utf-8")
        git(worktree, "add", "value.txt")
        with self.assertRaises(e.RunError) as error:
            e.ready(self.root)
        self.assertEqual("stale_spec_verification", error.exception.code)
        self.assertFalse(e.evidence_current(ready["rich_snapshot"], e.snapshot(e.load(self.root))))

    def test_integrated_gate_cannot_run_before_check_authorization(self):
        self.integrate_medium()
        self.capture_medium()
        self.check_ready()
        prior = len(e.load(self.root)["gates"])
        with self.assertRaises(e.RunError):
            e.execute_gate(self.root, case_id="value-check")
        self.assertEqual(prior, len(e.load(self.root)["gates"]))

    def test_case_plan_drift_refuses_execution_instead_of_trusting_old_authority(self):
        self.integrate_medium()
        self.capture_medium()
        check_root = self.check_ready()
        self.authorize_check()
        check = json.loads((check_root / "check.json").read_text(encoding="utf-8"))
        check["cases"][0]["steps"][0]["action"] = "An unapproved altered execution step."
        (check_root / "check.json").write_text(json.dumps(check), encoding="utf-8")
        prior = len(e.load(self.root)["gates"])
        with self.assertRaises(e.RunError):
            e.execute_gate(self.root, case_id="value-check")
        self.assertEqual(prior, len(e.load(self.root)["gates"]))

    def test_authorized_case_cannot_substitute_a_different_delivery_command(self):
        self.integrate_medium()
        self.capture_medium()
        self.check_ready(command_override=[sys.executable, "-B", "-c", "pass"])
        self.authorize_check()
        prior = len(e.load(self.root)["gates"])
        with self.assertRaises(e.RunError) as error:
            e.execute_gate(self.root, case_id="value-check")
        self.assertEqual("check_command_mismatch", error.exception.code)
        self.assertEqual(prior, len(e.load(self.root)["gates"]))

    def test_readiness_requires_completed_rich_checks_not_only_authorization(self):
        self.integrate_medium()
        self.capture_medium()
        self.check_ready()
        self.authorize_check()
        self.finish_verification(complete_check=False, mark_ready=False)
        self.assertEqual("DONE", self.rich("status")["state"])
        self.assertEqual("CHECK_EXECUTING", self.rich("status", check=True)["state"])
        with self.assertRaises(e.RunError):
            e.ready(self.root)
        self.assertEqual("VERIFYING", e.load(self.root)["state"])

    def test_an_accepted_rich_case_cannot_be_left_out_of_the_delivery_execution_plan(self):
        self.integrate_medium()
        self.capture_medium()
        self.check_ready(extra_case=True)
        self.authorize_check()
        prior = len(e.load(self.root)["gates"])
        with self.assertRaises(e.RunError):
            e.execute_gate(self.root, case_id="value-check")
        self.assertEqual(prior, len(e.load(self.root)["gates"]))

    def test_rebase_preserves_semantics_but_requires_fresh_snapshot_cases_and_receipts(self):
        import publisher

        ready = self.medium_ready()
        approval = self.rich("status")["approval"]
        previous_check = ready["check_root"]
        previous_snapshot = ready["rich_snapshot"]
        provider = LocalPublicationProvider(self.remote)
        self.cli("authorize", "--scope", "publish", "--actor", "fixture-user", "--evidence-file", self.authority)
        publisher.commit(self.root, "Update the documented fixture value")
        published = publisher.publish(self.root, "Update fixture value", "The local unit fixture now contains the approved value. Verified by the exact file-content assertion.", provider=provider)
        self.assertEqual("PR_OPEN", published["state"])
        self.cli("authorize", "--scope", "merge", "--actor", "fixture-user", "--evidence-file", self.authority)

        upstream = self.home / "independent-base-writer"
        subprocess.run(["git", "clone", str(self.remote), str(upstream)], check=True, capture_output=True)
        git(upstream, "config", "user.name", "Fixture Base Maintainer")
        git(upstream, "config", "user.email", "base-fixture@example.invalid")
        (upstream / "readme.md").write_text("A newer unrelated base for the rebase fixture.\n", encoding="utf-8")
        git(upstream, "add", "readme.md")
        git(upstream, "commit", "-m", "Advance the local test base")
        git(upstream, "push", "origin", "main")
        latest_base = git(upstream, "rev-parse", "HEAD")

        refreshed = publisher.refresh(self.root, provider=provider)
        self.assertEqual("VERIFYING", refreshed["state"])
        self.assertEqual(latest_base, refreshed["integration"]["base_sha"])
        self.assertEqual(approval, self.rich("status")["approval"])
        self.assertTrue(refreshed["spec_refresh_required"])
        self.assertIsNone(refreshed["validated"])
        self.assertFalse([receipt for receipt in refreshed["gates"] if receipt["task"] is None])
        self.assertFalse([receipt for receipt in refreshed["reviews"] if receipt["task"] is None])
        with self.assertRaises(e.RunError):
            e.ready(self.root)

        recaptured = self.capture_medium("The local fixture branch was rebased onto fresh main")
        self.assertNotEqual(previous_check, recaptured["check_root"])
        self.assertNotEqual(previous_snapshot["before"]["tree"], recaptured["rich_snapshot"]["before"]["tree"])
        self.assertTrue(Path(previous_snapshot["before"]["path"]).is_dir())
        self.assertTrue(Path(previous_check).is_dir())
        self.assertEqual(2, self.rich("status")["verification_generation"])
        self.assertEqual(approval, self.rich("status")["approval"])
        self.check_ready()
        self.authorize_check()
        reready = self.finish_verification()
        self.assertEqual("PR_OPEN", reready["state"])
        self.assertEqual(latest_base, reready["validated"]["base_sha"])
        self.assertEqual(self.original, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual(1, sum(call[0] == "create" for call in provider.calls))


if __name__ == "__main__":
    import unittest
    unittest.main()
