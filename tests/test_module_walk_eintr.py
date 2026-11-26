"""An interrupted read of the staged module tree is retried.

A directory that cannot be read for another reason refuses the tree. The
tracer has to be the parent of the program named by TOOL_BIN, so this file
does not go through ``tests.support.run_cli``.
"""

import errno
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.support import _program, classic_install, golden, scrub, tool_bin

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")
_REFUSED = (errno.EACCES, errno.EIO, errno.ENOTDIR)


class ModuleWalkEintrTest(unittest.TestCase):
    def setUp(self):
        self.program = _program(self, "eb-fs-fail-walk")

    def _layout(self):
        root = tempfile.mkdtemp(prefix="walk-eintr-")
        self.addCleanup(shutil.rmtree, root, True)
        software = os.path.join(root, "software")
        modules = os.path.join(root, "modules")
        state = os.path.join(root, "state")
        os.makedirs(software)
        os.makedirs(modules)
        classic_install(software, modules, "demo/1", "old\n", "all/demo/1.lua")
        eb = os.path.join(root, "eb")
        with open(eb, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\nexec {sys.executable} {_FAKE} \"$@\"\n")
        os.chmod(eb, 0o700)
        args = [
            "run",
            "--software", software,
            "--modules", modules,
            "--state-root", state,
            "--backend", "installpath",
            "--eb", eb,
            "--run-id", "run1",
            "--skip-live-check",
            "--lock-timeout", "0",
            "--module", "demo/1",
            "--",
            "demo-1.eb",
        ]
        env = {
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        return root, state, args, env

    def test_an_interrupted_module_walk_is_retried(self):
        root, state, args, env = self._layout()
        staged = os.path.join(state, "generations", "run1", "modules")
        got = subprocess.run(
            [self.program, "4", "open", "1", staged, "--", tool_bin(), *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            recorded = json.load(handle)["state"]
        self.assertEqual(recorded, "complete")

    def test_an_interrupted_subdirectory_is_retried(self):
        root, state, args, env = self._layout()
        staged = os.path.join(state, "generations", "run1", "modules")
        got = subprocess.run(
            [self.program, "4", "open", "1", os.path.join(staged, "all", "demo"), "--", tool_bin(), *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)

    def _refused_run(self, command, number=errno.EACCES):
        root, state, args, env = self._layout()
        staged = os.path.join(state, "generations", "run1", "modules")
        got = subprocess.run(
            [self.program, str(number), "open", "1", os.path.join(staged, "all", "demo"), "--", *command, *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            recorded = json.load(handle)["state"]
        return root, staged, got, recorded

    def test_an_unreadable_module_directory_refuses_the_run(self):
        root, staged, got, recorded = self._refused_run([tool_bin()])
        self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
        self.assertTrue(got.stdout.endswith(f"eb-staged-rebuild: module tree is unreadable: {staged}\n"), got.stdout)
        self.assertEqual(recorded, "failed")
        golden(self, "stdout", scrub(got.stdout, ROOT=root))
        golden(self, "stderr", scrub(got.stderr, ROOT=root))

    def test_each_error_that_is_not_an_interrupt_refuses_the_tree(self):
        for number in _REFUSED:
            with self.subTest(errno=number):
                _root, staged, got, recorded = self._refused_run([tool_bin()], number)
                self.assertEqual(got.returncode, 2, got.stdout + got.stderr)
                self.assertTrue(got.stdout.endswith(f"eb-staged-rebuild: module tree is unreadable: {staged}\n"), got.stdout)
                self.assertEqual(recorded, "failed")
