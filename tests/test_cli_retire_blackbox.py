"""Black-box CLI checks for the retire subcommand."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli


class RetireBlackBoxTests(unittest.TestCase):
    def test_retire_rejects_non_finite_lock_timeout(self) -> None:
        """Reproducer: eb-staged-rebuild retire --state-root TMP --module-file MISSING --lock-timeout nan"""
        with tempfile.TemporaryDirectory() as state_root:
            with tempfile.TemporaryDirectory() as outside:
                missing = os.path.join(outside, "missing.lua")
                proc = run_cli(
                    "retire",
                    "--state-root",
                    state_root,
                    "--module-file",
                    missing,
                    "--lock-timeout",
                    "nan",
                )
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn("--lock-timeout is not a finite number", proc.stderr)

    def test_retire_rejects_missing_module_file(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            with tempfile.TemporaryDirectory() as outside:
                missing = os.path.join(outside, "missing.lua")
                proc = run_cli("retire", "--state-root", state_root, "--module-file", missing)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(f"{missing} is not a regular file (missing)", proc.stderr)

    def test_retire_refuses_module_inside_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            module = os.path.join(state_root, "demo.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write("help('x')\n")
            proc = run_cli("retire", "--state-root", state_root, "--module-file", module)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"{module} is inside the state root", proc.stderr)

    def test_retire_honors_state_root_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            module = os.path.join(state_root, "demo.lua")
            with open(module, "w", encoding="utf-8") as handle:
                handle.write("help('x')\n")
            env = {"EB_STAGED_REBUILD_STATE_ROOT": state_root}
            proc = run_cli("retire", "--module-file", module, "--dry-run", env=env)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"{module} is inside the state root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
