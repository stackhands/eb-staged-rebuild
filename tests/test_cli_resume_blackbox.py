"""Black-box CLI checks for the resume subcommand."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli


class ResumeBlackBoxTests(unittest.TestCase):
    def test_missing_journal_returns_refusal(self) -> None:
        rows = (
            ("resume", "r-missing"),
            ("rollback", "run-missing"),
        )
        with tempfile.TemporaryDirectory() as state_root:
            for command, run_id in rows:
                with self.subTest(command=command):
                    proc = run_cli(command, "--state-root", state_root, run_id)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn(
                        f"no journal at {state_root}/runs/{run_id}/journal.json",
                        proc.stderr,
                    )

    def test_state_root_is_required_when_environment_is_empty(self) -> None:
        env = {"EB_STAGED_REBUILD_STATE_ROOT": ""}
        rows = (
            ("gc", ("gc",)),
            ("resume", ("resume", "r-any")),
        )
        for command, argv in rows:
            with self.subTest(command=command):
                proc = run_cli(*argv, env=env)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertIn("--state-root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
