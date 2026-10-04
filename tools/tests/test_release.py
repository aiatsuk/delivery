import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest


def module(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / f"{name}.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


release = module("release")
REPO = Path(__file__).resolve().parents[2]


class RepositoryTests(unittest.TestCase):
    def test_the_repository_versions_agree_with_the_changelog(self):
        version = release.primary_version(REPO)
        self.assertEqual(release.check(REPO, f"v{version}"), version)

    def test_version_command_prints_the_version_file(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(release.main(["--root", str(REPO), "version"]), 0)
        self.assertEqual(out.getvalue().strip(), (REPO / "VERSION").read_text().strip())


class FixtureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for path in {entry[0] for entry in release.VERSION_FILES}:
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / path, target)
        self.version = release.primary_version(REPO)
        self.tag = f"v{self.version}"
        (self.root / "CHANGELOG.md").write_text(
            "# Changes\n\n"
            f"## {self.version}\n\nTitle line.\n\n- First change.\n  continued.\n\n"
            "## 0.0.1 — 2020-01-01\n\n- Old change.\n",
            encoding="utf-8",
        )

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = release.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_agreement_passes(self):
        code, out, _ = self.run_main("check", "--tag", self.tag)
        self.assertEqual(code, 0)
        self.assertIn(self.version, out)

    def test_each_mismatched_version_file_fails(self):
        for path, _, _ in release.VERSION_FILES:
            with self.subTest(path=path):
                file = self.root / path
                original = file.read_text(encoding="utf-8")
                file.write_text(original.replace(self.version, "9.9.9", 1), encoding="utf-8")
                try:
                    code, _, err = self.run_main("check", "--tag", self.tag)
                finally:
                    file.write_text(original, encoding="utf-8")
                self.assertEqual(code, 1)
                self.assertIn(path, err)
                self.assertIn("9.9.9", err)

    def test_second_marketplace_entry_is_checked(self):
        file = self.root / ".cursor-plugin/marketplace.json"
        text = file.read_text(encoding="utf-8")
        head, sep, tail = text.rpartition(f'"version": "{self.version}"')
        file.write_text(head + '"version": "9.9.9"' + tail, encoding="utf-8")
        with self.assertRaises(release.ReleaseError) as caught:
            release.check(self.root, self.tag)
        self.assertIn("plugins.0.version", str(caught.exception))

    def test_tag_that_disagrees_with_the_files_fails(self):
        (self.root / "CHANGELOG.md").write_text("## 9.9.9\n\n- Next.\n", encoding="utf-8")
        code, _, err = self.run_main("check", "--tag", "v9.9.9")
        self.assertEqual(code, 1)
        self.assertIn("VERSION", err)

    def test_missing_changelog_section_fails(self):
        (self.root / "CHANGELOG.md").write_text("# Changes\n\n## 0.0.1\n\n- Old.\n", encoding="utf-8")
        code, _, err = self.run_main("check", "--tag", self.tag)
        self.assertEqual(code, 1)
        self.assertIn(f"no '## {self.version}' section", err)

    def test_missing_version_file_fails(self):
        (self.root / "plugins/delivery-harness/README.md").unlink()
        code, _, err = self.run_main("check", "--tag", self.tag)
        self.assertEqual(code, 1)
        self.assertIn("plugins/delivery-harness/README.md: cannot read", err)

    def test_malformed_tag_fails(self):
        for tag in ("1.4.1", "v1.4", "v1.4.1-rc1", "release"):
            with self.subTest(tag=tag):
                code, _, err = self.run_main("check", "--tag", tag)
                self.assertEqual(code, 1)
                self.assertIn("vX.Y.Z", err)

    def test_notes_returns_exactly_the_section_body(self):
        code, out, _ = self.run_main("notes", "--tag", self.tag)
        self.assertEqual(code, 0)
        self.assertEqual(out, "Title line.\n\n- First change.\n  continued.\n")

    def test_notes_accept_a_dated_heading(self):
        self.assertEqual(release.changelog_section(self.root, "0.0.1"), "- Old change.\n")

    def test_notes_for_an_absent_version_fail(self):
        code, out, err = self.run_main("notes", "--tag", "v8.8.8")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("8.8.8", err)

    def test_heading_prefix_does_not_match_a_longer_version(self):
        (self.root / "CHANGELOG.md").write_text(f"## {self.version}0\n\n- Other.\n", encoding="utf-8")
        with self.assertRaises(release.ReleaseError):
            release.changelog_section(self.root, self.version)


if __name__ == "__main__":
    unittest.main()
