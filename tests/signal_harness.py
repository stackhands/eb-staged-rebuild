"""Signal a run while its build command runs, for TOOL_BIN.

The fake eb's build step is a worker that starts
descendants, and the harness signals the tool (or lets the worker exit
with stray workers left). It requires the state lock to stay held until
the command group has stopped, no descendant to outlive the run, no
process to hold the worker's output file open, and the exit code each
case names. A retire while another process holds the
state lock must be refused and leave the module file.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 tests/signal_harness.py
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
sys.path.insert(0, REPO)

from tests.support import StateRootLock  # noqa: E402


def group_members(pgid):
    """The live processes whose process group is PGID."""
    members = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", encoding="utf-8") as handle:
                fields = handle.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[2]) == pgid and fields[0] not in ("Z", "X"):
            members.append(int(name))
    return members


def holders(path):
    """The processes that hold PATH open."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            for fd in os.listdir(f"/proc/{name}/fd"):
                if os.readlink(f"/proc/{name}/fd/{fd}") == str(path):
                    found.append(int(name))
        except OSError:
            continue
    return found

# The exit status of a run whose build command exits while its workers still run.
STRAY_EXIT = 2

LEAF = (
    "import signal, sys, time\nfrom pathlib import Path\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    "Path(sys.argv[1]).touch()\n"
    "while True: time.sleep(0.05)\n"
)


def lifecycle_worker(root, ignore_term):
    return (
        "import json, os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + f"root = Path({root!r})\n"
        "leaf = subprocess.Popen([sys.executable, str(root / 'leaf.py'), str(root / 'leaf-ready')])\n"
        "while not (root / 'leaf-ready').exists(): time.sleep(0.01)\n"
        "(root / 'pids.json.tmp').write_text(json.dumps([os.getpid(), leaf.pid])); os.replace(root / 'pids.json.tmp', root / 'pids.json')\n"
        "while True: time.sleep(0.05)\n"
    )


def group_worker(root, interrupt):
    # Eight stray workers that ignore SIGTERM and hold the output file open.
    return (
        "import json, os, signal, time\nfrom pathlib import Path\n"
        f"root = Path({root!r})\n"
        "fd = os.open(str(root / 'output'), os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "for _ in range(8):\n"
        "    if os.fork() == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        while True: time.sleep(0.05)\n"
        "(root / 'pids.json.tmp').write_text(json.dumps([os.getpid()])); os.replace(root / 'pids.json.tmp', root / 'pids.json')\n"
        + ("while True: time.sleep(0.05)\n" if interrupt else "time.sleep(0.2)\n")
    )


def fixture(root, worker):
    os.makedirs(os.path.join(root, "modules", "all", "demo"))
    os.makedirs(os.path.join(root, "software", "demo", "1", "easybuild"))
    os.makedirs(os.path.join(root, "state"))
    with open(os.path.join(root, "software", "demo", "1", "easybuild", "demo-1.eb"), "w") as handle:
        handle.write("name = 'demo'\n")
    with open(os.path.join(root, "modules", "all", "demo", "1.lua"), "w") as handle:
        handle.write(f'local root = "{root}/software/demo/1"\n')
    with open(os.path.join(root, "leaf.py"), "w") as handle:
        handle.write(LEAF)
    with open(os.path.join(root, "worker.py"), "w") as handle:
        handle.write(worker)
    # The preflight and the version probe go to the fake eb; the build runs the worker.
    eb = os.path.join(root, "eb")
    with open(eb, "w") as handle:
        handle.write(
            "#!/bin/sh\n"
            "for arg in \"$@\"; do case \"$arg\" in --dry-run|--version) "
            f"exec \"{sys.executable}\" \"{FAKE_EB}\" \"$@\";; esac; done\n"
            f"exec \"{sys.executable}\" \"{root}/worker.py\"\n"
        )
    os.chmod(eb, 0o755)


def command():
    return [os.environ["TOOL_BIN"]]


def start(root):
    args = [
        "run", "--software", f"{root}/software", "--modules", f"{root}/modules",
        "--state-root", f"{root}/state", "--backend", "installpath", "--eb", f"{root}/eb",
        "--run-id", "r1", "--module", "demo/1", "--", "demo-1.eb",
    ]
    return subprocess.Popen(command() + args, env=os.environ, cwd=root,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for(path, proc):
    # The helpers write pids.json through a rename, so it exists only once complete.
    deadline = time.monotonic() + 30
    while not os.path.exists(path):
        if proc.poll() is not None:
            raise AssertionError(f"the run exited {proc.returncode} before the command was ready")
        if time.monotonic() > deadline:
            raise AssertionError("the command never became ready")
        time.sleep(0.01)
    with open(path) as handle:
        return json.load(handle)


def lock_is_held(root):
    contender = StateRootLock(os.path.join(root, "state"))
    try:
        contender.acquire()
    except BlockingIOError:
        return True
    contender.release()
    return False


def alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state not in ("Z", "X")


def lifecycle(root, sig, ignore_term):
    fixture(root, lifecycle_worker(root, ignore_term))
    proc = start(root)
    pids = []
    try:
        pids = wait_for(os.path.join(root, "pids.json"), proc)
        proc.send_signal(sig)
        problems = []
        if ignore_term and not lock_is_held(root):
            problems.append("lock released while the command group ran")
        rc = proc.wait(timeout=30)
        problems += [f"descendant {pid} outlived the run" for pid in pids if alive(pid)]
        if lock_is_held(root):
            problems.append("lock still held after the run")
        return rc, problems
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def group_exit(root, interrupt):
    fixture(root, group_worker(root, interrupt))
    proc = start(root)
    pgid = None
    try:
        pgid = wait_for(os.path.join(root, "pids.json"), proc)[0]
        if interrupt:
            proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=60)
        problems = [f"group member {pid} outlived the run" for pid in group_members(pgid)]
        problems += [f"{pid} still holds the output file" for pid in holders(os.path.join(root, "output"))]
        return rc, problems
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if pgid:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def lock_held(root):
    """retire while another process holds the state lock: refused, module kept."""
    fixture(root, "")
    module = os.path.join(root, "modules", "all", "demo", "1.lua")
    before = open(module).read()
    lock = StateRootLock(os.path.join(root, "state"))
    lock.acquire()
    try:
        done = subprocess.run(
            command() + ["retire", "--state-root", f"{root}/state", "--module-file", module],
            env=os.environ, cwd=root, capture_output=True, text=True, timeout=60,
        )
    finally:
        lock.release()
    problems = []
    if "lock" not in done.stderr + done.stdout:
        problems.append(f"no lock in the refusal: {done.stderr.strip()!r}")
    if not os.path.exists(module) or open(module).read() != before:
        problems.append("module file changed")
    return done.returncode, problems


def main():
    if not os.environ.get("TOOL_BIN"):
        print("set TOOL_BIN", file=sys.stderr)
        return 2
    cases = [
        ("SIGTERM, command ignores SIGTERM: lock held until the group stops",
         lambda root: lifecycle(root, signal.SIGTERM, True), 128 + signal.SIGTERM),
        ("SIGINT, direct child exits first: descendants stopped",
         lambda root: lifecycle(root, signal.SIGINT, False), 128 + signal.SIGINT),
        ("command exits with stray workers: run returns after they exited",
         lambda root: group_exit(root, False), STRAY_EXIT),
        ("SIGTERM with stray workers: run returns after every worker exited",
         lambda root: group_exit(root, True), 128 + signal.SIGTERM),
        ("retire while the state lock is held: refused, module kept",
         lock_held, 2),
    ]
    failures = 0
    for label, body, expected in cases:
        root = tempfile.mkdtemp(prefix="sig-")
        try:
            rc, problems = body(root)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        bad = list(problems)
        if rc != expected:
            bad.append(f"exit {rc}, expected {expected}")
        if bad:
            failures += 1
            print(f"BAD  {label}: {'; '.join(bad)}")
        else:
            print(f"same {label}: exit {rc}")
    print(f"{len(cases) - failures} of {len(cases)} cases pass")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
