"""Extra black-box CLI checks for the rollback subcommand."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli


class RollbackBlackBoxExtraTests(unittest.TestCase):
    def test_rollback_missing_journal_returns_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "run-missing")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"no journal at {state_root}/runs/run-missing/journal.json", proc.stderr)

    def test_rollback_rejects_non_finite_lock_timeout(self) -> None:
        """Reproducer: eb-staged-rebuild rollback --state-root TMP --lock-timeout nan run1"""
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "--lock-timeout", "nan", "run1")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("--lock-timeout is not a finite number", proc.stderr)

    def test_rollback_rejects_empty_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "--", "")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("run id is not a plain name: ''", proc.stderr)


if __name__ == "__main__":
    unittest.main()
