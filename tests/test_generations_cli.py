"""list from the command line, for both implementations.

Each case publishes demo/1 one or more times with tests/fake_eb.py, each run
with its own payload, and reads the generations back with ``list``. Runs the
Python tool and, when TOOL_BIN is set, the program it names.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_generations_cli
"""

import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

from tests.support import Site, implementations, write

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
MISSING = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
PASS = "pass (modules skipped)"


def second(site):
    """A live demo/2 beside demo/1, so that one run can publish both."""
    live = os.path.join(site.software, "demo", "2")
    write(os.path.join(live, "PAYLOAD"), "old\n")
    write(os.path.join(site.modules, "all", "demo", "2.lua"), f'local root = "{live}"\nsetenv("EBROOTDEMO", root)\n')
    return live


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

    def sites(self):
        for implementation in implementations():
            with self.subTest(tool=implementation.name):
                root = tempfile.mkdtemp(prefix="generations-cli-")
                try:
                    yield Site(implementation, root)
                finally:
                    shutil.rmtree(root)

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
        for site in self.sites():
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
        for site in self.sites():
            refused = site.run(run_id="r1", extra=MISSING)
            self.assertEqual(refused.returncode, 2, refused.stdout + refused.stderr)
            self.assertEqual(self.listed(site), ["demo/1  SW/demo/1", row("live", "-", "-", when="-")])

    def test_a_tree_whose_run_has_no_journal_is_unknown_and_gc_removes_it(self):
        for site in self.sites():
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
        ready = os.path.join(site.root, "paused")
        env = site.environ(EB_STAGED_REBUILD_PAUSE_AT=step, EB_STAGED_REBUILD_PAUSE_READY=ready)
        command = [*site.implementation.command, "rollback", "--state-root", site.state, *args]
        with subprocess.Popen(command, env=env, cwd=site.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            deadline = time.monotonic() + 60
            while not os.path.exists(ready):
                self.assertIsNone(proc.poll(), f"rollback exited before {step}")
                self.assertLess(time.monotonic(), deadline, f"rollback never reached {step}")
                time.sleep(0.05)
            proc.kill()

    def module(self, site):
        with open(os.path.join(site.modules, "all", "demo", "1.lua"), encoding="utf-8") as handle:
            return handle.read()

    def state(self, site, run_id):
        journal = site.journal(run_id)
        return journal["state"], journal.get("rollback_to"), journal["message"]

    def test_rollback_without_a_run_id_or_latest_is_a_usage_error(self):
        for site in self.sites():
            self.publish(site, "r1")
            refused = self.rollback(site, code=2)
            self.assertEqual(
                refused.stderr.splitlines()[-1],
                "eb-staged-rebuild rollback: error: the following arguments are required: run_id",
            )
            self.assertEqual(site.payload(), "r1\n")
            self.assertEqual(site.journal("r1")["state"], "complete")

    def test_rollback_latest_steps_back_one_generation_at_a_time(self):
        for site in self.sites():
            self.publish(site, "r1")
            self.publish(site, "r2")
            self.assertIn("run r2 rolled back", self.rollback(site, "--latest").stdout)
            self.assertEqual(site.payload(), "r1\n")
            self.assertIn("run r1 rolled back", self.rollback(site, "--latest").stdout)
            self.assertEqual(site.payload(), "old\n")
            refused = self.rollback(site, "--latest", code=2)
            self.assertIn("no complete run to roll back", refused.stderr)

    def test_rollback_to_switches_once_and_keeps_each_tree_at_its_retired_path(self):
        for site in self.sites():
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
        for site in self.sites():
            self.publish(site, "r1")
            self.publish(site, "r2")
            done = self.rollback(site, "--to", "r1")
            self.assertIn("run r2 rolled back; replacement kept at its retired path", done.stdout)
            self.assertEqual(self.state(site, "r2"), ("rolled_back", None, "rolled back"))

    def test_rollback_to_refuses_what_it_cannot_switch_to(self):
        for site in self.sites():
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
        for site in self.sites():
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
        for site in self.sites():
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


if __name__ == "__main__":
    unittest.main()
