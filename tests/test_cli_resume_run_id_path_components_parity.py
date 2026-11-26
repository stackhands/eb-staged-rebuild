"""Resume and rollback refuse a run id that is not a plain name."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CliRunIdPathComponentsParityTests(unittest.TestCase):
    def test_refuses_run_id_with_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            for command in ("resume", "rollback"):
                with self.subTest(command=command):
                    binary = run_cli(command, "--state-root", state_root, "../escape")
                    self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
                    self.assertEqual(binary.stdout, "")
                    self.assertIn("run id is not a plain name: '../escape'", binary.stderr)
                    golden(self, f"{command}.stderr", scrub(binary.stderr, STATE=state_root))


if __name__ == "__main__":
    unittest.main()
