"""Vendored native evidence regression suite, using only temporary host storage."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills/delivery"
SCRIPT = SKILL_ROOT / "internal/product/session_evidence.py"
SESSION_ID = "01a03832-da77-7552-9ff6-684020e89341"
OTHER_ID = "01a03949-3e4c-7ed0-884c-d681acd3f31e"

SPEC = importlib.util.spec_from_file_location("product_session_evidence", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
EVIDENCE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = EVIDENCE
SPEC.loader.exec_module(EVIDENCE)


class SessionEvidenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.product = self.root / "products" / "sample-product"
        (self.product / "sessions").mkdir(parents=True)
        (self.product / "product.md").write_text("# Sample product\n")

    def run_cli(self, *args: str) -> tuple[int, dict, str]:
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-B", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        raw = proc.stdout.strip()
        self.assertTrue(raw, msg=f"empty stdout; stderr={proc.stderr!r}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(f"stdout is not one JSON object: {raw!r}; stderr={proc.stderr!r}; {exc}")
        self.assertNotIn(str(self.root), raw)
        return proc.returncode, payload, proc.stderr

    def run_api(
        self,
        args: list[str],
        *,
        limits=None,
        after_read=None,
    ) -> tuple[int, dict]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = EVIDENCE.main(
                args,
                home=self.home,
                limits=limits,
                after_read=after_read,
            )
        raw = output.getvalue().strip()
        self.assertTrue(raw)
        payload = json.loads(raw)
        self.assertNotIn(str(self.root), raw)
        return code, payload

    def binding(
        self,
        host: str,
        session_id: str = SESSION_ID,
        *,
        state: str = "linked",
        product: Path | None = None,
    ) -> Path:
        owner = product or self.product
        path = owner / "sessions" / f"{host}-{session_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(
                [
                    "# Session binding",
                    "",
                    "- Schema: product-session-binding/v1",
                    f"- Product: {owner.name}",
                    f"- Host: {host}",
                    f"- Session ID: {session_id}",
                    f"- Binding: {state}",
                    "- Related work: none",
                    "- Last verified: never",
                    "- Source state: unknown",
                    "- Coverage state: unknown",
                    "",
                    "## Summary",
                    "",
                ]
            )
        )
        return path

    @staticmethod
    def write_jsonl(path: Path, records: list[dict], tail: str = "") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "".join(json.dumps(record) + "\n" for record in records)
        path.write_text(text + tail)

    def codex_source(
        self,
        session_id: str = SESSION_ID,
        *,
        archived: bool = False,
        records: list[dict] | None = None,
        tail: str = "",
    ) -> Path:
        base = self.home / ".codex" / ("archived_sessions" if archived else "sessions/2026/08/26")
        source = base / f"rollout-2026-08-26T10-00-00-{session_id}.jsonl"
        default = [
            {"type": "session_meta", "payload": {"id": session_id}},
            {
                "timestamp": "2026-08-26T10:00:01Z",
                "type": "response_item",
                "payload": {
                    "id": "u1",
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "find translation.key.example"}],
                },
            },
            {
                "timestamp": "2026-08-26T10:00:02Z",
                "type": "response_item",
                "payload": {
                    "id": "a1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Added translation.key.example"}],
                },
            },
        ]
        self.write_jsonl(source, records or default, tail=tail)
        return source

    def claude_source(self, session_id: str = SESSION_ID, records: list[dict] | None = None) -> Path:
        project = self.home / ".claude" / "projects" / "sample-project"
        source = project / f"{session_id}.jsonl"
        default = [
            {
                "type": "user",
                "sessionId": session_id,
                "uuid": "cu1",
                "message": {
                    "id": "cm1",
                    "role": "user",
                    "content": [{"type": "text", "text": "Claude visible needle"}],
                },
            },
            {
                "type": "assistant",
                "sessionId": session_id,
                "uuid": "ca1",
                "message": {
                    "id": "cm2",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Claude answer needle"}],
                },
            },
        ]
        self.write_jsonl(source, records or default)
        (project / "sessions-index.json").write_text(
            json.dumps({"entries": [{"sessionId": session_id, "fullPath": str(source)}]})
        )
        return source

    def grok_source(self, session_id: str = SESSION_ID, records: list[dict] | None = None) -> Path:
        session = self.home / ".grok" / "sessions" / "encoded-workspace" / session_id
        self.write_jsonl(
            session / "events.jsonl",
            [{"type": "turn_started", "session_id": session_id, "ts": "2026-08-26T10:00:00Z"}],
        )
        default = [
            {"type": "system", "content": "hidden system canary"},
            {"type": "user", "content": [{"type": "text", "text": "Grok visible needle"}]},
            {"type": "assistant", "content": "Grok answer needle", "model_id": "grok"},
            {"type": "reasoning", "summary": "hidden reasoning canary", "encrypted_content": "cipher"},
        ]
        self.write_jsonl(session / "chat_history.jsonl", records or default)
        return session

    def scoped_args(self, command: str, host: str, query: str | None = None) -> list[str]:
        args = [
            command,
            "--host",
            host,
            "--session-id",
            SESSION_ID,
            "--product-root",
            str(self.product),
        ]
        if query is not None:
            args += ["--query", query]
        return args

    def test_invalid_uuid_is_rejected_before_source_access(self) -> None:
        code, payload, _ = self.run_cli(
            "inspect",
            "--host",
            "codex",
            "--session-id",
            "../../etc/passwd",
            "--product-root",
            str(self.product),
            "--allow-unbound",
        )
        self.assertEqual(2, code)
        self.assertEqual("invalid_request", payload["code"])
        self.assertEqual("not_read", payload["source_state"])

    def test_both_commands_require_binding_or_explicit_one_time_scope(self) -> None:
        self.codex_source()
        for command in ("inspect", "search"):
            args = self.scoped_args(command, "codex", "needle" if command == "search" else None)
            code, payload, _ = self.run_cli(*args)
            self.assertEqual(5, code)
            self.assertEqual("binding_required", payload["code"])
            code, payload, _ = self.run_cli(*args, "--allow-unbound")
            self.assertIn(code, (0, 3))
            self.assertNotEqual("binding_required", payload["code"])

    def test_linked_binding_allows_inspect_and_emits_no_content(self) -> None:
        self.codex_source()
        self.binding("codex")
        code, payload, raw_err = self.run_cli(*self.scoped_args("inspect", "codex"))
        self.assertEqual(0, code, raw_err)
        self.assertEqual("inspected", payload["outcome"])
        self.assertEqual([], payload["matches"])
        self.assertEqual("linked", payload["binding_mode"])

    def test_explicit_one_time_scope_is_reported_as_unbound(self) -> None:
        self.codex_source()
        code, payload, _ = self.run_cli(
            *self.scoped_args("inspect", "codex"), "--allow-unbound"
        )
        self.assertEqual(0, code)
        self.assertEqual("unbound", payload["binding_mode"])

    def test_revoked_or_malformed_binding_cannot_be_bypassed(self) -> None:
        self.codex_source()
        binding = self.binding("codex", state="revoked")
        code, payload, _ = self.run_cli(
            *self.scoped_args("inspect", "codex"), "--allow-unbound"
        )
        self.assertEqual(5, code)
        self.assertEqual("binding_revoked", payload["code"])
        binding.write_text(
            binding.read_text().replace(
                "- Host: codex\n",
                "- Host: codex\n- Host: codex\n",
                1,
            )
        )
        code, payload, _ = self.run_cli(
            *self.scoped_args("inspect", "codex"), "--allow-unbound"
        )
        self.assertEqual(5, code)
        self.assertEqual("binding_invalid", payload["code"])

    def test_codex_search_uses_first_root_identity_and_exact_session(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Exact root needle"}],
                },
            },
            {"type": "session_meta", "payload": {"id": OTHER_ID}},
        ]
        self.codex_source(records=records)
        self.codex_source(session_id=OTHER_ID)
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "root needle"))
        self.assertEqual(0, code)
        self.assertEqual("supported", payload["outcome"])
        self.assertEqual(SESSION_ID, payload["session_id"])
        self.assertEqual(1, len(payload["matches"]))

    def test_codex_hidden_fields_and_tool_scope(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "developer canary"}],
                },
            },
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "encrypted_content": "reasoning canary"},
            },
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "tool result needle"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": [
                        {"type": "input_text", "text": "array tool needle"},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,image-canary",
                            "detail": "high",
                        },
                    ],
                },
            },
        ]
        self.codex_source(records=records)
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "tool result"))
        self.assertEqual("not_found_in_source", payload["outcome"])
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "codex", "tool result"),
            "--scope",
            "messages-and-tools",
        )
        self.assertEqual("supported", payload["outcome"])
        raw = json.dumps(payload)
        self.assertNotIn("developer canary", raw)
        self.assertNotIn("reasoning canary", raw)
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "codex", "array tool"),
            "--scope",
            "messages-and-tools",
        )
        self.assertEqual("supported", payload["outcome"])
        self.assertNotIn("image-canary", json.dumps(payload))

    def test_unknown_codex_event_subtype_makes_no_match_unverified(self) -> None:
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {
                "type": "event_msg",
                "payload": {"type": "future_visible_event", "message": "unknown canary"},
            },
        ]
        self.codex_source(records=records)
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "absent"))
        self.assertEqual(3, code)
        self.assertEqual("unverified", payload["outcome"])
        self.assertEqual("partial_parse", payload["coverage"]["state"])
        self.assertGreater(payload["coverage"]["unknown_records"], 0)
        self.assertNotIn("unknown canary", json.dumps(payload))

    def test_claude_primary_messages_and_subagents_exclusion(self) -> None:
        self.claude_source()
        subagent = self.home / ".claude" / "projects" / "sample-project" / "subagents" / f"{SESSION_ID}.jsonl"
        self.write_jsonl(
            subagent,
            [{"type": "assistant", "sessionId": SESSION_ID, "message": {"role": "assistant", "content": "subagent canary"}}],
        )
        self.binding("claude")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "claude", "Claude answer"))
        self.assertEqual(0, code)
        self.assertEqual("supported", payload["outcome"])
        self.assertNotIn("subagent canary", json.dumps(payload))

    def test_claude_conflicting_authoritative_session_id_discards_matches(self) -> None:
        records = [
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "uuid": "one",
                "message": {"id": "m", "role": "assistant", "content": [{"type": "text", "text": "buffered needle"}]},
            },
            {
                "type": "assistant",
                "sessionId": OTHER_ID,
                "uuid": "two",
                "message": {"id": "m2", "role": "assistant", "content": [{"type": "text", "text": "foreign"}]},
            },
        ]
        self.claude_source(records=records)
        self.binding("claude")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "claude", "buffered"))
        self.assertEqual(5, code)
        self.assertEqual("identity_mismatch", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_claude_latest_message_revision_replaces_or_removes_match(self) -> None:
        records = [
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "uuid": "one",
                "message": {
                    "id": "streaming-message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "transient needle"}],
                },
            },
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "uuid": "two",
                "message": {
                    "id": "streaming-message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "final answer"}],
                },
            },
        ]
        self.claude_source(records=records)
        self.binding("claude")
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "claude", "transient needle")
        )
        self.assertEqual(0, code)
        self.assertEqual("not_found_in_source", payload["outcome"])
        self.assertEqual([], payload["matches"])

    def test_claude_hidden_blocks_and_symlink_candidate_fail_closed(self) -> None:
        records = [
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "message": {
                    "id": "hidden",
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "private canary"},
                        {"type": "tool_use", "input": {"secret": "argument canary"}},
                        {"type": "text", "text": "visible answer"},
                    ],
                },
            }
        ]
        source = self.claude_source(records=records)
        self.binding("claude")
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "claude", "visible answer")
        )
        self.assertEqual(0, code)
        raw = json.dumps(payload)
        self.assertNotIn("private canary", raw)
        self.assertNotIn("argument canary", raw)

        target = source.with_name("real-session.jsonl")
        source.replace(target)
        source.symlink_to(target)
        code, payload, _ = self.run_cli(*self.scoped_args("inspect", "claude"))
        self.assertEqual(5, code)
        self.assertEqual("unsupported_format", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_claude_nested_role_mismatch_is_never_searchable(self) -> None:
        records = [
            {
                "type": "assistant",
                "sessionId": SESSION_ID,
                "message": {
                    "id": "role-mismatch",
                    "role": "system",
                    "content": [{"type": "text", "text": "role mismatch canary"}],
                },
            }
        ]
        self.claude_source(records=records)
        self.binding("claude")
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "claude", "role mismatch")
        )
        self.assertEqual(3, code)
        self.assertEqual("unverified", payload["outcome"])
        self.assertEqual("partial_parse", payload["coverage"]["state"])
        self.assertEqual([], payload["matches"])
        self.assertNotIn("role mismatch canary", json.dumps(payload))

    def test_grok_messages_require_matching_event_identity(self) -> None:
        self.grok_source()
        self.binding("grok")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "grok", "Grok answer"))
        self.assertEqual(0, code)
        self.assertEqual("supported", payload["outcome"])
        raw = json.dumps(payload)
        self.assertNotIn("hidden system canary", raw)
        self.assertNotIn("hidden reasoning canary", raw)

    def test_grok_missing_event_identity_exposes_no_content(self) -> None:
        session = self.grok_source()
        (session / "events.jsonl").write_text(json.dumps({"type": "phase_changed"}) + "\n")
        self.binding("grok")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "grok", "needle"))
        self.assertEqual(5, code)
        self.assertIn(payload["code"], {"identity_mismatch", "unsupported_format"})
        self.assertEqual([], payload["matches"])

    def test_resolution_to_open_replacement_exposes_no_grok_content(self) -> None:
        self.grok_source()
        self.binding("grok")
        original = EVIDENCE.resolve_candidate

        def replace_after_resolution(home, host, session_id, budget):
            candidate, count = original(home, host, session_id, budget)
            replacement = candidate.message_path.with_suffix(".replacement")
            self.write_jsonl(
                replacement,
                [{"type": "assistant", "content": "replacement needle"}],
            )
            os.replace(replacement, candidate.message_path)
            return candidate, count

        EVIDENCE.resolve_candidate = replace_after_resolution
        try:
            code, payload = self.run_api(
                self.scoped_args("search", "grok", "replacement needle")
            )
        finally:
            EVIDENCE.resolve_candidate = original
        self.assertEqual(4, code)
        self.assertEqual("source_unavailable", payload["code"])
        self.assertEqual("replaced", payload["source_state"])
        self.assertEqual([], payload["matches"])

    def test_grok_identity_guard_rejects_post_resolution_append(self) -> None:
        session = self.grok_source()
        self.binding("grok")
        events = session / "events.jsonl"
        original = EVIDENCE.resolve_candidate

        def append_after_resolution(home, host, session_id, budget):
            candidate, count = original(home, host, session_id, budget)
            with events.open("a") as stream:
                stream.write(
                    json.dumps(
                        {"type": "turn_started", "session_id": OTHER_ID}
                    )
                    + "\n"
                )
            return candidate, count

        EVIDENCE.resolve_candidate = append_after_resolution
        try:
            code, payload = self.run_api(
                self.scoped_args("search", "grok", "Grok answer")
            )
        finally:
            EVIDENCE.resolve_candidate = original
        self.assertEqual(3, code)
        self.assertEqual("resolution_truncated", payload["code"])
        self.assertEqual("appended", payload["source_state"])
        self.assertEqual([], payload["matches"])

    def test_distinct_duplicate_codex_sources_are_ambiguous(self) -> None:
        self.codex_source()
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "different"}]}},
        ]
        self.codex_source(archived=True, records=records)
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("inspect", "codex"))
        self.assertEqual(5, code)
        self.assertEqual("ambiguous_source", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_unterminated_tail_makes_no_match_unverified(self) -> None:
        self.codex_source(tail='{"type":"response_item"')
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "absent text"))
        self.assertEqual(3, code)
        self.assertEqual("unverified", payload["outcome"])
        self.assertEqual("partial_parse", payload["coverage"]["state"])

    def test_malformed_complete_record_is_partial_and_never_echoed(self) -> None:
        source = self.codex_source()
        with source.open("a") as stream:
            stream.write("{malformed secret canary}\n")
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "missing"))
        self.assertEqual(3, code)
        self.assertEqual("unverified", payload["outcome"])
        self.assertNotIn("malformed secret canary", json.dumps(payload))

    def test_malformed_first_codex_record_after_resolution_fails_identity(self) -> None:
        self.codex_source()
        self.binding("codex")
        original = EVIDENCE.resolve_candidate

        def rewrite_after_resolution(home, host, session_id, budget):
            candidate, count = original(home, host, session_id, budget)
            candidate.message_path.write_text(
                "malformed\n"
                + json.dumps({"type": "session_meta", "payload": {"id": SESSION_ID}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "race needle"}
                            ],
                        },
                    }
                )
                + "\n"
            )
            return candidate, count

        EVIDENCE.resolve_candidate = rewrite_after_resolution
        try:
            code, payload = self.run_api(
                self.scoped_args("search", "codex", "race needle")
            )
        finally:
            EVIDENCE.resolve_candidate = original
        self.assertEqual(5, code)
        self.assertEqual("unsupported_format", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_candidate_uses_inode_from_the_validating_identity_read(self) -> None:
        self.codex_source()
        self.binding("codex")
        original = EVIDENCE._validate_codex_identity

        def replace_after_validation(path, session_id, budget):
            revision = original(path, session_id, budget)
            replacement = path.with_suffix(".replacement")
            self.write_jsonl(
                replacement,
                [
                    {"type": "session_meta", "payload": {"id": session_id}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "replacement canary"}
                            ],
                        },
                    },
                ],
            )
            os.replace(replacement, path)
            return revision

        EVIDENCE._validate_codex_identity = replace_after_validation
        try:
            code, payload = self.run_api(self.scoped_args("inspect", "codex"))
        finally:
            EVIDENCE._validate_codex_identity = original
        self.assertEqual(4, code)
        self.assertEqual("source_unavailable", payload["code"])
        self.assertEqual("replaced", payload["source_state"])
        self.assertEqual([], payload["matches"])
        self.assertNotIn("replacement canary", json.dumps(payload))

    def test_sensitive_match_returns_marker_without_value(self) -> None:
        planted = "sk-" + ("z" * 32)
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"credential token={planted}"}]}},
        ]
        self.codex_source(records=records)
        self.binding("codex")
        # Search for a non-secret-shaped literal inside the detected secret span.  A
        # complete secret-shaped query is rejected before traversal by a separate test.
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "zzzzzzzz"))
        self.assertEqual(0, code)
        self.assertEqual("sensitive_match", payload["code"])
        raw = json.dumps(payload)
        self.assertIn("[REDACTED:SENSITIVE_MATCH]", raw)
        self.assertNotIn(planted, raw)

    def test_sensitive_values_are_redacted_before_excerpt_slicing(self) -> None:
        fixtures = [
            ("Authorization: Bearer alpha-secret-canary", "secret-canary"),
            ("Cookie: sid=cookie-secret-canary", "cookie-secret"),
            ("https://user:password-secret-canary@example.test/path", "password-secret"),
            ("token=token-secret-canary", "token-secret"),
            (
                "-----BEGIN PRIVATE KEY-----\nprivate-secret-canary\n-----END PRIVATE KEY-----",
                "private-secret",
            ),
        ]
        self.binding("codex")
        for index, (value, query) in enumerate(fixtures):
            with self.subTest(index=index):
                records = [
                    {"type": "session_meta", "payload": {"id": SESSION_ID}},
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": value}],
                        },
                    },
                ]
                self.codex_source(records=records)
                code, payload, _ = self.run_cli(
                    *self.scoped_args("search", "codex", query)
                )
                self.assertEqual(0, code)
                self.assertEqual("sensitive_match", payload["code"])
                raw = json.dumps(payload)
                self.assertIn("[REDACTED:SENSITIVE_MATCH]", raw)
                self.assertNotIn("secret-canary", raw)

    def test_sensitive_span_crossing_reader_boundary_is_never_split(self) -> None:
        value = ("a" * 65_520) + " token=boundary-secret-canary"
        records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": value}],
                },
            },
        ]
        self.codex_source(records=records)
        self.binding("codex")
        code, payload, _ = self.run_cli(
            *self.scoped_args("search", "codex", "boundary-secret")
        )
        self.assertEqual(0, code)
        self.assertEqual("sensitive_match", payload["code"])
        raw = json.dumps(payload)
        self.assertIn("[REDACTED:SENSITIVE_MATCH]", raw)
        self.assertNotIn("boundary-secret-canary", raw)

    def test_json_inline_header_and_tool_secrets_are_sanitized(self) -> None:
        fixtures = [
            ('{"token": "json-secret-canary"}', "token", "messages"),
            (
                '{"authorization": "Bearer json-auth-secret-canary"}',
                "authorization",
                "messages",
            ),
            (
                "curl -H 'Authorization: Bearer shell-secret-canary' https://example.test",
                "curl",
                "messages",
            ),
            ('{"token": "tool-secret-canary"}', "token", "messages-and-tools"),
        ]
        self.binding("codex")
        for index, (value, query, scope) in enumerate(fixtures):
            with self.subTest(index=index):
                payload = (
                    {
                        "type": "function_call_output",
                        "output": value,
                    }
                    if scope == "messages-and-tools"
                    else {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": value}],
                    }
                )
                self.codex_source(
                    records=[
                        {"type": "session_meta", "payload": {"id": SESSION_ID}},
                        {"type": "response_item", "payload": payload},
                    ]
                )
                args = self.scoped_args("search", "codex", query)
                if scope == "messages-and-tools":
                    args += ["--scope", scope]
                code, result, _ = self.run_cli(*args)
                self.assertEqual(0, code)
                raw = json.dumps(result)
                self.assertIn("[REDACTED]", raw)
                self.assertNotIn("secret-canary", raw)

    def test_secret_shaped_query_is_rejected_before_traversal(self) -> None:
        planted = "sk-" + ("q" * 32)
        self.codex_source()
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", planted))
        self.assertEqual(2, code)
        self.assertEqual("invalid_request", payload["code"])
        self.assertNotIn(planted, json.dumps(payload))

    def test_cross_product_without_binding_is_denied(self) -> None:
        self.codex_source()
        self.binding("codex")
        other = self.root / "products" / "other-product"
        (other / "sessions").mkdir(parents=True)
        (other / "product.md").write_text("# Other\n")
        args = self.scoped_args("search", "codex", "needle")
        args[args.index(str(self.product))] = str(other)
        code, payload, _ = self.run_cli(*args)
        self.assertEqual(5, code)
        self.assertEqual("binding_required", payload["code"])

    def test_binding_product_mismatch_and_symlink_are_invalid(self) -> None:
        self.codex_source()
        binding = self.binding("codex")
        binding.write_text(binding.read_text().replace("Product: sample-product", "Product: other"))
        code, payload, _ = self.run_cli(*self.scoped_args("inspect", "codex"))
        self.assertEqual(5, code)
        self.assertEqual("binding_invalid", payload["code"])

        binding.unlink()
        target = self.product / "real-binding.md"
        target.write_text("not a binding")
        binding.symlink_to(target)
        code, payload, _ = self.run_cli(
            *self.scoped_args("inspect", "codex"), "--allow-unbound"
        )
        self.assertEqual(5, code)
        self.assertEqual("binding_invalid", payload["code"])

    def test_binding_growth_during_bounded_read_fails_closed(self) -> None:
        self.codex_source()
        binding = self.binding("codex")
        original = EVIDENCE.os.read
        injected = False

        def grow_before_first_read(descriptor, amount):
            nonlocal injected
            if not injected:
                injected = True
                with binding.open("a") as stream:
                    stream.write("x" * (EVIDENCE.Limits().binding_bytes + 1))
            return original(descriptor, amount)

        EVIDENCE.os.read = grow_before_first_read
        try:
            code, payload = self.run_api(self.scoped_args("inspect", "codex"))
        finally:
            EVIDENCE.os.read = original
        self.assertTrue(injected)
        self.assertEqual(5, code)
        self.assertEqual("binding_invalid", payload["code"])
        self.assertEqual("not_read", payload["source_state"])
        self.assertEqual([], payload["matches"])

    def test_unbound_scope_cannot_bypass_a_symlink_sessions_directory(self) -> None:
        self.codex_source()
        (self.product / "sessions").rmdir()
        for target in (self.root / "missing-sessions", self.home):
            with self.subTest(target=target):
                (self.product / "sessions").symlink_to(target, target_is_directory=True)
                code, payload, _ = self.run_cli(*self.scoped_args("inspect", "codex"), "--allow-unbound")
                self.assertEqual(5, code)
                self.assertEqual("binding_invalid", payload["code"])
                self.assertEqual("not_read", payload["source_state"])
                (self.product / "sessions").unlink()

    def test_query_length_limit_is_enforced(self) -> None:
        self.codex_source()
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "x" * 513))
        self.assertEqual(2, code)
        self.assertEqual("invalid_request", payload["code"])

    def test_match_limit_is_reported_as_truncated_coverage(self) -> None:
        records = [{"type": "session_meta", "payload": {"id": SESSION_ID}}]
        records.extend(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": f"needle {index}"}],
                },
            }
            for index in range(22)
        )
        self.codex_source(records=records)
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "needle"))
        self.assertEqual(3, code)
        self.assertEqual("supported", payload["outcome"])
        self.assertEqual(20, len(payload["matches"]))
        self.assertEqual("limit_truncated", payload["coverage"]["state"])
        self.assertIn("matches", payload["coverage"]["limit_reasons"])

    def test_record_field_and_resolution_bounds_fail_closed(self) -> None:
        self.codex_source()
        self.binding("codex")

        code, payload = self.run_api(
            self.scoped_args("search", "codex", "translation.key"),
            limits=EVIDENCE.Limits(search_records=1),
        )
        self.assertEqual(3, code)
        self.assertEqual("unverified", payload["outcome"])
        self.assertIn("records", payload["coverage"]["limit_reasons"])
        self.assertEqual([], payload["matches"])

        code, payload = self.run_api(
            self.scoped_args("search", "codex", "translation.key"),
            limits=EVIDENCE.Limits(field_bytes=8),
        )
        self.assertEqual(3, code)
        self.assertEqual("partial_parse", payload["coverage"]["state"])
        self.assertGreater(payload["coverage"]["oversized_fields"], 0)
        self.assertEqual([], payload["matches"])

        code, payload = self.run_api(
            self.scoped_args("inspect", "codex"),
            limits=EVIDENCE.Limits(resolution_entries=0),
        )
        self.assertEqual(3, code)
        self.assertEqual("resolution_truncated", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_auxiliary_identity_bound_returns_no_fragments(self) -> None:
        self.codex_source()
        self.binding("codex")
        code, payload = self.run_api(
            self.scoped_args("search", "codex", "translation.key"),
            limits=EVIDENCE.Limits(auxiliary_file_records=0),
        )
        self.assertEqual(3, code)
        self.assertEqual("resolution_truncated", payload["code"])
        self.assertEqual([], payload["matches"])

    def test_auxiliary_byte_time_and_record_size_bounds_return_no_fragments(self) -> None:
        self.codex_source()
        self.binding("codex")
        limits = (
            EVIDENCE.Limits(auxiliary_file_bytes=1),
            EVIDENCE.Limits(auxiliary_file_seconds=-1.0),
            EVIDENCE.Limits(auxiliary_record_bytes=1),
        )
        for value in limits:
            with self.subTest(limits=value):
                code, payload = self.run_api(
                    self.scoped_args("search", "codex", "translation.key"),
                    limits=value,
                )
                self.assertEqual(3, code)
                self.assertEqual("resolution_truncated", payload["code"])
                self.assertEqual([], payload["matches"])

    def test_claude_over_limit_index_is_ignored_as_hint(self) -> None:
        source = self.claude_source()
        index = source.parent / "sessions-index.json"
        index.write_text(
            json.dumps(
                {
                    "entries": [
                        {"sessionId": SESSION_ID, "fullPath": str(source)},
                        {"sessionId": SESSION_ID, "fullPath": "/outside/untrusted"},
                    ]
                }
            )
        )
        self.binding("claude")
        code, payload = self.run_api(
            self.scoped_args("search", "claude", "Claude answer"),
            limits=EVIDENCE.Limits(auxiliary_file_records=1),
        )
        self.assertEqual(0, code)
        self.assertEqual("supported", payload["outcome"])

    def test_main_byte_time_and_record_size_bounds_are_explicit(self) -> None:
        source = self.codex_source()
        self.binding("codex")
        first_line_size = len(source.read_bytes().splitlines(keepends=True)[0])

        for reason, limits in (
            ("bytes", EVIDENCE.Limits(search_bytes=first_line_size - 1)),
            ("time", EVIDENCE.Limits(search_seconds=-1.0)),
        ):
            with self.subTest(reason=reason):
                code, payload = self.run_api(
                    self.scoped_args("search", "codex", "translation.key"),
                    limits=limits,
                )
                self.assertEqual(3, code)
                self.assertEqual("unverified", payload["outcome"])
                self.assertEqual("limit_truncated", payload["coverage"]["state"])
                self.assertIn(reason, payload["coverage"]["limit_reasons"])
                self.assertEqual([], payload["matches"])

        large_records = [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "x" * 2_000}],
                },
            },
        ]
        self.codex_source(records=large_records)
        code, payload = self.run_api(
            self.scoped_args("search", "codex", "missing"),
            limits=EVIDENCE.Limits(search_record_bytes=512),
        )
        self.assertEqual(3, code)
        self.assertEqual("partial_parse", payload["coverage"]["state"])
        self.assertGreater(payload["coverage"]["oversized_records"], 0)
        self.assertEqual([], payload["matches"])

    def test_append_truncate_replace_and_modify_are_reported(self) -> None:
        self.binding("codex")

        def append(path: Path) -> None:
            with path.open("a") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {}}) + "\n")

        def truncate(path: Path) -> None:
            path.write_bytes(path.read_bytes()[:10])

        def replace(path: Path) -> None:
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(path.read_bytes())
            os.replace(replacement, path)

        def modify(path: Path) -> None:
            data = bytearray(path.read_bytes())
            data[-2] = ord(" ") if data[-2] != ord(" ") else ord("x")
            path.write_bytes(bytes(data))
            stat_result = path.stat()
            os.utime(
                path,
                ns=(stat_result.st_atime_ns, stat_result.st_mtime_ns + 1_000_000_000),
            )

        for expected, callback in (
            ("appended", append),
            ("truncated", truncate),
            ("replaced", replace),
            ("modified", modify),
        ):
            with self.subTest(state=expected):
                self.codex_source()
                code, payload = self.run_api(
                    self.scoped_args("search", "codex", "translation.key"),
                    after_read=callback,
                )
                self.assertEqual(3, code)
                self.assertEqual("supported", payload["outcome"])
                self.assertEqual(expected, payload["source_state"])

    def test_output_schema_has_orthogonal_state_and_coverage(self) -> None:
        self.codex_source()
        self.binding("codex")
        code, payload, _ = self.run_cli(*self.scoped_args("search", "codex", "translation.key"))
        self.assertEqual(0, code)
        expected = {
            "schema_version",
            "command",
            "status",
            "code",
            "outcome",
            "host",
            "session_id",
            "observed_at",
            "binding_mode",
            "source_state",
            "revision",
            "sources",
            "coverage",
            "matches",
            "diagnostics",
        }
        self.assertTrue(expected.issubset(payload))
        self.assertIn(payload["coverage"]["state"], {"complete", "partial_parse", "limit_truncated"})
        self.assertNotIn("translation.key", payload.get("query", ""))


if __name__ == "__main__":
    unittest.main()
