"""Black-box CLI checks for the resume subcommand."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli


class ResumeBlackBoxTests(unittest.TestCase):
    def test_resume_missing_journal_returns_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("resume", "--state-root", state_root, "r-missing")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"no journal at {state_root}/runs/r-missing/journal.json", proc.stderr)

    def test_resume_requires_state_root_when_env_absent(self) -> None:
        env = {"EB_STAGED_REBUILD_STATE_ROOT": ""}
        proc = run_cli("resume", "r-any", env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--state-root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
