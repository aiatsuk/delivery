"""Check release versions and print release notes for Delivery.

Commands:
  version                  print the version from VERSION
  check --tag vX.Y.Z       verify every version file and the CHANGELOG section
  notes --tag vX.Y.Z       print the CHANGELOG section body for the version

Standard library only. Nothing is packaged or published here.
"""

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PRIMARY_VERSION_FILE = "VERSION"
CHANGELOG = "CHANGELOG.md"

# Every place that carries the release version, kept in sync by hand in the release PR.
# Each entry is (path, kind, locator): "plain" is the whole file, "json" is a key path,
# "regex" is a pattern whose first group is the version.
VERSION_FILES = (
    ("VERSION", "plain", None),
    ("plugins/delivery-harness/.claude-plugin/plugin.json", "json", ("version",)),
    ("plugins/delivery-harness/.codex-plugin/plugin.json", "json", ("version",)),
    ("plugins/delivery-harness/.cursor-plugin/plugin.json", "json", ("version",)),
    (".cursor-plugin/marketplace.json", "json", ("metadata", "version")),
    (".cursor-plugin/marketplace.json", "json", ("plugins", 0, "version")),
    ("README.md", "regex", r"\[`plugins/delivery-harness`\]\(plugins/delivery-harness\), version (\d+\.\d+\.\d+)\."),
    ("plugins/delivery-harness/README.md", "regex", r"\bVersion (\d+\.\d+\.\d+) is maintained\b"),
)

TAG_PATTERN = re.compile(r"v(\d+\.\d+\.\d+)")


class ReleaseError(Exception):
    pass


def version_from_tag(tag):
    match = TAG_PATTERN.fullmatch(tag or "")
    if not match:
        raise ReleaseError(f"tag {tag!r} is not of the form vX.Y.Z")
    return match.group(1)


def read_version(root, path, kind, locator):
    file = Path(root) / path
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as error:
        raise ReleaseError(f"{path}: cannot read ({error.strerror or error})") from None
    if kind == "plain":
        return text.strip()
    if kind == "json":
        try:
            value = json.loads(text)
            for key in locator:
                value = value[key]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ReleaseError(f"{path}: no version at {'.'.join(map(str, locator))}") from None
        return value
    match = re.search(locator, text)
    if not match:
        raise ReleaseError(f"{path}: version line not found")
    return match.group(1)


def primary_version(root=ROOT):
    return read_version(root, PRIMARY_VERSION_FILE, "plain", None)


def changelog_section(root, version):
    path = Path(root) / CHANGELOG
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        raise ReleaseError(f"{CHANGELOG}: cannot read") from None
    heading = re.compile(rf"## {re.escape(version)}(?: — .*)?")
    for index, line in enumerate(lines):
        if heading.fullmatch(line.rstrip()):
            body = []
            for following in lines[index + 1:]:
                if following.startswith("## "):
                    break
                body.append(following)
            text = "\n".join(body).strip("\n")
            if not text.strip():
                raise ReleaseError(f"{CHANGELOG}: section {version} is empty")
            return text + "\n"
    raise ReleaseError(f"{CHANGELOG}: no '## {version}' section")


def check(root, tag):
    version = version_from_tag(tag)
    problems = []
    for path, kind, locator in VERSION_FILES:
        try:
            found = read_version(root, path, kind, locator)
        except ReleaseError as error:
            problems.append(str(error))
            continue
        if found != version:
            where = path if kind != "json" else f"{path} ({'.'.join(map(str, locator))})"
            problems.append(f"{where}: version {found!r}, tag says {version}")
    try:
        changelog_section(root, version)
    except ReleaseError as error:
        problems.append(str(error))
    if problems:
        raise ReleaseError("\n".join(problems))
    return version


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(ROOT), help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("version")
    for name in ("check", "notes"):
        commands.add_parser(name).add_argument("--tag", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "version":
            print(primary_version(args.root))
        elif args.command == "check":
            version = check(args.root, args.tag)
            print(f"ok: every version file and {CHANGELOG} agree on {version}")
        else:
            sys.stdout.write(changelog_section(args.root, version_from_tag(args.tag)))
    except ReleaseError as error:
        print(f"release check failed:\n{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
