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
