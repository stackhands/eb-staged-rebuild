"""CLI behaviour for a state root path that is an existing non-directory."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class StateRootNotDirectoryParityTests(unittest.TestCase):
    def test_rollback_refuses_a_state_root_that_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state-root-file-") as tmp:
            state_root = os.path.join(tmp, "state-root")
            with open(state_root, "w", encoding="utf-8") as handle:
                handle.write("marker\n")
            done = run_cli("rollback", "--state-root", state_root, "run1")
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn(f"[Errno 20] Not a directory: '{state_root}'", done.stderr)

    def test_python_and_zig_report_the_same_state_root_file_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state-root-file-") as tmp:
            state_root = os.path.join(tmp, "state-root")
            with open(state_root, "w", encoding="utf-8") as handle:
                handle.write("marker\n")
            zig_done = run_cli("rollback", "--state-root", state_root, "run1")
            self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
            golden(self, "stdout", scrub(zig_done.stdout, TMP=tmp))
            golden(self, "stderr", scrub(zig_done.stderr, TMP=tmp))


if __name__ == "__main__":
    unittest.main()
