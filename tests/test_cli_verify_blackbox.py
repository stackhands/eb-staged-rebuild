"""Black-box CLI checks for the verify subcommand, in both front ends."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class VerifyBlackBoxTests(unittest.TestCase):
    def _assert_rejects_non_finite_lock_timeout(self, runner) -> None:
        """Reproducer: eb-staged-rebuild verify --state-root TMP --lock-timeout nan run1"""
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as state_root:
                proc = runner("verify", "--state-root", state_root, f"--lock-timeout={value}", "run1")
                if proc is None:
                    self.skipTest("TOOL_BIN is unset")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(
                    proc.stderr.splitlines()[-1],
                    "eb-staged-rebuild verify: error: argument --lock-timeout: --lock-timeout is not a finite number",
                )
                self.assertEqual(os.listdir(state_root), [])

    def test_verify_rejects_non_finite_lock_timeout_python(self) -> None:
        self._assert_rejects_non_finite_lock_timeout(run_cli)

    def test_verify_rejects_non_finite_lock_timeout_zig(self) -> None:
        self._assert_rejects_non_finite_lock_timeout(run_cli_optional_binary)


if __name__ == "__main__":
    unittest.main()
