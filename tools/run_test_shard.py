"""Run a disjoint slice after importing the complete test catalog."""
import hashlib
import json
from pathlib import Path
import sys
import time
import unittest

index, count, destination = int(sys.argv[1]), int(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(Path.cwd()))
loader = unittest.TestLoader()
catalog = loader.discover("tests", top_level_dir=".")
if loader.errors:
    raise RuntimeError("\n".join(loader.errors))

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

tests = sorted(flatten(catalog), key=lambda case: case.id())
ids = [test.id() for test in tests]
assert 0 <= index < count and len(ids) == len(set(ids))
selected = tests[index::count]
print(json.dumps({"shard": index, "total": len(tests), "selected": len(selected)}), flush=True)
started = time.monotonic()
result = unittest.TextTestRunner(verbosity=1).run(unittest.TestSuite(selected))
report = {
    "shard": index, "shards": count, "catalog": ids,
    "catalog_hash": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
    "selected": ids[index::count], "executed": result.testsRun,
    "successful": result.wasSuccessful(), "duration_seconds": round(time.monotonic() - started, 3),
    "failures": [(case.id(), trace) for case, trace in result.failures],
    "errors": [(case.id(), trace) for case, trace in result.errors],
    "skipped": [(case.id(), reason) for case, reason in result.skipped],
}
destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
print(json.dumps({key: report[key] for key in ("shard", "executed", "successful", "duration_seconds", "catalog_hash")}), flush=True)
raise SystemExit(0 if result.wasSuccessful() else 1)
