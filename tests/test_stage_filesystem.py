import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from eb_staged_rebuild.engine import run
from eb_staged_rebuild.errors import Refused
from tests.test_engine import _fixture, _payload, _spec


class StageFilesystemTests(unittest.TestCase):
    def test_default_run_keeps_exact_status_and_stream_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            software, modules, state, _installed, builder = _fixture(root)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = run(_spec(software, modules, state, builder))
            expected = (
                f"eb-staged-rebuild: run run1 backend installpath generation "
                f"{state}/generations/run1\n"
                "eb-staged-rebuild: swapped all/demo/1.lua (rename-fallback)\n"
                "eb-staged-rebuild: published module all/demo/1.lua\n"
                "eb-staged-rebuild: run run1 published 1 module(s)\n"
            ).encode()
        self.assertEqual(
            (result.returncode, out.getvalue().encode(), err.getvalue().encode()),
            (0, expected, b""),
        )

    def test_different_filesystem_refuses_before_the_journal_and_build(self):
        second = "/dev/shm"
        if not os.path.isdir(second) or os.stat(second).st_dev == os.stat(tempfile.gettempdir()).st_dev:
            self.skipTest("a second filesystem is required")
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory(dir=second) as other:
            software, modules, _state, installed, real_builder = _fixture(root)
            state = os.path.join(other, "state")
            builder = mock.Mock(side_effect=real_builder)
            with self.assertRaisesRegex(Refused, "different filesystems"):
                run(_spec(software, modules, state, builder))
            builder.assert_not_called()
            self.assertFalse(os.path.exists(os.path.join(state, "runs")))
            for row in installed.values():
                self.assertEqual(os.lstat(row["install"]).st_ino, row["inode"])
                self.assertEqual(_payload(row["install"]), row["old"])
                with open(row["module"], encoding="utf-8") as handle:
                    self.assertEqual(handle.read(), row["module_text"])
