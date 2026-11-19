from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class StatusJsonRefusalContractParityTests(unittest.TestCase):
    def test_status_json_refusal_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proc = run_cli("status", "--state-root", root, "--json", "missing-run")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("no journal for missing-run", proc.stderr)

            binary = run_cli_optional_binary("status", "--state-root", root, "--json", "missing-run")
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(binary.stdout, "")
                self.assertEqual(binary.stderr, proc.stderr)


if __name__ == "__main__":
    unittest.main()
