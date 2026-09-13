from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/delivery/scripts/product.py"
SPEC = importlib.util.spec_from_file_location("delivery_product", SCRIPT)
assert SPEC and SPEC.loader
PRODUCT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PRODUCT
SPEC.loader.exec_module(PRODUCT)


class ProductTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "products"
        self.product = self.home / "sample-product"

    def make_product(self):
        self.product.mkdir(parents=True)
        (self.product / "product.md").write_text("# Sample product\n\n## Purpose\n\nUseful work.\n", encoding="utf-8")
        (self.product / "memory.md").write_text("# Product memory\n\nKeep this fact.\n", encoding="utf-8")
        return self.product

    def make_item(self, path="projects/P-123", kind="project"):
        item = self.product / path
        item.mkdir(parents=True)
        (item / f"{kind}.md").write_text(f"# {kind.title()} outcome\n\nA scoped improvement.\n", encoding="utf-8")
        return item

    def note(self, **changes):
        args = dict(product_root=self.product, run_id="run-123", summary="The requested timeout now preserves the cart.",
                    evidence=["/delivery/runs/run-123/verification.md"], next_action="Review the pull request.")
        args.update(changes)
        return PRODUCT.record_outcome(**args)

    def assert_code(self, code, function, *args, **kwargs):
        with self.assertRaises(PRODUCT.ProductError) as raised:
            function(*args, **kwargs)
        self.assertEqual(code, raised.exception.code)
        return raised.exception

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), *map(str, args)],
                                text=True, capture_output=True, check=False,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        return result.returncode, json.loads(result.stdout), result.stderr

    def test_home_resolution_does_not_create_or_select(self):
        with mock.patch.dict(os.environ, {"PRODUCT_MEMORY_HOME": str(self.home)}):
            self.assertEqual(self.home, PRODUCT.product_home())
            self.assertEqual(self.root / "override", PRODUCT.product_home(self.root / "override"))
            self.assertEqual([], PRODUCT.list_products(self.home))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, "home", return_value=self.root):
            self.assertEqual(self.root / ".local/share/product-driven-development/products", PRODUCT.product_home())
        self.assertFalse(self.home.exists())
        with mock.patch.dict(os.environ, {"PRODUCT_MEMORY_HOME": ""}):
            self.assert_code("invalid_path", PRODUCT.product_home)

    def test_create_only_initial_markdown_at_new_path(self):
        result = PRODUCT.create_product(self.home, "sample-product", "Sample product", "Useful work.", "/repo/platform")
        self.assertTrue(result["created"])
        self.assertEqual(self.product.name, result["id"])
        self.assertEqual("Useful work.", result["purpose"])
        self.assertEqual({"product.md", "memory.md"}, {path.name for path in self.product.iterdir()})
        self.assertEqual(0o600, (self.product / "product.md").stat().st_mode & 0o777)
        self.assertEqual(0o700, self.product.stat().st_mode & 0o777)
        self.assertIn("/repo/platform", result["product_markdown"])
        self.assertNotIn("accepted", result["memory_markdown"].lower())
        self.assertFalse((self.product / "sessions").exists())

    def test_create_preserves_existing_empty_or_populated_path(self):
        self.product.mkdir(parents=True)
        self.assert_code("already_exists", PRODUCT.create_product, self.home, "sample-product", "Title", "Purpose.", "/repo/main")
        sentinel = self.product / "unrelated.md"
        sentinel.write_text("Keep this.")
        self.assert_code("already_exists", PRODUCT.create_product, self.home, "sample-product", "Title", "Purpose.", "/repo/main")
        self.assertEqual("Keep this.", sentinel.read_text())
        self.assertEqual([sentinel], list(self.product.iterdir()))

    def test_create_rolls_back_own_documents_on_second_publish_failure(self):
        publish = PRODUCT._publish

        def fail_memory(fd, name, text):
            if name == "memory.md":
                raise PRODUCT.ProductError("write_failed", "Fixture failure.")
            return publish(fd, name, text)

        with mock.patch.object(PRODUCT, "_publish", side_effect=fail_memory):
            self.assert_code("write_failed", PRODUCT.create_product, self.home, "sample-product", "Title", "Purpose.", "/repo/main")
        self.assertFalse(self.product.exists())

    def test_create_rejects_traversal_ids_before_writes(self):
        for slug in ("../escape", "/absolute", "UPPER", "x/y", ".", "..", "a" * 65, "ending-", ""):
            with self.subTest(slug=slug):
                self.assert_code("invalid_id", PRODUCT.create_product, self.home, slug, "Title", "Purpose.", "/repo/main")
        self.assertFalse(self.home.exists())

    def test_create_rejects_symlink_destination(self):
        self.home.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        self.product.symlink_to(outside, target_is_directory=True)
        self.assert_code("already_exists", PRODUCT.create_product, self.home, "sample-product", "Title", "Purpose.", "/repo/main")
        self.assertEqual([], list(outside.iterdir()))

    def test_list_uses_direct_markdown_maps_and_ignores_legacy_files(self):
        self.make_product()
        (self.home / "products.json").write_text('{"selected": "legacy"}')
        (self.home / "legacy").mkdir()
        (self.home / "legacy/product.json").write_text('{"id": "legacy"}')
        (self.home / "alpha").mkdir()
        (self.home / "alpha/product.md").write_text("# Alpha\n")
        (self.home / "linked").symlink_to(self.product, target_is_directory=True)
        self.assertEqual(["alpha", "sample-product"], [item["id"] for item in PRODUCT.list_products(self.home)])
        self.assertEqual("Useful work.", PRODUCT.list_products(self.home)[1]["purpose"])

    def test_list_does_not_read_memory_or_sessions(self):
        self.make_product()
        (self.product / "memory.md").unlink()
        (self.product / "memory.md").symlink_to(self.root / "absent")
        self.assertEqual("Sample product", PRODUCT.list_products(self.home)[0]["title"])

    def test_load_requires_exact_path_even_when_only_one_product_exists(self):
        self.make_product()
        for value in (None, ""):
            self.assert_code("invalid_path", PRODUCT.load_product, value)
        self.assert_code("not_found", PRODUCT.load_product, self.home)
        self.assert_code("not_found", PRODUCT.load_product, self.home / "latest")
        loaded = PRODUCT.load_product(self.product / "product.md")
        self.assertEqual("Sample product", loaded["title"])
        self.assertIn("Keep this fact.", loaded["memory_markdown"])
        (self.product / "memory.md").unlink()
        self.assertIsNone(PRODUCT.load_product(self.product)["memory_markdown"])

    def test_load_exposes_links_without_following_them(self):
        self.make_product()
        (self.product / "product.md").write_text(
            "# Sample product\n\n[Project](projects/P-1/project.md)\n"
            "[Tracker](https://example.test/tickets/T-1)\n"
            "[Outside](../outside/secret.md)\n[Native](sessions/exact.md)\n"
            "[Repo](</repo/with spaces/README.md>)\n[Part](#purpose)\n"
        )
        original = PRODUCT._read
        names = []

        def record_read(fd, name, **kwargs):
            names.append(name)
            return original(fd, name, **kwargs)

        with mock.patch.object(PRODUCT, "_read", side_effect=record_read):
            result = PRODUCT.load_product(self.product)
        self.assertEqual(["product.md", "memory.md"], names)
        self.assertEqual(6, len(result["links"]))
        self.assertTrue(all(not link["followed"] for link in result["links"]))
        self.assertEqual(str(self.product / "projects/P-1/project.md"), result["links"][0]["path"])

    def test_load_rejects_map_memory_and_root_symlinks(self):
        self.make_product()
        alias = self.root / "alias"
        alias.symlink_to(self.product, target_is_directory=True)
        self.assert_code("unsafe_path", PRODUCT.load_product, alias)
        target = self.root / "foreign.md"
        target.write_text("Private content.")
        for name in ("memory.md", "product.md"):
            (self.product / name).unlink()
            (self.product / name).symlink_to(target)
            self.assert_code("unsafe_path", PRODUCT.load_product, self.product)
            (self.product / name).unlink()
            (self.product / name).write_text("# Restored\n")

    def test_load_refuses_oversized_invalid_and_nonregular_markdown(self):
        self.make_product()
        marker = self.product / "product.md"
        marker.write_bytes(b"x" * (PRODUCT.MARKDOWN_BYTES + 1))
        self.assert_code("invalid_markdown", PRODUCT.load_product, self.product)
        marker.write_bytes(b"\xff")
        self.assert_code("invalid_markdown", PRODUCT.load_product, self.product)
        marker.write_text("# Map\x00")
        self.assert_code("invalid_markdown", PRODUCT.load_product, self.product)
        marker.unlink()
        os.mkfifo(marker)
        self.assert_code("invalid_markdown", PRODUCT.load_product, self.product)

    def test_work_item_accepts_markdown_directory_and_exact_document(self):
        self.make_product()
        project = self.make_item()
        ticket = self.make_item("tickets/T-1", "ticket")
        dotted = self.make_item("projects/release.1")
        for path in (project, project / "project.md", "projects/P-123", "projects/P-123/project.md", dotted):
            with self.subTest(path=path):
                result = PRODUCT.validate_work_item(self.product, path)
                self.assertEqual("project", result["kind"])
                self.assertEqual("project.md", Path(result["path"]).name)
        result = PRODUCT.validate_work_item(self.product, ticket)
        self.assertEqual("tickets/T-1/ticket.md", result["relative_path"])

    def test_work_item_requires_exactly_one_markdown_marker_not_json(self):
        self.make_product()
        item = self.product / "projects/P-1"
        item.mkdir(parents=True)
        (item / "project.json").write_text('{"id": "P-1"}')
        self.assert_code("missing_work_item", PRODUCT.validate_work_item, self.product, item)
        self.assert_code("invalid_work_item", PRODUCT.validate_work_item, self.product, item / "project.json")
        (item / "project.md").write_text("# Project\n")
        self.assertEqual("project", PRODUCT.validate_work_item(self.product, item)["kind"])
        (item / "ticket.md").write_text("# Ticket\n")
        self.assert_code("ambiguous_work_item", PRODUCT.validate_work_item, self.product, item / "project.md")

    def test_work_item_cannot_escape_by_traversal_absolute_or_prefix(self):
        self.make_product()
        for item in ("../outside", self.root / "outside", str(self.product) + "-other/projects/P-1"):
            code = "unsafe_path" if str(item).startswith("..") else "outside_product"
            self.assert_code(code, PRODUCT.validate_work_item, self.product, item)

    def test_work_item_rejects_symlinks_at_every_child_level(self):
        self.make_product()
        target = self.root / "outside"
        target.mkdir()
        (target / "project.md").write_text("# External project\n")
        (self.product / "projects").symlink_to(target, target_is_directory=True)
        self.assert_code("unsafe_path", PRODUCT.validate_work_item, self.product, "projects")
        (self.product / "projects").unlink()
        (self.product / "projects").mkdir()
        (self.product / "projects/P-1").symlink_to(target, target_is_directory=True)
        self.assert_code("unsafe_path", PRODUCT.validate_work_item, self.product, "projects/P-1")
        linked = self.product / "projects/P-2"
        linked.mkdir()
        (linked / "project.md").symlink_to(target / "project.md")
        self.assert_code("unsafe_path", PRODUCT.validate_work_item, self.product, linked)

    def test_note_is_owned_bounded_and_grants_no_approval(self):
        self.make_product()
        item = self.make_item("tickets/T-1", "ticket")
        notes = item / "notes"
        notes.mkdir()
        unrelated = notes / "2026-01-01-manual.md"
        unrelated.write_text("An unrelated note.")
        original_memory = (self.product / "memory.md").read_bytes()
        result = self.note(work_item="tickets/T-1/ticket.md")
        self.assertTrue(result["created"])
        self.assertEqual("ticket:T-1", result["owner"])
        path = Path(result["path"])
        self.assertEqual(notes, path.parent)
        self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2}-\d{4}-delivery-run-123\.md$")
        text = path.read_text()
        self.assertIn("## Next action", text)
        self.assertIn("[Evidence 1](</delivery/runs/run-123/verification.md>)", text)
        self.assertIn("this note grants no approval", text)
        self.assertNotIn("Accepted:", text)
        self.assertNotIn("## Verification", text)
        self.assertEqual("An unrelated note.", unrelated.read_text())
        self.assertEqual(original_memory, (self.product / "memory.md").read_bytes())
        self.assertLessEqual(path.stat().st_size, PRODUCT.NOTE_BYTES)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertEqual(2, len(list(notes.iterdir())))
        self.assertFalse((self.product / "notes").exists())

    def test_exact_replay_across_dates_creates_one_note(self):
        self.make_product()
        with mock.patch.object(PRODUCT, "datetime") as clock:
            clock.now.return_value = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
            first = self.note()
            before = Path(first["path"]).read_bytes()
            clock.now.return_value = datetime(2026, 9, 15, 3, 21, tzinfo=timezone.utc)
            replay = self.note()
        self.assertTrue(first["created"])
        self.assertFalse(replay["created"])
        self.assertEqual(first["path"], replay["path"])
        self.assertEqual(first["digest"], replay["digest"])
        self.assertEqual(before, Path(first["path"]).read_bytes())
        self.assertEqual(1, len(list((self.product / "notes").iterdir())))

    def test_changed_replay_or_edited_note_never_overwrites(self):
        self.make_product()
        first = self.note()
        path = Path(first["path"])
        before = path.read_bytes()
        for change in ({"summary": "A different result."}, {"next_action": "Investigate the queue."},
                       {"evidence": ["/delivery/runs/run-123/other.md"]}):
            self.assert_code("note_conflict", self.note, **change)
            self.assertEqual(before, path.read_bytes())
        path.write_bytes(before + b"\nManual addition.\n")
        self.assert_code("note_conflict", self.note)
        self.assertTrue(path.read_bytes().endswith(b"Manual addition.\n"))

    def test_duplicate_run_notes_fail_closed(self):
        self.make_product()
        first = self.note()
        path = Path(first["path"])
        duplicate = path.parent / "2000-01-01-0000-delivery-run-123.md"
        duplicate.write_bytes(path.read_bytes())
        self.assert_code("note_conflict", self.note)
        self.assertEqual(2, len(list(path.parent.iterdir())))

    def test_invalid_run_ids_are_rejected_without_artifacts(self):
        self.make_product()
        for run_id in ("../escape", "a/b", "a.b", "", "x" * 97, "-dash", "a\n- Accepted: yes"):
            self.assert_code("invalid_id", self.note, run_id=run_id)
        self.assertFalse((self.product / "notes").exists())

    def test_note_rejects_symlink_owner_notes_and_existing_note(self):
        self.make_product()
        target = self.root / "outside"
        target.mkdir()
        (self.product / "notes").symlink_to(target, target_is_directory=True)
        self.assert_code("unsafe_path", self.note)
        self.assertEqual([], list(target.iterdir()))
        (self.product / "notes").unlink()
        result = self.note()
        path = Path(result["path"])
        path.unlink()
        sentinel = target / "sentinel.md"
        sentinel.write_text("Private content.")
        path.symlink_to(sentinel)
        self.assert_code("unsafe_path", self.note)
        self.assertEqual("Private content.", sentinel.read_text())

    def test_note_atomic_publication_and_no_clobber_under_race(self):
        self.make_product()
        original = PRODUCT.os.link

        def create_competing_target(source, target, **kwargs):
            fd = kwargs["dst_dir_fd"]
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
            os.write(descriptor, b"Concurrent unrelated note.\n")
            os.close(descriptor)
            return original(source, target, **kwargs)

        with mock.patch.object(PRODUCT.os, "link", side_effect=create_competing_target):
            self.assert_code("already_exists", self.note)
        paths = list((self.product / "notes").iterdir())
        self.assertEqual(1, len(paths))
        self.assertEqual("Concurrent unrelated note.\n", paths[0].read_text())

    def test_cooperating_concurrent_writers_create_exactly_once(self):
        self.make_product()

        def attempt(_):
            try:
                return self.note()
            except PRODUCT.ProductError as error:
                self.assertEqual("product_busy", error.code)
                return None

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(12)))
        self.assertEqual(1, sum(bool(result and result["created"]) for result in results))
        self.assertFalse(self.note()["created"])
        self.assertEqual(1, len(list((self.product / "notes").iterdir())))

    def test_summary_markdown_cannot_inject_note_metadata(self):
        self.make_product()
        result = self.note(summary="A fact.\n\n## Decisions\n- Accepted: invented")
        text = Path(result["path"]).read_text()
        self.assertNotIn("\n## Decisions\n", text)
        self.assertIn(r"\#\# Decisions", text)

    def test_sensitive_fields_are_rejected_without_echo_or_writes(self):
        self.make_product()
        sensitive = [
            "token=private-canary", "Authorization: Bearer private-canary", "Cookie: id=private-canary",
            "-----BEGIN PRIVATE KEY-----", "https://person:private-canary@example.test/path",
            "sk-" + "s" * 32, "eyJ" + "a" * 10 + "." + "b" * 12 + "." + "c" * 12,
            "api%5Fkey%3Dprivate-canary", "token%253Dprivate-canary",
        ]
        for value in sensitive:
            with self.subTest(value=value):
                error = self.assert_code("sensitive_content", self.note, summary=value)
                self.assertNotIn("private-canary", error.message)
        self.assert_code("sensitive_content", self.note, next_action="Use token=private-canary")
        self.assertFalse((self.product / "notes").exists())

    def test_evidence_is_links_only_and_disallows_signed_or_unsafe_urls(self):
        self.make_product()
        for value in ("All tests passed", '{"log":"raw"}', "raw\nlog.md", "javascript:alert(1)",
                      "data:text/plain,raw", "../secret.md", "https://example.test/<payload>", "//server/path.md"):
            with self.subTest(value=value), self.assertRaises(PRODUCT.ProductError):
                self.note(evidence=[value])
        for value in ("https://example.test/report?key=private", "https://example.test/?X-Amz-Signature=private",
                      "https://example.test/?%74oken=private", "https://person:private@example.test/"):
            with self.subTest(value=value):
                self.assert_code("sensitive_content", self.note, evidence=[value])
        self.assertFalse((self.product / "notes").exists())

    def test_raw_payloads_and_oversized_notes_are_rejected(self):
        self.make_product()
        for summary in ('{"event":"raw"}', "```text\nraw log\n```", "line\n" * 21):
            self.assert_code("raw_payload", self.note, summary=summary)
        self.assert_code("invalid_text", self.note, summary="x" * 4_001)
        self.assert_code("invalid_evidence", self.note, evidence="/log.md")
        self.assert_code("invalid_evidence", self.note, evidence=["/log.md"] * 21)
        links = [f"/delivery/{number}/" + "x" * 1_900 + ".md" for number in range(20)]
        self.assert_code("limit_exceeded", self.note, evidence=links)
        self.assertEqual([], list((self.product / "notes").iterdir()))

    def test_standalone_cli_roundtrip_and_exact_product_requirement(self):
        code, result, _ = self.cli("list", "--home", self.home)
        self.assertEqual((0, []), (code, result["result"]))
        self.assertFalse(self.home.exists())
        code, result, _ = self.cli("create", "--home", self.home, "--slug", "sample-product", "--title", "Sample product",
                                   "--purpose", "Useful work.", "--repository", "/repo/platform")
        self.assertEqual(0, code)
        self.assertEqual("sample-product", result["result"]["id"])
        code, result, _ = self.cli("show", self.product)
        self.assertEqual((0, "Sample product"), (code, result["result"]["title"]))
        args = ("note", "--product-root", self.product, "--run-id", "run-cli", "--summary", "A verified outcome.",
                "--evidence", "/delivery/evidence.md", "--next-action", "Review the outcome.")
        first = self.cli(*args)
        second = self.cli(*args)
        self.assertEqual((0, True), (first[0], first[1]["result"]["created"]))
        self.assertEqual((0, False), (second[0], second[1]["result"]["created"]))
        code, result, _ = self.cli("show", self.home)
        self.assertEqual((2, "not_found"), (code, result["error"]["code"]))

    def test_cli_does_not_echo_sensitive_values(self):
        self.make_product()
        code, result, errors = self.cli("note", "--product-root", self.product, "--run-id", "run-cli",
                                        "--summary", "token=private-canary", "--next-action", "Review.")
        self.assertEqual((2, "sensitive_content"), (code, result["error"]["code"]))
        self.assertNotIn("private-canary", json.dumps(result) + errors)

    def test_created_product_accepts_original_markdown_session_binding(self):
        PRODUCT.create_product(self.home, "sample-product", "Sample product", "Useful work.", "/repo/platform")
        spec = importlib.util.spec_from_file_location("delivery_binding_compatibility",
                                                     ROOT / "skills/delivery/internal/product/session_evidence.py")
        assert spec and spec.loader
        evidence = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = evidence
        spec.loader.exec_module(evidence)
        session_id = "01a03832-da77-7552-9ff6-684020e89341"
        sessions = self.product / "sessions"
        sessions.mkdir()
        binding = sessions / f"codex-{session_id}.md"
        binding.write_text(
            "# Session binding\n\n- Schema: product-session-binding/v1\n- Product: sample-product\n"
            f"- Host: codex\n- Session ID: {session_id}\n- Binding: linked\n"
            "- Related work: none\n- Last verified: never\n- Source state: unknown\n"
            "- Coverage state: unknown\n\n## Summary\n\nA reviewed navigation aid.\n"
        )
        native_home = self.root / "native-fixture"
        native = native_home / ".codex/sessions/2026/09/13" / f"rollout-2026-09-13T12-00-00-{session_id}.jsonl"
        native.parent.mkdir(parents=True)
        native.write_text(json.dumps({"type": "session_meta", "payload": {"id": session_id}}) + "\n")
        args = ["inspect", "--host", "codex", "--session-id", session_id, "--product-root", str(self.product)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = evidence.main(args, home=native_home)
        result = json.loads(output.getvalue())
        self.assertEqual((0, "linked", "inspected"), (code, result["binding_mode"], result["outcome"]))
        before = binding.read_bytes()
        self.note()
        self.assertEqual(before, binding.read_bytes())
        self.assertEqual([binding], list(sessions.iterdir()))


if __name__ == "__main__":
    unittest.main()
