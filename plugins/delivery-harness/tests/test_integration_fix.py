"""Same-scope review corrections use actual-dispatch-shaped, synthetic receipts."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from tests.test_run_engine import RunFixture, SCRIPTS, e, gate, git, plan


class IntegrationFixTests(RunFixture):
    def setup_integration(self):
        value = plan()
        value["requirements"][0]["behavior"] = "The fixture value begins with after."
        check = gate([sys.executable, "-B", "-c", "from pathlib import Path; assert Path('value.txt').read_text().startswith('after')"])
        value["tasks"][0]["gates"] = [check]
        value["verification"][0].update(check)
        self.begin(value); self.implement(); self.verify(); e.integrate(self.root)
        e.execute_gate(self.root, case_id="value-check")
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Synthetic initial integration review.")
        e.ready(self.root)
        return Path(e.load(self.root)["integration"]["path"])

    def register(self, suffix="one"):
        return e.register_fix(self.root, "fixture-fix-writer", "unit-test", "synthetic:fix-" + suffix, "Synthetic review correction within approved behavior.")

    def report_correction(self):
        current = e.load(self.root)["integration_fix"]
        return e.report_fix(self.root, "fixture-fix-writer", {"dispatch_id": current["dispatch_id"], "summary": "Refined the same approved value.", "tests": "Host gate pending.", "limitations": "Synthetic fixture only.", "source_event": "synthetic-fix-result:" + current["dispatch_id"]})

    def test_pending_fix_cannot_verify(self):
        self.setup_integration()
        self.register()
        with self.assertRaisesRegex(e.RunError, "real report"):
            e.execute_gate(self.root, case_id="value-check")

    def test_same_scope_fix_reruns_gates_and_preserves_previous_receipts(self):
        path = self.setup_integration()
        before = e.load(self.root)["validated"]["content"]
        self.register()
        (path / "value.txt").write_text("after revised\n")
        git(path, "add", "--", "value.txt")
        self.report_correction()
        e.execute_gate(self.root, case_id="value-check")
        with self.assertRaisesRegex(e.RunError, "reviewer"):
            e.review(self.root, "fixture-fix-writer", "PASS", "Invalid synthetic self-review.")
        e.review(self.root, "fixture-new-reviewer", "PASS", "Synthetic independent correction review.")
        ready = e.ready(self.root)
        self.assertEqual(ready["state"], "READY_TO_PUBLISH")
        self.assertNotEqual(before, ready["validated"]["content"])
        self.assertTrue(ready["verification_history"][0]["gates"])

    def test_old_fix_report_cannot_complete_replacement_dispatch(self):
        self.setup_integration()
        self.register()
        prior = self.report_correction()["integration_fix"]["report"]
        self.register("two")
        with self.assertRaisesRegex(e.RunError, "this dispatch"):
            e.report_fix(self.root, "fixture-fix-writer", prior)

    def test_fix_cannot_expand_scope(self):
        path = self.setup_integration()
        self.register()
        (path / "unplanned.txt").write_text("not authorized\n")
        git(path, "add", "--", "unplanned.txt")
        with self.assertRaisesRegex(e.RunError, "cannot expand"):
            self.report_correction()

    def test_semantic_revision_archives_fix_without_blocking_the_new_version(self):
        path = self.setup_integration()
        self.register()
        (path / "value.txt").write_text("after revised\n")
        git(path, "add", "--", "value.txt")
        self.report_correction()
        e.execute_gate(self.root, case_id="value-check")
        e.review(self.root, "fixture-correction-reviewer", "PASS", "Synthetic independent correction review.")
        before = e.ready(self.root)

        revised = e.revise(self.root, "The authorized fixture now requires the exact value, not a prefix.")
        self.assertIsNone(revised["integration_fix"])
        self.assertEqual(revised["integration_fix_history"], [])
        previous = revised["previous_versions"][-1]
        self.assertEqual(previous["integration_fix"], before["integration_fix"])
        self.assertEqual(previous["gates"], before["gates"])
        self.assertEqual(previous["reviews"], before["reviews"])
        self.assertEqual(revised["writer_history"], before["writer_history"])
        self.assertTrue(path.is_dir())

        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        with self.assertRaisesRegex(e.RunError, "reviewer"):
            e.review(self.root, "fixture-fix-writer", "PASS", "A prior implementer must remain ineligible to review.")
        e.review(self.root, "fixture-next-reviewer", "PASS", "Synthetic independent review of the new exact-value version.")
        ready = e.ready(self.root)
        self.assertEqual(ready["spec_version"], 2)
        self.assertEqual(ready["state"], "READY_TO_PUBLISH")
        self.assertNotEqual(ready["validated"]["content"], before["integration_fix"]["report"]["snapshot"]["content"])
        self.assertEqual(self.register("new-version")["integration_fix"]["attempt"], 1)

    def exhaust_default_budget(self):
        for suffix in ("one", "two"):
            self.register(suffix)
            self.report_correction()
        with self.assertRaises(e.RunError) as caught:
            self.register("three")
        self.assertEqual("rework_budget", caught.exception.code)
        return caught.exception

    def test_fix_budget_extension_is_explicit_bounded_and_counted(self):
        self.setup_integration()
        refused = self.exhaust_default_budget()
        self.assertIn("authorize --scope fix-budget --count N", refused.message)
        self.assertEqual({"used": 2, "limit": 2}, e.status(self.root)["fix_budget"])
        for count in (None, 0, 4, True, "1"):
            with self.subTest(count=count), self.assertRaises(e.RunError) as caught:
                e.authorize(self.root, "fix-budget", "fixture-user", "Synthetic invalid extension.", count=count)
            self.assertEqual("invalid_fix_budget", caught.exception.code)
        with self.assertRaises(e.RunError) as caught:
            e.authorize(self.root, "implement", "fixture-user", "Synthetic authority.", count=1)
        self.assertEqual("invalid_authorization_count", caught.exception.code)
        evidence = self.home / "fix-budget.md"
        evidence.write_text("Synthetic explicit extension of one integration-fix round.\n")
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), "authorize", "--run", str(self.root),
                                 "--scope", "fix-budget", "--count", "1", "--actor", "fixture-user", "--evidence-file", str(evidence)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        grants = json.loads(result.stdout)["result"]["fix_budget_grants"]
        self.assertEqual([(1, "fixture-user", e.load(self.root)["plan_hash"])],
                         [(grant["count"], grant["actor"], grant["plan_hash"]) for grant in grants])
        self.assertNotIn("fix-budget", e.load(self.root)["authorizations"])
        self.assertEqual(3, self.register("three")["integration_fix"]["attempt"])
        self.report_correction()
        with self.assertRaises(e.RunError) as caught:
            self.register("four")
        self.assertEqual("rework_budget", caught.exception.code)
        self.assertEqual({"used": 3, "limit": 3}, e.status(self.root)["fix_budget"])

    def test_fix_budget_grant_for_an_older_plan_does_not_count(self):
        e.set_plan(self.root, plan())
        with self.assertRaises(e.RunError) as caught:
            e.authorize(self.root, "fix-budget", "fixture-user", "Synthetic early extension.", count=1)
        self.assertEqual("fix_budget_state", caught.exception.code)
        self.setup_integration()
        e.authorize(self.root, "fix-budget", "fixture-user", "Synthetic extension for the prefix plan.", count=3)
        old_hash = e.load(self.root)["plan_hash"]
        e.revise(self.root, "The authorized fixture now requires the exact value, not a prefix.")
        self.begin()
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-next-reviewer", "PASS", "Synthetic independent review of the exact-value plan.")
        current = e.ready(self.root)
        self.assertNotEqual(old_hash, current["plan_hash"])
        self.assertEqual([old_hash], [grant["plan_hash"] for grant in current["fix_budget_grants"]])
        self.exhaust_default_budget()
        self.assertEqual({"used": 2, "limit": 2}, e.status(self.root)["fix_budget"])


    def test_fix_budget_grant_does_not_revive_for_an_identical_plan_approved_again(self):
        self.setup_integration()
        e.authorize(self.root, "fix-budget", "fixture-user", "Synthetic extension for the first version.", count=2)
        first = e.load(self.root)
        e.revise(self.root, "Synthetic revision that ends with the same plan.")
        self.setup_integration()
        second = e.load(self.root)
        self.assertEqual(first["plan_hash"], second["plan_hash"])
        self.assertNotEqual(first["spec_version"], second["spec_version"])
        self.exhaust_default_budget()
        self.assertEqual({"used": 2, "limit": 2}, e.status(self.root)["fix_budget"])

if __name__ == "__main__":
    unittest.main()
