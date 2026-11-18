"""Extra black-box CLI checks for the rollback subcommand."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest


def run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, "-m", "eb_staged_rebuild.cli", *args],
        capture_output=True,
        text=True,
        check=False,
        env=merged,
    )


class RollbackBlackBoxExtraTests(unittest.TestCase):
    def test_rollback_missing_journal_returns_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "run-missing")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn(f"no journal at {state_root}/runs/run-missing/journal.json", proc.stderr)

    def test_rollback_rejects_empty_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("rollback", "--state-root", state_root, "--", "")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("run id is not a plain name: ''", proc.stderr)


if __name__ == "__main__":
    unittest.main()
