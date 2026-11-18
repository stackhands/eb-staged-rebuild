"""list from the command line, for both implementations.

Each case publishes demo/1 one or more times with tests/fake_eb.py, each run
with its own payload, and reads the generations back with ``list``. Runs the
Python tool and, when TOOL_BIN is set, the program it names.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_generations_cli
"""

import os
import re
import shutil
import tempfile
import unittest

from tests.support import Site, implementations

ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
MISSING = {"elf": {"bin/demo": {"needed": ["libgone.so.1"], "runpath": "$ORIGIN/../lib"}}}
PASS = "pass (modules skipped)"


def row(state, run, verify, path=None, when=None):
    """One list line; ``when`` is a literal published column, or a time when None."""
    stamp = "TIME" if when is None else f"{when:<20}"
    text = f"  {state:<12} {stamp}  {run}  verify {verify}"
    return text if path is None else f"{text}  {path}"


class GenerationsCliTests(unittest.TestCase):
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
        done = site.run(*options, run_id=run_id, FAKE_EB_PAYLOAD=f"{run_id}\n", **env)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        return done

    def listed(self, site):
        done = site.cli("list", "--state-root", site.state)
        self.assertEqual((done.returncode, done.stderr), (0, ""), done.stdout + done.stderr)
        return re.sub(ISO, "TIME", done.stdout).replace(site.software, "SW").splitlines()

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


if __name__ == "__main__":
    unittest.main()
