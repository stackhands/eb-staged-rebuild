from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class GcJsonRefusalContractParityTests(unittest.TestCase):
    def test_gc_json_refusal_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = f"{root}/state"
            proc = run_cli("gc", "--state-root", state_root, "--json", "--grace-hours", "bogus")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("invalid float value: 'bogus'", proc.stderr)

            binary = run_cli_optional_binary(
                "gc", "--state-root", state_root, "--json", "--grace-hours", "bogus"
            )
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(binary.stdout, "")
                self.assertEqual(binary.stderr, proc.stderr)


if __name__ == "__main__":
    unittest.main()
