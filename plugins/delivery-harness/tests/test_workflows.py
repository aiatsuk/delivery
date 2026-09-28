"""Shipped workflow scripts run under node with scripted agents, alone and end to end with the engine.

The scripted agents and the host journal written from their results are synthetic unit-test
artifacts under temporary directories; they do not prove that a host workflow ran.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.test_run_engine import RunFixture, SCRIPTS, e, git

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
    return {"dispatch_id": dispatch, "summary": "changed", "tests": [{"command": ["python3", "-m", "unittest"], "exit_code": 0, "outcome": "3 passed"}], "limitations": [], "tree": "abc123"}


EVIDENCE = ("acceptance t1 works: observed the new value path in src/t1.py and its unit test asserting the exact text\n"
            "gate python3 -m unittest: exit 0, 3 tests passed, including the new regression case for the value")


def review_args(*lenses, task="t1"):
    return {"run_root": "/runs/r1", "targets": [
        {"task": task, "worktree": "/wt/t1", "base_sha": "abc", "acceptance": "t1 works", "requirements": ["R1: value is after (exact file text)"],
         "gates": [["python3", "-m", "unittest"]], "brief": "/runs/r1/tasks/t1.json",
         "lenses": [{"lens": lens, "token": f"review-{lens}"} for lens in lenses]}]}


def verdict(token, value="PASS"):
    return {"review_token": token, "verdict": value, "evidence": EVIDENCE, "defects": []}


def run_workflow(name, flow_args, responses):
    with tempfile.TemporaryDirectory() as tmp:
        scenario = Path(tmp) / "scenario.json"
        scenario.write_text(json.dumps({"args": flow_args, "responses": responses}))
        proc = subprocess.run([NODE, str(HARNESS), str(WORKFLOWS / f"{name}.js"), str(scenario)], capture_output=True, text=True, timeout=60)
    return json.loads(proc.stdout)


@unittest.skipUnless(NODE, "node is required to run workflow scripts")
class WorkflowScriptTests(unittest.TestCase):
    def run_flow(self, name, flow_args, responses):
        out = run_workflow(name, flow_args, responses)
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
        self.assertEqual(calls["implement:t1"]["schema"]["required"], ["dispatch_id", "summary", "tests", "limitations", "tree"])
        self.assertEqual(calls["implement:t1"]["schema"]["properties"]["tests"]["items"]["properties"]["command"]["minItems"], 1)
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

    def test_contract_gate_objects_are_accepted_and_only_safe_ones_listed(self):
        value = implement_args("t1")
        value["tasks"][0]["gates"] = [{"command": ["python3", "-m", "unittest"], "risk": "safe", "oracle": "o", "cleanup": "c"},
                                      {"command": ["deploy-preview"], "risk": "external", "oracle": "o", "cleanup": "c"}]
        out = self.run_flow("delivery-implement", value, {"implement:t1": [report("d-t1")]})
        prompt = out["calls"][0]["prompt"]
        self.assertIn('"python3" "-m" "unittest"', prompt)
        self.assertNotIn("deploy-preview", prompt)
        self.assertIn("Do not run the 1 other planned gate(s)", prompt)
        review = review_args("conformance")
        review["targets"][0]["gates"] = value["tasks"][0]["gates"]
        out = self.run_flow("delivery-review", review, {"review:t1:conformance": [verdict("review-conformance")]})
        self.assertNotIn("deploy-preview", out["calls"][0]["prompt"])

    def test_review_schema_requires_the_evidence_minimum(self):
        out = self.run_flow("delivery-review", review_args("conformance"), {"review:t1:conformance": [verdict("review-conformance")]})
        self.assertEqual(out["calls"][0]["schema"]["properties"]["evidence"]["minLength"], 160)
        self.assertIn("at least two such lines and 160 characters", out["calls"][0]["prompt"])

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


@unittest.skipUnless(NODE, "node is required to run workflow scripts")
class WorkflowEndToEndTests(RunFixture):
    """The shipped scripts' agent results, written as a host journal, drive the real engine through its CLI."""

    def setUp(self):
        super().setUp()
        self.host = self.host_root

    def cli(self, *arguments):
        result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), *map(str, arguments)], cwd=self.home, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)["result"]

    def flow(self, name, flow_args, responses):
        out = run_workflow(name, flow_args, {label: [value] for label, value in responses.items()})
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(sorted(responses), sorted(call["label"] for call in out["calls"]))
        return out

    def journal(self, run_id, out, responses):
        """Write what the host records for these agent calls: a started and a result line per label, plus a transcript."""
        directory = self.host / "fixture-project" / "fixture-session" / "subagents" / "workflows" / run_id
        directory.mkdir(parents=True)
        lines = [{"type": "launched"}]
        for call in out["calls"]:
            agent = "a" + hashlib.sha256(call["label"].encode()).hexdigest()[:16]
            key = "v2:" + hashlib.sha256((run_id + call["label"]).encode()).hexdigest()[:16]
            lines += [{"type": "started", "key": key, "agentId": agent, "label": call["label"], "phase": call["phase"]},
                      {"type": "result", "key": key, "agentId": agent, "result": responses[call["label"]]}]
            (directory / f"agent-{agent}.jsonl").write_text(json.dumps({"type": "fixture transcript", "prompt": call["prompt"]}) + "\n")
        (directory / "journal.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

    def test_shipped_workflows_drive_a_registered_task_to_verified(self):
        self.begin()
        self.cli("task-prepare", "--run", self.root, "--task", "value")
        dispatch_id = self.cli("task-register", "--run", self.root, "--task", "value", "--via-workflow")["tasks"]["value"]["agent"]["dispatch_id"]
        contract_path = self.root / "tasks" / "value.json"
        contract = json.loads(contract_path.read_text())
        task = contract["task"]
        target = {"task": "value", "worktree": contract["worktree"], "brief": str(contract_path), "acceptance": task["acceptance"], "gates": task["gates"]}
        # The scripted implementation agent does its work in the real worktree and reports the staged tree.
        worktree = Path(contract["worktree"])
        (worktree / "value.txt").write_text("after\n")
        git(worktree, "add", "--", "value.txt")
        report = {"dispatch_id": dispatch_id, "summary": "Set value.txt to the planned text.", "limitations": ["Scripted fixture agent."],
                  "tests": [{"command": task["gates"][0]["command"], "exit_code": 0, "outcome": "The planned fixture assertion passed."}],
                  "tree": git(worktree, "write-tree")}
        implemented = self.flow("delivery-implement", {"run_root": str(self.root), "tasks": [{**target, "dispatch_id": dispatch_id, "branch": contract["branch"], "paths": task["paths"]}]},
                                {"implement:value": report})
        self.assertEqual([True], [item["returned"] for item in implemented["result"]["tasks"]])
        self.assertIn(f"dispatch_id must be exactly {dispatch_id}", implemented["calls"][0]["prompt"])
        self.journal("wf_e2e-implement", implemented, {"implement:value": report})
        imported = self.cli("task-import", "--run", self.root, "--task", "value")
        self.assertEqual(("REPORTED", report["tree"]), (imported["tasks"]["value"]["status"], imported["tasks"]["value"]["report"]["tree"]))
        self.assertTrue(self.cli("gate", "--run", self.root, "--task", "value")["passed"])
        tokens = {name: self.cli("review-token", "--run", self.root, "--task", "value", "--lens", name)["token"] for name in ("conformance", "adversary")}
        verdicts = {f"review:value:{name}": verdict(token) for name, token in tokens.items()}
        reviewed = self.flow("delivery-review", {"run_root": str(self.root), "targets": [{**target, "base_sha": imported["tasks"]["value"]["base_sha"],
                                                                                          "lenses": [{"lens": name, "token": token} for name, token in tokens.items()]}]}, verdicts)
        self.assertEqual([True, True], [item["returned"] for item in reviewed["result"]["reviews"]])
        self.journal("wf_e2e-review", reviewed, verdicts)
        verified = self.cli("review-import", "--run", self.root, "--task", "value", "--lens", "conformance", "--lens", "adversary")
        self.assertEqual("VERIFIED", verified["tasks"]["value"]["status"])
        receipt = verified["reviews"][-1]
        self.assertEqual(("PASS", ["conformance", "adversary"]), (receipt["verdict"], receipt["lenses"]))
        self.assertEqual(2, len(set(receipt["reviewers"])))
        self.assertNotIn(verified["tasks"]["value"]["agent"]["actor"], receipt["reviewers"])


if __name__ == "__main__":
    unittest.main()
