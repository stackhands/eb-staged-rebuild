"""CLI parity for a state root path that is an existing non-directory."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIG_BIN = os.path.join(REPO, "zig-out", "bin", "eb-staged-rebuild")


def _run_python(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild.cli", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _run_zig(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ZIG_BIN, *args],
        capture_output=True,
        text=True,
        check=False,
    )


class StateRootNotDirectoryParityTests(unittest.TestCase):
    def test_rollback_refuses_a_state_root_that_is_a_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state-root-file-") as tmp:
            state_root = os.path.join(tmp, "state-root")
            with open(state_root, "w", encoding="utf-8") as handle:
                handle.write("marker\n")
            done = _run_python("rollback", "--state-root", state_root, "run1")
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn(f"[Errno 20] Not a directory: '{state_root}'", done.stderr)

    @unittest.skipUnless(os.path.exists(ZIG_BIN), "zig frontend binary is unavailable")
    def test_python_and_zig_report_the_same_state_root_file_error(self) -> None:
        with tempfile.TemporaryDirectory(prefix="state-root-file-") as tmp:
            state_root = os.path.join(tmp, "state-root")
            with open(state_root, "w", encoding="utf-8") as handle:
                handle.write("marker\n")
            py_done = _run_python("rollback", "--state-root", state_root, "run1")
            zig_done = _run_zig("rollback", "--state-root", state_root, "run1")
            self.assertEqual(py_done.returncode, 2, py_done.stdout + py_done.stderr)
            self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
            self.assertEqual(py_done.stdout, zig_done.stdout)
            self.assertEqual(py_done.stderr, zig_done.stderr)


if __name__ == "__main__":
    unittest.main()
