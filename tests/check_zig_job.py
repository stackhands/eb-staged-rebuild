"""Fail unless the zig job discovers every test module, exclusions included.

tests/zig_job_exclude.txt is the only list of modules the zig job skips.
Each entry has to name a real tests/test_*.py and say why. Every other
module has to yield at least one test, so a pytest-style function module
that discover would load and then ignore cannot land quietly. The unit
job still runs discover with no TOOL_BIN. The zig job installs every
``python -m pip install`` line from the unit job, then runs
tests/run_zig_unittests.py with TOOL_BIN and TOOL_BIN_TEST_OPS, and does
not name unittest modules.

    python3 tests/check_zig_job.py
"""

from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "test.yml")
NAMED = re.compile(r"unittest(?: -v)? tests\.test_|unittest -v tests\.")
JOB = re.compile(r"^  [A-Za-z0-9_-]+:\s*$")


def job_body(text, name):
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith(f"  {name}:")), None)
    if start is None:
        raise SystemExit(f"workflow has no {name} job")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if JOB.match(lines[i]):
            end = i
            break
    return "".join(lines[start:end])


def main():
    sys.path.insert(0, REPO)
    from tests.run_zig_unittests import discover, iter_tests, read_exclude

    problems = []
    excluded = read_exclude()
    tests_dir = os.path.join(REPO, "tests")
    modules = sorted(
        entry[:-3]
        for entry in os.listdir(tests_dir)
        if entry.startswith("test_") and entry.endswith(".py")
    )
    for module, reason in excluded.items():
        if module not in modules:
            problems.append(f"exclude list names no module: {module}")
        if not reason or reason.lower().startswith("not yet"):
            problems.append(f"exclude list has no reason: {module}")
    suite = discover(excluded)
    kept = {test.id().split(".", 1)[0] for test in iter_tests(suite)}
    for module in modules:
        if module in excluded:
            continue
        if module not in kept:
            problems.append(f"discover loads no tests from {module}")
    dropped = sorted(set(modules) - kept - set(excluded))
    for module in dropped:
        problems.append(f"module is neither run nor excluded: {module}")
    with open(WORKFLOW, encoding="utf-8") as handle:
        workflow = handle.read()
    unit = job_body(workflow, "unit")
    zig = job_body(workflow, "zig")
    if "python -m unittest discover -s tests -v" not in unit:
        problems.append("unit job does not discover tests")
    if "TOOL_BIN" in unit:
        problems.append("unit job sets TOOL_BIN")
    if "python tests/run_zig_unittests.py" not in zig:
        problems.append("zig job does not run tests/run_zig_unittests.py")
    if "python tests/check_zig_job.py" not in zig:
        problems.append("zig job does not run tests/check_zig_job.py")
    if "TOOL_BIN:" not in zig or "TOOL_BIN_TEST_OPS:" not in zig:
        problems.append("zig job does not set TOOL_BIN and TOOL_BIN_TEST_OPS")
    runner_at = zig.find("python tests/run_zig_unittests.py")
    for line in unit.splitlines():
        stripped = line.strip()
        if not stripped.startswith("python -m pip install"):
            continue
        at = zig.find(stripped)
        if at < 0 or at > runner_at:
            problems.append(f"zig job does not install before discover: {stripped}")
    named = NAMED.findall(zig)
    if named:
        problems.append("zig job still names unittest modules")
    for problem in problems:
        print(problem)
    ran = suite.countTestCases()
    print(f"{len(modules)} modules, {len(excluded)} excluded, {ran} tests in the zig job")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
