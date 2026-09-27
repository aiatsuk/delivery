"""Local-only delivery on a real repository with no Git remote; synthetic actors test guards only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.test_run_engine import PASS_EVIDENCE, RunFixture, SCRIPTS, e, gate, git, plan
import publisher as p


ISOLATED_GIT = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Fixture Maintainer", "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture Maintainer", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def cli(*args):
    """Run the real delivery CLI and return (exit code, JSON payload)."""
    result = subprocess.run([sys.executable, "-B", str(SCRIPTS / "delivery.py"), *map(str, args)],
                            capture_output=True, text=True)
    return result.returncode, json.loads(result.stdout if result.returncode == 0 else result.stderr)


class LocalFixture(RunFixture):
    """A primary checkout on main with no remote at all."""

    def setUp(self):
        isolated = mock.patch.dict(os.environ, ISOLATED_GIT)
        isolated.start()
        self.addCleanup(isolated.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="delivery-local-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.repo = self.home / "project"
        subprocess.run(["git", "init", "--initial-branch=main", str(self.repo)], check=True, capture_output=True)
        git(self.repo, "config", "user.name", "Fixture Maintainer")
        git(self.repo, "config", "user.email", "fixture@example.invalid")
        (self.repo / "value.txt").write_text("before\n")
        git(self.repo, "add", "value.txt")
        git(self.repo, "commit", "-m", "Create fixture")
        self.original = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual("", git(self.repo, "remote"))
        request = self.home / "request.md"
        request.write_text("Update the fixture value and verify it locally.\n")
        code, payload = cli("new", "--repo", self.repo, "--id", "fixture", "--request-file", request,
                            "--store", self.home / "runs", "--local-only")
        self.assertEqual(0, code, payload)
        self.root = Path(payload["result"]["root"])

    def committed(self, value=None):
        if value is None:
            ready = self.integrated()
        else:
            self.begin(value)
            self.implement()
            self.verify()
            e.integrate(self.root)
            self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
            e.review(self.root, "fixture-integration-reviewer", "PASS", PASS_EVIDENCE)
            ready = e.ready(self.root)
        self.assertEqual(("READY_TO_PUBLISH", self.original), (ready["state"], ready["base_sha"]))
        return p.commit(self.root, "Update the fixture value")

    def cache_writing_plan(self):
        """A normal Python repository: gates leave an ignored __pycache__ in the tree they run in."""
        (self.repo / ".gitignore").write_text("__pycache__/\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-m", "Ignore Python bytecode caches")
        self.original = git(self.repo, "rev-parse", "HEAD")
        check = gate([sys.executable, "-B", "-c",
                      "from pathlib import Path; cache = Path('__pycache__'); cache.mkdir(exist_ok=True); "
                      "(cache / 'value_check.cpython-310.pyc').write_bytes(b'cached'); "
                      "assert Path('value.txt').read_text() == 'after\\n'"])
        check["cleanup"] = "Leaves an ignored __pycache__ directory in the tree it ran in, as Python tooling does."
        value = plan()
        value["tasks"][0]["gates"] = [check]
        value["verification"][0].update(check)
        return value

    def authorize_cli(self, scope, *extra):
        evidence = self.home / f"evidence-{scope}.md"
        evidence.write_text(f"Synthetic explicit {scope} authority for this fixture, not a real user's approval.\n")
        code, payload = cli("authorize", "--run", self.root, "--scope", scope, "--actor", "fixture-user",
                            "--evidence-file", evidence, *extra)
        self.assertEqual(0, code, payload)
        return payload["result"]

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises((e.RunError, e.git_ops.GitError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)
        return caught.exception


class LocalModeTests(LocalFixture):
    def test_small_local_run_finishes_on_its_branch_without_remote_and_keeps_main(self):
        self.assertEqual("local", e.status(self.root)["delivery_mode"])
        run = self.committed()
        status = e.status(self.root)
        self.assertEqual("Local-only run: commit, then finish-local (add --fast-forward-main with local-merge authority to update main).",
                         status["next_action"])
        head, branch = run["commit"]["head"], run["integration"]["branch"]
        task = run["tasks"]["value"]
        code, payload = cli("finish-local", "--run", self.root)
        self.assertEqual(0, code, payload)
        result = payload["result"]
        self.assertEqual("COMPLETE", result["state"])
        outcome = result["local_outcome"]
        self.assertEqual({"branch": branch, "head": head, "fast_forwarded": False, "main_head": self.original},
                         {key: outcome[key] for key in ("branch", "head", "fast_forwarded", "main_head")})
        self.assertEqual(outcome, e.status(self.root)["local_outcome"])
        # Main is untouched; the delivered branch and its worktree remain.
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))
        self.assertEqual("main", git(self.repo, "branch", "--show-current"))
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual(head, git(self.repo, "rev-parse", "refs/heads/" + branch))
        self.assertTrue(Path(run["integration"]["path"]).is_dir())
        # Task trees are retired with their reviewed result preserved on the branch.
        self.assertFalse(Path(task["path"]).exists())
        self.assertEqual(["value.txt"], result["cleanup"]["worktrees"]["value"]["committed_reviewed_files"])
        self.assertEqual("after\n", git(self.repo, "show", task["branch"] + ":value.txt") + "\n")
        self.assertNotIn("integration", result["cleanup"]["worktrees"])
        self.assertEqual("", git(self.repo, "remote"))
        self.assertEqual(result, p.finish_local(self.root))

    def test_fast_forward_needs_local_merge_authority_then_moves_main_to_committed_head(self):
        run = self.committed()
        head, branch = run["commit"]["head"], run["integration"]["branch"]
        self.assert_code("authorization_required", p.finish_local, self.root, fast_forward=True)
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))
        self.assertIsNone(e.load(self.root).get("local_finish"))
        self.assertTrue(Path(run["tasks"]["value"]["path"]).is_dir())
        granted = self.authorize_cli("local-merge")
        self.assertEqual(granted["plan_hash"], granted["authorizations"]["local-merge"]["plan_hash"])
        code, payload = cli("finish-local", "--run", self.root, "--fast-forward-main")
        self.assertEqual(0, code, payload)
        result = payload["result"]
        self.assertEqual("COMPLETE", result["state"])
        self.assertEqual(head, git(self.repo, "rev-parse", "refs/heads/main"))
        self.assertEqual(head, git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual("main", git(self.repo, "branch", "--show-current"))
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual("after\n", (self.repo / "value.txt").read_text())
        self.assertEqual({"branch": branch, "head": head, "fast_forwarded": True, "main_head": head},
                         {key: result["local_outcome"][key] for key in ("branch", "head", "fast_forwarded", "main_head")})
        self.assertFalse(Path(run["integration"]["path"]).exists())
        self.assertFalse(Path(run["tasks"]["value"]["path"]).exists())
        self.assertEqual(head, git(self.repo, "rev-parse", "refs/heads/" + branch))
        self.assertEqual(result, p.finish_local(self.root, fast_forward=True))

    def test_finish_local_preserves_and_lists_a_task_tree_with_ignored_gate_caches(self):
        run = self.committed(self.cache_writing_plan())
        task = run["tasks"]["value"]
        cache = Path(task["path"]) / "__pycache__" / "value_check.cpython-310.pyc"
        self.assertTrue(cache.is_file())
        result = p.finish_local(self.root)
        self.assertEqual("COMPLETE", result["state"])
        self.assertEqual([{"owner": "value", "path": task["path"], "branch": task["branch"], "head": run["tasks"]["value"]["base_sha"],
                           "blocked_by": ["__pycache__/"]}], result["local_outcome"]["preserved_worktrees"])
        self.assertEqual(b"cached", cache.read_bytes())
        # Preserved means untouched: no cleanup commit, the reviewed change stays staged.
        self.assertEqual(task["base_sha"], git(Path(task["path"]), "rev-parse", "HEAD"))
        self.assertEqual("M  value.txt", git(Path(task["path"]), "status", "--porcelain"))
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))
        self.assertEqual(run["commit"]["head"], git(self.repo, "rev-parse", "refs/heads/" + run["integration"]["branch"]))
        self.assertEqual(result["local_outcome"], e.status(self.root)["local_outcome"])
        self.assertEqual(result, p.finish_local(self.root))

    def test_fast_forward_still_happens_when_gate_caches_preserve_trees(self):
        run = self.committed(self.cache_writing_plan())
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        result = p.finish_local(self.root, fast_forward=True)
        head = run["commit"]["head"]
        self.assertEqual(("COMPLETE", True, head), (result["state"], result["local_outcome"]["fast_forwarded"], result["local_outcome"]["main_head"]))
        self.assertEqual(head, git(self.repo, "rev-parse", "refs/heads/main"))
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        preserved = {item["owner"]: item for item in result["local_outcome"]["preserved_worktrees"]}
        self.assertEqual({"value", "integration"}, set(preserved))
        self.assertEqual(["__pycache__/"], preserved["value"]["blocked_by"])
        self.assertEqual(["__pycache__/"], preserved["integration"]["blocked_by"])
        self.assertEqual((run["integration"]["path"], head), (preserved["integration"]["path"], preserved["integration"]["head"]))
        for owner in (run["tasks"]["value"], run["integration"]):
            self.assertTrue((Path(owner["path"]) / "__pycache__" / "value_check.cpython-310.pyc").is_file())

    def test_unreviewed_tracked_change_still_blocks_a_tree_with_gate_caches(self):
        run = self.committed(self.cache_writing_plan())
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        path = Path(run["tasks"]["value"]["path"])
        (path / "value.txt").write_text("unreviewed\n")
        self.assert_code("stale_report", p.finish_local, self.root, fast_forward=True)
        self.assertEqual("unreviewed\n", (path / "value.txt").read_text())
        current = e.load(self.root)
        self.assertEqual(("READY_TO_PUBLISH", None), (current["state"], current.get("local_finish")))
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))
        # The same change made after finishing started is caught before preservation.
        (path / "value.txt").write_text("after\n")
        original = p._fast_forward_main

        def edit_after_preflight(run_state, intent):
            original(run_state, intent)
            (path / "value.txt").write_text("unreviewed\n")

        with mock.patch.object(p, "_fast_forward_main", side_effect=edit_after_preflight):
            self.assert_code("cleanup_changed", p.finish_local, self.root, fast_forward=True)
        interrupted = e.load(self.root)
        self.assertEqual("READY_TO_PUBLISH", interrupted["state"])
        self.assertFalse(interrupted["cleanup"]["worktrees"]["value"].get("preserved"))
        self.assertEqual("unreviewed\n", (path / "value.txt").read_text())

    def test_publication_commands_refuse_local_run_before_git_or_provider(self):
        self.committed()
        provider = mock.Mock()
        calls = [(p.publish, ("Update the value", "The value changes.")), (p.refresh, ()), (p.merge, ()), (p.cleanup, ())]
        with mock.patch.object(p.git_ops, "_git", side_effect=AssertionError("No Git command may run.")):
            for function, args in calls:
                with self.subTest(command=function.__name__):
                    error = self.assert_code("local_only_run", function, self.root, *args, provider=provider)
                    self.assertEqual("This run is local-only; use finish-local.", error.message)
        self.assertEqual([], provider.mock_calls)
        body = self.home / "body.md"
        body.write_text("The value changes.\n")
        code, payload = cli("publish", "--run", self.root, "--title", "Update the value", "--body-file", body)
        self.assertEqual((2, "local_only_run"), (code, payload["error"]["code"]))
        current = e.load(self.root)
        self.assertEqual(("READY_TO_PUBLISH", None, None), (current["state"], current.get("publication"), current["pr"]))

    def test_fast_forward_refuses_after_local_main_moved(self):
        run = self.committed()
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        (self.repo / "other.txt").write_text("Main moved after the run started.\n")
        git(self.repo, "add", "other.txt")
        git(self.repo, "commit", "-m", "Move the fixture main")
        moved = git(self.repo, "rev-parse", "HEAD")
        self.assert_code("main_moved", p.finish_local, self.root, fast_forward=True)
        self.assertEqual(moved, git(self.repo, "rev-parse", "refs/heads/main"))
        current = e.load(self.root)
        self.assertEqual("READY_TO_PUBLISH", current["state"])
        self.assertIsNone(current.get("local_finish"))
        self.assertTrue(Path(run["tasks"]["value"]["path"]).is_dir())
        self.assertTrue(Path(run["integration"]["path"]).is_dir())

    def test_local_start_refuses_primary_on_another_branch_without_switching(self):
        e.set_plan(self.root, plan())
        e.authorize(self.root, "implement", "fixture-user", "Synthetic unit fixture authority.")
        git(self.repo, "switch", "-c", "topic")
        error = self.assert_code("LOCAL_BASE_BRANCH", e.start, self.root, within_request=True)
        self.assertIn("topic", error.message)
        self.assertEqual("topic", git(self.repo, "branch", "--show-current"))
        self.assertEqual("READY_FOR_APPROVAL", e.load(self.root)["state"])

    def test_finish_local_recovers_an_interrupted_removal_and_blocks_reopening(self):
        run = self.committed()
        original = p.git_ops.remove_worktree

        def remove_then_lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fixture process interruption after removal.")

        with mock.patch.object(p.git_ops, "remove_worktree", side_effect=remove_then_lose_response):
            with self.assertRaises(OSError):
                p.finish_local(self.root)
        interrupted = e.load(self.root)
        self.assertEqual(("READY_TO_PUBLISH", "retiring"), (interrupted["state"], interrupted["local_finish"]["phase"]))
        self.assertFalse(Path(run["tasks"]["value"]["path"]).exists())
        self.assert_code("local_finish_started", e.register_fix, self.root, "fixture-fix-writer", "unit-test",
                         "synthetic:late-fix", "A late correction after finishing started.")
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        self.assert_code("local_finish_mode", p.finish_local, self.root, fast_forward=True)
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))
        result = p.finish_local(self.root)
        self.assertEqual("COMPLETE", result["state"])
        self.assertTrue(result["cleanup"]["worktrees"]["value"]["recovered"])
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/main"))

    def test_finish_local_recovers_a_fast_forward_with_lost_response(self):
        run = self.committed()
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        original = p.git_ops.fast_forward_local_base

        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fixture process interruption after the fast-forward.")

        with mock.patch.object(p.git_ops, "fast_forward_local_base", side_effect=lose_response):
            with self.assertRaises(OSError):
                p.finish_local(self.root, fast_forward=True)
        interrupted = e.load(self.root)
        self.assertEqual("fast_forward_attempted", interrupted["local_finish"]["phase"])
        self.assertEqual(run["commit"]["head"], git(self.repo, "rev-parse", "refs/heads/main"))
        with mock.patch.object(p.git_ops, "fast_forward_local_base", side_effect=AssertionError("No second fast-forward.")):
            result = p.finish_local(self.root, fast_forward=True)
        self.assertEqual(("COMPLETE", True), (result["state"], result["local_outcome"]["fast_forwarded"]))
        self.assertFalse(Path(run["integration"]["path"]).exists())


    def test_finish_local_records_only_the_explicitly_bound_product_outcome(self):
        import product
        context = product.create_product(self.home / "products", "fixture", "Fixture product", "Test the lifecycle.", str(self.repo))
        created = e.create(str(self.repo), "product-fixture", "Update the fixture value locally.", store=str(self.home / "runs"),
                           product_root=context["root"], delivery_mode="local")
        self.root = Path(created["root"])
        run = self.committed()
        e.authorize(self.root, "local-merge", "fixture-user", "Synthetic explicit local-merge authority.")
        result = p.finish_local(self.root, fast_forward=True)
        note = Path(result["product_outcome"]["path"]).read_text()
        self.assertIn(f"Finished local-only run on branch {run['integration']['branch']} at {run['commit']['head']}. Fast-forwarded main.", note)
        self.assertEqual(1, len(list(Path(result["product_outcome"]["path"]).parent.glob("*.md"))))


class LocalModeBoundaryTests(RunFixture):
    """Repositories that do have an origin: local mode ignores it, GitHub mode is unchanged."""

    def test_local_run_uses_local_main_ahead_of_origin_without_fetching(self):
        (self.repo / "ahead.txt").write_text("Only on local main.\n")
        git(self.repo, "add", "ahead.txt")
        git(self.repo, "commit", "-m", "Advance local main only")
        ahead = git(self.repo, "rev-parse", "HEAD")
        local = Path(e.create(str(self.repo), "local-ahead", "Update the value on local main.",
                              store=str(self.home / "runs"), delivery_mode="local")["root"])
        e.set_plan(local, plan())
        e.authorize(local, "implement", "fixture-user", "Synthetic unit fixture authority.")
        with mock.patch.object(e.git_ops, "_fetch", side_effect=AssertionError("A local-only run must not fetch.")):
            started = e.start(local, within_request=True)
        self.assertEqual(("IMPLEMENTING", ahead), (started["state"], started["base_sha"]))
        # The same repository still refuses GitHub-mode delivery from a local main ahead of origin.
        e.set_plan(self.root, plan())
        e.authorize(self.root, "implement", "fixture-user", "Synthetic unit fixture authority.")
        with self.assertRaises(e.git_ops.GitError) as caught:
            e.start(self.root, within_request=True)
        self.assertEqual("MAIN_DIVERGED", caught.exception.code)

    def test_github_run_refuses_local_merge_authority_and_finish_local(self):
        self.assertEqual("github", e.status(self.root)["delivery_mode"])
        self.integrated()
        with self.assertRaises(e.RunError) as caught:
            e.authorize(self.root, "local-merge", "fixture-user", "Synthetic local-merge request.")
        self.assertEqual("local_merge_scope", caught.exception.code)
        with self.assertRaises(e.RunError) as caught:
            p.finish_local(self.root)
        self.assertEqual("not_local_run", caught.exception.code)
        self.assertEqual("Obtain publication authority and publish the tested integration branch.", e.status(self.root)["next_action"])

    def test_unknown_delivery_mode_is_refused(self):
        with self.assertRaises(e.RunError) as caught:
            e.create(str(self.repo), "unknown-mode", "A bounded request.", store=str(self.home / "runs"), delivery_mode="gitlab")
        self.assertEqual("invalid_delivery_mode", caught.exception.code)


if __name__ == "__main__":
    unittest.main()
