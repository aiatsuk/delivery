"""Offline regression tests: every repository, remote and artifact is temporary."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "delivery" / "scripts"))
import git_ops as git


class GitSafetyTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author", "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author", "GIT_COMMITTER_EMAIL": "author@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        })
        self.environment.start()
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-git-test-")
        self.folder = Path(self.temporary.name).resolve()
        self.primary = self.folder / "primary"
        self.remote = self.folder / "remote.git"
        self.command(self.folder, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.command(self.folder, "init", "--initial-branch=main", str(self.primary))
        self.write(self.primary, "one.txt", "one\n")
        self.write(self.primary, "two.txt", "two\n")
        self.write(self.primary, ".gitignore", "build/\n*.log\n")
        self.command(self.primary, "add", "--", "one.txt", "two.txt", ".gitignore")
        self.command(self.primary, "commit", "-m", "Create test fixture")
        self.command(self.primary, "remote", "add", "origin", str(self.remote))
        self.command(self.primary, "push", "--set-upstream", "origin", "main")
        self.base = self.command(self.primary, "rev-parse", "HEAD").strip()
        self.counter = 0

    def tearDown(self):
        self.temporary.cleanup()
        self.environment.stop()

    def command(self, root, *args):
        result = subprocess.run(["git", "--literal-pathspecs", "-C", str(root), *args], stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode(errors="surrogateescape")

    def write(self, root, name, content):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())
        return target

    def task(self, label=None):
        self.counter += 1
        label = label or f"work-{self.counter}"
        root = self.folder / label
        git.create_worktree(self.primary, root, f"task/{label.replace(' ', '-')}", self.base)
        return root

    def staged_patch(self, changes, label=None):
        root = self.task(label)
        for name, content in changes.items():
            self.write(root, name, content)
        self.command(root, "add", "--", *changes)
        output = self.folder / f"patch-{self.counter}.diff"
        return root, git.export_patch(root, output, list(changes))

    def remote_commit(self, changes):
        self.counter += 1
        writer = self.folder / f"writer-{self.counter}"
        self.command(self.folder, "clone", str(self.remote), str(writer))
        for name, content in changes.items():
            self.write(writer, name, content)
        self.command(writer, "add", "--", *changes)
        self.command(writer, "commit", "-m", "Advance remote fixture")
        self.command(writer, "push", "origin", "main")
        return self.command(writer, "rev-parse", "HEAD").strip()

    def assert_code(self, code, callable_, *args, **kwargs):
        with self.assertRaises(git.GitError) as raised:
            callable_(*args, **kwargs)
        self.assertEqual(raised.exception.code, code, raised.exception.as_dict())
        return raised.exception

    def test_stale_primary_fast_forwards_to_fetched_main(self):
        latest = self.remote_commit({"one.txt": "remote update\n"})
        self.assertNotEqual(self.base, latest)
        result = git.prepare_primary(self.primary)
        self.assertEqual(result["head"], latest)
        self.assertEqual(result["base_sha"], latest)
        self.assertEqual(result["branch"], "main")
        self.assertEqual((self.primary / "one.txt").read_text(), "remote update\n")

    def test_dirty_primary_preserves_tracked_and_untracked_files(self):
        self.write(self.primary, "one.txt", "local changes\n")
        sentinel = self.write(self.primary, "personal notes.txt", "keep\n")
        self.assert_code("DIRTY_WORKTREE", git.prepare_primary, self.primary)
        self.assertEqual(self.command(self.primary, "rev-parse", "HEAD").strip(), self.base)
        self.assertEqual(sentinel.read_text(), "keep\n")
        self.assertEqual((self.primary / "one.txt").read_text(), "local changes\n")

    def test_ahead_main_is_not_reset(self):
        self.write(self.primary, "one.txt", "local committed change\n")
        self.command(self.primary, "add", "--", "one.txt")
        self.command(self.primary, "commit", "-m", "Preserve local fixture change")
        head = self.command(self.primary, "rev-parse", "HEAD").strip()
        self.assert_code("MAIN_DIVERGED", git.prepare_primary, self.primary)
        self.assertEqual(self.command(self.primary, "rev-parse", "HEAD").strip(), head)

    def test_clean_primary_feature_branch_returns_to_main_without_deleting_branch(self):
        self.command(self.primary, "switch", "--create", "existing-work")
        result = git.prepare_primary(self.primary)
        self.assertEqual(result["branch"], "main")
        self.assertEqual(self.command(self.primary, "rev-parse", "existing-work").strip(), self.base)

    def test_worktree_identity_uses_common_directory(self):
        root = self.task("work with spaces")
        nested = root / "nested"
        nested.mkdir()
        self.assertEqual(git.canonical_repo(root), self.primary)
        self.assertEqual(git.canonical_repo(nested), self.primary)
        snapshot = git.inspect_repo(root)
        self.assertEqual(snapshot["primary"], str(self.primary))
        self.assertEqual(snapshot["worktree"], str(root))
        self.assertEqual(snapshot["origin_url"], str(self.remote))
        self.assertEqual(snapshot["branch"], "task/work-with-spaces")

    def test_bare_remote_and_nonrepository_rejected_for_working_operations(self):
        self.assert_code("BARE_REPOSITORY", git.canonical_repo, self.remote)
        self.assert_code("NOT_REPOSITORY", git.canonical_repo, self.folder)

    def test_task_worktree_requires_unused_external_exact_path_and_base(self):
        self.assert_code("WORKTREE_PATH_IN_USE", git.create_worktree,
                         self.primary, self.primary / "nested", "task/nested", self.base)
        existing = self.folder / "existing"
        existing.mkdir()
        self.assert_code("WORKTREE_PATH_IN_USE", git.create_worktree,
                         self.primary, existing, "task/existing", self.base)
        self.assert_code("INVALID_PATH", git.create_worktree, self.primary,
                         self.folder / "missing" / ".." / "escape", "task/traversal", self.base)
        self.assert_code("INVALID_SHA", git.create_worktree, self.primary,
                         self.folder / "short", "task/short", self.base[:8])
        latest = self.remote_commit({"one.txt": "new main\n"})
        git.prepare_primary(self.primary)
        self.assert_code("STALE_BASE", git.create_worktree, self.primary,
                         self.folder / "stale", "task/stale", self.base)
        self.assertNotEqual(latest, self.base)

    def test_inventory_records_staging_working_untracked_and_ignored_separately(self):
        root = self.task()
        self.write(root, "one.txt", "staged\n")
        self.command(root, "add", "--", "one.txt")
        self.write(root, "one.txt", "working\n")
        self.write(root, "new file.txt", "new\n")
        self.write(root, "build/dependency.log", "private log\n")
        result = git.inventory(root, self.base)
        self.assertEqual(result["files"], ["new file.txt", "one.txt"])
        self.assertEqual(result["staged"], ["one.txt"])
        self.assertEqual(result["unstaged"], ["one.txt"])
        self.assertEqual(result["untracked"], ["new file.txt"])
        self.assertTrue(result["ignored"])
        one = next(record for record in result["records"] if record["path"] == "one.txt")
        self.assertEqual(one["sha256"], hashlib.sha256(b"working\n").hexdigest())
        self.assertEqual(one["index"][0]["blob"], self.command(root, "rev-parse", ":one.txt").strip())
        self.assertEqual(len(result["fingerprint"]), 64)
        self.assertEqual(len(result["content_fingerprint"]), 64)
        self.assertIsNotNone(result["tree"])

    def test_content_evidence_stays_stable_when_tested_files_are_committed(self):
        root = self.task()
        self.write(root, "one.txt", "approved\n")
        self.write(root, "new file.txt", "new\n")
        before = git.inventory(root, self.base)
        committed = git.commit_changes(root, ["one.txt", "new file.txt"], "Update fixture behavior")
        after = git.inventory(root, self.base)
        self.assertEqual(before["content_fingerprint"], after["content_fingerprint"])
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(after["tree"], committed["tree"])
        self.assertEqual(after["files"], ["new file.txt", "one.txt"])
        self.assertTrue(committed["status"]["clean"])

    def test_content_and_index_hashes_change_for_independent_edits(self):
        root = self.task()
        self.write(root, "one.txt", "first\n")
        first = git.inventory(root, self.base)
        self.command(root, "add", "--", "one.txt")
        staged = git.inventory(root, self.base)
        self.assertEqual(first["content_fingerprint"], staged["content_fingerprint"])
        self.assertNotEqual(first["fingerprint"], staged["fingerprint"])
        self.write(root, "one.txt", "second\n")
        second = git.inventory(root, self.base)
        self.assertNotEqual(staged["content_fingerprint"], second["content_fingerprint"])
        self.assertEqual(staged["tree"], second["tree"])

    def test_hidden_index_flags_cannot_bypass_inventory_or_primary_cleanliness(self):
        self.command(self.primary, "update-index", "--assume-unchanged", "one.txt")
        self.write(self.primary, "one.txt", "hidden local changes\n")
        self.assertEqual(git.inspect_repo(self.primary)["status"]["hidden"], ["one.txt"])
        self.assert_code("HIDDEN_INDEX_STATE", git.prepare_primary, self.primary)
        self.assert_code("HIDDEN_INDEX_STATE", git.inventory, self.primary, self.base)
        self.assertEqual((self.primary / "one.txt").read_text(), "hidden local changes\n")

    def test_skip_worktree_flag_cannot_hide_a_changed_task_file(self):
        root = self.task()
        self.command(root, "update-index", "--skip-worktree", "one.txt")
        self.write(root, "one.txt", "hidden local changes\n")
        self.assert_code("HIDDEN_INDEX_STATE", git.inventory, root, self.base)
        self.assert_code("HIDDEN_INDEX_STATE", git.commit_changes, root, ["one.txt"], "Update fixture")

    def test_commit_normalizes_staged_content_to_exact_reviewed_working_files(self):
        root = self.task()
        self.write(root, "one.txt", "unreviewed index content\n")
        self.command(root, "add", "--", "one.txt")
        self.write(root, "one.txt", "reviewed working content\n")
        reviewed = git.inventory(root, self.base)
        result = git.commit_changes(root, ["one.txt"], "Use reviewed fixture content")
        self.assertEqual(self.command(root, "show", result["head"] + ":one.txt"), "reviewed working content\n")
        committed = git.inventory(root, self.base)
        self.assertEqual(reviewed["content_fingerprint"], committed["content_fingerprint"])
        self.assertNotEqual(reviewed["tree"], committed["tree"])

    def test_export_requires_staging_and_never_stages_untracked_work(self):
        root = self.task()
        self.write(root, "one.txt", "change\n")
        self.write(root, "new.txt", "untracked\n")
        before = self.command(root, "diff", "--cached")
        self.assert_code("UNSTAGED_CHANGES", git.export_patch, root, self.folder / "out.diff",
                         ["one.txt", "new.txt"])
        self.assertEqual(self.command(root, "diff", "--cached"), before)
        self.assertEqual((root / "new.txt").read_text(), "untracked\n")

    def test_export_binary_exact_paths_with_spaces_and_literal_pathspecs(self):
        root, exported = self.staged_patch({"space name.bin": b"\x00\xff\x01", ":(glob)payload.txt": "literal\n"})
        payload = Path(exported["path"]).read_bytes()
        self.assertIn(b"GIT binary patch", payload)
        self.assertEqual(exported["files"], [":(glob)payload.txt", "space name.bin"])
        integrated = self.task("integration")
        result = git.integrate_patches(integrated, [exported], self.base)
        self.assertEqual((integrated / "space name.bin").read_bytes(), b"\x00\xff\x01")
        self.assertEqual((integrated / ":(glob)payload.txt").read_text(), "literal\n")
        self.assertEqual(result["staged"], exported["files"])

    def test_dependency_logs_never_enter_reviewed_patch(self):
        root = self.task()
        self.write(root, "one.txt", "reviewed\n")
        log = self.write(root, "build/dependency.log", "DO NOT PUBLISH\n")
        self.command(root, "add", "--", "one.txt")
        exported = git.export_patch(root, self.folder / "safe.diff", ["one.txt"])
        self.assertNotIn(b"DO NOT PUBLISH", Path(exported["path"]).read_bytes())
        self.assertEqual(log.read_text(), "DO NOT PUBLISH\n")
        self.command(root, "add", "--force", "--", "build/dependency.log")
        self.assert_code("OUT_OF_SCOPE", git.export_patch, root, self.folder / "unsafe.diff", ["one.txt"])
        self.assertFalse((self.folder / "unsafe.diff").exists())

    def test_untracked_unrelated_file_refuses_export_and_commit(self):
        root = self.task()
        self.write(root, "one.txt", "reviewed\n")
        self.command(root, "add", "--", "one.txt")
        sentinel = self.write(root, "personal.txt", "keep\n")
        self.assert_code("OUT_OF_SCOPE", git.export_patch, root, self.folder / "unsafe.diff", ["one.txt"])
        self.assert_code("OUT_OF_SCOPE", git.commit_changes, root, ["one.txt"], "Update fixture")
        self.assertEqual(sentinel.read_text(), "keep\n")
        self.assertEqual(self.command(root, "rev-parse", "HEAD").strip(), self.base)

    def test_export_preserves_existing_output_and_refuses_in_repository_evidence(self):
        root, exported = self.staged_patch({"one.txt": "change\n"})
        payload = Path(exported["path"]).read_bytes()
        self.assert_code("OUTPUT_EXISTS", git.export_patch, root, exported["path"], ["one.txt"])
        self.assertEqual(Path(exported["path"]).read_bytes(), payload)
        self.assert_code("EVIDENCE_IN_REPOSITORY", git.export_patch, root, root / "evidence.diff", ["one.txt"])

    def test_scopes_reject_parent_traversal_metadata_directories_and_symlink_parents(self):
        root = self.task()
        self.write(root, "one.txt", "change\n")
        self.command(root, "add", "--", "one.txt")
        for scope in (["../one.txt"], [str(root / "one.txt")], [".git/config"], ["./one.txt"]):
            self.assert_code("INVALID_PATH", git.export_patch, root, self.folder / "scope.diff", scope)
        (root / "directory").mkdir()
        self.assert_code("DIRECTORY_SCOPE", git.export_patch, root, self.folder / "scope.diff", ["directory"])
        (root / "linked").symlink_to(self.folder, target_is_directory=True)
        self.assert_code("UNSAFE_SYMLINK", git.export_patch, root, self.folder / "scope.diff", ["linked/outside.txt"])

    def test_multiple_independent_patches_integrate_and_stage_only_reviewed_files(self):
        _, first = self.staged_patch({"one.txt": "one changed\n"})
        _, second = self.staged_patch({"two.txt": "two changed\n"})
        root = self.task("integration")
        result = git.integrate_patches(root, [first, second], self.base)
        self.assertEqual(result["staged"], ["one.txt", "two.txt"])
        self.assertEqual(result["overlaps"], [])
        self.assertEqual(result["head"], self.base)
        self.assertEqual((root / "one.txt").read_text(), "one changed\n")
        self.assertEqual((root / "two.txt").read_text(), "two changed\n")
        self.assertEqual(result["unstaged"], [])
        self.assertEqual(result["untracked"], [])

    def test_dependent_patches_apply_in_order_and_report_overlap(self):
        source, first = self.staged_patch({"one.txt": "first change\n"})
        git.commit_changes(source, ["one.txt"], "Apply first fixture change")
        self.write(source, "one.txt", "dependent change\n")
        self.command(source, "add", "--", "one.txt")
        second = git.export_patch(source, self.folder / "dependent.diff", ["one.txt"])
        root = self.task("integration")
        result = git.integrate_patches(root, [first, second], self.base)
        self.assertEqual(result["overlaps"], ["one.txt"])
        self.assertEqual(result["staged"], ["one.txt"])
        self.assertEqual((root / "one.txt").read_text(), "dependent change\n")

    def test_conflicting_later_patch_preserves_real_index_and_working_files(self):
        _, first = self.staged_patch({"one.txt": "first replacement\n"})
        _, second = self.staged_patch({"one.txt": "conflicting replacement\n"})
        root = self.task("integration")
        before = git.inventory(root, self.base)
        index_path = Path(self.command(root, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        index_before = index_path.read_bytes()
        self.assert_code("PATCH_CONFLICT", git.integrate_patches, root, [first, second], self.base)
        self.assertEqual(git.inventory(root, self.base)["fingerprint"], before["fingerprint"])
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual((root / "one.txt").read_text(), "one\n")

    def test_patch_hash_and_scope_tampering_fail_before_integration(self):
        _, exported = self.staged_patch({"one.txt": "replacement\n"})
        root = self.task("integration")
        before = git.inventory(root, self.base)
        bad_hash = dict(exported, sha256="0" * 64)
        self.assert_code("PATCH_HASH_MISMATCH", git.integrate_patches, root, [bad_hash], self.base)
        bad_scope = dict(exported, allowed_paths=["two.txt"])
        self.assert_code("OUT_OF_SCOPE", git.integrate_patches, root, [bad_scope], self.base)
        self.assertEqual(git.inventory(root, self.base)["fingerprint"], before["fingerprint"])

    def test_patch_parent_traversal_is_rejected_before_writing_anywhere(self):
        _, exported = self.staged_patch({"one.txt": "replacement\n"})
        payload = Path(exported["path"]).read_bytes()
        payload = payload.replace(b"a/one.txt", b"a/../../escaped.txt").replace(b"b/one.txt", b"b/../../escaped.txt")
        tampered_path = self.folder / "traversal.diff"
        tampered_path.write_bytes(payload)
        tampered = dict(exported, path=str(tampered_path), sha256=hashlib.sha256(payload).hexdigest())
        root = self.task("integration")
        before = git.inventory(root, self.base)
        with self.assertRaises(git.GitError) as raised:
            git.integrate_patches(root, [tampered], self.base)
        self.assertIn(raised.exception.code, {"INVALID_PATCH", "INVALID_PATH"})
        self.assertEqual(git.inventory(root, self.base)["fingerprint"], before["fingerprint"])
        self.assertFalse((self.folder / "escaped.txt").exists())

    def test_dirty_integration_preserves_untracked_file(self):
        _, exported = self.staged_patch({"one.txt": "replacement\n"})
        root = self.task("integration")
        sentinel = self.write(root, "personal.txt", "keep\n")
        self.assert_code("DIRTY_WORKTREE", git.integrate_patches, root, [exported], self.base)
        self.assertEqual(sentinel.read_text(), "keep\n")
        self.assertEqual((root / "one.txt").read_text(), "one\n")

    def test_ignored_file_collision_refuses_integration_without_overwrite(self):
        source = self.task()
        self.write(source, "artifact.log", "reviewed tracked file\n")
        self.command(source, "add", "--force", "--", "artifact.log")
        exported = git.export_patch(source, self.folder / "artifact.diff", ["artifact.log"])
        root = self.task("integration")
        sentinel = self.write(root, "artifact.log", "personal artifact\n")
        self.assert_code("IGNORED_COLLISION", git.integrate_patches, root, [exported], self.base)
        self.assertEqual(sentinel.read_text(), "personal artifact\n")
        self.assertEqual(git.inspect_repo(root)["status"]["staged"], [])

    def test_delete_and_rename_like_changes_preserve_exact_inventory(self):
        source = self.task()
        (source / "one.txt").rename(source / "renamed file.txt")
        self.command(source, "add", "--", "one.txt", "renamed file.txt")
        exported = git.export_patch(source, self.folder / "renamed.diff", ["one.txt", "renamed file.txt"])
        root = self.task("integration")
        result = git.integrate_patches(root, [exported], self.base)
        self.assertFalse((root / "one.txt").exists())
        self.assertEqual((root / "renamed file.txt").read_text(), "one\n")
        self.assertEqual(result["staged"], ["one.txt", "renamed file.txt"])
        committed = git.commit_changes(root, ["one.txt", "renamed file.txt"], "Rename fixture file")
        self.assertEqual(committed["files"], ["one.txt", "renamed file.txt"])
        self.assertTrue(committed["status"]["clean"])

    def test_already_staged_deletion_can_be_committed_without_broad_staging(self):
        root = self.task()
        (root / "one.txt").unlink()
        self.command(root, "add", "--", "one.txt")
        result = git.commit_changes(root, ["one.txt"], "Remove unused fixture file")
        self.assertEqual(result["files"], ["one.txt"])
        self.assertTrue(result["status"]["clean"])

    def test_cleanup_refuses_ignored_artifact_and_preserves_it(self):
        root = self.task()
        branch = git.inspect_repo(root)["branch"]
        sentinel = self.write(root, "build/dependency.log", "keep ignored output\n")
        self.assert_code("DIRTY_WORKTREE", git.remove_worktree, self.primary, root, branch, self.base)
        self.assertEqual(sentinel.read_text(), "keep ignored output\n")
        self.assertTrue(root.exists())

    def test_cleanup_refuses_untracked_empty_directory(self):
        root = self.task()
        (root / "empty").mkdir()
        branch = git.inspect_repo(root)["branch"]
        self.assert_code("WORKTREE_ARTIFACTS", git.remove_worktree, self.primary, root, branch, self.base)
        self.assertTrue((root / "empty").exists())

    def test_cleanup_requires_exact_branch_head_and_clean_owned_worktree(self):
        root = self.task()
        branch = git.inspect_repo(root)["branch"]
        self.assert_code("WORKTREE_IDENTITY", git.remove_worktree, self.primary, root, "task/wrong", self.base)
        self.assert_code("PRIMARY_WRITE", git.remove_worktree, self.primary, self.primary, "main", self.base)
        result = git.remove_worktree(self.primary, root, branch, self.base)
        self.assertTrue(result["removed"])
        self.assertFalse(root.exists())
        self.assertEqual(self.command(self.primary, "rev-parse", branch).strip(), self.base)

    def test_in_progress_operation_is_reported_and_not_cleaned_up(self):
        root = self.task()
        branch = git.inspect_repo(root)["branch"]
        merge_head = Path(self.command(root, "rev-parse", "--path-format=absolute", "--git-path", "MERGE_HEAD").strip())
        merge_head.write_text(self.base + "\n")
        self.assertIn("MERGE_HEAD", git.inspect_repo(root)["in_progress"])
        self.assert_code("OPERATION_IN_PROGRESS", git.remove_worktree, self.primary, root, branch, self.base)
        self.assertTrue(merge_head.exists())

    def test_commits_and_pushes_refuse_the_primary_checkout(self):
        self.assert_code("PRIMARY_WRITE", git.commit_changes, self.primary, ["one.txt"], "Update fixture")
        self.assert_code("PRIMARY_WRITE", git.push_branch, self.primary)

    def test_commit_rejects_agent_attribution(self):
        root = self.task()
        self.write(root, "one.txt", "change\n")
        self.assert_code("HISTORY_POLICY", git.commit_changes, root, ["one.txt"], "Update fixture with Codex")

    def test_initial_push_and_explicit_feature_lease(self):
        root = self.task()
        self.write(root, "one.txt", "published first\n")
        first = git.commit_changes(root, ["one.txt"], "Update first fixture")
        pushed = git.push_branch(root)
        self.assertEqual(pushed["remote_head"], first["head"])
        self.assert_code("REMOTE_BRANCH_EXISTS", git.push_branch, root)
        self.write(root, "one.txt", "published second\n")
        second = git.commit_changes(root, ["one.txt"], "Update second fixture")
        self.assert_code("REMOTE_CHANGED", git.push_branch, root, self.base)
        updated = git.push_branch(root, first["head"])
        self.assertEqual(updated["remote_head"], second["head"])
        self.assertEqual(self.command(self.primary, "ls-remote", "origin", "refs/heads/main").split()[0], self.base)

    def test_push_refuses_an_uninspected_separate_push_destination(self):
        root = self.task()
        other_remote = self.folder / "other.git"
        self.command(self.folder, "init", "--bare", "--initial-branch=main", str(other_remote))
        self.command(root, "remote", "set-url", "--push", "origin", str(other_remote))
        self.assert_code("REMOTE_PUSH_URL_MISMATCH", git.push_branch, root)
        self.assertEqual(self.command(self.folder, "ls-remote", str(other_remote)), "")

    def test_rebase_conflicts_are_surfaced_and_left_for_explicit_resolution(self):
        root = self.task()
        self.write(root, "one.txt", "feature change\n")
        feature = git.commit_changes(root, ["one.txt"], "Update feature fixture")
        latest = self.remote_commit({"one.txt": "conflicting main change\n"})
        error = self.assert_code("REBASE_CONFLICT", git.rebase_worktree, root)
        self.assertEqual(error.details["base_sha"], latest)
        self.assertTrue(error.details["in_progress"])
        self.assertTrue(git.inspect_repo(root)["status"]["unmerged"])
        self.assertEqual(self.command(self.primary, "rev-parse", feature["branch"]).strip(), feature["head"])

    def test_rebase_success_and_remote_snapshot_observe_latest_main(self):
        root = self.task()
        self.write(root, "one.txt", "feature change\n")
        feature = git.commit_changes(root, ["one.txt"], "Update feature fixture")
        latest = self.remote_commit({"two.txt": "new main change\n"})
        snapshot = git.main_snapshot(self.primary)
        self.assertEqual(snapshot["head"], self.base)
        self.assertEqual(snapshot["base_sha"], latest)
        rebased = git.rebase_worktree(root)
        self.assertEqual(rebased["base_sha"], latest)
        self.assertNotEqual(rebased["head"], feature["head"])
        self.assertEqual((root / "one.txt").read_text(), "feature change\n")
        self.assertEqual((root / "two.txt").read_text(), "new main change\n")


if __name__ == "__main__":
    unittest.main()
