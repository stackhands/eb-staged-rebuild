from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class GcJsonRefusalContractParityTests(unittest.TestCase):
    def test_gc_json_refusal_keeps_stdout_empty(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            state_root = f"{root}/state"
            binary = run_cli("gc", "--state-root", state_root, "--json", "--grace-hours", "bogus")
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("invalid float value: 'bogus'", binary.stderr)
            golden(self, "stderr", scrub(binary.stderr, STATE=state_root, ROOT=root))


if __name__ == "__main__":
    unittest.main()
