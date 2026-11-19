from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRollbackRunIdPathComponentsParityTests(unittest.TestCase):
    def test_rollback_refuses_run_id_with_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "../escape")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("run id is not a plain name: '../escape'", proc.stderr)

            binary = run_cli_optional_binary("rollback", "--state-root", state_root, "../escape")
            if binary is not None:
                self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                self.assertEqual(binary.stdout, "")
                self.assertEqual(binary.stderr, proc.stderr)


if __name__ == "__main__":
    unittest.main()
