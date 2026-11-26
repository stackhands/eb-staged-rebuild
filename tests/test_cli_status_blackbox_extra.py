"""Extra black-box CLI checks for the status subcommand."""

from __future__ import annotations

import json
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class StatusBlackBoxExtraTests(unittest.TestCase):
    def test_status_prints_no_runs_for_empty_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("status", "--state-root", state_root)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(proc.stdout, "no runs\n")

    def test_status_json_reports_empty_runs_for_empty_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("status", "--state-root", state_root, "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertEqual(json.loads(proc.stdout), {"runs": []})

    def test_status_missing_run_id_refuses_with_exit_two(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("status", "--state-root", state_root, "missing-run")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout, "")
            self.assertIn("no journal for missing-run", proc.stderr)
            golden(self, "stderr", scrub(proc.stderr, STATE=state_root))


if __name__ == "__main__":
    unittest.main()
