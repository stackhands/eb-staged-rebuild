"""list, rollback --latest and rollback --to from the command line.

Each case publishes demo/1 one or more times with tests/fake_eb.py, each run
with its own payload, and reads the generations back with ``list``. Runs the
program TOOL_BIN names.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_generations_cli
"""

import os
import re
import shutil
import subprocess
import sys
import unittest

from tests.support import pause_fifo, tool_bin, wait_for_pause, write
from tests.support import site as make_site

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
MISSING = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
PASS = "pass (modules skipped)"
# Holds a tree in use the way its first argument names, prints "ready", and
# exits when its standard input closes.
HOLDER = r"""
import ctypes, os, sys
how, path = sys.argv[1:]
if how == "open":
    held = open(path, "rb")
elif how == "map":
    libc = ctypes.CDLL(None, use_errno=True)
    libc.mmap.restype = ctypes.c_void_p
    libc.mmap.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long)
    fd = os.open(path, os.O_RDONLY)
    if libc.mmap(None, os.fstat(fd).st_size, 1, 2, fd, 0) == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_errno(), "mmap")
    os.close(fd)
elif how == "chroot":
    os.unshare(os.CLONE_NEWUSER)
    os.chroot(path)
print("ready", flush=True)
sys.stdin.read()
"""


def second(site):
    """A live demo/2 beside demo/1, so that one run can publish both."""
    live = os.path.join(site.software, "demo", "2")
    write(os.path.join(live, "PAYLOAD"), "old\n")
    write(os.path.join(site.modules, "all", "demo", "2.lua"), f'local root = "{live}"\nsetenv("EBROOTDEMO", root)\n')
    return live


def unprivileged_namespaces():
    if shutil.which("unshare") is None:
        return False
    return subprocess.run(["unshare", "-rm", "true"], capture_output=True).returncode == 0


def row(state, run, verify, path=None, when=None):
    """One list line; ``when`` is a literal published column, or a time when None."""
    stamp = "TIME" if when is None else f"{when:<20}"
    text = f"  {state:<12} {stamp}  {run}  verify {verify}"
    return text if path is None else f"{text}  {path}"


class Generations(unittest.TestCase):
    def setUp(self):
        self.previous_umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self.previous_umask)

    def site(self):
        return make_site(self, "generations-cli-")

    def publish(self, site, run_id, *options, **env):
        done = site.run(*options, run_id=run_id, FAKE_EB_PAYLOAD=f"{run_id}\n", FAKE_EB_MODULE_NOTE=run_id, **env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def listed(self, site):
        done = site.cli("list", "--state-root", site.state)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout + done.stderr)
        return re.sub(ISO, "TIME", done.stdout).replace(site.software, "SW").splitlines()


class GenerationsCliTests(Generations):
    def test_list_shows_each_generation_live_first_then_newest(self):
        site = self.site()
        self.assertEqual(self.listed(site), ["no generations"])
        self.publish(site, "r1")
        self.publish(site, "r2", "--force", extra=MISSING)
        self.publish(site, "r3")
        back = site.cli("rollback", "--state-root", site.state, "r3")
        self.assertEqual(back.returncode, 0, back.stdout + back.stderr)
        self.assertEqual(site.payload(), "r2\n")
        self.assertEqual(
            self.listed(site),
            [
                "demo/1  SW/demo/1",
                row("live", "r2", "forced (1 problem)"),
                row("rolled back", "r3", PASS, "SW/demo/.1.r3"),
                row("replaced", "r1", PASS, "SW/demo/.1.r2"),
                row("original", "-", "-", "SW/demo/.1.r1", when="-"),
            ],
        )

    def test_a_stage_that_was_never_published_is_not_a_generation(self):
        site = self.site()
        refused = site.run(run_id="r1", extra=MISSING)
        self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
        self.assertEqual(self.listed(site), ["demo/1  SW/demo/1", row("live", "-", "-", when="-")])

    def test_a_tree_whose_run_has_no_journal_is_unknown_and_gc_removes_it(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        shutil.rmtree(os.path.join(site.state, "runs", "r1"))
        self.assertEqual(
            self.listed(site),
            ["demo/1  SW/demo/1", row("live", "r2", PASS), row("unknown", "r1", "-", "SW/demo/.1.r2", when="-")],
        )
        collected = site.cli("gc", "--state-root", site.state, "--grace-hours", "0")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        self.assertEqual(self.listed(site), ["demo/1  SW/demo/1", row("live", "r2", PASS)])


class RollbackCliTests(Generations):
    """rollback --latest, and rollback --to a named generation."""

    def rollback(self, site, *args, code=0, **env):
        done = site.cli("rollback", "--state-root", site.state, *args, **env)
        self.assertEqual(done.returncode, code, done.stdout + done.stderr)
        return done

    def killed_at(self, site, step, *args):
        """Run rollback until it pauses before ``step``, then SIGKILL it there."""
        ready, fifo = pause_fifo(site.root)
        env = site.environ(EB_STAGED_REBUILD_PAUSE_AT=step, EB_STAGED_REBUILD_PAUSE_READY=ready)
        command = [tool_bin(), "rollback", "--state-root", site.state, *args]
        with subprocess.Popen(command, env=env, cwd=site.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            reached = wait_for_pause(proc, fifo)
            proc.kill()
        self.assertTrue(reached, f"rollback exited, or did not reach {step} within 60 seconds")

    def module(self, site):
        with open(os.path.join(site.modules, "all", "demo", "1.lua"), encoding="utf-8") as handle:
            return handle.read()

    def state(self, site, run_id):
        journal = site.journal(run_id)
        return journal["state"], journal.get("rollback_to"), journal["message"]

    def test_rollback_without_a_run_id_or_latest_is_a_usage_error(self):
        site = self.site()
        self.publish(site, "r1")
        refused = self.rollback(site, code=2)
        self.assertEqual(
            refused.stderr.splitlines()[-1],
            "eb-staged-rebuild rollback: error: the following arguments are required: run_id",
        )
        self.assertEqual(site.payload(), "r1\n")
        self.assertEqual(site.journal("r1")["state"], "complete")

    def test_rollback_latest_steps_back_one_generation_at_a_time(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        self.assertIn("run r2 rolled back", self.rollback(site, "--latest").stdout)
        self.assertEqual(site.payload(), "r1\n")
        self.assertIn("run r1 rolled back", self.rollback(site, "--latest").stdout)
        self.assertEqual(site.payload(), "old\n")
        refused = self.rollback(site, "--latest", code=2)
        self.assertIn("no complete run to roll back", refused.stderr)

    def test_rollback_to_switches_once_and_keeps_each_tree_at_its_retired_path(self):
        site = self.site()
        for run_id in ("r1", "r2", "r3", "r4"):
            self.publish(site, run_id)
        done = self.rollback(site, "--to", "r2")
        self.assertIn("rolled back to r2: runs r4, r3 rolled back; replacements kept at their retired paths", done.stdout)
        self.assertEqual(site.payload(), "r2\n")
        self.assertTrue(self.module(site).endswith("-- r2\n"), self.module(site))
        for run_id in ("r3", "r4"):
            self.assertEqual(self.state(site, run_id), ("rolled_back", "r2", "rolled back to r2"))
        self.assertEqual(
            self.listed(site),
            [
                "demo/1  SW/demo/1",
                row("live", "r2", PASS),
                row("rolled back", "r4", PASS, "SW/demo/.1.r4"),
                row("rolled back", "r3", PASS, "SW/demo/.1.r3"),
                row("replaced", "r1", PASS, "SW/demo/.1.r2"),
                row("original", "-", "-", "SW/demo/.1.r1", when="-"),
            ],
        )
        self.assertIn("run r2 is already live", self.rollback(site, "--to", "r2").stdout)
        self.assertIn("run r2 rolled back", self.rollback(site, "--latest").stdout)
        self.assertEqual(site.payload(), "r1\n")

    def test_rollback_to_the_generation_one_run_back_is_a_plain_rollback(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        done = self.rollback(site, "--to", "r1")
        self.assertIn("run r2 rolled back; replacement kept at its retired path", done.stdout)
        self.assertEqual(self.state(site, "r2"), ("rolled_back", None, "rolled back"))

    def test_rollback_to_refuses_what_it_cannot_switch_to(self):
        site = self.site()
        for run_id in ("r1", "r2", "r3"):
            self.publish(site, run_id)
        self.rollback(site, "r3")
        cases = [
            (("--to", "r1", "r2"), "give one of a run id, --latest and --to GEN"),
            (("--latest", "r2"), "give one of a run id, --latest and --to GEN"),
            (("--latest", "--to", "r1"), "give one of a run id, --latest and --to GEN"),
            (("--to", "r3"), "run r3 is rolled_back; rollback --to returns to a complete run"),
            (("--to", "r9"), "no journal at " + os.path.join(site.state, "runs", "r9", "journal.json")),
        ]
        for args, message in cases:
            self.assertIn(message, self.rollback(site, *args, code=2).stderr)
        self.publish(site, "r4")
        collected = site.cli("gc", "--state-root", site.state, "--grace-hours", "0")
        self.assertEqual(collected.returncode, 0, collected.stdout + collected.stderr)
        refused = self.rollback(site, "--to", "r1", code=2)
        hidden = os.path.join(site.software, "demo", ".1.r2")
        self.assertIn(f"cannot roll back to r1: {hidden} is missing, not a kept tree", refused.stderr)
        self.assertEqual(site.payload(), "r4\n")
        self.assertEqual(self.state(site, "r2")[0], "complete")

    def test_rollback_to_refuses_a_run_a_later_run_replaced_on_another_path(self):
        site = self.site()
        other = second(site)
        self.publish(site, "r1")
        self.publish(site, "r2")
        self.publish(site, "r3", modules=("demo/1", "demo/2"), FAKE_EB_RELS="demo/1,demo/2")
        self.publish(site, "r4", modules=("demo/2",), FAKE_EB_RELS="demo/2")
        refused = self.rollback(site, "--to", "r1", code=2)
        self.assertIn(f"run r4 published {other} after run r3; roll back run r4 first", refused.stderr)
        self.assertEqual(site.payload(), "r3\n")
        self.rollback(site, "r4")
        self.assertIn("rolled back to r1: runs r3, r2 rolled back", self.rollback(site, "--to", "r1").stdout)
        self.assertEqual(site.payload(), "r1\n")
        with open(os.path.join(other, "PAYLOAD"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "old\n")

    def test_an_interrupted_rollback_to_is_finished_by_running_it_again(self):
        site = self.site()
        for run_id in ("r1", "r2", "r3", "r4"):
            self.publish(site, run_id)
        self.killed_at(site, "rollback.swap:all/demo/1.lua.exchange.before", "--to", "r2")
        self.assertEqual(site.payload(), "r4\n")
        for run_id in ("r3", "r4"):
            self.assertEqual(self.state(site, run_id), ("rolling_back", "r2", "rolling back to r2"))
        self.assertEqual(
            self.listed(site),
            [
                "demo/1  SW/demo/1",
                row("live", "r4", PASS),
                row("rolling back", "r3", PASS, "SW/demo/.1.r3"),
                row("replaced", "r2", PASS, "SW/demo/.1.r4"),
                row("replaced", "r1", PASS, "SW/demo/.1.r2"),
                row("original", "-", "-", "SW/demo/.1.r1", when="-"),
            ],
        )
        finish = "run r4 is rolling back to r2; run rollback --to r2 to finish"
        self.assertIn(finish, self.rollback(site, "r4", code=2).stderr)
        self.assertIn(finish, site.cli("resume", "--state-root", site.state, "r4").stderr)
        self.assertIn("run r3 is rolling back to r2", self.rollback(site, "--to", "r1", code=2).stderr)
        self.assertIn("rolled back to r2: runs r4, r3 rolled back", self.rollback(site, "--to", "r2").stdout)
        self.assertEqual(site.payload(), "r2\n")
        self.assertTrue(self.module(site).endswith("-- r2\n"), self.module(site))
        self.assertEqual(site.journal("r3")["modules_plan"][0]["rollback_method"], "recovered")


class GcCliTests(Generations):
    """gc --keep N, and the trees gc --check-in-use keeps because this node shows them in use."""

    def gc(self, site, *args, command=()):
        done = site.cli("gc", "--state-root", site.state, *args, prefix=command)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout + done.stderr)
        return done.stdout.replace(site.software, "SW").replace(site.root, "ROOT").splitlines()

    def hidden(self, site, run_id):
        return os.path.join(site.software, "demo", f".1.{run_id}")

    def removed(self, run_id, why="previous live tree", run="complete run"):
        """The lines for a run's hidden tree and then its generation going."""
        return [f"remove: SW/demo/.1.{run_id} ({why})", f"remove: ROOT/state/generations/{run_id} (generation of a {run})"]

    def held(self, *command):
        holder = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait, 60)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline(), "ready\n")
        return holder

    def waiting(self, program, cwd):
        """PROGRAM, with its working directory in CWD, reading its standard input until the test closes it."""
        proc = subprocess.Popen([program], stdin=subprocess.PIPE, cwd=cwd)
        self.addCleanup(proc.wait, 60)
        self.addCleanup(proc.stdin.close)
        return proc

    def test_gc_keep_keeps_each_install_paths_newest_generations(self):
        site = self.site()
        for run_id in ("r1", "r2", "r3", "r4"):
            self.publish(site, run_id)
        self.assertEqual(
            self.gc(site, "--keep", "2", "--grace-hours", "144"),
            [
                "keep: SW/demo/.1.r1 (within grace (previous live tree))",
                "keep: SW/demo/.1.r2 (within grace (previous live tree))",
                "keep: SW/demo/.1.r3 (kept by --keep 2)",
                "keep: SW/demo/.1.r4 (kept by --keep 2)",
            ],
        )
        self.assertEqual(
            self.gc(site, "--keep", "2", "--grace-hours", "0"),
            [
                *self.removed("r1", "previous live tree, beyond --keep 2"),
                *self.removed("r2", "previous live tree, beyond --keep 2"),
                "keep: SW/demo/.1.r3 (kept by --keep 2)",
                "keep: SW/demo/.1.r4 (kept by --keep 2)",
            ],
        )
        self.assertEqual(
            self.listed(site),
            [
                "demo/1  SW/demo/1",
                row("live", "r4", PASS),
                row("replaced", "r3", PASS, "SW/demo/.1.r4"),
                row("replaced", "r2", PASS, "SW/demo/.1.r3"),
            ],
        )
        back = site.cli("rollback", "--state-root", site.state, "--to", "r2")
        self.assertEqual(back.returncode, 0, back.stdout + back.stderr)
        self.assertEqual(site.payload(), "r2\n")
        self.assertEqual(
            self.gc(site, "--keep", "2", "--grace-hours", "0"),
            [
                *self.removed("r3", "rolled-back candidate", "rolled-back run"),
                *self.removed("r4", "rolled-back candidate", "rolled-back run"),
            ],
        )
        self.assertEqual(self.listed(site), ["demo/1  SW/demo/1", row("live", "r2", PASS)])

    def test_without_check_in_use_gc_removes_a_tree_a_process_uses(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        self.waiting("cat", self.hidden(site, "r1"))
        self.assertEqual(self.gc(site, "--grace-hours", "0"), [*self.removed("r1"), *self.removed("r2")])
        self.assertFalse(os.path.lexists(self.hidden(site, "r1")))

    def test_gc_check_in_use_keeps_a_tree_a_process_on_this_node_uses(self):
        namespaces = unprivileged_namespaces()
        site = self.site()
        for run_id in ("r1", "r2", "r3", "r4", "r5"):
            self.publish(site, run_id)
        program = os.path.join(self.hidden(site, "r4"), "bin", "cat")
        os.makedirs(os.path.dirname(program))
        shutil.copy2(shutil.which("cat"), program)
        working = self.waiting("cat", self.hidden(site, "r1"))
        running = self.waiting(program, site.root)
        opened = self.held(sys.executable, "-c", HOLDER, "open", os.path.join(self.hidden(site, "r2"), "PAYLOAD"))
        mapped = self.held(sys.executable, "-c", HOLDER, "map", os.path.join(self.hidden(site, "r3"), "PAYLOAD"))
        expected = [
            f"keep: SW/demo/.1.r1 (in use on this node: pid {working.pid} has its working directory in it)",
            f"keep: SW/demo/.1.r2 (in use on this node: pid {opened.pid} has a file open in it)",
            f"keep: SW/demo/.1.r3 (in use on this node: pid {mapped.pid} maps a file in it)",
            f"keep: SW/demo/.1.r4 (in use on this node: pid {running.pid} runs a program in it)",
            *self.removed("r5"),
        ]
        if namespaces:
            rooted = self.held(sys.executable, "-c", HOLDER, "chroot", self.hidden(site, "r5"))
            expected[4:] = [f"keep: SW/demo/.1.r5 (in use on this node: pid {rooted.pid} has its root directory in it)"]
        everything = [line for run_id in ("r1", "r2", "r3", "r4", "r5") for line in self.removed(run_id)]
        self.assertEqual(self.gc(site, "--grace-hours", "0", "--dry-run"), everything)
        self.assertEqual(self.gc(site, "--grace-hours", "0", "--dry-run", "--check-in-use"), expected)
        self.assertEqual(
            self.gc(site, "--grace-hours", "0", "--keep", "1", "--check-in-use"),
            expected[:4] + ["keep: SW/demo/.1.r5 (kept by --keep 1)"],
        )
        for holder in [working, running, opened, mapped] + ([rooted] if namespaces else []):
            holder.stdin.close()
            holder.wait(60)
        self.assertEqual(self.gc(site, "--grace-hours", "0", "--check-in-use"), everything)

    def test_gc_check_in_use_keeps_a_generation_a_process_on_this_node_uses(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        generation = os.path.join(site.state, "generations", "r1")
        working = self.waiting("cat", generation)
        self.assertEqual(
            self.gc(site, "--grace-hours", "0", "--check-in-use"),
            [
                "remove: SW/demo/.1.r1 (previous live tree)",
                f"keep: ROOT/state/generations/r1 (in use on this node: pid {working.pid} has its working directory in it)",
                *self.removed("r2"),
            ],
        )
        self.assertTrue(os.path.isdir(generation))
        working.stdin.close()
        working.wait(60)
        self.assertEqual(self.gc(site, "--grace-hours", "0"), self.removed("r1")[1:])

    @unittest.skipUnless(unprivileged_namespaces(), "needs unprivileged user and mount namespaces")
    def test_gc_check_in_use_keeps_a_tree_another_mount_namespace_binds(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        target = os.path.join(site.root, "mnt")
        os.mkdir(target)
        script = 'mount --bind "$1" "$2" && echo ready && exec cat'
        bound = self.held("unshare", "-rm", "sh", "-c", script, "sh", self.hidden(site, "r2"), target)
        self.assertEqual(
            self.gc(site, "--grace-hours", "0", "--check-in-use"),
            [
                *self.removed("r1"),
                f"keep: SW/demo/.1.r2 (in use on this node: mounted at ROOT/mnt in the mount namespace of pid {bound.pid})",
            ],
        )
        bound.stdin.close()
        bound.wait(60)
        self.assertEqual(self.gc(site, "--grace-hours", "0"), self.removed("r2"))

    @unittest.skipUnless(unprivileged_namespaces(), "needs unprivileged user and mount namespaces")
    def test_gc_check_in_use_keeps_a_tree_with_a_mount_point_inside(self):
        site = self.site()
        self.publish(site, "r1")
        self.publish(site, "r2")
        inside = os.path.join(self.hidden(site, "r2"), "sub")
        os.mkdir(inside)
        mounted = ["unshare", "-rm", "sh", "-c", 'mount -t tmpfs tmpfs "$0" && exec "$@"', inside]
        self.assertEqual(
            self.gc(site, "--grace-hours", "0", "--check-in-use", command=mounted),
            [
                *self.removed("r1"),
                "keep: SW/demo/.1.r2 (in use on this node: SW/demo/.1.r2/sub is a mount point)",
            ],
        )
        self.assertTrue(os.path.isfile(os.path.join(self.hidden(site, "r2"), "PAYLOAD")))
        self.assertEqual(self.gc(site, "--grace-hours", "0"), self.removed("r2"))


if __name__ == "__main__":
    unittest.main()
