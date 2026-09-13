"""Observable regressions for the selectively integrated reliability update."""
import contextlib
import io
import json
from pathlib import Path
import signal
import sys
import tempfile
import unittest

from tests.test_run_engine import RunFixture, plan, gate, e
import analyzer_delta as delta


class AnalyzerTests(unittest.TestCase):
    def test_new_eslint_warning_even_when_other_warning_resolved(self):
        result = delta.compare("src/a.js\n  1:2 warning old rule\n", "src/a.js\n  2:3 warning new rule\n")
        self.assertEqual(result["new"], ["src/a.js: 2:3 warning new rule"])
        self.assertEqual(result["resolved"], ["src/a.js: 1:2 warning old rule"])

    def test_same_warning_in_different_file_is_new(self):
        for name in ("file.js", "file.json", "file.yaml", "file.md", "no-extension", "file name.custom"):
            with self.subTest(name=name):
                result = delta.compare(f"a/{name}\n  1:2 warning text rule\n", f"b/{name}\n  1:2 warning text rule\n")
                self.assertEqual(result["new"], [f"b/{name}: 1:2 warning text rule"])

    def test_occurrences_are_not_collapsed(self):
        self.assertEqual(delta.compare("warning duplicate\n", "warning duplicate\nwarning duplicate\n")["new"], ["warning duplicate"])

    def test_severity_first_and_colors_ignore_non_diagnostics(self):
        self.assertEqual(delta.compare("", "banner\n\x1b[33m info - finding - file.dart:2\x1b[0m\nDone\n")["new"], ["info - finding - file.dart:2"])

    def test_unchanged_and_resolved_issues_are_not_new(self):
        self.assertEqual(delta.compare("info kept\nwarning removed\n", "info kept\n")["new"], [])

    def test_explicit_format(self):
        self.assertEqual(delta.compare("", "DIAGNOSTIC custom\n", r"^DIAGNOSTIC\b")["new"], ["DIAGNOSTIC custom"])

    def test_cli_exit_codes_hashes_and_input_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            before, after = Path(folder) / "before", Path(folder) / "after"
            before.write_text("warning existing\n")
            after.write_text("warning existing\n")
            argv = ["--baseline", str(before), "--current", str(after)]
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(delta.main(argv), 0)
            record = json.loads(output.getvalue())
            self.assertEqual(record["baseline_sha256"], record["current_sha256"])
            after.write_text("warning new\n")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(delta.main(argv), 1)
                self.assertEqual(delta.main(argv + ["--issue-regex", "["]), 2)
                self.assertEqual(delta.main(["--baseline", str(before), "--current", str(after / "missing")]), 2)


class GateReliabilityTests(RunFixture):
    def test_signal_is_failed_receipt_with_observed_outcome(self):
        value = plan()
        value["tasks"][0]["gates"] = [gate([sys.executable, "-B", "-c", "import os,signal; os.kill(os.getpid(),signal.SIGTERM)"])]
        self.begin(value)
        self.implement()
        receipt = e.execute_gate(self.root, task_id="value")
        self.assertFalse(receipt["passed"])
        self.assertEqual(receipt["exit_code"], -signal.SIGTERM)
        self.assertEqual(receipt["outcome"], f"killed by signal {signal.SIGTERM}")
        self.assertEqual(receipt["attempts"][0]["outcome"], receipt["outcome"])

    def test_repeated_gate_preserves_first_log_and_receipt(self):
        self.begin()
        self.implement()
        first = e.execute_gate(self.root, task_id="value")
        original = Path(first["log"]).read_bytes()
        second = e.execute_gate(self.root, task_id="value")
        self.assertTrue(first["passed"] and second["passed"])
        self.assertNotEqual(first["log"], second["log"])
        self.assertEqual(Path(first["log"]).read_bytes(), original)
        self.assertEqual(e.load(self.root)["gates"][-2], first)


if __name__ == "__main__":
    unittest.main()
