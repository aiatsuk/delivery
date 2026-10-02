"""Actual-diff snapshots retain exact bytes and omit non-tree artifacts."""
from pathlib import Path
import os
import unittest

from tests.test_run_engine import RunFixture, git
import snapshots


class SnapshotTests(RunFixture):
    def capture(self, label="captured"):
        return snapshots.capture_tree(str(self.repo), git(self.repo, "rev-parse", "HEAD"), str(self.home / label))

    def test_raw_blobs_ignore_export_attributes(self):
        (self.repo / ".gitattributes").write_text("value.txt export-ignore\nformat.txt export-subst\n")
        (self.repo / "format.txt").write_text("$Format:%H$\n")
        git(self.repo, "add", ".gitattributes", "format.txt")
        git(self.repo, "commit", "-m", "Add snapshot attributes fixture")
        result = self.capture()
        self.assertEqual((Path(result["path"]) / "value.txt").read_text(), "before\n")
        self.assertEqual((Path(result["path"]) / "format.txt").read_text(), "$Format:%H$\n")

    def test_binary_symlink_executable_and_ignored(self):
        (self.repo / "binary.dat").write_bytes(bytes(range(256)))
        (self.repo / "run.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "run.sh").chmod(0o755)
        (self.repo / "link").symlink_to("value.txt")
        (self.repo / ".gitignore").write_text("cache/\n")
        git(self.repo, "add", "binary.dat", "run.sh", "link", ".gitignore")
        git(self.repo, "commit", "-m", "Add snapshot mode fixtures")
        (self.repo / "cache").mkdir()
        (self.repo / "cache/data").write_text("not source")
        result = self.capture()
        path = Path(result["path"])
        self.assertEqual((path / "binary.dat").read_bytes(), bytes(range(256)))
        self.assertTrue((path / "run.sh").stat().st_mode & 0o111)
        self.assertEqual(os.readlink(path / "link"), "value.txt")
        self.assertFalse((path / "cache").exists())

    def gitlink(self, path="vendor/lib"):
        """Commit a gitlink entry (mode 160000) pointing at a commit of a separate repository."""
        library = self.home / ("library-" + path.replace("/", "-"))
        library.mkdir()
        git(library, "init", "--initial-branch=main")
        git(library, "config", "user.name", "Fixture Maintainer")
        git(library, "config", "user.email", "fixture@example.invalid")
        (library / "lib.txt").write_text("one\n")
        git(library, "add", "lib.txt")
        git(library, "commit", "-m", "First library commit")
        first = git(library, "rev-parse", "HEAD")
        (library / "lib.txt").write_text("two\n")
        git(library, "commit", "-am", "Second library commit")
        second = git(library, "rev-parse", "HEAD")
        git(self.repo, "update-index", "--add", "--cacheinfo", f"160000,{first},{path}")
        git(self.repo, "commit", "-m", "Add a gitlink fixture")
        return first, second

    def test_gitlink_is_recorded_without_content(self):
        first, _ = self.gitlink()
        result = self.capture()
        self.assertEqual([{"path": "vendor/lib", "commit": first}], result["gitlinks"])
        self.assertFalse((Path(result["path"]) / "vendor/lib").exists())
        self.assertEqual((Path(result["path"]) / "value.txt").read_text(), "before\n")
        self.assertEqual(1, result["files"])

    def test_changed_gitlink_between_snapshots_is_reported(self):
        first, second = self.gitlink()
        before = self.capture("before")
        git(self.repo, "update-index", "--cacheinfo", f"160000,{second},vendor/lib")
        git(self.repo, "commit", "-m", "Move the gitlink fixture")
        after = self.capture("after")
        self.assertEqual([{"path": "vendor/lib", "before": first, "after": second}], snapshots.gitlink_changes(before, after))
        self.assertEqual([], snapshots.gitlink_changes(before, before))

    def test_staged_gitlink_change_is_found_before_capture(self):
        first, second = self.gitlink()
        base = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual([], snapshots.staged_gitlink_changes(str(self.repo), base))
        git(self.repo, "update-index", "--cacheinfo", f"160000,{second},vendor/lib")
        self.assertEqual([{"path": "vendor/lib", "before": first, "after": second}], snapshots.staged_gitlink_changes(str(self.repo), base))
        git(self.repo, "update-index", "--force-remove", "vendor/lib")
        self.assertEqual([{"path": "vendor/lib", "before": first, "after": None}], snapshots.staged_gitlink_changes(str(self.repo), base))


    def test_existing_destination_refused(self):
        self.capture()
        with self.assertRaisesRegex(snapshots.git_ops.GitError, "immutable"):
            self.capture()

    def test_repository_destination_refused(self):
        with self.assertRaisesRegex(snapshots.git_ops.GitError, "outside"):
            snapshots.capture_tree(str(self.repo), self.original, str(self.repo / "snapshot"))

    def test_snapshot_does_not_follow_live_worktree_edits(self):
        (self.repo / "value.txt").write_text("unapproved working content")
        result = self.capture()
        self.assertEqual((Path(result["path"]) / "value.txt").read_text(), "before\n")


if __name__ == "__main__":
    unittest.main()
