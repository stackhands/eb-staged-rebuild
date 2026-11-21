"""An interrupted read of the staged module tree is an operating-system error.

A directory that cannot be read for another reason refuses the tree, in
the Python tool and in the program named by TOOL_BIN. The tracer has to
be the parent of the interpreter and of that program, so this file does
not go through ``tests.support.run_cli``.
"""

import errno
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from tests.support import _program, _python314, classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")
_REFUSED = (errno.EACCES, errno.EIO, errno.ENOTDIR)

_CHILD = """\
import sys
sys.path.insert(0, sys.argv[1])
from eb_staged_rebuild.discover import walk_module_tree
from eb_staged_rebuild.errors import Refused
try:
    files, _links = walk_module_tree(sys.argv[2])
except OSError as exc:
    sys.stderr.write("raised %s\\n" % exc.errno)
    raise SystemExit(3)
except Refused as exc:
    sys.stderr.write("refused %s\\n" % exc)
    raise SystemExit(2)
sys.stdout.write("%s\\n" % len(files))
"""

class ModuleWalkEintrTest(unittest.TestCase):
    def setUp(self):
        self.python = _python314()
        if self.python is None:
            self.skipTest("python3.14 is not installed")
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
            handle.write(f"#!/bin/sh\nexec {self.python} {_FAKE} \"$@\"\n")
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
            "PYTHONPATH": _REPO,
            "PYTHONIOENCODING": "utf-8:strict",
            "LANG": "C.UTF-8",
            "FAKE_EB_VERSION": "5.4.2",
            "FAKE_EB_PAYLOAD": "new\n",
            "FAKE_EB_REL": "demo/1",
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
        }
        return root, state, args, env

    def test_an_interrupted_module_walk_is_reported(self):
        program = self.program
        root, state, args, env = self._layout()
        staged = os.path.join(state, "generations", "run1", "modules")
        got = subprocess.run(
            [program, "4", "open", "1", staged, "--", self.python, "-m", "eb_staged_rebuild.cli", *args],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        expected = f"eb-staged-rebuild: [Errno 4] Interrupted system call: '{staged}'\n"
        self.assertEqual(got.returncode, 2)
        self.assertEqual(got.stderr, expected)
        with open(os.path.join(state, "runs", "run1", "journal.json"), encoding="utf-8") as handle:
            recorded = json.load(handle)["state"]
        self.assertEqual(recorded, "building")

    def _refused_run(self, command):
        root, state, args, env = self._layout()
        staged = os.path.join(state, "generations", "run1", "modules")
        got = subprocess.run(
            [self.program, str(errno.EACCES), "open", "1", os.path.join(staged, "all", "demo"), "--", *command, *args],
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
        root, staged, python, recorded = self._refused_run([self.python, "-m", "eb_staged_rebuild.cli"])
        self.assertEqual(python.returncode, 2, python.stdout + python.stderr)
        self.assertTrue(python.stdout.endswith(f"eb-staged-rebuild: module tree is unreadable: {staged}\n"), python.stdout)
        self.assertEqual(recorded, "failed")
        tool = os.environ.get("TOOL_BIN")
        if not tool:
            return
        zig_root, _zig_staged, binary, zig_recorded = self._refused_run([tool])
        self.assertEqual(binary.returncode, python.returncode, binary.stdout + binary.stderr)
        self.assertEqual(binary.stdout.replace(zig_root, "ROOT"), python.stdout.replace(root, "ROOT"))
        self.assertEqual(binary.stderr.replace(zig_root, "ROOT"), python.stderr.replace(root, "ROOT"))
        self.assertEqual(zig_recorded, recorded)

    def _tree(self):
        root = tempfile.mkdtemp(prefix="walk-sibling-")
        self.addCleanup(shutil.rmtree, root, True)
        readable = os.path.join(root, "all", "demo")
        os.makedirs(readable)
        with open(os.path.join(readable, "1.lua"), "w", encoding="utf-8") as handle:
            handle.write("return {}\n")
        os.makedirs(os.path.join(root, "tools"))
        return root

    def _walk_under(self, number, root):
        sibling = os.path.join(root, "tools")
        script = os.path.join(root, "walk.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(_CHILD)
        return subprocess.run(
            [self.program, str(number), "open", "1", sibling, "--", self.python, script, _REPO, root],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PYTHONPATH": _REPO,
                "PYTHONIOENCODING": "utf-8:strict",
                "LANG": "C.UTF-8",
                "PATH": os.environ.get("PATH", ""),
                "HOME": os.environ.get("HOME", ""),
            },
        )

    def test_an_interrupted_subdirectory_is_raised(self):
        got = self._walk_under(errno.EINTR, self._tree())
        self.assertEqual(got.returncode, 3, got.stderr)
        self.assertEqual(got.stderr, "raised 4\n")

    def test_an_unreadable_subdirectory_is_refused(self):
        for number in _REFUSED:
            with self.subTest(errno=number):
                root = self._tree()
                got = self._walk_under(number, root)
                self.assertEqual(got.returncode, 2, got.stderr)
                self.assertEqual(got.stderr, f"refused module tree is unreadable: {root}\n")
