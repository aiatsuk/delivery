#!/usr/bin/env python3
"""Explicit Markdown product memory. This module never opens native sessions.

Read APIs return JSON-compatible values and do not change the workspace. Write APIs
create new artifacts only. A note is not an approval or a current external status.
The calling run engine owns authorization and immutable product/work-item binding.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit


MARKDOWN_BYTES = 256 * 1024
NOTE_BYTES = 16 * 1024
MAX_ENTRIES = 10_000
MAX_PRODUCTS = 1_000
MAX_EVIDENCE = 20
SLUG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
NOTE_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
LINK = re.compile(r"(?<!!)\[([^\]\n]{0,200})\]\(\s*(?:<([^>\n]+)>|([^\s()]+))(?:\s+\"[^\"\n]*\")?\s*\)")
SECRET = re.compile(
    r"(?i)-----BEGIN (?:ENCRYPTED |RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:proxy-)?authorization\b[\"']?\s*:\s*\S|"
    r"\b(?:set-)?cookie\b[\"']?\s*:\s*\S|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|passwd|secret|token)\b[\"']?\s*(?:=|:)\s*\S|"
    r"--(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
    r"password|secret|token)(?:=|\s+)\S|"
    r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@|"
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|"
    r"github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,}|AKIA[0-9A-Z]{16})\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)


class ProductError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _path(value, label="path") -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise ProductError("invalid_path", f"An explicit {label} is required.") from exc
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise ProductError("invalid_path", f"An explicit {label} is required.")
    path = Path(raw).expanduser()
    if ".." in path.parts:
        raise ProductError("unsafe_path", "Parent traversal is not allowed.")
    return path


def product_home(override=None) -> Path:
    """Resolve configuration only; no directory creation or product selection."""
    value = override if override is not None else os.environ.get("PRODUCT_MEMORY_HOME")
    if value is None:
        value = Path.home() / ".local/share/product-driven-development/products"
    return Path(os.path.abspath(_path(value, "product home")))


def _flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@contextmanager
def _directory(path):
    requested = _path(path)
    try:
        info = requested.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ProductError("unsafe_path", "The selected directory must not be a symlink.")
        root = requested.resolve(strict=True)
        fd = os.open(root, _flags())
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(fd)
            raise ProductError("changed_path", "The selected directory changed during access.")
    except FileNotFoundError as exc:
        raise ProductError("not_found", "The selected directory does not exist.") from exc
    except OSError as exc:
        raise ProductError("unsafe_path", "The selected directory could not be opened safely.") from exc
    try:
        yield root, fd
    finally:
        os.close(fd)


@contextmanager
def _child_directory(parent_fd: int, parts, *, create=False):
    fd = os.dup(parent_fd)
    try:
        for part in parts:
            if part in {"", ".", ".."} or "/" in part or "\x00" in part:
                raise ProductError("unsafe_path", "The child directory path is invalid.")
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd
    except FileNotFoundError as exc:
        raise ProductError("not_found", "The selected directory does not exist.") from exc
    except OSError as exc:
        raise ProductError("unsafe_path", "A child directory is inaccessible or is a symlink.") from exc
    finally:
        os.close(fd)


def _read(fd: int, name: str, *, optional=False, limit=MARKDOWN_BYTES):
    descriptor = None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        start = os.fstat(descriptor)
        if not stat.S_ISREG(start.st_mode) or start.st_size > limit:
            raise ProductError("invalid_markdown", "A Markdown document is not a bounded regular file.")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        end = os.fstat(descriptor)
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if len(data) > limit or (start.st_size, start.st_mtime_ns) != (end.st_size, end.st_mtime_ns):
            raise ProductError("changed_markdown", "A Markdown document changed or exceeded its size limit.")
        if (start.st_dev, start.st_ino) != (current.st_dev, current.st_ino):
            raise ProductError("changed_markdown", "A Markdown document changed during access.")
        text = bytes(data).decode("utf-8", errors="strict")
        if "\x00" in text:
            raise ProductError("invalid_markdown", "A Markdown document contains invalid text.")
        return text
    except FileNotFoundError as exc:
        if optional and descriptor is None:
            return None
        raise ProductError("not_found", "The requested Markdown document is missing.") from exc
    except UnicodeError as exc:
        raise ProductError("invalid_markdown", "Markdown documents must contain UTF-8 text.") from exc
    except OSError as exc:
        raise ProductError("unsafe_path", "A Markdown document could not be read safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _metadata(root: Path, markdown: str) -> dict:
    title = re.search(r"^# ([^\n]+)", markdown, re.MULTILINE)
    purpose = re.search(r"^## Purpose\s*\n(.*?)(?=^## |\Z)", markdown, re.MULTILINE | re.DOTALL)
    return {
        "id": root.name,
        "root": str(root),
        "title": title.group(1).strip()[:200] if title else root.name,
        "purpose": purpose.group(1).strip()[:2_000] if purpose else "",
        "product_file": str(root / "product.md"),
    }


def _links(root: Path, source: str, markdown: str) -> list[dict]:
    """Extract navigation references only. Never stat, resolve, fetch, or follow them."""
    links = []
    for match in LINK.finditer(markdown):
        target = match.group(2) or match.group(3)
        try:
            parsed = urlsplit(target)
        except ValueError:
            continue
        item = {"source": source, "label": match.group(1), "target": target, "followed": False}
        if parsed.scheme or target.startswith("//"):
            item["kind"] = "uri"
        elif not parsed.path:
            item["kind"] = "anchor"
        else:
            item["kind"] = "path"
            item["path"] = os.path.abspath(root / unquote(parsed.path))
        links.append(item)
    return links


def list_products(home) -> list[dict]:
    """Discover direct child product.md maps. Legacy JSON and session files are ignored."""
    location = product_home(home)
    if not location.exists() and not location.is_symlink():
        return []
    result = []
    with _directory(location) as (root, fd):
        with os.scandir(fd) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_ENTRIES:
                    raise ProductError("limit_exceeded", "Product discovery exceeded its directory limit.")
                if not entry.is_dir(follow_symlinks=False) or entry.name.startswith("."):
                    continue
                with _child_directory(fd, (entry.name,)) as child_fd:
                    markdown = _read(child_fd, "product.md", optional=True)
                if markdown is None:
                    continue
                result.append(_metadata(root / entry.name, markdown))
                if len(result) > MAX_PRODUCTS:
                    raise ProductError("limit_exceeded", "Product discovery exceeded its product limit.")
    return sorted(result, key=lambda product: product["id"])


def load_product(path) -> dict:
    """Load only the explicitly supplied product directory (or its product.md)."""
    requested = _path(path, "product path")
    if requested.name == "product.md":
        requested = requested.parent
    with _directory(requested) as (root, fd):
        markdown = _read(fd, "product.md")
        memory = _read(fd, "memory.md", optional=True)
    result = _metadata(root, markdown)
    result.update({
        "memory_file": str(root / "memory.md") if memory is not None else None,
        "product_markdown": markdown,
        "memory_markdown": memory,
        "links": _links(root, "product.md", markdown) + _links(root, "memory.md", memory or ""),
    })
    return result


def _item_relative(root: Path, requested_root, item_path) -> Path:
    item = _path(item_path, "work item")
    if item.is_absolute():
        for base in (root, Path(os.path.abspath(_path(requested_root)))):
            try:
                return item.relative_to(base)
            except ValueError:
                pass
        raise ProductError("outside_product", "The work item must belong to the selected product.")
    return item


def _work_item(root: Path, fd: int, requested_root, item_path) -> dict:
    relative = _item_relative(root, requested_root, item_path)
    expected = relative.name if relative.name in {"project.md", "ticket.md"} else None
    if relative.suffix == ".json" and expected is None:
        raise ProductError("invalid_work_item", "Select a directory, project.md, or ticket.md; JSON is not a work item.")
    owner = relative.parent if expected else relative
    with _child_directory(fd, owner.parts) as owner_fd:
        found = []
        for name in ("project.md", "ticket.md"):
            try:
                os.stat(name, dir_fd=owner_fd, follow_symlinks=False)
                found.append(name)
            except FileNotFoundError:
                pass
        if len(found) != 1:
            raise ProductError("ambiguous_work_item" if found else "missing_work_item",
                               "A work item must contain exactly one project.md or ticket.md.")
        if expected and expected != found[0]:
            raise ProductError("invalid_work_item", "The selected Markdown work-item document is missing.")
        markdown = _read(owner_fd, found[0])
    title = re.search(r"^# ([^\n]+)", markdown, re.MULTILINE)
    path = owner / found[0]
    return {
        "id": (root / owner).name,
        "kind": Path(found[0]).stem,
        "root": str(root / owner),
        "path": str(root / path),
        "relative_path": path.as_posix(),
        "title": title.group(1).strip()[:200] if title else (root / owner).name,
        "markdown": markdown,
    }


def validate_work_item(product_root, item_path) -> dict:
    with _directory(product_root) as (root, fd):
        _read(fd, "product.md")
        return _work_item(root, fd, product_root, item_path)


def _text(value, label, maximum, *, multiline=False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ProductError("invalid_text", f"{label} must contain 1 to {maximum} characters.")
    text = value.strip()
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in text):
        raise ProductError("invalid_text", f"{label} contains control characters.")
    if not multiline and ("\n" in text or "\t" in text):
        raise ProductError("invalid_text", f"{label} must be one line.")
    decoded = text
    for _ in range(3):
        if SECRET.search(decoded):
            raise ProductError("sensitive_content", "Do not store credentials or secret-shaped values in product memory.")
        decoded = unquote(decoded)
    if "```" in text or "~~~" in text or text.startswith(("{", "[{")):
        raise ProductError("raw_payload", "Store a reviewed prose outcome and links, not raw logs or payloads.")
    if len(text.splitlines()) > 20:
        raise ProductError("raw_payload", "A milestone must be a short reviewed summary.")
    return text


def _link(value, *, label="Evidence") -> str:
    text = _text(value, label, 2_048)
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.scheme not in {"http", "https", "ssh", "git", "file"}:
            raise ValueError
        if text.startswith("//") or any(char in text for char in "<>`"):
            raise ValueError
        if parsed.username or parsed.password:
            raise ValueError
        if parsed.scheme in {"http", "https", "ssh", "git"} and not parsed.hostname:
            raise ValueError
        if parsed.scheme and any(char.isspace() for char in text):
            raise ValueError
        keys = {key.lower().replace("_", "-") for key, _ in parse_qsl(parsed.query)}
        if keys & {"key", "token", "access-token", "api-key", "secret", "password", "auth", "code",
                   "signature", "x-amz-signature", "x-amz-credential", "x-goog-signature", "x-goog-credential"}:
            raise ProductError("sensitive_content", "Evidence URLs must not carry credentials or signed access values.")
        if not parsed.scheme:
            decoded_path = unquote(parsed.path)
            if ".." in Path(decoded_path).parts or not decoded_path:
                raise ValueError
            if not ("/" in decoded_path or Path(decoded_path).suffix):
                raise ValueError
    except ValueError as exc:
        raise ProductError("invalid_evidence", f"{label} must be a local artifact path or an ordinary URL.") from exc
    return text


def _plain(text: str) -> str:
    return re.sub(r"([\\`*_{}\[\]<>#!|])", r"\\\1", text)


def _sync_directory(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
            raise


def _publish(fd: int, name: str, text: str) -> tuple[int, int]:
    """Publish a completely written private file with an atomic no-clobber link."""
    temporary = f".delivery-{uuid.uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        data = memoryview(text.encode("utf-8"))
        while data:
            written = os.write(descriptor, data)
            if written <= 0:
                raise OSError("incomplete write")
            data = data[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        os.link(temporary, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        _sync_directory(fd)
        return info.st_dev, info.st_ino
    except FileExistsError as exc:
        raise ProductError("already_exists", "The destination already exists and was not overwritten.") from exc
    except OSError as exc:
        raise ProductError("write_failed", "The artifact could not be published safely.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=fd)
            except FileNotFoundError:
                pass


def create_product(home, slug, title, purpose, repository) -> dict:
    if not isinstance(slug, str) or not SLUG.fullmatch(slug):
        raise ProductError("invalid_id", "Product IDs use 1 to 64 lowercase letters, digits, and internal hyphens.")
    title = _text(title, "Title", 200)
    purpose = _text(purpose, "Purpose", 2_000, multiline=True)
    repository = _link(repository, label="Repository")
    location = product_home(home)
    markdown = (f"# {_plain(title)}\n\n## Purpose\n\n{_plain(purpose)}\n\n"
                f"## Product map\n\n- Product ID: {slug}\n\n"
                f"## Repositories and services\n\n- [Repository](<{repository}>)\n\n"
                "## Authoritative links\n\n## Active work\n")
    memory = "# Product memory\n\nRecord compact, reviewed facts here. Link to detailed work-item notes.\n"
    try:
        location.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise ProductError("write_failed", "The product home could not be created.") from exc
    with _directory(location) as (root, fd):
        try:
            os.mkdir(slug, 0o700, dir_fd=fd)
        except FileExistsError as exc:
            raise ProductError("already_exists", "The product path already exists and was not changed.") from exc
        except OSError as exc:
            raise ProductError("write_failed", "The product directory could not be created.") from exc
        written = []
        try:
            with _child_directory(fd, (slug,)) as product_fd:
                try:
                    for name, contents in (("product.md", markdown), ("memory.md", memory)):
                        identity = _publish(product_fd, name, contents)
                        written.append((name, identity))
                except Exception:
                    for name, identity in written:
                        current = os.stat(name, dir_fd=product_fd, follow_symlinks=False)
                        if (current.st_dev, current.st_ino) == identity:
                            os.unlink(name, dir_fd=product_fd)
                    raise
        except Exception:
            try:
                os.rmdir(slug, dir_fd=fd)
            except OSError:
                pass
            raise
        _sync_directory(fd)
    result = load_product(root / slug)
    result["created"] = True
    return result


def _note_text(root, run_id, summary, evidence, next_action, owner, work_path, date, digest):
    metadata = (f"# Delivery outcome\n\n- Date: {date}\n- Product: {_plain(root.name)}\n"
                f"- Owner: {_plain(owner)}\n- Kind: result\n- Run ID: {run_id}\n"
                "- Authority: memory only; this note grants no approval.\n"
                f"\n<!-- delivery-outcome/v1 digest={digest} -->\n")
    if work_path:
        metadata += f"\n## Context\n\nWork item: {_plain(work_path)} (relative to the product root).\n"
    result = metadata + f"\n## Result\n\n{_plain(summary)}\n"
    if evidence:
        result += "\n## Evidence and links\n\n"
        result += "".join(f"- [Evidence {number}](<{link}>)\n" for number, link in enumerate(evidence, 1))
    return result + f"\n## Next action\n\n{_plain(next_action)}\n"


def record_outcome(product_root, run_id, summary, evidence: list[str], next_action, work_item=None) -> dict:
    """One immutable note per (product, owner, run_id). Different replay data fails.

    Inputs are reviewed prose and navigation links, never raw evidence content.
    Replay requires exact content equality, including the original date, so a
    manually modified note cannot be mistaken for an unchanged delivery receipt.
    """
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ProductError("invalid_id", "Run IDs use 1 to 96 letters, digits, hyphens, and underscores.")
    summary = _text(summary, "Summary", 4_000, multiline=True)
    next_action = _text(next_action, "Next action", 1_000)
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise ProductError("invalid_evidence", "Evidence must be a list of at most 20 artifact links.")
    evidence = list(dict.fromkeys(_link(value) for value in evidence))
    with _directory(product_root) as (root, fd):
        _read(fd, "product.md")
        item = _work_item(root, fd, product_root, work_item) if work_item is not None else None
        work_path = item["relative_path"] if item else None
        owner = f"{item['kind']}:{item['id']}" if item else "product"
        owner_path = Path(work_path).parent if work_path else Path()
        request = {"product": root.name, "run_id": run_id, "summary": summary, "evidence": evidence,
                   "next_action": next_action, "work_item": work_path}
        digest = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()
        with _child_directory(fd, owner_path.parts) as owner_fd:
            with _child_directory(owner_fd, ("notes",), create=True) as notes_fd:
                try:
                    fcntl.flock(notes_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ProductError("product_busy", "Another writer owns this notes directory; retry the same run.") from exc
                try:
                    pattern = re.compile(r"\d{4}-\d{2}-\d{2}-\d{4}-delivery-" + re.escape(run_id) + r"\.md\Z")
                    matches = []
                    with os.scandir(notes_fd) as entries:
                        for count, entry in enumerate(entries, 1):
                            if count > MAX_ENTRIES:
                                raise ProductError("limit_exceeded", "The notes directory exceeded its discovery limit.")
                            if pattern.fullmatch(entry.name):
                                matches.append(entry.name)
                    if len(matches) > 1:
                        raise ProductError("note_conflict", "More than one note claims this run; no note was changed.")
                    if matches:
                        name = matches[0]
                        existing = _read(notes_fd, name, limit=NOTE_BYTES)
                        date_match = re.search(r"^- Date: (.+)$", existing, re.MULTILINE)
                        date = date_match.group(1) if date_match else ""
                        expected = _note_text(root, run_id, summary, evidence, next_action, owner, work_path, date, digest)
                        if not NOTE_TIME.fullmatch(date) or existing != expected:
                            raise ProductError("note_conflict", "This run already has a different note; no note was changed.")
                        created = False
                    else:
                        now = datetime.now(timezone.utc)
                        date = now.strftime("%Y-%m-%dT%H:%M:%SZ")
                        name = now.strftime("%Y-%m-%d-%H%M") + f"-delivery-{run_id}.md"
                        note = _note_text(root, run_id, summary, evidence, next_action, owner, work_path, date, digest)
                        if len(note.encode("utf-8")) > NOTE_BYTES:
                            raise ProductError("limit_exceeded", "The milestone note exceeds 16 KiB; use fewer or shorter links.")
                        _publish(notes_fd, name, note)
                        created = True
                finally:
                    fcntl.flock(notes_fd, fcntl.LOCK_UN)
    relative = owner_path / "notes" / name
    return {"run_id": run_id, "path": str(root / relative), "relative_path": relative.as_posix(),
            "owner": owner, "created": created, "digest": digest}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List explicitly configured product maps")
    listing.add_argument("--home")
    show = commands.add_parser("show", help="Load one exact product directory or product.md")
    show.add_argument("path")
    create = commands.add_parser("create", help="Create a product at a new path")
    create.add_argument("--home")
    create.add_argument("--slug", required=True)
    create.add_argument("--title", required=True)
    create.add_argument("--purpose", required=True)
    create.add_argument("--repository", required=True)
    note = commands.add_parser("note", help="Record a bounded outcome; replay is idempotent")
    note.add_argument("--product-root", required=True)
    note.add_argument("--run-id", required=True)
    note.add_argument("--summary", required=True)
    note.add_argument("--evidence", action="append", default=[])
    note.add_argument("--next-action", required=True)
    note.add_argument("--work-item")
    return parser


def main(argv=None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command == "list":
            result = list_products(product_home(args.home))
        elif args.command == "show":
            result = load_product(args.path)
        elif args.command == "create":
            result = create_product(args.home, args.slug, args.title, args.purpose, args.repository)
        else:
            result = record_outcome(args.product_root, args.run_id, args.summary, args.evidence,
                                    args.next_action, args.work_item)
        print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except ProductError as exc:
        print(json.dumps({"ok": False, "error": {"code": exc.code, "message": exc.message}}))
        return 2
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": {"code": "io_error", "message": "Product memory access failed."}}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
