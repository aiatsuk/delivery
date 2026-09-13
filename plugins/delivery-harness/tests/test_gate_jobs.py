"""Real local processes exercise exclusion and conservative crash recovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.test_run_engine import RunFixture, SCRIPTS, e, gate, plan
import gate_jobs as jobs
import publisher


WAIT = """from pathlib import Path
import sys, time
assert Path(sys.argv[3]).read_text() == sys.argv[4]
Path(sys.argv[1]).write_text('started')
deadline = time.monotonic() + 15
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
assert Path(sys.argv[2]).exists(), 'Fixture release timed out'
"""


class GateJobTests(RunFixture):
    def controlled_gate(self, name, file="value.txt", value="after\n"):
        started, release = self.home / (name + ".started"), self.home / (name + ".release")
        self.addCleanup(lambda: release.write_text("release"))
        return gate([sys.executable, "-B", "-c", WAIT, str(started), str(release), file, value])

    def wait_for(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.02)
        self.fail("The actual fixture process did not reach its checkpoint.")

    def running_job(self, task=None, key=None):
        return next((job for job in e.load(self.root).get("gate_jobs", {}).values()
                     if job["status"] == "RUNNING" and (task is None or job["task"] == task)
                     and (key is None or job["key"] == key) and job["child_pid"]), None)

    @contextlib.contextmanager
    def running(self, task="value", index=0, name="first", case=None):
        pool = ThreadPoolExecutor(max_workers=1)
        arguments = {"task_id": task, "gate_index": index} if task else {"case_id": case}
        future = pool.submit(e.execute_gate, self.root, timeout=15, **arguments)
        try:
            key = f"{task}:{index}" if task else case
            job = self.wait_for(lambda: self.running_job(task, key) if (self.home / (name + ".started")).exists() else None)
            yield future, job
        finally:
            (self.home / (name + ".release")).write_text("release")
            pool.shutdown(wait=True)
        future.result()

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(e.RunError) as caught:
            function(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_same_resource_concurrent_gates_are_excluded(self):
        value = plan()
        value["tasks"][0].update(resources=["device:one"], gates=[self.controlled_gate("first"), gate()])
        self.begin(value)
        self.implement()
        with self.running() as (first, job):
            self.assert_code("resource_in_use", e.execute_gate, self.root, task_id="value", gate_index=1)
            self.assertEqual(job["resources"], ["device:one"])
        self.assertTrue(first.result()["passed"])
        self.assertEqual(e.load(self.root)["gate_jobs"][job["id"]]["status"], "SUCCEEDED")

    def test_duplicate_gate_excluded_without_named_resources(self):
        value = plan()
        value["tasks"][0]["gates"] = [self.controlled_gate("first")]
        self.begin(value)
        self.implement()
        with self.running() as (_, job):
            self.assertEqual(job["resources"], [])
            self.assert_code("resource_in_use", e.execute_gate, self.root, task_id="value")

    def test_disjoint_gates_run_together_and_keep_receipts_and_lock_inodes(self):
        value = plan()
        value["tasks"][0].update(resources=["device:one"], gates=[self.controlled_gate("first")])
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", paths=["other.txt"], resources=["device:two"], gates=[self.controlled_gate("second", "other.txt", "second\n")])
        value["tasks"].append(other)
        self.begin(value)
        self.implement()
        self.implement("other", {"other.txt": "second\n"})
        with self.running() as (first, one):
            directory = jobs._resource_dir(e.load(self.root))
            inodes = {key: (directory / (key + ".lock")).stat().st_ino for key in one["lock_keys"]}
            with self.running("other", name="second") as (second, two):
                self.assertNotEqual(one["child_pid"], two["child_pid"])
                self.assertEqual(len([j for j in e.load(self.root)["gate_jobs"].values() if j["status"] == "RUNNING"]), 2)
        run = e.load(self.root)
        for future, job in ((first, one), (second, two)):
            receipt = future.result()
            self.assertTrue(receipt["passed"])
            self.assertEqual(receipt["job_id"], job["id"])
            self.assertEqual(run["gate_jobs"][job["id"]]["receipt"], receipt)
            self.assertIn(receipt, run["gates"])
        self.assertEqual(inodes, {key: (directory / (key + ".lock")).stat().st_ino for key in inodes})

    def test_active_worker_prevents_gate_on_shared_resource(self):
        value = plan()
        value["tasks"][0]["resources"] = ["device:one"]
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", depends_on=["value"], paths=["other.txt"])
        value["tasks"].append(other)
        self.begin(value)
        self.implement()
        self.verify()
        e.prepare_task(self.root, "other")
        e.register_agent(self.root, "other", "fixture-other", "unit-test", "fixture-other-handle")
        self.assert_code("resource_in_use", e.execute_gate, self.root, task_id="value")
        before = e.load(self.root)
        self.assert_code("active_task", e.revise, self.root, "Fixture revision cannot retire a live implementation dispatch.")
        self.assertEqual(e.load(self.root), before)

    def test_active_gate_prevents_same_task_rework_and_resource_redispatch(self):
        value = plan()
        value["tasks"][0].update(resources=["device:one"], gates=[gate(), self.controlled_gate("first")])
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", depends_on=["value"], paths=["other.txt"], gates=[gate()])
        value["tasks"].append(other)
        self.begin(value)
        self.implement()
        (self.home / "first.release").write_text("release")
        self.assertTrue(e.execute_gate(self.root, task_id="value", gate_index=1)["passed"])
        (self.home / "first.release").unlink()
        (self.home / "first.started").unlink()
        self.verify()
        self.implement("other", {"other.txt": "second\n"})
        e.rework(self.root, "other", "Fixture secondary task correction before redispatch.")
        with self.running(index=1):
            self.assert_code("resource_in_use", e.rework, self.root, "value", "Fixture requested return to editing.")
            self.assert_code("resource_in_use", e.register_agent, self.root, "other", "fixture-other", "unit-test", "fixture-other-handle")

    def test_dependent_running_gate_prevents_dependency_rework(self):
        value = plan()
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", depends_on=["value"], paths=["other.txt"], gates=[self.controlled_gate("second", "other.txt", "second\n")])
        value["tasks"].append(other)
        self.begin(value)
        self.implement()
        self.verify()
        self.implement("other", {"other.txt": "second\n"})
        with self.running("other", name="second"):
            self.assert_code("active_descendant", e.rework, self.root, "value", "Fixture dependency correction.")

    def test_running_task_gate_blocks_review_integration_and_semantic_revision(self):
        value = plan()
        value["tasks"][0]["gates"] = [self.controlled_gate("first")]
        self.begin(value)
        self.implement()
        (self.home / "first.release").write_text("release")
        self.verify()
        (self.home / "first.release").unlink()
        (self.home / "first.started").unlink()
        with self.running():
            before = e.load(self.root)
            self.assert_code("gate_job_active", e.review, self.root, "fixture-new-reviewer", "PASS", "Prior receipt cannot stand in for the active job.", task_id="value")
            self.assert_code("gate_job_active", e.integrate, self.root)
            self.assert_code("gate_job_active", e.revise, self.root, "Fixture semantic revision while its gate is active.")
            self.assertEqual(e.load(self.root), before)

    def test_running_integrated_gate_blocks_readiness_fixes_and_all_publisher_writes(self):
        value = plan()
        value["verification"][0].update(self.controlled_gate("integrated"))
        self.begin(value)
        self.implement()
        self.verify()
        e.integrate(self.root)
        (self.home / "integrated.release").write_text("release")
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Observed completed fixture integration gate.")
        e.ready(self.root)
        (self.home / "integrated.release").unlink()
        (self.home / "integrated.started").unlink()
        provider = mock.Mock()
        operations = [
            (e.ready, [], {}),
            (e.capture_verification, ["Fixture capture while testing."], {}),
            (e.review, ["fixture-new-reviewer", "PASS", "Do not adopt the older receipt."], {}),
            (e.register_fix, ["fixture-fix-writer", "unit-test", "fixture-fix-handle", "Fixture same-scope correction."], {}),
            (e.revise, ["Fixture semantic correction."], {}),
            (publisher.commit, ["Preserve the fixture value"], {}),
            (publisher.publish, ["Update the fixture value", "Fixture summary and validation."], {"provider": provider}),
            (publisher.refresh, [], {"provider": provider}),
            (publisher.merge, [], {"provider": provider}),
            (publisher.cleanup, [], {"provider": provider}),
        ]
        with self.running(task=None, name="integrated", case="value-check"):
            before = e.load(self.root)
            for function, arguments, keywords in operations:
                with self.subTest(operation=function.__name__):
                    self.assert_code("gate_job_active", function, self.root, *arguments, **keywords)
                    self.assertEqual(e.load(self.root), before)
            self.assertEqual(provider.mock_calls, [])
            self.assertTrue(Path(before["integration"]["path"]).is_dir())
        e.register_fix(self.root, "fixture-fix-writer", "unit-test", "fixture-fix-handle", "Fixture same-scope correction after the gate ended.")
        before = e.load(self.root)
        self.assert_code("active_task", e.revise, self.root, "Fixture revision cannot retire the active integration correction.")
        self.assertEqual(e.load(self.root), before)

    def test_failed_gate_records_failed_job_and_actual_receipt(self):
        value = plan()
        value["tasks"][0]["gates"] = [gate([sys.executable, "-B", "-c", "raise SystemExit(7)"])]
        self.begin(value)
        self.implement()
        receipt = e.execute_gate(self.root, task_id="value")
        self.assertFalse(receipt["passed"])
        job = e.load(self.root)["gate_jobs"][receipt["job_id"]]
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["receipt"]["exit_code"], 7)
        self.assertEqual(len(job["children"]), 1)

    def test_timeout_reaps_recorded_child_and_releases_resources(self):
        value = plan()
        value["tasks"][0]["gates"] = [self.controlled_gate("timeout")]
        self.begin(value)
        self.implement()
        receipt = e.execute_gate(self.root, task_id="value", timeout=1)
        job = e.load(self.root)["gate_jobs"][receipt["job_id"]]
        self.assertTrue(receipt["timed_out"])
        self.assertEqual(job["status"], "FAILED")
        self.assertFalse(jobs._observe(job["child_pid"])["alive"])

    def test_process_identity_hook_failure_still_records_and_reaps_actual_child(self):
        value = plan()
        value["tasks"][0]["gates"] = [self.controlled_gate("hook")]
        self.begin(value)
        self.implement()
        original, failed = jobs._observe, []
        def fail_once(pid):
            if pid != os.getpid() and not failed:
                failed.append(pid)
                raise e.RunError("process_unknown", "Injected fixture observation failure.")
            return original(pid)
        with mock.patch.object(jobs, "_observe", side_effect=fail_once):
            self.assert_code("process_unknown", e.execute_gate, self.root, task_id="value")
        job = next(iter(e.load(self.root)["gate_jobs"].values()))
        self.assertEqual(job["child_pid"], failed[0])
        self.assertFalse(original(job["child_pid"])["alive"])
        self.assertEqual(job["status"], "FAILED")
        self.assertFalse(job["outcome"]["returned"])

    def test_stale_revision_refused_before_job_start(self):
        self.begin()
        self.implement()
        before = e.load(self.root)
        self.assert_code("stale_revision", e.execute_gate, self.root, task_id="value", expected_revision=before["revision"] - 1)
        self.assertEqual(before, e.load(self.root))

    def test_explicit_revision_accounts_for_own_job_checkpoint(self):
        self.begin()
        self.implement()
        receipt = e.execute_gate(self.root, task_id="value", expected_revision=e.load(self.root)["revision"])
        self.assertTrue(receipt["passed"])

    def crashed(self, with_child=False):
        self.begin()
        path = self.implement()
        script = """import os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import gate_jobs as jobs
@jobs.guarded
def crash(root, **kwargs):
    if sys.argv[3] == 'child':
        child = subprocess.Popen([sys.executable, '-B', '-c', sys.argv[4], sys.argv[5], sys.argv[6], 'value.txt', 'after\\n'], cwd=sys.argv[7], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        jobs.set_process(child.pid)
    os._exit(19)
crash(sys.argv[2], task_id='value')
"""
        release = self.home / "orphan.release"
        self.addCleanup(lambda: release.write_text("release"))
        result = subprocess.run([sys.executable, "-B", "-c", script, str(SCRIPTS), str(self.root), "child" if with_child else "alone", WAIT, str(self.home / "orphan.started"), str(release), str(path)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 19, result.stderr)
        job = next(job for job in e.load(self.root)["gate_jobs"].values() if job["status"] == "RUNNING")
        self.assertFalse(jobs._observe(job["runner_pid"])["alive"])
        return job

    def test_crashed_runner_blocks_reuse_until_observed_recovery(self):
        job = self.crashed()
        self.assert_code("resource_recovery_required", e.execute_gate, self.root, task_id="value")
        run = jobs.recover(self.root, job["id"], "Fixture runner actually exited with status 19.")
        self.assertEqual(run["gate_jobs"][job["id"]]["status"], "INTERRUPTED")
        self.assertTrue(e.execute_gate(self.root, task_id="value")["passed"])

    def test_dead_runner_does_not_allow_recovery_of_live_child(self):
        job = self.crashed(with_child=True)
        self.wait_for(lambda: (self.home / "orphan.started").exists())
        self.assert_code("gate_process_alive", jobs.recover, self.root, job["id"], "Fixture runner exited but the recorded child has not.")
        self.assertEqual(e.load(self.root)["gate_jobs"][job["id"]]["status"], "RUNNING")
        (self.home / "orphan.release").write_text("release")
        self.wait_for(lambda: not jobs._observe(job["child_pid"])["alive"])
        self.assertEqual(jobs.recover(self.root, job["id"], "The runner exited and the released child was observed ended.")["gate_jobs"][job["id"]]["status"], "INTERRUPTED")

    def test_return_with_live_child_stays_running_and_live_runner_cannot_recover(self):
        self.begin()
        path = self.implement()
        release = self.home / "live.release"
        self.addCleanup(lambda: release.write_text("release"))
        children = []
        @jobs.guarded
        def premature(root, **kwargs):
            process = subprocess.Popen([sys.executable, "-B", "-c", WAIT, str(self.home / "live.started"), str(release), "value.txt", "after\n"], cwd=path)
            children.append(process)
            jobs.set_process(process.pid)
            return {"passed": True}
        try:
            self.assert_code("gate_process_alive", premature, self.root, task_id="value")
            job = next(iter(e.load(self.root)["gate_jobs"].values()))
            self.assertEqual(job["status"], "RUNNING")
            self.assert_code("gate_process_alive", jobs.recover, self.root, job["id"], "Fixture runner is still this live test process.")
        finally:
            release.write_text("release")
            for process in children:
                process.wait(timeout=20)

    def test_recovery_refuses_host_and_metadata_identity_drift(self):
        job = self.crashed()
        run = e.load(self.root)
        run["gate_jobs"][job["id"]]["host"] = "different-fixture-host"
        e.atomic_json(self.root / "run.json", run)
        self.assert_code("job_host_mismatch", jobs.recover, self.root, job["id"], "This is deliberately different-host fixture data.")
        run["gate_jobs"][job["id"]] = job
        e.atomic_json(self.root / "run.json", run)
        metadata = jobs._resource_dir(run) / (job["lock_keys"][0] + ".json")
        altered = {**job, "runner_pid": os.getpid()}
        e.atomic_json(metadata, altered)
        self.assert_code("job_identity_drift", jobs.recover, self.root, job["id"], "Fixture metadata was deliberately changed.")
        self.assertEqual(json.loads(metadata.read_text()), altered)

    def test_recovery_refuses_reused_pid_identity(self):
        job = self.crashed()
        run = e.load(self.root)
        job["runner_pid"] = os.getpid()
        run["gate_jobs"][job["id"]] = job
        e.atomic_json(self.root / "run.json", run)
        jobs._write_metadata(jobs._resource_dir(run), job)
        self.assert_code("process_identity_drift", jobs.recover, self.root, job["id"], "Fixture PID now refers to this unrelated live process.")
        self.assertEqual(e.load(self.root)["gate_jobs"][job["id"]]["status"], "RUNNING")

    def test_completed_job_cannot_be_recovered_again(self):
        job = self.crashed()
        jobs.recover(self.root, job["id"], "Fixture crash was observed.")
        self.assert_code("job_state", jobs.recover, self.root, job["id"], "Duplicate recovery should be refused.")


class ResourcePlanTests(unittest.TestCase):
    def test_whitespace_is_normalized_before_resource_conflict_check(self):
        value = plan()
        value["tasks"][0]["resources"] = [" device:one ", "device:one"]
        self.assertEqual(e.validate_plan(value)["tasks"][0]["resources"], ["device:one"])
        other = copy.deepcopy(value["tasks"][0])
        other.update(id="other", paths=["other.txt"], resources=["device:one"])
        value["tasks"].append(other)
        with self.assertRaises(e.RunError) as caught:
            e.validate_plan(value)
        self.assertEqual(caught.exception.code, "overlapping_tasks")

    def test_integrated_resources_are_validated_and_fallback_is_conservative(self):
        value = plan()
        value["tasks"][0]["resources"] = ["device:one"]
        self.assertEqual(jobs._resources({"plan": e.validate_plan(value)}, None, "value-check", 0), ("value-check", ["device:one"]))
        value["verification"][0]["resources"] = [" device:other "]
        self.assertEqual(jobs._resources({"plan": e.validate_plan(value)}, None, "value-check", 0), ("value-check", ["device:other"]))
        for invalid in (None, "device:one", [None], [" "]):
            with self.subTest(resources=invalid):
                value["verification"][0]["resources"] = invalid
                with self.assertRaises(e.RunError) as caught:
                    e.validate_plan(value)
                self.assertEqual(caught.exception.code, "invalid_resource")
