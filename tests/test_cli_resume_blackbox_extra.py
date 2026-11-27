"""Extra black-box CLI checks for the resume subcommand."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli


class ResumeBlackBoxExtraTests(unittest.TestCase):
    def test_resume_uses_state_root_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            env = {"EB_STAGED_REBUILD_STATE_ROOT": state_root}
            proc = run_cli("resume", "run-missing", env=env)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"no journal at {state_root}/runs/run-missing/journal.json", proc.stderr)

if __name__ == "__main__":
    unittest.main()
