"""Kill a run at every step it reaches, resume it, and compare the outcome.

For each fault step a run reaches (tests/golden/crash_matrix/steps.json), the
program named by TOOL_BIN runs on a fresh copy of one fixture, is paused at
that step (EB_STAGED_REBUILD_PAUSE_AT), killed with SIGKILL, and resumed. It
must leave the live tree, the module file and the journal state that
tests/golden/crash_matrix/results.json records; a run must end either with
the old tree untouched or fully published. The same is done for rollback of
a complete run. RECORD=1 rewrites results.json.

Six fixtures: one module; two modules (lib/1 and app/1); one module
whose build leaves the installation and module tree read-only, which is
published with its modes and is not built again once the build was
journalled; one module with
a --prepare policy that counts its invocations, which must run once,
or twice when the kill falls after it exited and before the journal
recorded it; one module run with --verify, whose stage is checked
before the swap, also by the resume; and one module forced onto the
ordinary-rename fallback. Run and rollback take the swap method from the
journal's recorded rename_exchange probe, so the fallback fixture stops a
run at build.start, records the probe as failed, and kills the resumed
run or a later rollback at each fallback rename. A retire of the first
fixture's module file, with a module class link and a default link to
it, and a rollback of one, are killed at every step they reach and then
rolled back; the module file and both links must end as they began. A
rollback --to over two runs is killed at every step it reaches, on one
module, two modules and the fallback, and run again; it must end with
each tree where it belongs.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/crash_matrix.py
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_EB = os.path.join(REPO, "tests", "fake_eb.py")
GOLDEN = os.path.join(REPO, "tests", "golden", "crash_matrix")

FALLBACK = ("live-to-hold", "staged-to-live", "hold-to-staged")


class Fixture:
    def __init__(self, name, rels, prepare=False, seal=False, verify=False):
        self.name = name
        self.rels = rels
        self.prepare = prepare
        self.seal = seal
        self.verify = verify

    def build(self, root):
        for rel in self.rels:
            name, version = rel.split("/")
            os.makedirs(os.path.join(root, "modules", "all", name))
            os.makedirs(os.path.join(root, "software", name, version, "easybuild"))
            with open(os.path.join(root, "software", name, version, "easybuild", f"{name}-{version}.eb"), "w") as handle:
                handle.write(f"name = '{name}'\n")
            with open(os.path.join(root, "software", name, version, "PAYLOAD"), "w") as handle:
                handle.write("old\n")
            with open(os.path.join(root, "modules", "all", name, f"{version}.lua"), "w") as handle:
                handle.write(f'local root = "{root}/software/{rel}"\nsetenv("EBROOT{name.upper()}", root)\n')
        os.makedirs(os.path.join(root, "state"))
        eb = os.path.join(root, "eb")
        with open(eb, "w") as handle:
            handle.write(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_EB}" "$@"\n')
        os.chmod(eb, 0o755)
        if self.prepare:
            policy = os.path.join(root, "prepare")
            with open(policy, "w") as handle:
                handle.write(f'#!/bin/sh\necho x >> "{root}/prepare-count"\n')
            os.chmod(policy, 0o755)

    def env(self, root):
        env = {"FAKE_EB_RELS": ",".join(self.rels), "FAKE_EB_ARGV": os.path.join(root, "eb-argv")}
        if self.seal:
            env["FAKE_EB_SEAL"] = "1"
        return env

    def run_args(self, root, run_id):
        args = [
            "run", "--software", f"{root}/software", "--modules", f"{root}/modules",
            "--state-root", f"{root}/state", "--backend", "installpath", "--eb", f"{root}/eb",
            "--run-id", run_id,
        ]
        if self.prepare:
            args += ["--prepare", f"{root}/prepare"]
        if self.verify:
            args.append("--verify")
        for rel in self.rels:
            args += ["--module", rel]
        return args + ["--", "demo-1.eb"]


ONE = Fixture("one module", ["demo/1"])
TWO = Fixture("two modules", ["lib/1", "app/1"])
PREPARE = Fixture("prepare", ["demo/1"], prepare=True)
SEALED = Fixture("sealed", ["demo/1"], seal=True)
VERIFY = Fixture("verify", ["demo/1"], verify=True)


def unseal(root):
    """Make every directory writable again, so the tree can be removed."""
    for directory, dirs, _files in os.walk(root):
        for name in dirs:
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                os.chmod(path, 0o755)
    if os.path.isdir(root):
        os.chmod(root, 0o755)


def remove(root):
    unseal(root)
    shutil.rmtree(root, ignore_errors=True)


def modes(top):
    """Mode of every path under `top`, relative, without the tool's stamp."""
    found = {".": oct(os.lstat(top).st_mode & 0o7777)}
    for directory, dirs, files in os.walk(top):
        for name in dirs + files:
            if name == ".eb-staged-rebuild-complete":
                continue
            path = os.path.join(directory, name)
            found[os.path.relpath(path, top)] = oct(os.lstat(path).st_mode & 0o7777)
    return found


def builds(root):
    """Builds the fake eb ran, not counting the preflight or a version probe."""
    path = os.path.join(root, "eb-argv")
    if not os.path.exists(path):
        return 0
    with open(path) as handle:
        return sum(1 for line in handle if any(arg.startswith("--installpath-software=") for arg in json.loads(line)))


def golden_steps(name):
    """The fault steps the golden list names for NAME, in the order a command reaches them."""
    with open(os.path.join(GOLDEN, "steps.json"), encoding="utf-8") as handle:
        return json.load(handle)[name]


def tree_run_id(path):
    """The run id in the completion stamp of the tree at PATH, or None."""
    try:
        with open(os.path.join(path, ".eb-staged-rebuild-complete"), encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value.get("run_id") if isinstance(value, dict) else None


def command():
    return [os.environ["TOOL_BIN"]]


def env_for(fixture, root, step, ready):
    env = dict(os.environ)
    env.update(fixture.env(root))
    if step:
        env["EB_STAGED_REBUILD_PAUSE_AT"] = step
        env["EB_STAGED_REBUILD_PAUSE_READY"] = ready
        env["EB_STAGED_REBUILD_PAUSE_SECONDS"] = "30"
    return env


def kill_at(fixture, args, step, root):
    """Start the command, wait until it pauses at `step`, then SIGKILL it."""
    ready = os.path.join(root, "ready")
    proc = subprocess.Popen(
        command() + args, env=env_for(fixture, root, step, ready), cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + 60
    while not os.path.exists(ready):
        if proc.poll() is not None:
            return f"exited {proc.returncode} before the pause"
        if time.monotonic() > deadline:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            return "never paused"
        time.sleep(0.02)
    os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()
    os.unlink(ready)
    return "killed"


def finish(fixture, args, root, **extra):
    env = env_for(fixture, root, None, "")
    env.update(extra)
    return subprocess.run(command() + args, env=env, cwd=root, capture_output=True, text=True).returncode


def journal_file(root, run_id):
    return os.path.join(root, "state", "runs", run_id, "journal.json")


def without_exchange(root, run_id):
    """Record the rename_exchange probe as failed, so the swap uses the fallback."""
    path = journal_file(root, run_id)
    with open(path) as handle:
        journal = json.load(handle)
    journal["probes"]["rename_exchange"]["ok"] = False
    with open(path, "w") as handle:
        json.dump(journal, handle, indent=2, sort_keys=True)
        handle.write("\n")


def outcome(fixture, root, run_id):
    payloads = []
    modules = []
    for rel in fixture.rels:
        name, version = rel.split("/")
        with open(os.path.join(root, "software", name, version, "PAYLOAD")) as handle:
            payloads.append(handle.read())
        with open(os.path.join(root, "modules", "all", name, f"{version}.lua")) as handle:
            modules.append(handle.read().replace(root, "W"))
    state = methods = None
    if os.path.exists(journal_file(root, run_id)):
        with open(journal_file(root, run_id)) as handle:
            journal = json.load(handle)
        plan = journal.get("modules_plan") or []
        state = (journal["state"], [mod.get("status") for mod in plan])
        methods = [mod.get("swap_method") for mod in plan]
    hidden = sorted(
        name
        for rel in fixture.rels
        for name in os.listdir(os.path.join(root, "software", rel.split("/")[0]))
        if name.startswith(".")
    )
    info = {"payloads": payloads, "modules": modules, "state": state, "methods": methods, "hidden": hidden}
    if fixture.seal:
        info["builds"] = builds(root)
        info["live_modes"] = [modes(os.path.join(root, "software", rel)) for rel in fixture.rels]
    count = os.path.join(root, "prepare-count")
    if fixture.prepare:
        info["prepare_runs"] = open(count).read().count("x") if os.path.exists(count) else 0
    return info


RECORDED = {}


def settled(label, result, name="results.json"):
    """Whether RESULT is what the golden file NAME holds for LABEL; with RECORD=1, keep it for the file."""
    seen = json.loads(json.dumps(result))
    if os.environ.get("RECORD") == "1":
        RECORDED.setdefault(name, {})[label] = seen
        return True
    with open(os.path.join(GOLDEN, name), encoding="utf-8") as handle:
        expected = json.load(handle)
    return expected.get(label) == seen


def write_recorded():
    """With RECORD=1, write the golden files the run collected."""
    for name, labels in RECORDED.items():
        with open(os.path.join(GOLDEN, name), "w", encoding="utf-8") as handle:
            json.dump(labels, handle, indent=1, sort_keys=True)
            handle.write("\n")


def compare(label, fixture, results, kind, must_pause=False, method=None, prepare_runs=1, one_build=False):
    if not settled(label, results):
        print(f"DIFF {label}")
        print(f"     got: {results}")
        return 1
    final = results
    published = ("complete", ["published"] * len(fixture.rels))
    if kind == "run":
        # Published in full, or untouched when no journal was written.
        ok = (final["state"] == published and set(final["payloads"]) == {"from-eb\n"}) or (
            final["state"] is None and set(final["payloads"]) == {"old\n"}
        )
        # The policy runs once, or twice when the kill fell after it exited
        # and before the journal recorded it as complete.
        if ok and fixture.prepare and final["state"] == published:
            ok = final["prepare_runs"] == prepare_runs
        # A sealed installation is published as the build left it, and a
        # resume after the build was journalled does not build again.
        if ok and fixture.seal and final["state"] == published:
            sealed = {mode for tree in final["live_modes"] for mode in tree.values()}
            ok = sealed <= {"0o555", "0o444"} and (final["builds"] == 1 or not one_build)
    else:
        ok = final["state"] == ("rolled_back", ["restored"] * len(fixture.rels)) and set(final["payloads"]) == {"old\n"}
    if ok and must_pause:
        ok = final["kill"] == "killed"
    if ok and method:
        ok = final["methods"] == [method] * len(fixture.rels)
    if not ok:
        print(f"BAD  {label}: {final}")
        return 1
    print(f"same {label}: {final['state']} payloads {[p.strip() for p in final['payloads']]}")
    return 0


def fresh(prefix, body):
    """BODY run on a fresh directory, which is removed afterwards."""
    root = tempfile.mkdtemp(prefix=f"{prefix}-")
    try:
        return body(root)
    finally:
        remove(root)


def run_matrix(fixture):
    failures = 0
    total_extra = []
    steps = golden_steps(f"{fixture.name}: run")
    # The policy's success is durable once journal.prepared is renamed into
    # place; a kill after prepare.invoke and up to that rename reruns it.
    rerun = set()
    built = set(steps[steps.index("journal.built.rename") + 1:])
    if fixture.prepare:
        start = steps.index("prepare.invoke") + 1
        rerun = set(steps[start:steps.index("journal.prepared.rename", start) + 1])
    for step in steps:
        def body(root):
            fixture.build(root)
            how = kill_at(fixture, fixture.run_args(root, "r1"), step, root)
            rc = finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
            return {"kill": how, "resume_rc": rc, **outcome(fixture, root, "r1")}
        failures += compare(
            f"{fixture.name}: run killed at {step}", fixture, fresh("crash", body), "run",
            prepare_runs=2 if step in rerun else 1, one_build=step in built,
        )
        if step.endswith(".exchange.after"):
            # Killed after the exchange, resumed to published, then rolled back.
            def back(root):
                fixture.build(root)
                how = kill_at(fixture, fixture.run_args(root, "r1"), step, root)
                assert finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root) == 0
                rc = finish(fixture, ["rollback", "--state-root", f"{root}/state", "r1"], root)
                return {"kill": how, "rollback_rc": rc, **outcome(fixture, root, "r1")}
            failures += compare(
                f"{fixture.name}: run killed at {step}, resumed, rolled back", fixture,
                fresh("crash-back", back), "rollback", must_pause=True,
            )
            total_extra.append(1)
    rollback_steps = golden_steps(f"{fixture.name}: rollback")
    for step in rollback_steps:
        def body(root):
            fixture.build(root)
            assert finish(fixture, fixture.run_args(root, "r1"), root) == 0
            how = kill_at(fixture, ["rollback", "--state-root", f"{root}/state", "r1"], step, root)
            resume_rc = None
            if step.startswith("journal.restore:") and step.endswith(".write"):
                resume_rc = finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
            rc = finish(fixture, ["rollback", "--state-root", f"{root}/state", "r1"], root)
            return {"kill": how, "resume_rc": resume_rc, "rollback_rc": rc, **outcome(fixture, root, "r1")}
        results = fresh("crash-rb", body)
        failures += compare(f"{fixture.name}: rollback killed at {step}", fixture, results, "rollback")
        if step.startswith("journal.restore:") and step.endswith(".write"):
            if results["resume_rc"] != 2:
                print(f"BAD  {fixture.name}: resume reports rollback killed at {step}: {results}")
                failures += 1
    return failures, len(steps) + len(rollback_steps) + len(total_extra)


def fallback_matrix():
    """The ordinary-rename fallback, killed after each of its renames."""
    fixture = ONE
    failures = total = 0

    def published_by_fallback(root):
        fixture.build(root)
        assert kill_at(fixture, fixture.run_args(root, "r1"), "build.start", root) == "killed"
        without_exchange(root, "r1")

    def complete(root):
        published_by_fallback(root)
        rc = finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
        return {"kill": "killed", "resume_rc": rc, **outcome(fixture, root, "r1")}

    failures += compare("fallback: run published", fixture, fresh("crash-fb", complete), "run", method="rename-fallback")
    total += 1
    for rel in fixture.rels:
        for name in FALLBACK:
            step = f"swap:all/{rel}.lua.fallback.{name}"

            def body(root):
                published_by_fallback(root)
                how = kill_at(fixture, ["resume", "--state-root", f"{root}/state", "r1"], step, root)
                rc = finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root)
                return {"kill": how, "resume_rc": rc, **outcome(fixture, root, "r1")}
            failures += compare(f"fallback: run killed at {step}", fixture, fresh("crash-fb", body), "run", must_pause=True, method="rename-fallback")
            total += 1

            rb_step = f"rollback.{step}"

            def rb_body(root):
                published_by_fallback(root)
                assert finish(fixture, ["resume", "--state-root", f"{root}/state", "r1"], root) == 0
                how = kill_at(fixture, ["rollback", "--state-root", f"{root}/state", "r1"], rb_step, root)
                rc = finish(fixture, ["rollback", "--state-root", f"{root}/state", "r1"], root)
                return {"kill": how, "rollback_rc": rc, **outcome(fixture, root, "r1")}
            failures += compare(f"fallback: rollback killed at {rb_step}", fixture, fresh("crash-fb", rb_body), "rollback", must_pause=True)
            total += 1
    return failures, total


RETIRE_LINKS = {"tools/demo/1.lua": "W/modules/all/demo/1.lua", "all/demo/default": "1.lua"}


def retire_fixture(root):
    """ONE, with a module class link and a default link to its module file."""
    ONE.build(root)
    for rel, target in RETIRE_LINKS.items():
        path = os.path.join(root, "modules", rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target.replace("W", root, 1) if target.startswith("W/") else target, path)


OVER = ("r1", "r2", "r3")


def trees(fixture, root):
    """The run marker of every live and hidden tree of each install path."""
    found = {}
    for rel in fixture.rels:
        name = rel.split("/")[0]
        parent = os.path.join(root, "software", name)
        for entry in sorted(os.listdir(parent)):
            found[f"{name}/{entry}"] = tree_run_id(os.path.join(parent, entry))
    return found


def module_texts(fixture, root):
    texts = []
    for rel in fixture.rels:
        name, version = rel.split("/")
        with open(os.path.join(root, "modules", "all", name, f"{version}.lua")) as handle:
            texts.append(handle.read().replace(root, "W"))
    return texts


def to_outcome(fixture, root):
    states = {}
    for run_id in OVER:
        with open(journal_file(root, run_id)) as handle:
            journal = json.load(handle)
        plan = journal.get("modules_plan") or []
        states[run_id] = [journal["state"], journal.get("rollback_to"), [mod.get("status") for mod in plan]]
    return {"states": states, "trees": trees(fixture, root), "modules": module_texts(fixture, root)}


def to_expected(fixture):
    """What rollback --to r1 over r2 and r3 must end with.

    Every live tree is r1's, each hidden tree holds the run that names it
    (the first holds the original), and r2 and r3 are rolled back.
    """
    trees_after = {}
    for rel in fixture.rels:
        name, version = rel.split("/")
        trees_after.update({f"{name}/{version}": "r1", f"{name}/.{version}.r1": None})
        trees_after.update({f"{name}/.{version}.{run_id}": run_id for run_id in OVER[1:]})
    states = {"r1": ["complete", None, ["published"] * len(fixture.rels)]}
    states.update({run_id: ["rolled_back", "r1", ["restored"] * len(fixture.rels)] for run_id in OVER[1:]})
    return trees_after, states


def published_over(fixture, root, fallback, relinked=False):
    """Publish r1, r2 and r3, each with its own module file; r1's module files.

    With relinked, r1's class links are removed before r2, which makes them.
    """
    fixture.build(root)
    for run_id in OVER:
        assert finish(fixture, fixture.run_args(root, run_id), root, FAKE_EB_MODULE_NOTE=run_id) == 0
        if run_id == "r1":
            modules = module_texts(fixture, root)
            if relinked:
                for rel in fixture.rels:
                    os.unlink(os.path.join(root, "modules", "tools", f"{rel}.lua"))
    if fallback:
        without_exchange(root, OVER[-1])
    return modules


def rollback_to_matrix(fixture, fallback=False):
    """rollback --to r1 over r2 and r3, killed at every step, then run again.

    It must end as to_expected says, with the module files r1 published.
    """
    label = fixture.name + (", rename fallback" if fallback else "")
    expected, states = to_expected(fixture)
    failures = total = 0
    for step in golden_steps(f"{label}: rollback --to"):
        def body(root):
            modules = published_over(fixture, root, fallback)
            args = ["rollback", "--state-root", f"{root}/state", "--to", "r1"]
            how = kill_at(fixture, args, step, root)
            rc = finish(fixture, args, root)
            return {"kill": how, "rollback_rc": rc, "published_modules": modules, **to_outcome(fixture, root)}
        results = fresh("crash-to", body)
        final = results
        ok = settled(f"{label}: rollback --to killed at {step}", results) and final["kill"] == "killed" and final["rollback_rc"] == 0
        ok = ok and final["trees"] == expected and final["states"] == states
        ok = ok and final["modules"] == final["published_modules"]
        failures += report(f"{label}: rollback --to killed at {step}", results, ok)
        total += 1
    return failures, total


def retire_outcome(root):
    module = os.path.join(root, "modules", "all", "demo", "1.lua")
    text = open(module).read().replace(root, "W") if os.path.exists(module) else None
    runs = os.path.join(root, "state", "runs")
    states = []
    for name in sorted(os.listdir(runs)) if os.path.isdir(runs) else []:
        # A kill before the first journal rename leaves a run without one.
        if not os.path.exists(os.path.join(runs, name, "journal.json")):
            continue
        with open(os.path.join(runs, name, "journal.json")) as handle:
            states.append(json.load(handle)["state"])
    links = {}
    for rel in RETIRE_LINKS:
        path = os.path.join(root, "modules", rel)
        links[rel] = os.readlink(path).replace(root, "W") if os.path.islink(path) else None
    return {"module": text, "links": links, "states": states}


def retire_run_id(root):
    runs = os.path.join(root, "state", "runs")
    names = sorted(
        name for name in os.listdir(runs)
        if name.startswith("retire-") and os.path.exists(os.path.join(runs, name, "journal.json"))
    ) if os.path.isdir(runs) else []
    return names[-1] if names else None


def retire_matrix():
    """A retire, and a rollback of one, killed at every step, then rolled back.

    The module file and the links to it must end as they began; a retire
    record left by the kill must refuse a second retire until it is rolled
    back.
    """
    original = 'local root = "W/software/demo/1"\nsetenv("EBROOTDEMO", root)\n'
    retire_list, rollback_list = golden_steps("retire: retire"), golden_steps("retire: rollback")
    failures = total = 0
    retire_args = lambda root: [  # noqa: E731
        "retire", "--state-root", f"{root}/state", "--module-file", f"{root}/modules/all/demo/1.lua",
    ]
    for step in retire_list:
        def body(root):
            retire_fixture(root)
            how = kill_at(ONE, retire_args(root), step, root)
            run_id = retire_run_id(root)
            again = finish(ONE, retire_args(root), root) if run_id else None
            back = finish(ONE, ["rollback", "--state-root", f"{root}/state", run_id], root) if run_id else None
            return {"kill": how, "again_rc": again, "rollback_rc": back, **retire_outcome(root)}
        results = fresh("crash-retire", body)
        final = results
        ok = settled(f"retire killed at {step}", results) and final["kill"] == "killed" and final["module"] == original
        ok = ok and final["links"] == RETIRE_LINKS
        ok = ok and (final["again_rc"] in (None, 2)) and set(final["states"]) <= {"rolled_back"}
        failures += report(f"retire killed at {step}", results, ok)
        total += 1
    for step in rollback_list:
        def body(root):
            retire_fixture(root)
            assert finish(ONE, retire_args(root), root) == 0
            run_id = retire_run_id(root)
            how = kill_at(ONE, ["rollback", "--state-root", f"{root}/state", run_id], step, root)
            back = finish(ONE, ["rollback", "--state-root", f"{root}/state", run_id], root)
            return {"kill": how, "rollback_rc": back, **retire_outcome(root)}
        results = fresh("crash-retire-rb", body)
        final = results
        ok = settled(f"retire rollback killed at {step}", results) and final["kill"] == "killed" and final["module"] == original
        ok = ok and final["links"] == RETIRE_LINKS
        ok = ok and final["rollback_rc"] == 0 and final["states"] == ["rolled_back"]
        failures += report(f"retire rollback killed at {step}", results, ok)
        total += 1
    return failures, total


def report(label, results, ok):
    if ok:
        print(f"same {label}: {results.get('states')}")
        return 0
    print(f"BAD  {label}")
    print(f"     got: {results}")
    return 1


def main():
    if not os.environ.get("TOOL_BIN"):
        print("set TOOL_BIN", file=sys.stderr)
        return 2
    failures = total = 0
    for fixture in (ONE, TWO, PREPARE, SEALED, VERIFY):
        f, t = run_matrix(fixture)
        failures, total = failures + f, total + t
    f, t = fallback_matrix()
    failures, total = failures + f, total + t
    for fixture, fallback in ((ONE, False), (TWO, False), (ONE, True)):
        f, t = rollback_to_matrix(fixture, fallback)
        failures, total = failures + f, total + t
    f, t = retire_matrix()
    failures, total = failures + f, total + t
    write_recorded()
    print(f"{total - failures} of {total} cases match")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
