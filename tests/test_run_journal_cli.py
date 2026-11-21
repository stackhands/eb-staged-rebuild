"""verify and resume check a run's journal once, where they load it, for both implementations.

Each test changes the journal of a built run in one way the commands must
not act on, then runs ``verify``, ``resume --verify`` and ``resume`` on it.
Both front ends refuse it with exit 2 and the same message, before anything
under the site is written. Runs the Python tool and, when TOOL_BIN is set,
the program it names.

    TOOL_BIN=zig-out/bin/eb-staged-rebuild python3 -m unittest -v tests.test_run_journal_cli
"""

import json
import os
import unittest

from tests.support import FAKE_LMOD, Site, sites, write

LOCK_FILES = (".lock", ".lock.posix")


def snapshot(root):
    """Every path under ROOT with its bytes or link target; the state root's lock files are left out."""
    found = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(directory, name)
            if name in LOCK_FILES:
                continue
            if os.path.islink(path):
                found[path] = ("link", os.readlink(path))
            elif os.path.isfile(path):
                with open(path, "rb") as handle:
                    found[path] = ("file", handle.read())
            else:
                found[path] = ("dir", None)
    return found


class RunJournalCliTests(unittest.TestCase):
    def setUp(self):
        self.previous_umask = os.umask(0o022)

    def tearDown(self):
        os.umask(self.previous_umask)

    def built(self):
        """One site per front end whose run r1 is built and not yet verified."""
        made = sites(self, Site, "run-journal-cli-")
        for site in made:
            prepare = os.path.join(site.root, "prepare")
            write(prepare, "#!/bin/sh\nexit 42\n", 0o755)
            stopped = site.run("--prepare", prepare, verify=False)
            self.assertEqual(stopped.returncode, 42, stopped.stdout + stopped.stderr)
            self.assertEqual(site.journal()["state"], "built")
            write(prepare, "#!/bin/sh\nexit 0\n", 0o755)
        return made

    def assert_refused(self, site, message, run_id="r1", commands=(["verify"], ["resume", "--verify"], ["resume"]), **env):
        """Each of COMMANDS on RUN_ID exits 2 with MESSAGE and writes nothing."""
        for command in commands:
            with self.subTest(command=command[0], verify=command[1:]):
                before = snapshot(site.root)
                done = site.cli(*command, "--state-root", site.state, run_id, **env)
                self.assertEqual(
                    (done.returncode, done.stdout, done.stderr), (2, "", f"eb-staged-rebuild: {message}\n")
                )
                self.assertEqual(snapshot(site.root), before)

    def edit(self, site, change):
        journal = site.journal()
        change(journal)
        write(site.journal_path(), json.dumps(journal, indent=2, sort_keys=True) + "\n")

    def test_with_lmod_a_journal_without_its_staged_module_tree(self):
        verifying = (["verify"], ["resume", "--verify"])
        for site in self.built():
            with self.subTest(tool=site.name):
                original = site.journal()
                for change in (lambda journal: journal.pop("stage_modules"), lambda journal: journal.update(stage_modules="")):
                    self.edit(site, change)
                    self.assert_refused(site, "journal does not record stage_modules", commands=verifying, LMOD_CMD=FAKE_LMOD)
                    self.edit(site, lambda journal: journal.update(original))


if __name__ == "__main__":
    unittest.main()
