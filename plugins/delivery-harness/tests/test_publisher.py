"""Fake GitHub plus real local bare remotes. No test invokes live gh or pushes externally."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delivery/scripts"
sys.path.insert(0, str(SCRIPTS))
import publisher as p
import product
import run_engine as e
from tests.test_run_engine import RunFixture, git, plan, gate


class FakeGitHub:
    """A deliberately synthetic provider bound to one local fixture origin."""

    def __init__(self, remote):
        self.remote = Path(remote)
        self.repo = "fixture/project"
        self.prs = {}
        self.calls = []
        self.fail_create_after = False
        self.fail_merge_after = False
        self.fail_merge_before = False
        self.head_override = None
        self.base_override = None

    def repository(self, origin):
        if origin == str(self.remote):
            return self.repo
        return p.github_repository(origin)

    def view(self, repo, number):
        assert repo == self.repo
        self.calls.append(("view", repo, number))
        row = copy.deepcopy(self.prs[number])
        row["headRefOid"] = self.head_override or git(self.remote, "rev-parse", "refs/heads/" + row["headRefName"])
        row["baseRefOid"] = self.base_override or git(self.remote, "rev-parse", "refs/heads/main")
        return row

    def find(self, repo, branch):
        self.calls.append(("find", repo, branch))
        return [self.view(repo, number) for number, row in self.prs.items() if row["headRefName"] == branch]

    def create(self, repo, branch, title, body):
        assert repo == self.repo
        self.calls.append(("create", repo, branch, title, body))
        number = 7 + len(self.prs)
        self.prs[number] = {
            "number": number, "url": f"https://github.com/{repo}/pull/{number}", "state": "OPEN",
            "title": title, "body": body, "headRefName": branch, "baseRefName": "main",
            "isCrossRepository": False, "isDraft": False, "reviewDecision": "",
            "statusCheckRollup": [{"__typename": "CheckRun", "name": "fixture", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "mergedAt": None, "mergeCommit": None,
            "autoMergeRequest": None,
        }
        if self.fail_create_after:
            self.fail_create_after = False
            raise e.RunError("fixture_uncertain", "The fake create response was lost after creation.")
        return self.view(repo, number)

    def update(self, repo, number, title, body):
        self.calls.append(("update", repo, number, title, body))
        self.prs[number].update(title=title, body=body)
        return self.view(repo, number)

    def merge(self, repo, number, head):
        self.calls.append(("merge", repo, number, head))
        if self.fail_merge_before:
            raise e.RunError("fixture_merge_failed", "The fake merge did not run.")
        row = self.view(repo, number)
        if row["headRefOid"] != head:
            raise e.RunError("fixture_head_lock", "The fake exact head guard rejected the request.")
        previous = git(self.remote, "rev-parse", "refs/heads/main")
        git(self.remote, "merge-base", "--is-ancestor", previous, head)
        git(self.remote, "update-ref", "refs/heads/main", head, previous)
        self.prs[number].update(state="MERGED", mergedAt=e.now(), mergeCommit={"oid": head})
        if self.fail_merge_after:
            self.fail_merge_after = False
            raise e.RunError("fixture_uncertain", "The fake merge response was lost after merge.")


class PublisherFixture(RunFixture):
    def setUp(self):
        self.isolated_environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0", "GIT_AUTHOR_NAME": "Fixture Maintainer",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_NAME": "Fixture Maintainer",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid", "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.isolated_environment.start()
        self.addCleanup(self.isolated_environment.stop)
        super().setUp()
        self.provider = FakeGitHub(self.remote)
        self.writer_count = 0

    def committed(self):
        self.integrated()
        return p.commit(self.root, "Preserve the expected fixture value")

    def report(self, task="value"):
        actor = e.load(self.root)["tasks"][task]["agent"]
        e.report_task(self.root, task, actor["actor"], {
            "summary": "Changed only the owned fixture files.", "tests": "Host gates pending.",
            "limitations": "Synthetic actor for unit testing only.",
            "source_event": "publisher-fixture:" + actor["dispatch_id"], "dispatch_id": actor["dispatch_id"],
        })

    def published(self):
        self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic explicit publication authority.")
        return p.publish(self.root, "Update the fixture value", "The value now reads after.\n\nValidated with the fixture checks.", provider=self.provider)

    def grant_merge(self):
        return e.authorize(self.root, "merge", "fixture-user", "Synthetic explicit merge authority for this fixture PR.")

    def merged(self):
        self.published()
        self.grant_merge()
        return p.merge(self.root, provider=self.provider)

    def remote_commit(self, changes):
        self.writer_count += 1
        writer = self.home / f"remote-writer-{self.writer_count}"
        subprocess.run(["git", "clone", str(self.remote), str(writer)], capture_output=True, check=True)
        for name, contents in changes.items():
            (writer / name).parent.mkdir(parents=True, exist_ok=True)
            (writer / name).write_text(contents)
        git(writer, "add", "--", *changes)
        git(writer, "commit", "-m", "Advance the fixture base")
        git(writer, "push", "origin", "main")
        return git(writer, "rev-parse", "HEAD")

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises((e.RunError, p.git_ops.GitError)) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def reverify(self):
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-after-rebase-reviewer", "PASS", "Synthetic review after the base refresh.")
        return e.ready(self.root)


class PublisherTests(PublisherFixture):
    def test_commit_requires_current_validation_and_exact_inventory(self):
        self.begin()
        self.assert_code("commit_state", p.commit, self.root, "Update the value")
        self.implement()
        self.verify()
        e.integrate(self.root)
        self.assertTrue(e.execute_gate(self.root, case_id="value-check")["passed"])
        e.review(self.root, "fixture-integration-reviewer", "PASS", "Synthetic review.")
        before = e.ready(self.root)
        result = p.commit(self.root, "Update the value", expected_revision=before["revision"])
        self.assertNotEqual(before["validated"]["head"], result["commit"]["head"])
        self.assertEqual(before["validated"]["content"], result["validated"]["content"])
        self.assertEqual(result["validated"]["head"], result["commit"]["head"])
        self.assertEqual(["value.txt"], git(Path(result["integration"]["path"]), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").splitlines())
        self.assertEqual("", git(self.repo, "status", "--porcelain"))
        self.assertEqual("main", git(self.repo, "branch", "--show-current"))

    def test_commit_rejects_stale_revision_or_changed_head(self):
        run = self.integrated()
        self.assert_code("stale_revision", p.commit, self.root, "Update the value", expected_revision=run["revision"] - 1)
        path = Path(run["integration"]["path"])
        git(path, "commit", "--allow-empty", "-m", "An unexpected commit")
        self.assert_code("head_changed", p.commit, self.root, "Update the value")

    def test_commit_content_change_preserves_recovery(self):
        run = self.integrated()
        original = p.git_ops.commit_changes

        def mutate_after(*args, **kwargs):
            result = original(*args, **kwargs)
            (Path(args[0]) / "value.txt").write_text("unverified\n")
            return result

        with mock.patch.object(p.git_ops, "commit_changes", side_effect=mutate_after):
            self.assert_code("commit_changed", p.commit, self.root, "Update the value")
        current = e.load(self.root)
        self.assertEqual(("VERIFYING", None, "commit"), (current["state"], current["validated"], current["recovery"]["operation"]))
        self.assertEqual("unverified\n", (Path(run["integration"]["path"]) / "value.txt").read_text())

    def test_commit_recovers_exact_commit_when_the_response_is_lost(self):
        self.integrated()
        original = p.git_ops.commit_changes

        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fixture response lost after the exact commit.")

        with mock.patch.object(p.git_ops, "commit_changes", side_effect=lose_response):
            result = p.commit(self.root, "Update the value")
        self.assertEqual("READY_TO_PUBLISH", result["state"])
        self.assertEqual("recovered-equivalent-commit", result["commit"]["kind"])
        self.assertEqual(result["commit"]["head"], result["validated"]["head"])

    def test_publish_requires_separate_authority_and_committed_head(self):
        run = self.integrated()
        self.assert_code("authorization_required", p.publish, self.root, "Update value", "The fixture value changes.", provider=self.provider)
        self.assertEqual([], self.provider.calls)
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        self.assert_code("uncommitted_head", p.publish, self.root, "Update value", "The fixture value changes.", provider=self.provider)
        self.assertEqual([], self.provider.calls)
        p.commit(self.root, "Update the value")
        published = p.publish(self.root, "Update value", "The fixture value changes.", provider=self.provider)
        self.assertEqual("PR_OPEN", published["state"])
        self.assertEqual(published["commit"]["head"], published["pr"]["head"])
        self.assertEqual(run["integration"]["base_sha"], published["pr"]["base_sha"])
        self.assertEqual(7, published["pr"]["number"])
        self.assertIsNone(published["authorizations"].get("merge"))

    def test_publish_recovers_same_owned_pr_after_lost_create_response(self):
        self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        self.provider.fail_create_after = True
        self.assert_code("fixture_uncertain", p.publish, self.root, "Update value", "The fixture value changes.", provider=self.provider)
        interrupted = e.load(self.root)
        self.assertEqual("pr_attempted", interrupted["publication"]["phase"])
        self.assertIsNone(interrupted["pr"])
        self.assertEqual(1, len(self.provider.prs))
        resumed = p.publish(self.root, "Update value", "The fixture value changes.", provider=self.provider)
        self.assertEqual(7, resumed["pr"]["number"])
        self.assertEqual(1, sum(call[0] == "create" for call in self.provider.calls))

    def test_publish_does_not_adopt_unrelated_same_head_pr(self):
        run = self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        p.git_ops.push_branch(run["integration"]["path"])
        unrelated = self.provider.create(self.provider.repo, run["integration"]["branch"], "Unrelated work", "An existing unrelated PR.")
        self.assert_code("pr_ownership", p.publish, self.root, "Update value", "The fixture value changes.", provider=self.provider)
        self.assertEqual("An existing unrelated PR.", self.provider.prs[unrelated["number"]]["body"])
        self.assertIsNone(e.load(self.root)["pr"])

    def test_publish_rejects_postcommit_drift_and_stale_base(self):
        run = self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        path = Path(run["integration"]["path"])
        (path / "extra.txt").write_text("Unexpected surface.\n")
        self.assert_code("stale_validation", p.publish, self.root, "Update value", "The value changes.", provider=self.provider)
        (path / "extra.txt").unlink()
        self.remote_commit({"new-main.txt": "Fresh main.\n"})
        self.assert_code("stale_base", p.publish, self.root, "Update value", "The value changes.", provider=self.provider)
        self.assertEqual({}, self.provider.prs)

    def test_pre_pr_refresh_recovers_initial_publication_stale_base(self):
        self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        fresh = self.remote_commit({"new-main.txt": "Fresh main.\n"})
        self.assert_code("stale_base", p.publish, self.root, "Update value", "The value changes.", provider=self.provider)
        refreshed = p.refresh(self.root, provider=self.provider)
        self.assertEqual(("VERIFYING", fresh, None), (refreshed["state"], refreshed["integration"]["base_sha"], refreshed["pr"]))
        self.reverify()
        result = p.publish(self.root, "Update value", "The value changes on fresh main.", provider=self.provider)
        self.assertEqual(("PR_OPEN", fresh), (result["state"], result["pr"]["base_sha"]))
        self.assertNotIn("merge", result["authorizations"])

    def test_pre_pr_refresh_does_not_bypass_grant_for_uncertain_existing_pr(self):
        self.committed()
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        self.provider.fail_create_after = True
        self.assert_code("fixture_uncertain", p.publish, self.root, "Update value", "The value changes.", provider=self.provider)
        self.remote_commit({"new-main.txt": "Fresh main.\n"})
        self.assert_code("authorization_required", p.refresh, self.root, provider=self.provider)
        recovered = e.load(self.root)
        self.assertEqual(("PR_OPEN", 7), (recovered["state"], recovered["pr"]["number"]))
        self.grant_merge()
        self.assertEqual("VERIFYING", p.refresh(self.root, provider=self.provider)["state"])

    def test_refresh_requires_matching_merge_authority_and_does_not_force_unchanged_base(self):
        run = self.published()
        self.assert_code("authorization_required", p.refresh, self.root, provider=self.provider)
        self.grant_merge()
        with mock.patch.object(p.git_ops, "rebase_worktree", wraps=p.git_ops.rebase_worktree) as rebase:
            checked = p.refresh(self.root, provider=self.provider)
        rebase.assert_not_called()
        self.assertEqual(run["validated"]["content"], checked["validated"]["content"])
        self.assertEqual("PR_OPEN", checked["state"])

    def test_refresh_invalidates_only_integrated_evidence_then_pushes_with_exact_lease(self):
        original = self.published()
        self.grant_merge()
        fresh = self.remote_commit({"new-main.txt": "Fresh main.\n"})
        refreshed = p.refresh(self.root, provider=self.provider)
        self.assertEqual(("VERIFYING", fresh, None), (refreshed["state"], refreshed["integration"]["base_sha"], refreshed["validated"]))
        self.assertNotEqual(original["commit"]["head"], refreshed["commit"]["head"])
        self.assertTrue(all(receipt["task"] is not None for receipt in refreshed["gates"] + refreshed["reviews"]))
        self.assertEqual(original["tasks"], refreshed["tasks"])
        self.assertEqual(7, refreshed["authorizations"]["merge"]["pr"])
        self.assert_code("publication_state", p.publish, self.root, "Update value", "Current behavior.", provider=self.provider)
        self.reverify()
        with mock.patch.object(p.git_ops, "push_branch", wraps=p.git_ops.push_branch) as push:
            published = p.publish(self.root, "Update value after refresh", "The current base and value passed verification.", provider=self.provider)
        self.assertEqual(original["pr"]["head"], push.call_args.kwargs["expected_remote_head"])
        self.assertEqual((7, fresh), (published["pr"]["number"], published["pr"]["base_sha"]))
        self.assertEqual(1, sum(call[0] == "create" for call in self.provider.calls))

    def test_rebase_conflict_preserves_operation_and_resumes_after_explicit_fixture_fix(self):
        original = self.published()
        self.grant_merge()
        target = self.remote_commit({"value.txt": "conflicting main\n"})
        self.assert_code("REBASE_CONFLICT", p.refresh, self.root, provider=self.provider)
        blocked = e.load(self.root)
        self.assertEqual(("BLOCKED", target), (blocked["state"], blocked["recovery"]["target_base"]))
        self.assertEqual(original["tasks"], blocked["tasks"])
        path = Path(blocked["integration"]["path"])
        self.assertTrue(p.git_ops.inspect_repo(path)["in_progress"])
        (path / "value.txt").write_text("after\n")
        git(path, "add", "--", "value.txt")
        git(path, "-c", "core.editor=true", "rebase", "--continue")
        refreshed = p.refresh(self.root, provider=self.provider)
        self.assertEqual(("VERIFYING", target, None), (refreshed["state"], refreshed["integration"]["base_sha"], refreshed["recovery"]))

    def test_remote_feature_drift_is_not_force_overwritten(self):
        original = self.published()
        self.grant_merge()
        self.remote_commit({"new-main.txt": "Fresh main.\n"})
        p.refresh(self.root, provider=self.provider)
        self.reverify()
        other = git(self.remote, "rev-parse", "main")
        branch = original["integration"]["branch"]
        git(self.remote, "update-ref", "refs/heads/" + branch, other)
        self.assert_code("pr_head_changed", p.publish, self.root, "Update value", "The value changes.", provider=self.provider)
        self.assertEqual(other, git(self.remote, "rev-parse", "refs/heads/" + branch))

    def test_merge_requires_matching_explicit_grant(self):
        self.published()
        self.assert_code("authorization_required", p.merge, self.root, provider=self.provider)
        self.grant_merge()
        with e.transaction(self.root, "fixture_wrong_pr_grant") as run:
            run["authorizations"]["merge"]["pr"] = 99
        self.assert_code("merge_authorization", p.merge, self.root, provider=self.provider)
        self.assertFalse(any(call[0] == "merge" for call in self.provider.calls))

    def test_merge_blocks_pending_failed_missing_checks_and_requested_changes(self):
        self.published()
        self.grant_merge()
        row = self.provider.prs[7]
        baseline = copy.deepcopy(row)
        variants = [
            ({"statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}]}, "checks_pending"),
            ({"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}]}, "checks_failed"),
            ({"statusCheckRollup": [{"state": "PENDING"}]}, "checks_pending"),
            ({"statusCheckRollup": [{"state": "ERROR"}]}, "checks_failed"),
            ({"statusCheckRollup": None}, "checks_unavailable"),
            ({"statusCheckRollup": [{"unknown": "field"}]}, "checks_unavailable"),
            ({"reviewDecision": "CHANGES_REQUESTED"}, "changes_requested"),
            ({"reviewDecision": "REVIEW_REQUIRED"}, "review_pending"),
            ({"isDraft": True}, "draft_pr"),
            ({"mergeStateStatus": "UNKNOWN"}, "merge_blocked"),
        ]
        for changes, code in variants:
            row.clear()
            row.update(copy.deepcopy(baseline))
            row.update(changes)
            self.assert_code(code, p.merge, self.root, provider=self.provider)
        self.assertFalse(any(call[0] == "merge" for call in self.provider.calls))

    def test_absent_hosted_checks_use_local_evidence_and_record_warning(self):
        self.published()
        self.grant_merge()
        self.provider.prs[7]["statusCheckRollup"] = []
        result = p.merge(self.root, provider=self.provider)
        self.assertEqual("MERGED", result["state"])
        self.assertIn("No hosted checks", result["merge_attempt"]["warnings"][0])
        self.assertEqual(result["pr"]["head"], result["merge"]["commit"])
        self.assertIn("branch protection", result["merge"]["base_race_limitation"])

    def test_merge_refuses_pr_head_drift_and_fresh_base_drift(self):
        self.published()
        self.grant_merge()
        self.provider.head_override = self.original
        self.assert_code("pr_head_changed", p.merge, self.root, provider=self.provider)
        self.provider.head_override = None
        self.remote_commit({"new-main.txt": "Fresh main.\n"})
        self.assert_code("stale_base", p.merge, self.root, provider=self.provider)
        self.assertFalse(any(call[0] == "merge" for call in self.provider.calls))

    def test_adjacent_base_recheck_catches_change_before_merge_call(self):
        self.published()
        self.grant_merge()
        original = p.git_ops.main_snapshot
        calls = 0

        def advance_between_checks(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.remote_commit({"new-main.txt": "Just advanced.\n"})
            return original(*args, **kwargs)

        with mock.patch.object(p.git_ops, "main_snapshot", side_effect=advance_between_checks):
            self.assert_code("stale_base", p.merge, self.root, provider=self.provider)
        self.assertGreaterEqual(calls, 2)
        self.assertFalse(any(call[0] == "merge" for call in self.provider.calls))

    def test_merge_pending_result_is_observed_without_resubmitting(self):
        self.published()
        self.grant_merge()
        self.provider.fail_merge_before = True
        self.assert_code("fixture_merge_failed", p.merge, self.root, provider=self.provider)
        self.assertEqual("merge", e.load(self.root)["recovery"]["operation"])
        pending = p.merge(self.root, provider=self.provider)
        self.assertEqual(("PR_OPEN", "pending"), (pending["state"], pending["merge_attempt"]["status"]))
        self.assertEqual(1, sum(call[0] == "merge" for call in self.provider.calls))
        self.provider.fail_merge_before = False
        # The fixture service later completes its existing requested operation.
        self.provider.merge(self.provider.repo, 7, pending["pr"]["head"])
        calls = sum(call[0] == "merge" for call in self.provider.calls)
        confirmed = p.merge(self.root, provider=self.provider)
        self.assertEqual("MERGED", confirmed["state"])
        self.assertEqual(calls, sum(call[0] == "merge" for call in self.provider.calls))

    def test_merge_recovers_uncertain_success(self):
        self.published()
        self.grant_merge()
        self.provider.fail_merge_after = True
        result = p.merge(self.root, provider=self.provider)
        self.assertEqual(("MERGED", None), (result["state"], result["recovery"]))

    def test_same_scope_pr_correction_uses_real_dispatch_contract_and_same_pr(self):
        original = self.published()
        self.grant_merge()
        dispatched = e.register_fix(self.root, "fixture-corrector", "unit-test", "synthetic:correction", "Verify a correction to this fixture's mode.")
        self.assert_code("fix_dispatch_active", p.refresh, self.root, provider=self.provider)
        path = Path(dispatched["integration"]["path"])
        (path / "value.txt").chmod(0o755)
        git(path, "add", "--", "value.txt")
        dispatch = dispatched["integration_fix"]["dispatch_id"]
        e.report_fix(self.root, "fixture-corrector", {"dispatch_id": dispatch, "summary": "Updated the owned fixture file's mode.",
                     "tests": "Verification follows the report.", "limitations": "Synthetic fixture actor.", "source_event": "fixture-correction:" + dispatch})
        self.reverify()
        committed = p.commit(self.root, "Preserve the reviewed fixture file mode")
        self.assertEqual("PR_OPEN", committed["state"])
        self.assertNotEqual(original["commit"]["head"], committed["commit"]["head"])
        result = p.publish(self.root, "Update the fixture value and mode", "The reviewed fixture correction passed the planned checks.", provider=self.provider)
        self.assertEqual(7, result["pr"]["number"])
        self.remote_commit({"new-main.txt": "New base after correction.\n"})
        refreshed = p.refresh(self.root, provider=self.provider)
        self.assertIsNone(refreshed["integration_fix"])
        self.assertEqual(dispatch, refreshed["integration_fix_history"][-1]["dispatch_id"])

    def test_cleanup_commits_only_reviewed_task_files_and_preserves_branches(self):
        merged = self.merged()
        branches = [owner["branch"] for owner in merged["tasks"].values()] + [merged["integration"]["branch"]]
        result = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", result["state"])
        self.assertEqual(result["merge"]["commit"], git(self.repo, "rev-parse", "HEAD"))
        self.assertEqual("main", git(self.repo, "branch", "--show-current"))
        self.assertEqual(["value.txt"], result["cleanup"]["worktrees"]["value"]["committed_reviewed_files"])
        for owner in list(merged["tasks"].values()) + [merged["integration"]]:
            self.assertFalse(Path(owner["path"]).exists())
        for branch in branches:
            self.assertTrue(git(self.repo, "rev-parse", "refs/heads/" + branch))
        calls = len(self.provider.calls)
        self.assertEqual(result, p.cleanup(self.root, provider=self.provider))
        self.assertEqual(calls, len(self.provider.calls))

    def test_cleanup_preserves_dirty_primary(self):
        merged = self.merged()
        (self.repo / "value.txt").write_text("A local edit.\n")
        self.assert_code("DIRTY_WORKTREE", p.cleanup, self.root, provider=self.provider)
        self.assertEqual("A local edit.\n", (self.repo / "value.txt").read_text())
        self.assertTrue(Path(merged["integration"]["path"]).exists())
        self.assertEqual("MERGED", e.load(self.root)["state"])

    def test_cleanup_preserves_task_changes_and_artifacts(self):
        merged = self.merged()
        path = Path(merged["tasks"]["value"]["path"])
        artifact = path / "personal-notes.txt"
        artifact.write_text("Keep this artifact.\n")
        self.assert_code("cleanup_artifacts", p.cleanup, self.root, provider=self.provider)
        self.assertEqual("Keep this artifact.\n", artifact.read_text())
        artifact.unlink()
        (self.repo / ".git/info/exclude").write_text("build/\n")
        ignored = path / "build/private.log"
        ignored.parent.mkdir()
        ignored.write_text("Keep this ignored artifact.\n")
        self.assert_code("cleanup_artifacts", p.cleanup, self.root, provider=self.provider)
        self.assertEqual("Keep this ignored artifact.\n", ignored.read_text())
        ignored.unlink()
        ignored.parent.rmdir()
        (path / "value.txt").write_text("Unreviewed work.\n")
        self.assert_code("stale_report", p.cleanup, self.root, provider=self.provider)
        self.assertEqual("Unreviewed work.\n", (path / "value.txt").read_text())
        self.assertEqual("MERGED", e.load(self.root)["state"])

    def test_cleanup_recovers_a_removed_tree_with_lost_receipt(self):
        self.merged()
        original = p.git_ops.remove_worktree
        first = True

        def remove_then_lose_response(*args, **kwargs):
            nonlocal first
            result = original(*args, **kwargs)
            if first:
                first = False
                raise OSError("Fixture process interruption.")
            return result

        with mock.patch.object(p.git_ops, "remove_worktree", side_effect=remove_then_lose_response):
            with self.assertRaises(OSError):
                p.cleanup(self.root, provider=self.provider)
        incomplete = e.load(self.root)
        self.assertFalse(Path(incomplete["tasks"]["value"]["path"]).exists())
        self.assertEqual("MERGED", incomplete["state"])
        result = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", result["state"])
        self.assertTrue(result["cleanup"]["worktrees"]["value"]["recovered"])

    def test_cleanup_recovers_an_exact_task_commit_with_lost_response(self):
        self.merged()
        original = p.git_ops.commit_changes

        def lose_response(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("Fixture cleanup commit response lost.")

        with mock.patch.object(p.git_ops, "commit_changes", side_effect=lose_response):
            with self.assertRaises(OSError):
                p.cleanup(self.root, provider=self.provider)
        result = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", result["state"])
        self.assertTrue(result["cleanup"]["worktrees"]["value"]["commit_recovered"])

    def test_cleanup_preserves_previous_versions_and_unknown_branches(self):
        prior = self.home / "prior-work"
        p.git_ops.create_worktree(self.repo, prior, "delivery/prior-version", self.original)
        git(self.repo, "branch", "unrelated-branch")
        self.merged()
        with e.transaction(self.root, "fixture_prior_version") as run:
            run["previous_versions"] = [{"version": 0, "tasks": {"prior": {"path": str(prior), "branch": "delivery/prior-version"}}, "integration": None}]
        result = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", result["state"])
        self.assertTrue(prior.exists())
        self.assertEqual([str(prior)], result["cleanup"]["preserved_prior_versions"][0]["worktrees"])
        self.assertEqual(self.original, git(self.repo, "rev-parse", "refs/heads/unrelated-branch"))

    def test_cleanup_preflights_dependency_graph_before_removing_upstream_tree(self):
        definition = plan()
        definition["tasks"].append({
            "id": "dependent", "title": "Add a dependent result", "acceptance": "A verified second file.",
            "paths": ["dependent.txt"], "depends_on": ["value"], "requirements": ["updated-value"],
            "gates": [gate([sys.executable, "-B", "-c", "from pathlib import Path; assert Path('dependent.txt').read_text() == 'verified\\n'"])],
        })
        self.begin(definition)
        self.implement()
        self.verify()
        self.implement("dependent", {"dependent.txt": "verified\n"})
        self.verify("dependent")
        e.integrate(self.root)
        self.reverify()
        p.commit(self.root, "Update the fixture and dependent result")
        e.authorize(self.root, "publish", "fixture-user", "Synthetic publication authority.")
        p.publish(self.root, "Update both fixture values", "Both task results passed verification.", provider=self.provider)
        self.grant_merge()
        p.merge(self.root, provider=self.provider)
        complete = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", complete["state"])
        self.assertEqual({"value", "dependent", "integration"}, set(complete["cleanup"]["worktrees"]))
        self.assertTrue(all(receipt["removed"] for receipt in complete["cleanup"]["worktrees"].values()))

    def test_cleanup_records_only_explicit_product_outcome_and_retries_memory_failure(self):
        context = product.create_product(self.home / "products", "fixture", "Fixture product", "Test the lifecycle.", str(self.repo))
        new = e.create(str(self.repo), "product-fixture", "Update the fixture value.", store=str(self.home / "runs"), product_root=context["root"])
        self.root = Path(new["root"])
        self.merged()
        with mock.patch.object(product, "record_outcome", side_effect=product.ProductError("fixture_memory", "Fixture write failure.")):
            self.assert_code("fixture_memory", p.cleanup, self.root, provider=self.provider)
        self.assertEqual("MERGED", e.load(self.root)["state"])
        complete = p.cleanup(self.root, provider=self.provider)
        self.assertEqual("COMPLETE", complete["state"])
        path = Path(complete["product_outcome"]["path"])
        self.assertIn("https://github.com/fixture/project/pull/7", path.read_text())
        self.assertEqual(1, len(list(path.parent.glob("*.md"))))


class ProviderContractTests(unittest.TestCase):
    def test_origin_parser_accepts_exact_github_and_rejects_ambiguous_destinations(self):
        for origin in ("https://github.com/owner/repo.git", "git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo.git"):
            self.assertEqual("owner/repo", p.github_repository(origin))
        for origin in ("https://github.com.evil.test/owner/repo", "https://user:password@github.com/owner/repo", "https://github.com/owner/repo/extra",
                       "https://github.com/owner/repo?token=value", "/tmp/bare.git", "ssh://other@github.com/owner/repo", "owner/repo"):
            with self.subTest(origin=origin), self.assertRaises(e.RunError):
                p.github_repository(origin)

    def test_transport_uses_explicit_body_file_and_exact_merge_head_without_shell(self):
        calls = []
        body = "Fix the behavior.\n\nA literal `command` and $(not-a-shell) remain prose.\n"
        head = "a" * 40

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertNotIn("shell", kwargs)
            self.assertIn("github.com/owner/repo", argv)
            if "--body-file" in argv:
                self.assertEqual(body, Path(argv[argv.index("--body-file") + 1]).read_text())
            if argv[1:3] == ["pr", "create"]:
                return subprocess.CompletedProcess(argv, 0, "https://github.com/owner/repo/pull/3\n", "")
            if argv[1:3] == ["pr", "view"]:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"number": 3}), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        provider = p.GitHub("/tmp")
        with mock.patch.object(p.subprocess, "run", side_effect=fake_run):
            provider.create("owner/repo", "delivery/run-v1", "Fix behavior", body)
            provider.update("owner/repo", 3, "Fix behavior", body)
            provider.merge("owner/repo", 3, head)
        merge = calls[-1][0]
        self.assertEqual(head, merge[merge.index("--match-head-commit") + 1])
        self.assertIn("--rebase", merge)
        self.assertFalse(set(merge) & {"--admin", "--auto", "--delete-branch"})
        self.assertNotIn(body, str([call[0] for call in calls]))

    def test_history_policy_preserves_normal_product_names(self):
        self.assertEqual("Fix Reader cursor focus", p._history("Fix Reader cursor focus", "Title", 250))
        self.assertEqual("Update Kino and Nesto", p._history("Update Kino and Nesto", "Title", 250))
        with self.assertRaises(e.RunError):
            p._history("Generated with Codex", "Title", 250)
        with self.assertRaises(e.RunError):
            p._history("Изменить интерфейс", "Title", 250)


if __name__ == "__main__":
    unittest.main()
