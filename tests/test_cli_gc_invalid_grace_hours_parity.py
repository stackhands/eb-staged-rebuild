from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliGcInvalidGraceHoursParityTests(unittest.TestCase):
    def test_gc_refuses_non_numeric_grace_hours(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = f"{root}/state"

            proc = run_cli("gc", "--state-root", state_root, "--grace-hours", "bogus")
            self.assertEqual(proc.returncode, 2)
            self.assertIn("invalid float value: 'bogus'", proc.stderr)

            binary = run_cli_optional_binary("gc", "--state-root", state_root, "--grace-hours", "bogus")
            if binary is not None:
                self.assertEqual(binary.returncode, 2)
                self.assertIn("invalid float value: 'bogus'", binary.stderr)


if __name__ == "__main__":
    unittest.main()
