"""Per-task routing: plan tier fields, the loop arguments they produce, and the execution path."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.test_run_engine import RunFixture, SCRIPTS, e, plan

import sys
sys.path.insert(0, str(SCRIPTS))
import workflow_steps as w  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT.parents[1] / "upstream/orchestrate/skill/routing.json"


def tiered(**fields):
    value = plan()
    value["tasks"][0].update(fields)
    return value


def two_tasks(**second):
    value = plan()
    extra = json.loads(json.dumps(value["tasks"][0]))
    extra.update(id="notes", title="Write notes", paths=["notes.txt"], **second)
    value["tasks"].append(extra)
    return value


class RoutingTableTests(unittest.TestCase):
    def test_the_table_equals_the_pinned_orchestrate_routing(self):
        if not UPSTREAM.is_file():
            self.skipTest("upstream submodule is not initialized (package test)")
        upstream = json.loads(UPSTREAM.read_text())
        ours = w.routing()
        for key in ("tiers", "reviewer_default_tier", "adversary", "utility"):
            self.assertEqual(upstream["claude"][key], ours[key], key)
        self.assertEqual((upstream["risky_tags"], upstream["risk_tags"]), (ours["risky_tags"], ours["risk_tags"]))

    def test_a_missing_or_malformed_table_is_a_run_error_only_for_routed_plans(self):
        good = json.loads(e.ROUTING_FILE.read_text())
        bad_tables = {"missing": None, "not json": "{", "no tiers": json.dumps({**good, "tiers": {}}),
                      "bool reviewer tier": json.dumps({**good, "reviewer_default_tier": True}),
                      "risky outside risk": json.dumps({**good, "risky_tags": ["speed"]})}
        for name, content in bad_tables.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "routing.json"
                if content is not None:
                    path.write_text(content)
                with mock.patch.object(e, "ROUTING_FILE", path):
                    with self.assertRaises(e.RunError) as caught:
                        e.validate_plan(tiered(tier=2))
                    self.assertEqual("invalid_routing", caught.exception.code)
                    e.validate_plan(plan())
                    with self.assertRaises(w.StepError):
                        w.routing()


class PlanFieldTests(unittest.TestCase):
    def refused(self, code, **fields):
        with self.assertRaises(e.RunError) as caught:
            e.validate_plan(tiered(**fields))
        self.assertEqual(code, caught.exception.code, caught.exception)

    def test_valid_routing_fields_pass_and_are_kept(self):
        [task] = e.validate_plan(tiered(tier=2, reviewer_tier=3, risk=["async", "ui"]))["tasks"]
        self.assertEqual((2, 3, ["async", "ui"]), (task["tier"], task["reviewer_tier"], task["risk"]))

    def test_a_plan_without_routing_fields_is_unchanged(self):
        [task] = e.validate_plan(plan())["tasks"]
        self.assertNotIn("tier", task)
        self.assertNotIn("reviewer_tier", task)

    def test_a_null_reviewer_tier_means_the_default(self):
        [task] = e.validate_plan(tiered(tier=2, reviewer_tier=None))["tasks"]
        self.assertEqual(2, task["tier"])
        e.validate_plan(tiered(reviewer_tier=None))

    def test_tier_must_be_one_to_three(self):
        for tier in (0, 4, "2", True, None, 2.0):
            with self.subTest(tier=tier):
                self.refused("invalid_tier", tier=tier)

    def test_reviewer_tier_needs_a_tier_and_may_not_be_below_it(self):
        self.refused("invalid_tier", reviewer_tier=3)
        self.refused("invalid_tier", tier=3, reviewer_tier=2)
        self.refused("invalid_tier", tier=2, reviewer_tier="3")

    def test_tier_one_is_reviewed_one_tier_up(self):
        self.refused("invalid_tier", tier=1, reviewer_tier=1)
        e.validate_plan(tiered(tier=1, reviewer_tier=2))

    def test_tier_one_never_takes_risky_work(self):
        for tag in sorted(e.routing_table()["risky_tags"]):
            with self.subTest(tag=tag):
                self.refused("invalid_tier", tier=1, risk=[tag])
        e.validate_plan(tiered(tier=1, risk=["tests"]))

    def test_risk_tags_are_known_distinct_names(self):
        self.refused("invalid_risk_tag", risk=["speed"])
        self.refused("invalid_risk_tag", risk=["ui", "ui"])
        self.refused("invalid_risk_tag", risk="async")
        self.refused("invalid_risk_tag", risk=[1])


class ArgsRoutingTests(RunFixture):
    def args(self, *extra, integration=False):
        argv = ["--run", str(self.root), "args", *extra] + (["--integration"] if integration else [])
        return w.build_args(w.parser().parse_args(argv))

    def lenses(self, task):
        return [(lens["key"], lens["model"]) for lens in task["lenses"]]

    def test_a_tier_routes_the_implementer_and_the_default_reviewer(self):
        self.begin(tiered(tier=2))
        [task] = self.args()["tasks"]
        self.assertEqual((2, "sonnet"), (task["chain"][0]["tier"], task["chain"][0]["model"]))
        self.assertEqual([("conformance", "opus")], self.lenses(task))

    def test_tier_one_with_a_reviewer_one_tier_up(self):
        self.begin(tiered(tier=1, reviewer_tier=2))
        [task] = self.args()["tasks"]
        self.assertEqual("haiku", task["chain"][0]["model"])
        self.assertEqual([("conformance", "sonnet")], self.lenses(task))

    def test_a_risky_tag_adds_an_adversary_on_another_model(self):
        self.begin(tiered(tier=2, risk=["async"]))
        [task] = self.args()["tasks"]
        self.assertEqual([("conformance", "opus"), ("adversary", "sonnet")], self.lenses(task))
        self.assertEqual(["async"], task["risk"])

    def test_an_adversary_that_would_match_the_reviewer_moves_to_the_top_tier(self):
        self.begin(tiered(tier=2, reviewer_tier=2, risk=["data"]))
        [task] = self.args()["tasks"]
        self.assertEqual([("conformance", "sonnet"), ("adversary", "opus")], self.lenses(task))

    def test_a_security_tag_adds_the_adversary_and_security_lenses(self):
        self.begin(tiered(tier=3, risk=["security"]))
        [task] = self.args()["tasks"]
        self.assertEqual([("conformance", "opus"), ("adversary", "sonnet"), ("security", "opus")], self.lenses(task))

    def test_security_adds_the_security_lens_once(self):
        self.begin(tiered(tier=3, risk=["security"]))
        [task] = self.args("--lens", "conformance", "--lens", "security")["tasks"]
        self.assertEqual([("conformance", "opus"), ("security", "opus"), ("adversary", "sonnet")], self.lenses(task))

    def test_a_selected_model_overrides_every_tier(self):
        self.begin(tiered(tier=1, reviewer_tier=2, risk=["tests"]))
        [task] = self.args("--model", "opus", "--effort", "high")["tasks"]
        self.assertEqual(("opus", "high", 0), (task["chain"][0]["model"], task["chain"][0]["effort"], task["chain"][0]["tier"]))
        self.assertEqual([("conformance", "opus")], self.lenses(task))

    def test_a_selected_review_model_overrides_the_reviewer_tier_only(self):
        self.begin(tiered(tier=2))
        [task] = self.args("--review-model", "sonnet")["tasks"]
        self.assertEqual(("sonnet", [("conformance", "sonnet")]), (task["chain"][0]["model"], self.lenses(task)))

    def test_an_untiered_task_keeps_the_configured_model(self):
        self.begin()
        [task] = self.args()["tasks"]
        self.assertEqual((None, 0, [("conformance", None)]), (task["chain"][0]["model"], task["chain"][0]["tier"], self.lenses(task)))

    def test_relay_steps_use_the_table_utility_unless_overridden(self):
        self.begin(tiered(tier=2))
        self.assertEqual({"tier": 0, "model": "haiku", "effort": "low"}, self.args()["utility"])
        self.assertEqual("sonnet", self.args("--utility-model", "sonnet")["utility"]["model"])

    def test_a_selected_effort_reaches_the_implementer_and_every_reviewer(self):
        self.begin(tiered(tier=2, risk=["async"]))
        [task] = self.args("--effort", "medium")["tasks"]
        self.assertEqual(["medium", "medium", "medium"], [task["chain"][0]["effort"]] + [lens["effort"] for lens in task["lenses"]])
        [task] = self.args("--review-effort", "high")["tasks"]
        self.assertEqual([None, "high", "high"], [task["chain"][0]["effort"]] + [lens["effort"] for lens in task["lenses"]])

    def corrupt(self, **fields):
        run = json.loads((self.root / "run.json").read_text())
        run["plan"]["tasks"][0].update(fields)
        (self.root / "run.json").write_text(json.dumps(run))

    def test_stored_fields_of_the_wrong_type_are_refused_as_json(self):
        for fields in ({"risk": None}, {"risk": 5}, {"risk": [["a"]]}, {"tier": "2"}, {"tier": 7}, {"reviewer_tier": "high"}):
            with self.subTest(fields=fields):
                self.setUp()
                self.begin(tiered(tier=2))
                self.corrupt(**fields)
                for step in ("args", "path"):
                    out = io.StringIO()
                    with contextlib.redirect_stdout(out):
                        code = w.main(["--run", str(self.root), step])
                    reply = json.loads(out.getvalue())
                    self.assertEqual(2, code)
                    self.assertIn("revise the plan", reply["blocked"])

    def test_integration_reviews_a_tiered_plan_at_the_top_tier(self):
        self.begin(two_tasks(tier=1, reviewer_tier=2))
        self.assertEqual([("conformance", "opus")], self.integration_lenses())

    def test_integration_adds_an_adversary_when_any_task_was_risky(self):
        self.begin(two_tasks(tier=2, risk=["async"]))
        self.assertEqual([("conformance", "opus"), ("adversary", "sonnet")], self.integration_lenses())

    def test_a_selected_review_model_also_drives_the_integration_review(self):
        self.begin(two_tasks(tier=2, risk=["async"]))
        run = e.load(self.root)
        run["integration"] = {"path": str(self.root), "base_sha": "0" * 40}
        stored = self.root / "run.json"
        original = stored.read_text()
        stored.write_text(json.dumps(run))
        try:
            flow = self.args("--review-model", "sonnet", integration=True)
        finally:
            stored.write_text(original)
        self.assertEqual([("conformance", "sonnet"), ("adversary", "sonnet")],
                         [(lens["key"], lens["model"]) for lens in flow["integration"]["lenses"]])

    def test_integration_of_an_untiered_plan_keeps_the_configured_model(self):
        self.begin()
        self.assertEqual([("conformance", None)], self.integration_lenses())

    def integration_lenses(self):
        run = e.load(self.root)
        run["integration"] = {"path": str(self.root), "base_sha": "0" * 40}
        stored = self.root / "run.json"
        original = stored.read_text()
        stored.write_text(json.dumps(run))
        try:
            flow = self.args(integration=True)
        finally:
            stored.write_text(original)
        return [(lens["key"], lens["model"]) for lens in flow["integration"]["lenses"]]


class PathTests(RunFixture):
    def path(self, value):
        e.set_plan(self.root, value)
        return w.choose_path(w.parser().parse_args(["--run", str(self.root), "path"]))

    def test_a_small_independent_task_without_risk_takes_the_direct_path(self):
        out = self.path(tiered(tier=2))
        self.assertEqual(("direct", []), (out["path"], out["reasons"]))
        self.assertEqual({"model": "sonnet", "effort": None}, out["tasks"][0]["implementer"])
        self.assertEqual([{"key": "conformance", "model": "opus", "effort": None}], out["tasks"][0]["reviewers"])

    def test_a_dependency_on_a_verified_task_does_not_link_the_open_ones(self):
        e.set_plan(self.root, two_tasks(depends_on=["value"]))
        run = json.loads((self.root / "run.json").read_text())
        run["tasks"] = {"value": {"status": "VERIFIED"}}
        (self.root / "run.json").write_text(json.dumps(run))
        out = w.choose_path(w.parser().parse_args(["--run", str(self.root), "path"]))
        self.assertEqual(("direct", ["notes"]), (out["path"], [t["id"] for t in out["tasks"]]))

    def test_a_run_without_a_plan_is_refused(self):
        with self.assertRaises(w.StepError):
            w.choose_path(w.parser().parse_args(["--run", str(self.root), "path"]))

    def test_risk_dependencies_size_or_level_need_the_loop(self):
        cases = {
            "risky tags: async": tiered(tier=2, risk=["async"]),
            "tasks depend on each other: notes": two_tasks(depends_on=["value"]),
            "classification is medium, not small": {**tiered(tier=2), "scores": {**{key: 0 for key in e.AXES}, "product": 2, "ux": 2, "technical": 2}},
        }
        for reason, value in cases.items():
            with self.subTest(reason=reason):
                self.setUp()
                out = self.path(value)
                self.assertEqual("loop", out["path"])
                self.assertIn(reason, out["reasons"])

    def test_no_open_task_needs_no_path(self):
        e.set_plan(self.root, tiered(tier=2))
        run = json.loads((self.root / "run.json").read_text())
        run["tasks"] = {"value": {"status": "VERIFIED"}}
        (self.root / "run.json").write_text(json.dumps(run))
        out = w.choose_path(w.parser().parse_args(["--run", str(self.root), "path"]))
        self.assertEqual(("none", []), (out["path"], out["tasks"]))

    def test_more_than_two_open_tasks_need_the_loop(self):
        value = two_tasks()
        third = json.loads(json.dumps(value["tasks"][0]))
        third.update(id="extra", title="Extra", paths=["extra.txt"])
        value["tasks"].append(third)
        out = self.path(value)
        self.assertEqual("loop", out["path"])
        self.assertIn("3 open tasks, more than 2", out["reasons"])


if __name__ == "__main__":
    unittest.main()
