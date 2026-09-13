import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


sync = module("upstreams")
package = module("package")


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / "repo"
        self.root.mkdir()
        self.git(self.root, "init", "-b", "main")
        self.identity(self.root)
        (self.root / "README.md").write_text("fixture\n")
        self.git(self.root, "add", "README.md")
        self.git(self.root, "commit", "-m", "Create fixture")
        for name, url in sync.SOURCES.items():
            origin = self.home / name
            origin.mkdir()
            self.git(origin, "init", "-b", "main")
            self.identity(origin)
            (origin / "README.md").write_text("first\n")
            self.git(origin, "add", "README.md")
            self.git(origin, "commit", "-m", "Create input fixture")
            self.git(self.root, "-c", "protocol.file.allow=always", "submodule", "add", str(origin), f"upstream/{name}")
            self.git(self.root, "config", "-f", ".gitmodules", f"submodule.upstream/{name}.url", url)
            path = self.root / sync.PLUGIN / f"{name}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("integration\n")
            self.make_review(name)
            sync.record(self.root, name, f"docs/integrations/{name}/review.json")

    @staticmethod
    def git(root, *args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()

    def identity(self, root):
        self.git(root, "config", "user.name", "Fixture Maintainer")
        self.git(root, "config", "user.email", "fixture@example.invalid")

    def make_review(self, name, **changes):
        path = self.root / f"docs/integrations/{name}/review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"source_revision": sync.source(self.root, name), "disposition": "integrated",
                "rationale": "Synthetic fixture review.", "reviewer": "fixture-only",
                "tests": "Fixture evidence, not a real review.", "upstream_paths": ["README.md"],
                "integration_paths": [f"{sync.PLUGIN}{name}.txt"]}
        data.update(changes)
        path.write_text(json.dumps(data))
        return path

    def test_initial_records_and_read_only_status(self):
        before = (self.root / sync.LOCK).read_bytes()
        self.assertTrue(sync.status(self.root)["ok"])
        self.assertEqual((self.root / sync.LOCK).read_bytes(), before)

    def test_pin_update_invalidates_only_changed_source(self):
        name = "orchestrate"
        module = self.root / "upstream" / name
        self.identity(module)
        (module / "README.md").write_text("second\n")
        self.git(module, "add", "README.md")
        self.git(module, "commit", "-m", "Advance fixture")
        self.git(self.root, "add", f"upstream/{name}")
        result = sync.status(self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["sources"][name]["state"], "review-required")
        self.assertTrue(all(result["sources"][other]["state"] == "reviewed" for other in sync.SOURCES if other != name))
        self.make_review(name)
        sync.record(self.root, name, f"docs/integrations/{name}/review.json")
        self.assertTrue(sync.status(self.root)["ok"])

    def test_runtime_mutation_requires_review(self):
        (self.root / sync.PLUGIN / "factory.txt").write_text("changed\n")
        self.assertFalse(sync.status(self.root)["ok"])

    def test_review_mutation_requires_record(self):
        self.make_review("factory", rationale="Changed review")
        self.assertFalse(sync.status(self.root)["ok"])

    def test_deferred_blocks_release(self):
        self.make_review("factory", disposition="deferred")
        sync.record(self.root, "factory", "docs/integrations/factory/review.json")
        self.assertEqual(sync.status(self.root)["sources"]["factory"]["state"], "deferred")
        self.assertFalse(sync.status(self.root)["ok"])

    def test_unknown_source_in_lock_rejected(self):
        data = sync.load_lock(self.root)
        data["sources"]["private"] = {}
        (self.root / sync.LOCK).write_text(json.dumps(data))
        with self.assertRaises(sync.Invalid): sync.status(self.root)

    def test_private_url_fails(self):
        self.git(self.root, "config", "-f", ".gitmodules", "submodule.upstream/factory.url", "ssh://private.invalid/repo")
        self.assertFalse(sync.status(self.root)["ok"])

    def test_lock_symlink_is_not_followed(self):
        path = self.root / sync.LOCK
        outside = self.home / "outside.json"
        path.rename(outside)
        path.symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(sync.Invalid): sync.record(self.root, "factory", "docs/integrations/factory/review.json")
        self.assertEqual(outside.read_bytes(), before)

    def test_dirty_submodule_fails(self):
        (self.root / "upstream/factory/scratch").write_text("keep\n")
        self.assertFalse(sync.status(self.root)["ok"])

    def test_missing_review_fails(self):
        (self.root / "docs/integrations/factory/review.json").unlink()
        self.assertFalse(sync.status(self.root)["ok"])

    def test_review_symlink_rejected_without_overwrite(self):
        path = self.root / "docs/integrations/factory/review.json"
        outside = self.home / "review.json"
        path.rename(outside)
        path.symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(sync.Invalid): sync.record(self.root, "factory", "docs/integrations/factory/review.json")
        self.assertEqual(outside.read_bytes(), before)

    def test_traversal_mapping_rejected(self):
        self.make_review("factory", integration_paths=[f"{sync.PLUGIN}../../../README.md"])
        with self.assertRaises(sync.Invalid): sync.record(self.root, "factory", "docs/integrations/factory/review.json")

    def test_gitlink_checkout_mismatch_fails(self):
        module = self.root / "upstream/factory"
        self.identity(module)
        (module / "README.md").write_text("new\n")
        self.git(module, "add", "README.md")
        self.git(module, "commit", "-m", "Advance unstaged pin")
        self.assertFalse(sync.status(self.root)["ok"])


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name).resolve()
        self.root = self.home / "repo"
        self.source = self.root / "plugins/delivery-harness"
        self.source.mkdir(parents=True)
        for name in package.FILES:
            (self.source / name).write_text("public\n")
        for name in package.DIRS:
            (self.source / name).mkdir()
            (self.source / name / "fixture").write_text("public\n")
        SynchronizationTests.git(self.root, "init", "-b", "main")
        SynchronizationTests.git(self.root, "add", "plugins/delivery-harness")
        self.output = self.home / "out/delivery-harness"

    def test_export_excludes_upstream_and_history(self):
        (self.source / "verification").mkdir()
        (self.source / "verification/private.txt").write_text("PRIVATE\n")
        (self.root / "upstream").mkdir()
        result = package.build(self.root, self.output)
        self.assertFalse((self.output / "verification").exists())
        self.assertFalse((self.output / "upstream").exists())
        self.assertEqual(len(result["files"]), len(package.FILES) + len(package.DIRS))

    def test_existing_destination_is_preserved(self):
        self.output.mkdir(parents=True)
        (self.output / "keep").write_text("keep\n")
        with self.assertRaises(ValueError): package.build(self.root, self.output)
        self.assertEqual((self.output / "keep").read_text(), "keep\n")

    def test_source_symlink_fails_before_output(self):
        (self.source / "skills/leak").symlink_to(self.source / "README.md")
        with self.assertRaises(ValueError): package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_inside_checkout_output_refused(self):
        with self.assertRaises(ValueError): package.build(self.root, self.root / "dist/delivery-harness")

    def test_untracked_runtime_file_is_not_published(self):
        (self.source / "skills/private-history.json").write_text("PRIVATE\n")
        with self.assertRaises(ValueError): package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_symlinked_source_root_is_rejected(self):
        outside = self.home / "outside"
        self.source.rename(outside)
        self.source.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError): package.build(self.root, self.output)
        self.assertFalse(self.output.exists())

    def test_unstaged_runtime_change_is_not_published(self):
        (self.source / "README.md").write_text("unreviewed\n")
        with self.assertRaises(ValueError): package.build(self.root, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
