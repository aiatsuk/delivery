"""Shipped workflow scripts run under node with scripted agents; the engine-side import is tested elsewhere."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "skills/delivery/workflows"
HARNESS = Path(__file__).resolve().with_name("workflow_harness.js")
NODE = shutil.which("node")


def implement_args(*tasks):
    return {"run_root": "/runs/r1", "tasks": [
        {"task": t, "dispatch_id": f"d-{t}", "worktree": f"/wt/{t}", "branch": f"delivery/r1/{t}", "brief": f"/runs/r1/tasks/{t}.json",
         "paths": [f"src/{t}.py"], "acceptance": f"{t} works", "gates": [["python3", "-m", "unittest", f"tests.test_{t}"]]}
        for t in tasks]}


def report(dispatch):
    return {"dispatch_id": dispatch, "summary": "changed", "tests": [{"command": ["python3", "-m", "unittest"], "exit_code": 0, "outcome": "3 passed"}], "limitations": []}


def review_args(*lenses, task="t1"):
    return {"run_root": "/runs/r1", "targets": [
        {"task": task, "worktree": "/wt/t1", "base_sha": "abc", "acceptance": "t1 works", "requirements": ["R1: value is after (exact file text)"],
         "gates": [["python3", "-m", "unittest"]], "brief": "/runs/r1/tasks/t1.json",
         "lenses": [{"lens": lens, "token": f"review-{lens}"} for lens in lenses]}]}


def verdict(token, value="PASS"):
    return {"review_token": token, "verdict": value, "evidence": "acceptance: observed\ngate: exit 0", "defects": []}


@unittest.skipUnless(NODE, "node is required to run workflow scripts")
class WorkflowScriptTests(unittest.TestCase):
    def run_flow(self, name, flow_args, responses):
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "scenario.json"
            scenario.write_text(json.dumps({"args": flow_args, "responses": responses}))
            proc = subprocess.run([NODE, str(HARNESS), str(WORKFLOWS / f"{name}.js"), str(scenario)], capture_output=True, text=True, timeout=60)
        out = json.loads(proc.stdout)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out["unknownPhases"], [])
        self.assertEqual(out["meta"]["name"], name)
        return out

    def test_implement_dispatches_every_task_with_its_dispatch_id_and_schema(self):
        out = self.run_flow("delivery-implement", implement_args("t1", "t2"),
                            {"implement:t1": [report("d-t1")], "implement:t2": [report("d-t2")]})
        calls = {c["label"]: c for c in out["calls"]}
        self.assertEqual(set(calls), {"implement:t1", "implement:t2"})
        self.assertIn("dispatch_id must be exactly d-t1", calls["implement:t1"]["prompt"])
        self.assertIn("You are not alone in this repository", calls["implement:t1"]["prompt"])
        self.assertEqual(calls["implement:t1"]["schema"]["required"], ["dispatch_id", "summary", "tests", "limitations"])
        self.assertNotIn("model", {k for c in out["calls"] for k, v in c.items() if v is not None and k == "model"})
        self.assertEqual([t["returned"] for t in out["result"]["tasks"]], [True, True])

    def test_implement_reports_missing_or_mismatched_results(self):
        out = self.run_flow("delivery-implement", implement_args("t1", "t2"), {"implement:t1": [None], "implement:t2": [report("d-other")]})
        tasks = {t["task"]: t for t in out["result"]["tasks"]}
        self.assertFalse(tasks["t1"]["returned"])
        self.assertFalse(tasks["t2"]["returned"])
        self.assertIn("d-other", tasks["t2"]["reason"])

    def test_implement_passes_an_explicit_model_only_when_given(self):
        value = implement_args("t1")
        value["tasks"][0]["model"] = "opus"
        out = self.run_flow("delivery-implement", value, {"implement:t1": [report("d-t1")]})
        self.assertEqual(out["calls"][0]["model"], "opus")

    def test_review_runs_one_agent_per_lens_with_its_token(self):
        out = self.run_flow("delivery-review", review_args("conformance", "adversary"),
                            {"review:t1:conformance": [verdict("review-conformance")], "review:t1:adversary": [verdict("review-adversary", "FAIL")]})
        calls = {c["label"]: c for c in out["calls"]}
        self.assertIn("review_token must be exactly review-adversary", calls["review:t1:adversary"]["prompt"])
        self.assertIn("duplicate delivery", calls["review:t1:adversary"]["prompt"])
        self.assertNotIn("duplicate delivery", calls["review:t1:conformance"]["prompt"])
        self.assertIn("Do not edit, stage or commit", calls["review:t1:conformance"]["prompt"])
        self.assertEqual({(r["lens"], r["verdict"]) for r in out["result"]["reviews"]}, {("conformance", "PASS"), ("adversary", "FAIL")})

    def test_review_flags_a_verdict_with_another_token(self):
        out = self.run_flow("delivery-review", review_args("conformance"), {"review:t1:conformance": [verdict("review-stale")]})
        self.assertFalse(out["result"]["reviews"][0]["returned"])

    def test_integrated_target_is_labelled(self):
        out = self.run_flow("delivery-review", review_args("conformance", task=None), {"review:integrated:conformance": [verdict("review-conformance")]})
        self.assertIn("the integrated diff", out["calls"][0]["prompt"])


if __name__ == "__main__":
    unittest.main()
