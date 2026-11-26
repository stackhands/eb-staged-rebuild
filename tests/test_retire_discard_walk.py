"""A retire that refuses still removes the directories it created.

The tracer has to be the parent of the binary. The last open under
the retired tree is the empty directory that would have held the module
file. Its index is the stamp plus each component of that path.
"""

import os
import subprocess
import tempfile
import unittest

from tests.support import _program, classic_install, tool_bin


class RetireDiscardWalkTest(unittest.TestCase):
    def setUp(self):
        self.program = _program(self, "eb-fs-fail-retire")
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
                got = subprocess.run(
                    [
                        self.program,
                        str(number),
                        "open",
                        str(nth),
                        os.path.join(state, "retired"),
                        "--",
                        tool_bin(),
                        "retire",
                        "--state-root",
                        state,
                        "--module-file",
                        module,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={"LANG": "C.UTF-8", "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
                )
                self.assertEqual(got.returncode, 2, got.stderr)
                self.assertIn("different filesystems", got.stderr)
                self.assertEqual(os.listdir(os.path.join(state, "retired")), [], got.stderr)
