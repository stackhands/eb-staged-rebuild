"""An interrupted read of the staged module tree is an operating-system error.

A directory that cannot be read for another reason is skipped. The tracer
has to be the parent of the interpreter, so this file does not go through
``tests.support.run_cli``.
"""

import ctypes
import errno
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from tests.support import classic_install

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FAKE = os.path.join(_REPO, "tests", "fake_eb.py")
_SKIPPED = (errno.EACCES, errno.EIO, errno.ENOTDIR)

_CHILD = """\
import sys
sys.path.insert(0, sys.argv[1])
from eb_staged_rebuild.discover import walk_module_tree
try:
    files, _links = walk_module_tree(sys.argv[2])
except OSError as exc:
    sys.stderr.write("raised %s\\n" % exc.errno)
    raise SystemExit(3)
sys.stdout.write("%s\\n" % len(files))
"""


def _python314():
    if sys.version_info[:2] == (3, 14):
        return sys.executable
    found = shutil.which("python3.14")
    if not found:
        return None
    completed = subprocess.run(
        [found, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2]==(3,14) else 1)"],
        check=False,
    )
    return found if completed.returncode == 0 else None


def _ptrace_problem():
    libc = ctypes.CDLL(None, use_errno=True)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        if libc.ptrace(0, 0, None, None) != 0:
            os.write(write_fd, os.strerror(ctypes.get_errno()).encode())
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    _pid, status = os.waitpid(pid, 0)
    detail = os.read(read_fd, 200).decode()
    os.close(read_fd)
    if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0:
        return None
    if detail:
        return detail
    return f"ptrace(PTRACE_TRACEME) failed with wait status {status}"


def _tracer():
    """Build tests/fs_fail.c. Return ``(path, None)`` or ``(None, reason)``."""
    if shutil.which("gcc") is None:
        return None, "gcc is not on PATH, so tests/fs_fail.c cannot be built"
    src = os.path.join(_REPO, "tests", "fs_fail.c")
    out = os.path.join(tempfile.gettempdir(), "eb-fs-fail-walk")
    if not os.path.exists(out) or os.path.getmtime(out) < os.path.getmtime(src):
        built = subprocess.run(
            ["gcc", "-O2", "-o", out, src],
            capture_output=True,
            text=True,
            check=False,
        )
        if built.returncode != 0:
            detail = (built.stderr or built.stdout or "gcc failed").strip()
            return None, f"gcc could not build tests/fs_fail.c: {detail}"
    problem = _ptrace_problem()
    if problem:
        return None, f"ptrace is unavailable: {problem}"
    return out, None


class ModuleWalkEintrTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = _python314()
        cls.program, cls.reason = _tracer()

    def setUp(self):
        if self.python is None:
            self.skipTest("python3.14 is not installed")
        if self.program is None:
            self.skipTest(self.reason)

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
        os.chmod(eb, stat.S_IRWXU)
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

    def test_an_unreadable_subdirectory_is_skipped(self):
        for number in _SKIPPED:
            with self.subTest(errno=number):
                got = self._walk_under(number, self._tree())
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual(got.stdout, "1\n")
