from __future__ import annotations

import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class CliResumeRunIdPathComponentsParityTests(unittest.TestCase):
    def test_resume_refuses_run_id_with_path_components(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            binary = run_cli("resume", "--state-root", state_root, "../escape")
            self.assertEqual(binary.returncode, 2, binary.stdout + binary.stderr)
            self.assertEqual(binary.stdout, "")
            self.assertIn("run id is not a plain name: '../escape'", binary.stderr)
            golden(self, "stderr", scrub(binary.stderr, STATE=state_root))


if __name__ == "__main__":
    unittest.main()
