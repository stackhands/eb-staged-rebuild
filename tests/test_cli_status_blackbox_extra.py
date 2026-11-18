"""Extra black-box CLI checks for the status subcommand."""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
