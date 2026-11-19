"""CLI coverage for rejecting a relative --prepare executable path."""

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
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_binary(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [ZIG_BIN, *args],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )


class PrepareAbsolutePathCliTests(unittest.TestCase):
    def test_run_refuses_a_relative_prepare_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-path-") as tmp:
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            state_root = os.path.join(tmp, "state")
            os.makedirs(software)
            os.makedirs(modules)
            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                state_root,
                "--dry-run",
                "--prepare",
                "relative-helper",
                "--",
                "demo.eb",
            )
            py_done = _run_python(*args)
            self.assertEqual(py_done.returncode, 2, py_done.stdout + py_done.stderr)
            self.assertIn(
                "publication preparation executable must be an absolute path",
                py_done.stderr,
            )
            if os.path.exists(ZIG_BIN):
                zig_done = _run_binary(*args)
                self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
                self.assertEqual(py_done.stdout, zig_done.stdout)
                self.assertEqual(py_done.stderr, zig_done.stderr)


if __name__ == "__main__":
    unittest.main()
