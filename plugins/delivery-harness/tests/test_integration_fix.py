"""Same-scope review corrections use actual-dispatch-shaped, synthetic receipts."""
from pathlib import Path
import sys
import unittest

from tests.test_run_engine import RunFixture, e, gate, git, plan


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


if __name__ == "__main__":
    unittest.main()
