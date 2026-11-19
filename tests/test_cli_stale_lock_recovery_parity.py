from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary, start_lock_holder_process


class CliStaleLockRecoveryParityTests(unittest.TestCase):
    def test_gc_succeeds_after_lock_holder_process_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            holder = start_lock_holder_process(state_root)
            holder.terminate()
            holder.join(5)
            self.assertFalse(holder.is_alive(), "lock holder process did not stop")

            proc = run_cli("gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "0")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")

            binary = run_cli_optional_binary(
                "gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "0"
            )
            if binary is not None:
                self.assertEqual(binary.returncode, 0, binary.stdout + binary.stderr)
                self.assertEqual(binary.stderr, "")
                self.assertEqual(binary.stdout, proc.stdout)


if __name__ == "__main__":
    unittest.main()
