#!/usr/bin/env python3
"""Dependency-free packaging preflight. Does not substitute for runtime tests."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys


def validate(root):
    errors = []
    root = Path(root).resolve()
    manifests = []
    for relative in (".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
        try:
            manifest = json.loads((root / relative).read_text())
            manifests.append(manifest)
            if manifest.get("name") != root.name: errors.append(f"{relative}: name must match folder")
            if manifest.get("skills") != "./skills/": errors.append(f"{relative}: wrong skills root")
            if not re.fullmatch(r"\d+\.\d+\.\d+(?:[+-][A-Za-z0-9.-]+)?", manifest.get("version", "")): errors.append(f"{relative}: invalid version")
            if not manifest.get("author", {}).get("name"): errors.append(f"{relative}: missing author")
            if "[TODO:" in json.dumps(manifest): errors.append(f"{relative}: unfinished manifest")
        except (OSError, ValueError) as error:
            errors.append(f"{relative}: {error}")
    if len(manifests) == 2 and manifests[0]["version"].split("+")[0] != manifests[1]["version"].split("+")[0]:
        errors.append("Host manifest base versions differ")
    skills = sorted(str(p.relative_to(root)) for p in root.rglob("SKILL.md"))
    if skills != ["skills/delivery/SKILL.md"]: errors.append(f"Only one coordinator may be discoverable: {skills}")
    required = ["README.md", "PROVENANCE.md", "examples/small-plan.json", "skills/delivery/agents/openai.yaml", "skills/delivery/scripts/delivery.py", "skills/delivery/scripts/run_engine.py", "skills/delivery/scripts/git_ops.py", "skills/delivery/scripts/publisher.py", "skills/delivery/scripts/product.py", "skills/delivery/scripts/snapshots.py", "skills/delivery/scripts/gate_jobs.py", "skills/delivery/internal/spec/scripts/spec_flow.py", "skills/delivery/internal/spec/engine-guide.md", "skills/delivery/internal/product/session_evidence.py"]
    for name in required:
        if not (root / name).is_file(): errors.append(f"Missing bundled file: {name}")
    for path in root.rglob("*.py"):
        try: ast.parse(path.read_text(), filename=str(path))
        except (SyntaxError, UnicodeError) as error: errors.append(str(error))
    for path in root.rglob("*.json"):
        try: json.loads(path.read_text())
        except (ValueError, UnicodeError) as error: errors.append(f"{path.relative_to(root)}: {error}")
    for path in (root / "skills").rglob("*.py"):
        source = path.read_text()
        if re.search(r"(?:/Users/[^/\s]+/|/private/tmp/delivery-harness-build|/\.codex/plugins/cache/)", source):
            errors.append(f"Machine-specific runtime path: {path.relative_to(root)}")
    for path in [root / "skills/delivery/SKILL.md", *(root / "skills/delivery/references").glob("*.md")]:
        source = path.read_text()
        for target in re.findall(r"\]\(([^)]+)\)", source):
            if "://" in target or target.startswith("#"): continue
            linked = (path.parent / target.split("#")[0]).resolve()
            if not linked.exists(): errors.append(f"Broken reference in {path.name}: {target}")
            if root != linked and root not in linked.parents: errors.append(f"Reference escapes bundle: {target}")
    return {"ok": not errors, "root": str(root), "skills": skills, "errors": errors}


if __name__ == "__main__":
    result = validate(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
