"""A failed release of the state-root lock, injected at the fcntl or flock call.

After a command that succeeded, the binary exits 2 with the OSError text and
no path. After a refusal under the lock the binary keeps the refusal.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import _program, _skip, classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")
_EIO = "eb-staged-rebuild: [Errno 5] Input/output error\n"
# The record lock's release is the second F_SETLK on .lock.posix, and the
# gate's the second flock on .lock: the first of each is the acquire.
_RELEASES = (("fcntl", "state/.lock.posix"), ("flock", "state/.lock"))


def _journal(root, run_id="run1"):
    path = os.path.join(root, "state", "runs", run_id, "journal.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _payload(root):
    with open(os.path.join(root, "software", "demo", "1", "PAYLOAD"), encoding="utf-8") as handle:
        return handle.read()


class LockReleaseErrorTest(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("TOOL_BIN"):
            _skip(self, "TOOL_BIN is unset")
        self.program = _program(self, "eb-fs-fail-lock-release")
        self.front = [os.environ["TOOL_BIN"]]

    def _layout(self):
        root = tempfile.mkdtemp(prefix="lock-release-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nexec {sys.executable} {_FAKE} \"$@\"\n")
        os.chmod(eb, stat.S_IRWXU)
        env = {
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
        }
        state = os.path.join(root, "state")
        run = [
            "run", "--software", software, "--modules", modules, "--state-root", state,
            "--backend", "installpath", "--eb", eb, "--run-id", "run1", "--skip-live-check",
            "--lock-timeout", "0", "--module", "demo/1", "--", "demo-1.eb",
        ]
        return root, env, state, run

    def _plain(self, argv, root, env):
        done = subprocess.run(argv, cwd=root, env=env, capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stderr)

    def _traced(self, argv, root, env, op, needle):
        # The tracer must be the parent of the tool, so run_cli cannot do this.
        done = subprocess.run(
            [self.program, "5", op, "2", os.path.join(root, needle), "--", *argv],
            cwd=root, env=env, capture_output=True, text=True, check=False,
        )
        return done.returncode, done.stdout.replace(root, "ROOT"), done.stderr.replace(root, "ROOT")

    def _each(self, before, command, check):
        """Run COMMAND under each injected release failure.

        BEFORE prepares a fresh layout; CHECK sees the outcome and the layout
        it left. The lock is free afterwards: a gc with --lock-timeout 0 takes it.
        """
        front = self.front
        for op, needle in _RELEASES:
            with self.subTest(op=op):
                root, env, state, run = self._layout()
                before(front, root, env, state, run)
                outcome = self._traced(command(front, state, run), root, env, op, needle)
                self._plain([*front, "gc", "--state-root", state, "--lock-timeout", "0"], root, env)
                check(outcome, root)

    def test_gc_reports_the_failed_release_and_prints_no_actions(self):
        def check(outcome, _root):
            self.assertEqual(outcome, (2, "", _EIO))

        self._each(
            lambda *_: None,
            lambda front, state, _run: [*front, "gc", "--state-root", state, "--lock-timeout", "0", "--json"],
            check,
        )

    def test_run_reports_the_failed_release_after_publishing(self):
        def check(outcome, root):
            self.assertEqual(outcome[0::2], (2, _EIO))
            self.assertEqual(_journal(root)["state"], "complete")
            self.assertEqual(_payload(root), "new\n")

        self._each(lambda *_: None, lambda front, _state, run: [*front, *run], check)

    def test_resume_of_a_complete_run_reports_the_failed_release(self):
        def check(outcome, root):
            self.assertEqual(outcome[0::2], (2, _EIO))
            self.assertEqual(_journal(root)["state"], "complete")

        self._each(
            lambda front, root, env, _state, run: self._plain([*front, *run], root, env),
            lambda front, state, _run: [*front, "resume", "--state-root", state, "--lock-timeout", "0", "run1"],
            check,
        )

    def test_rollback_reports_the_failed_release_after_rolling_back(self):
        def check(outcome, root):
            self.assertEqual(outcome[0::2], (2, _EIO))
            self.assertEqual(_journal(root)["state"], "rolled_back")
            self.assertEqual(_payload(root), "old\n")

        self._each(
            lambda front, root, env, _state, run: self._plain([*front, *run], root, env),
            lambda front, state, _run: [*front, "rollback", "--state-root", state, "--lock-timeout", "0", "run1"],
            check,
        )

    def test_a_refusal_under_the_lock_is_kept_by_the_binary(self):
        # The binary drops the failed release and reports the refusal.
        refusal = "eb-staged-rebuild: run run1 already exists; resume it or choose another --run-id\n"

        def check(outcome, _root):
            self.assertEqual(outcome[0::2], (2, refusal))

        self._each(
            lambda front, root, env, _state, run: self._plain([*front, *run], root, env),
            lambda front, _state, run: [*front, *run],
            check,
        )


if __name__ == "__main__":
    unittest.main()
