"""Regression for the first actual host forward-test report."""
import unittest
from tests.test_run_engine import e


class ReportSchemaTests(unittest.TestCase):
    def report(self):
        return {"summary": "Implemented the bounded change.", "dispatch_id": "fixture-only", "tests": [{"command": ["python3", "-B", "-m", "unittest"], "exit_code": 0, "outcome": "Ran six tests; OK."}, {"command": "git diff --cached --check", "exit_code": 0, "outcome": "No whitespace errors."}], "limitations": ["External deployment was not tested."], "source_event": "synthetic fixture observation"}

    def test_actual_host_shaped_structured_report_is_preserved(self):
        value = self.report()
        e.validate_report(value)
        self.assertIsInstance(value["tests"], list)
        self.assertIsInstance(value["limitations"], list)

    def test_explicit_no_tests_is_a_report_not_a_gate_pass(self):
        value = self.report(); value.update(tests=[], limitations=[])
        e.validate_report(value)
        self.assertNotIn("passed", value)

    def test_missing_observation_is_rejected(self):
        value = self.report(); del value["tests"][0]["outcome"]
        with self.assertRaises(e.RunError): e.validate_report(value)

    def test_bad_field_types_are_rejected(self):
        for field, bad in [("tests", None), ("limitations", {}), ("summary", []), ("source_event", 12)]:
            value = self.report(); value[field] = bad
            with self.subTest(field=field), self.assertRaises(e.RunError): e.validate_report(value)


if __name__ == "__main__":
    unittest.main()
