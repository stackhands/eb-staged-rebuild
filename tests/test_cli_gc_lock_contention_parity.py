from __future__ import annotations

import tempfile
import unittest

from tests.support import hold_state_root_lock, run_cli, run_cli_optional_binary


class CliGcLockContentionParityTests(unittest.TestCase):
    def test_gc_refuses_when_state_root_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            with hold_state_root_lock(state_root, timeout=0):
                proc = run_cli("gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "0")
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn(
                    f"another eb-staged-rebuild holds the lock at {state_root}/.lock",
                    proc.stderr,
                )

                binary = run_cli_optional_binary(
                    "gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "0"
                )
                if binary is not None:
                    self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                    self.assertEqual(binary.stdout, "")
                    self.assertEqual(binary.stderr, proc.stderr)


if __name__ == "__main__":
    unittest.main()
