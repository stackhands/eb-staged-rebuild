"""Fail a run, and a rollback, at every step they reach; compare the outcome.

EB_STAGED_REBUILD_FAIL_AT=STEP makes the step fail with an I/O error in the
-Dtest-ops artifact named by TOOL_BIN_TEST_OPS. A failed run must leave
every live tree and module file as it was, or have published all of
them; a resume must then publish. A failed rollback must leave the run
published or rolled back; a second rollback must roll it back. A failed
rollback --to r1 over r2 and r3 must be finished by running it again, as
must one that cannot unlink the class link r2 made. must leave the trees, module files, journal states and exit codes that
tests/golden/crash_matrix/fail_results.json records; RECORD=1 rewrites it.

    TOOL_BIN_TEST_OPS=zig-out/bin/eb-staged-rebuild-test-ops python3 tests/fail_matrix.py
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import crash_matrix as cm  # noqa: E402

def call(fixture, root, args, fail_at=None):
    env = cm.env_for(fixture, root, None, "")
    if fail_at:
        env["EB_STAGED_REBUILD_FAIL_AT"] = fail_at
    return subprocess.run(cm.command() + args, env=env, cwd=root, capture_output=True, text=True).returncode


def failed_run(fixture, step):
    def body(root):
        fixture.build(root)
        rc = call(fixture, root, fixture.run_args(root, "r1"), fail_at=step)
        after = cm.outcome(fixture, root, "r1")
        resumed = None
        if after["state"] is not None and after["state"][0] != "complete":
            resumed = call(fixture, root, ["resume", "--state-root", f"{root}/state", "r1"])
        return {"failed_rc": rc, "after": after, "resume_rc": resumed, "final": cm.outcome(fixture, root, "r1")}
    return body


def failed_rollback(fixture, step):
    def body(root):
        fixture.build(root)
        assert call(fixture, root, fixture.run_args(root, "r1")) == 0
        rc = call(fixture, root, ["rollback", "--state-root", f"{root}/state", "r1"], fail_at=step)
        after = cm.outcome(fixture, root, "r1")
        again = call(fixture, root, ["rollback", "--state-root", f"{root}/state", "r1"])
        return {"failed_rc": rc, "after": after, "again_rc": again, "final": cm.outcome(fixture, root, "r1")}
    return body


def failed_rollback_to(fixture, fallback, step, locked=None):
    def body(root):
        modules = cm.published_over(fixture, root, fallback, relinked=locked is not None)
        args = ["rollback", "--state-root", f"{root}/state", "--to", "r1"]
        if locked:
            os.chmod(os.path.join(root, "modules", locked), 0o555)
        try:
            rc = call(fixture, root, args, fail_at=step)
        finally:
            if locked:
                os.chmod(os.path.join(root, "modules", locked), 0o755)
        after = cm.to_outcome(fixture, root)
        again = call(fixture, root, args)
        return {"failed_rc": rc, "after": after, "again_rc": again, "published_modules": modules, "final": cm.to_outcome(fixture, root)}
    return body


def check_to(label, fixture, result):
    """A failed rollback --to fails, and running it again finishes it."""
    trees, states = cm.to_expected(fixture)
    final = result["final"]
    ok = cm.settled(label, result, "fail_results.json") and result["failed_rc"] == 2 and result["again_rc"] == 0
    ok = ok and final["trees"] == trees and final["states"] == states and final["modules"] == result["published_modules"]
    if ok:
        print(f"same {label}: exit 2, then rolled back to r1")
        return 0
    print(f"BAD  {label}")
    print(f"     got: {result}")
    return 1


def untouched_or_published(fixture, info):
    old = info["payloads"] == ["old\n"] * len(fixture.rels) and all("EBROOT" in m and "conflict" not in m for m in info["modules"])
    new = info["payloads"] == ["from-eb\n"] * len(fixture.rels) and all("conflict" in m for m in info["modules"])
    return old or new


def check(label, fixture, result, kind):
    same = cm.settled(label, result, "fail_results.json")
    published = ("complete", ["published"] * len(fixture.rels))
    rolled = ("rolled_back", ["restored"] * len(fixture.rels))
    if kind == "run":
        ok = untouched_or_published(fixture, result["after"]) and result["final"]["state"] in (published, None)
        ok = ok and (result["final"]["state"] is None or set(result["final"]["payloads"]) == {"from-eb\n"})
    else:
        ok = untouched_or_published(fixture, result["after"]) and result["final"]["state"] == rolled
        ok = ok and set(result["final"]["payloads"]) == {"old\n"}
    if same and ok:
        print(f"same {label}: exit {result['failed_rc']}, then {result['final']['state']}")
        return 0
    print(f"{'DIFF' if not same else 'BAD '} {label}")
    print(f"     got: {result}")
    return 1


def main():
    if not os.environ.get("TOOL_BIN_TEST_OPS"):
        print("set TOOL_BIN_TEST_OPS", file=sys.stderr)
        return 2
    os.environ["TOOL_BIN"] = os.environ["TOOL_BIN_TEST_OPS"]
    failures = total = 0
    for fixture in (cm.ONE, cm.TWO, cm.VERIFY):
        for step in cm.golden_steps(f"{fixture.name}: run"):
            failures += check(f"{fixture.name}: run fails at {step}", fixture, cm.fresh("fail", failed_run(fixture, step)), "run")
            total += 1
        for step in cm.golden_steps(f"{fixture.name}: rollback"):
            failures += check(f"{fixture.name}: rollback fails at {step}", fixture, cm.fresh("fail-rb", failed_rollback(fixture, step)), "rollback")
            total += 1
    # A failure whose restore cannot be recorded either: the publish path
    # reports the journal error, the rollback path needs intervention, and
    # neither leaves a half-swapped tree.
    fixture = cm.ONE
    for step, restore in (
        ("swap:all/demo/1.lua.exchange.after", "journal.restored.write"),
        ("rollback.swap:all/demo/1.lua.exchange.after", "journal.rollback-restored.write"),
    ):
        both = f"{step},{restore}"
        if step.startswith("rollback."):
            results = cm.fresh("fail-rb2", failed_rollback(fixture, both))
            failures += check(f"one module: rollback fails at {step} and its restore record", fixture, results, "rollback")
        else:
            results = cm.fresh("fail2", failed_run(fixture, both))
            failures += check(f"one module: run fails at {step} and its restore record", fixture, results, "run")
        total += 1
    for fixture, fallback in ((cm.ONE, False), (cm.TWO, False), (cm.ONE, True)):
        label = fixture.name + (", rename fallback" if fallback else "")
        for step in cm.golden_steps(f"{label}: rollback --to"):
            results = cm.fresh("fail-to", failed_rollback_to(fixture, fallback, step))
            failures += check_to(f"{label}: rollback --to fails at {step}", fixture, results)
            total += 1
    results = cm.fresh("fail-to-unlink", failed_rollback_to(cm.ONE, False, None, locked="tools/demo"))
    failures += check_to("one module: rollback --to cannot unlink tools/demo/1.lua", cm.ONE, results)
    total += 1
    cm.write_recorded()
    print(f"{total - failures} of {total} cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
