from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class StatusJsonRefusalContractParityTests(unittest.TestCase):
    def test_status_json_refusal_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            binary = run_cli("status", "--state-root", root, "--json", "missing-run")
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("no journal for missing-run", binary.stderr)
            golden(self, "stderr", scrub(binary.stderr, STATE=root))


if __name__ == "__main__":
    unittest.main()
