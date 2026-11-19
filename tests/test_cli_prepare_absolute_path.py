"""CLI coverage for rejecting a relative --prepare executable path."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


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
            py_done = run_cli(*args)
            self.assertEqual(py_done.returncode, 2, py_done.stdout + py_done.stderr)
            self.assertIn(
                "publication preparation executable must be an absolute path",
                py_done.stderr,
            )
            zig_done = run_cli_optional_binary(*args)
            if zig_done is not None:
                self.assertEqual(zig_done.returncode, 2, zig_done.stdout + zig_done.stderr)
                self.assertEqual(py_done.stdout, zig_done.stdout)
                self.assertEqual(py_done.stderr, zig_done.stderr)


if __name__ == "__main__":
    unittest.main()
