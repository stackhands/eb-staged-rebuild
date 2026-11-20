"""A retire that refuses still removes the directories it created.

The tracer has to be the parent of the interpreter. The last open under
the retired tree is the empty directory that would have held the module
file. Its index is the stamp plus each component of that path.
"""

import ctypes
import os
import subprocess
import tempfile
import unittest

from tests.support import classic_install, fs_fail_program, python314

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHILD = """\
import os, sys
sys.path.insert(0, sys.argv[1])
from eb_staged_rebuild.errors import Refused
from eb_staged_rebuild.retire import retire
state, module = sys.argv[2], sys.argv[3]
try:
    retire(state, module)
except Refused as exc:
    kids = os.listdir(os.path.join(state, "retired"))
    sys.stdout.write("kids %s\\n" % len(kids))
    sys.stderr.write(str(exc) + "\\n")
    raise SystemExit(2)
sys.stdout.write("no refuse\\n")
raise SystemExit(1)
"""


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
    return detail or f"ptrace(PTRACE_TRACEME) failed with wait status {status}"


def _tracer():
    path, reason = fs_fail_program("eb-fs-fail-retire")
    if path is None:
        return None, reason
    problem = _ptrace_problem()
    if problem:
        return None, f"ptrace is unavailable: {problem}"
    return path, None


class RetireDiscardWalkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.python = python314()
        cls.program, cls.reason = _tracer()

    def setUp(self):
        if self.python is None:
            self.skipTest("python3.14 is not installed")
        if self.program is None:
            self.skipTest(self.reason)
        shm = "/dev/shm"
        if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat(tempfile.gettempdir()).st_dev:
            self.skipTest("no second filesystem is available, so retire cannot be forced to clean up")

    def test_a_failed_listing_still_removes_the_retire_directory(self):
        # 4 is EINTR, 5 is EIO, 13 is EACCES. Zig retries the interrupt and
        # removes the directory when the open fails for another reason.
        for number in (4, 5, 13):
            with (
                self.subTest(errno=number),
                tempfile.TemporaryDirectory() as root,
                tempfile.TemporaryDirectory(dir="/dev/shm") as other,
            ):
                software = os.path.join(root, "software")
                modules = os.path.join(root, "modules")
                os.makedirs(software)
                os.makedirs(modules)
                _install, module, _text = classic_install(
                    software, modules, "demo/1", "old\n", "all/demo/1.lua"
                )
                state = os.path.join(other, "state")
                depth = len([part for part in os.path.dirname(module).split(os.sep) if part])
                nth = 1 + depth
                script = os.path.join(root, "discard.py")
                with open(script, "w", encoding="utf-8") as handle:
                    handle.write(_CHILD)
                got = subprocess.run(
                    [
                        self.program,
                        str(number),
                        "open",
                        str(nth),
                        os.path.join(state, "retired"),
                        "--",
                        self.python,
                        script,
                        _REPO,
                        state,
                        module,
                    ],
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
                self.assertEqual(got.returncode, 2, got.stderr)
                self.assertIn("different filesystems", got.stderr)
                self.assertEqual(got.stdout, "kids 0\n", got.stderr)
