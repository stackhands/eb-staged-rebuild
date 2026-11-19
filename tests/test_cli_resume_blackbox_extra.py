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

    @unittest.expectedFailure
    def test_resume_rejects_non_finite_lock_timeout(self) -> None:
        """Reproducer: eb-staged-rebuild resume --state-root TMP --lock-timeout nan run1"""
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("resume", "--state-root", state_root, "--lock-timeout", "nan", "run1")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("--lock-timeout is not a finite number", proc.stderr)


if __name__ == "__main__":
    unittest.main()
