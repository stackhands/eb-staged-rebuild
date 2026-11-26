from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CliGcInvalidGraceHoursParityTests(unittest.TestCase):
    def test_gc_refuses_non_numeric_grace_hours(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = f"{root}/state"

            binary = run_cli("gc", "--state-root", state_root, "--grace-hours", "bogus")
            self.assertEqual(binary.returncode, 2)
            self.assertIn("invalid float value: 'bogus'", binary.stderr)
            golden(self, "stdout", scrub(binary.stdout, STATE=state_root, ROOT=root))
            golden(self, "stderr", scrub(binary.stderr, STATE=state_root, ROOT=root))


if __name__ == "__main__":
    unittest.main()
