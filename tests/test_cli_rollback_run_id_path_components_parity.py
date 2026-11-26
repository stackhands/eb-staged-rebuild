from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CliRollbackRunIdPathComponentsParityTests(unittest.TestCase):
    def test_rollback_refuses_run_id_with_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            binary = run_cli("rollback", "--state-root", state_root, "../escape")
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("run id is not a plain name: '../escape'", binary.stderr)
            golden(self, "stderr", scrub(binary.stderr, STATE=state_root))


if __name__ == "__main__":
    unittest.main()
