"""The shared execution loop driving the real delivery engine through workflow_steps.py.

The loop script is the verbatim orchestrate workflow shipped in skills/delivery/workflows. The
scripted agents and the host journal the harness writes are synthetic unit-test artifacts under
temporary directories; they do not prove that a host workflow ran.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_run_engine import RunFixture, SCRIPTS, e, gate, git, plan

ROOT = Path(__file__).resolve().parents[1]
LOOP = ROOT / "skills/delivery/workflows/orchestrate-execute.js"
STEPS = SCRIPTS / "workflow_steps.py"
HARNESS = Path(__file__).resolve().with_name("workflow_harness.js")
NODE = shutil.which("node")


def dependent_plan():
    value = plan()
    second = json.loads(json.dumps(value["tasks"][0]))
    second.update(id="notes", title="Write notes", paths=["notes.txt"], depends_on=["value"],
                  gates=[gate([sys.executable, "-B", "-c", "from pathlib import Path; assert Path('value.txt').read_text() == 'after\\n' and Path('notes.txt').exists()"])])
    value["tasks"].append(second)
    return value


@unittest.skipUnless(NODE, "node is required to run workflow scripts")
class LoopTests(RunFixture):
    def steps(self, *arguments):
        result = subprocess.run([sys.executable, "-B", str(STEPS), "--run", str(self.root), *arguments], capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def loop(self, flow_args, agents=None, run_id="wf_loop-0001"):
        journal = self.host_root / "fixture-project" / "fixture-session" / "subagents" / "workflows" / run_id
        with tempfile.TemporaryDirectory() as tmp:
            scenario = Path(tmp) / "scenario.json"
            scenario.write_text(json.dumps({"args": flow_args, "journal": str(journal), "agents": agents or {}}))
            proc = subprocess.run([NODE, str(HARNESS), str(LOOP), str(scenario)], capture_output=True, text=True, timeout=600)
        out = json.loads(proc.stdout)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual([], out["unknownPhases"])
        return out

    def labels(self, out):
        return [call["label"] for call in out["calls"]]

    def result(self, out, task="value"):
        return next(item for item in out["result"]["tasks"] if item["id"] == task)

    def test_the_shipped_loop_is_the_pinned_orchestrate_workflow(self):
        upstream = ROOT.parents[1] / "upstream/orchestrate/skill/workflows/orchestrate-execute.js"
        if not upstream.is_file():
            self.skipTest("upstream submodule is not initialized (package test)")
        self.assertEqual(upstream.read_bytes(), LOOP.read_bytes())

    def test_args_carry_the_authority_and_the_loop_version(self):
        self.begin()
        flow = self.steps("args", "--lens", "conformance", "--lens", "adversary")
        version = re.search(r"^const SCRIPT_VERSION = '([^']+)'$", LOOP.read_text(), re.M).group(1)
        self.assertEqual(version, flow["version"])
        self.assertEqual(("delivery", ["--run", str(self.root.resolve())]), (flow["authority"]["name"], flow["authority"]["helper"][2:]))
        [task] = flow["tasks"]
        self.assertEqual((["value.txt"], None, ["conformance", "adversary"]), (task["scope"], task["chain"][0]["model"], [l["key"] for l in task["lenses"]]))
        self.assertEqual(["dispatch_id", "summary", "tests", "limitations", "tree"], flow["schemas"]["report"]["required"])

    def test_a_task_runs_from_prepare_to_verified_with_journal_provenance(self):
        self.begin()
        out = self.loop(self.steps("args"), {"impl:value:L0": [{"write": {"value.txt": "after\n"}}]})
        self.assertEqual(["prepare:value", "dispatch:value:r0", "impl:value:L0", "collect:value:r0", "gate:value:L0r0",
                          "review-open:value:r0", "review:value:r0:conformance", "review-close:value:r0", "finish:value"], self.labels(out))
        result = self.result(out)
        self.assertEqual(("PASS", 0), (result["status"], result["rounds"]), result.get("reason"))
        run = e.load(self.root)
        owner = run["tasks"]["value"]
        self.assertEqual("VERIFIED", owner["status"])
        self.assertTrue(owner["agent"]["actor"].startswith("workflow-agent:wf_loop-0001/"))
        self.assertEqual(result["patch"], owner["patch"]["path"])
        review = run["reviews"][-1]
        self.assertEqual(("PASS", "workflow-journal"), (review["verdict"], review["source"]))
        self.assertNotIn(owner["agent"]["actor"], review["reviewers"])

    def test_a_red_gate_is_reworked_under_the_engine_budget(self):
        self.begin()
        out = self.loop(self.steps("args"), {"impl:value:L0": [{"write": {"value.txt": "wrong\n"}}],
                                             "rework:value:L0r1": [{"write": {"value.txt": "after\n"}}]})
        self.assertEqual("PASS", self.result(out)["status"], self.result(out).get("reason"))
        self.assertIn("return:value:r0", self.labels(out))
        owner = e.load(self.root)["tasks"]["value"]
        self.assertEqual((1, "VERIFIED"), (owner["rework_rounds"], owner["status"]))
        self.assertEqual("code-fix", owner["rework_history"][0]["decision"])

    def test_a_review_fail_goes_back_through_the_engine(self):
        self.begin()
        defect = {"file": "value.txt", "line": 1, "kind": "behavior", "severity": "major", "summary": "Missing edge case.", "scenario": "Synthetic."}
        out = self.loop(self.steps("args"), {"impl:value:L0": [{"write": {"value.txt": "after\n"}}],
                                             "review:value:r0:conformance": [{"verdict": "FAIL", "defects": [defect]}],
                                             "rework:value:L0r1": [{"write": {"value.txt": "after\n", "extra.txt": "x\n"}}]})
        # The rework staged a file outside the owned paths: the engine refuses the import and ends that dispatch.
        labels = self.labels(out)
        self.assertIn("rework:value:L0r2", labels)
        self.assertIn("Missing edge case", next(c["prompt"] for c in out["calls"] if c["label"] == "rework:value:L0r1"))
        owner = e.load(self.root)["tasks"]["value"]
        self.assertEqual(["code-fix", "abandon"], [entry["decision"] for entry in owner["rework_history"][:2]])
        self.assertEqual(self.result(out)["status"] == "PASS", owner["status"] == "VERIFIED")

    def test_the_engine_budget_blocks_a_non_converging_task(self):
        self.begin()
        wrong = {"write": {"value.txt": "wrong\n"}}
        out = self.loop(self.steps("args"), {"impl:value:L0": [wrong], "rework:value:L0r1": [{"write": {"value.txt": "still wrong\n"}}]})
        result = self.result(out)
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("non_converging", result["reason"])
        self.assertEqual("BLOCKED", e.load(self.root)["state"])

    def test_an_implementer_that_fails_is_abandoned_and_redispatched(self):
        self.begin()
        out = self.loop(self.steps("args"), {"impl:value:L0": [None], "rework:value:L0r1": [{"write": {"value.txt": "after\n"}}]})
        self.assertEqual("PASS", self.result(out)["status"], self.result(out).get("reason"))
        owner = e.load(self.root)["tasks"]["value"]
        self.assertEqual(("abandon", 1), (owner["rework_history"][0]["decision"], owner["support_rounds"]))
        self.assertEqual(1, len(owner["rework_history"][0]["journal"]["failed"]))

    def test_a_dependent_is_prepared_on_the_verified_patch(self):
        self.begin(dependent_plan())
        out = self.loop(self.steps("args"), {"impl:value:L0": [{"write": {"value.txt": "after\n"}}],
                                             "impl:notes:L0": [{"write": {"notes.txt": "notes\n"}}]})
        self.assertEqual(["PASS", "PASS"], [item["status"] for item in out["result"]["tasks"]])
        labels = self.labels(out)
        self.assertLess(labels.index("finish:value"), labels.index("prepare:notes"))
        worktree = Path(e.load(self.root)["tasks"]["notes"]["path"])
        self.assertEqual("after\n", (worktree / "value.txt").read_text())

    def test_a_side_effect_gate_without_authority_blocks_and_is_not_listed_for_agents(self):
        value = plan()
        value["tasks"][0]["gates"] = [gate(), gate(risk="external")]
        value["triggers"] = []
        self.begin(value)
        flow = self.steps("args")
        self.assertEqual(1, len(flow["tasks"][0]["gate"]))
        out = self.loop(flow, {"impl:value:L0": [{"write": {"value.txt": "after\n"}}]})
        result = self.result(out)
        self.assertEqual("BLOCKED", result["status"])
        self.assertIn("authorization_required", result["reason"])
        self.assertNotIn("return:value:r0", self.labels(out))

    def test_integrated_review_is_imported_for_the_integration(self):
        self.begin()
        self.loop(self.steps("args"), {"impl:value:L0": [{"write": {"value.txt": "after\n"}}]})
        e.integrate(self.root)
        subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), "gate", "--run", str(self.root), "--case", "value-check"], check=True, capture_output=True)
        out = self.loop(self.steps("args", "--integration"), run_id="wf_loop-0002")
        self.assertEqual(["review-open:integration", "review:integration:conformance", "review-close:integration"], self.labels(out))
        self.assertEqual("PASS", out["result"]["integration"]["status"], out["result"]["integration"].get("reason"))
        review = e.load(self.root)["reviews"][-1]
        self.assertEqual((None, "PASS"), (review["task"], review["verdict"]))


class StepSchemaTests(unittest.TestCase):
    """A relay returns structured output against its schema, so every field the loop reads must be listed."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import workflow_steps
        self.steps = workflow_steps
        self.source = LOOP.read_text(encoding="utf-8")

    def reads(self, names, text=None):
        text = self.source if text is None else text
        fields = set()
        for name in names:
            fields |= set(re.findall(rf"(?<![\w.]){name}\.(\w+)", text))
            if re.search(rf"blockedBy\({name}\)", text):
                fields.add("blocked")
        return fields

    def assert_listed(self, schema, fields):
        missing = sorted(fields - set(schema["properties"]))
        self.assertEqual([], missing, f"the loop reads fields its schema drops: {missing}")

    def test_every_field_the_loop_reads_from_a_recorded_step_is_in_the_step_schema(self):
        authority = self.source[self.source.index("function record("):]
        names = set(re.findall(r"const (\w+) = await record\(", authority))
        self.assertTrue({"prep", "reg", "got", "back", "open", "close"} <= names, names)
        fields = self.reads(names, authority)
        self.assertTrue({"worktree", "head", "branch", "spec_sha256", "dispatch", "accepted", "tokens", "verdict"} <= fields, fields)
        self.assert_listed(self.steps.STEP, fields)

    def test_every_field_the_loop_reads_from_a_gate_or_finish_is_in_its_schema(self):
        self.assert_listed(self.steps.GATE, self.reads(["gate"]))
        self.assert_listed(self.steps.FINISH, self.reads(["finish"]))
        defects = self.source[self.source.index("function gateDefects("):self.source.index("\n}\n", self.source.index("function gateDefects("))]
        self.assert_listed(self.steps.GATE["properties"]["results"]["items"], self.reads(["r"], defects))

    def test_the_loop_uses_only_schemas_the_args_ship(self):
        authority = self.source[self.source.index("function record("):]
        used = set(re.findall(r"A\.schemas\.(\w+)", authority))
        self.assertTrue(used <= {"step", "gate", "finish", "verdict", "report"}, used)


class StepClassificationTests(unittest.TestCase):
    """Which engine refusals block the task and which send it back to rework (engine calls mocked)."""

    def setUp(self):
        sys.path.insert(0, str(SCRIPTS))
        import workflow_steps
        self.steps = workflow_steps
        self.args = mock.Mock(run="/run", task="value", label="task-value-r0", reason="r", key=None)

    def test_a_gate_refusal_blocks_instead_of_counting_as_a_red_gate(self):
        plan_run = {"plan": {"tasks": [{"id": "value", "paths": ["value.txt"], "gates": [gate()]}]}, "tasks": {"value": {"path": "/wt"}}}
        with mock.patch.object(self.steps, "run_state", return_value=plan_run), \
                mock.patch.object(self.steps, "delivery", return_value=(False, {"code": "resource_recovery_required", "message": "recover job"})):
            out = self.steps.step_gate(self.args)
        self.assertIn("resource_recovery_required", out["blocked"])
        self.assertEqual({"exit_code", "tail", "tree", "head", "results", "unstaged", "untracked", "outside_scope", "blocked"}, set(out))

    def test_a_journal_problem_blocks_without_abandoning(self):
        calls = []
        def fake(*arguments):
            calls.append(arguments[0])
            return False, {"code": "journal_missing", "message": "no host root"}
        with mock.patch.object(self.steps, "delivery", side_effect=fake):
            out = self.steps.step_collect(self.args)
        self.assertEqual((["task-import"], False), (calls, out["accepted"]))
        self.assertIn("journal_missing", out["blocked"])

    def test_a_refused_result_is_abandoned_and_reworked(self):
        calls = []
        def fake(*arguments):
            calls.append(arguments[0])
            if arguments[0] == "task-import":
                return False, {"code": "scope_violation", "message": "outside"}
            return True, {"tasks": {"value": {"status": "REWORK"}}}
        with mock.patch.object(self.steps, "delivery", side_effect=fake), mock.patch.object(self.steps, "blocker", return_value=""):
            out = self.steps.step_collect(self.args)
        self.assertEqual((["task-import", "task-abandon"], False, None), (calls, out["accepted"], out.get("blocked")))

    def test_an_unexpected_error_prints_an_object_every_step_schema_accepts(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout") as stdout:
            code = self.steps.main(["--run", tmp, "gate", "--task", "value"])
        out = json.loads("".join(call.args[0] for call in stdout.write.call_args_list))
        self.assertEqual(2, code)
        for schema in (self.steps.GATE, self.steps.FINISH, self.steps.STEP):
            self.assertTrue(set(schema["required"]) <= set(out), schema["required"])


if __name__ == "__main__":
    unittest.main()
