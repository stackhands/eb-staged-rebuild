"""Run unittest discover against the built binaries.

The zig CI job sets TOOL_BIN and TOOL_BIN_TEST_OPS and runs this instead of
naming modules. Discover loads every tests/test_*.py, which is also what
pytest collects from these TestCase modules. tests/zig_job_exclude.txt names
any module that stays out, with a reason. The unit job keeps discovering
every module, including those, with neither variable set.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild \\
        TOOL_BIN_TEST_OPS=zig-out/bin/eb-staged-rebuild-test-ops \\
        python3 tests/run_zig_unittests.py
"""

from __future__ import annotations

import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXCLUDE = os.path.join(REPO, "tests", "zig_job_exclude.txt")


def read_exclude(path=EXCLUDE):
    found = {}
    with open(path, encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if "\t" not in line.rstrip("\n"):
                raise SystemExit(f"{path}:{lineno}: expected a module, a tab, and a reason")
            module, reason = line.rstrip("\n").split("\t", 1)
            module, reason = module.strip(), reason.strip()
            if not module or not reason or module in found:
                raise SystemExit(f"{path}:{lineno}: missing module, missing reason, or a repeat")
            found[module] = reason
    return found


def iter_tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def discover(excluded):
    sys.path.insert(0, REPO)
    loader = unittest.TestLoader()
    found = loader.discover(
        os.path.join(REPO, "tests"),
        pattern="test_*.py",
        top_level_dir=os.path.join(REPO, "tests"),
    )
    kept = unittest.TestSuite()
    seen = set()
    for test in iter_tests(found):
        module = test.id().split(".", 1)[0]
        seen.add(module)
        if module not in excluded:
            kept.addTest(test)
    missing = sorted(set(excluded) - seen)
    if missing:
        raise SystemExit("exclude list names modules discover did not load: " + ", ".join(missing))
    if kept.countTestCases() == 0:
        raise SystemExit("discover kept no tests")
    return kept


def main():
    missing = [name for name in ("TOOL_BIN", "TOOL_BIN_TEST_OPS") if not os.environ.get(name)]
    if missing:
        print("unset: " + ", ".join(missing), file=sys.stderr)
        return 2
    excluded = read_exclude()
    for module, reason in excluded.items():
        print(f"excluded {module}: {reason}", file=sys.stderr, flush=True)
    suite = discover(excluded)
    print(f"{suite.countTestCases()} tests", file=sys.stderr, flush=True)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
