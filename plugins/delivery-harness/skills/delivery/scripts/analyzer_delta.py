#!/usr/bin/env python3
"""Read-only diagnostic comparison for planned analyzer gates."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

DEFAULT_ISSUE_REGEX = r"^\s*(info|warning|error)\b|^\s*\d+:\d+\s+(warning|error)\b"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ESLINT = re.compile(r"^\s*\d+:\d+\s+(warning|error)\b")


def issues(text: str, regex: str = DEFAULT_ISSUE_REGEX) -> Counter:
    pattern = re.compile(regex)
    found = Counter()
    context = ""
    for raw in text.splitlines():
        clean = ANSI.sub("", raw)
        line = clean.strip()
        if pattern.search(line):
            key = f"{context}: {line}" if context and ESLINT.match(line) else line
            found[key] += 1
        elif clean and not clean[0].isspace():
            # Stylish headings occupy their own unindented line. Do not
            # restrict extensions: plugins also lint JSON, Markdown and more.
            context = line
    return found


def compare(baseline: str, current: str, regex: str = DEFAULT_ISSUE_REGEX) -> dict:
    before, after = issues(baseline, regex), issues(current, regex)
    return {"baseline_count": sum(before.values()), "current_count": sum(after.values()),
            "new": sorted((after - before).elements()),
            "resolved": sorted((before - after).elements())}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--issue-regex", default=DEFAULT_ISSUE_REGEX)
    args = parser.parse_args(argv)
    try:
        before = Path(args.baseline).read_bytes()
        after = Path(args.current).read_bytes()
        result = compare(before.decode("utf-8"), after.decode("utf-8"), args.issue_regex)
        result["baseline_sha256"] = hashlib.sha256(before).hexdigest()
        result["current_sha256"] = hashlib.sha256(after).hexdigest()
    except (OSError, UnicodeError, re.error) as error:
        print(json.dumps({"error": str(error)}))
        return 2
    print(json.dumps(result, indent=2))
    return 1 if result["new"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
