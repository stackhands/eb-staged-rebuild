"""Extra black-box CLI checks for the gc subcommand."""

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


class GcBlackBoxExtraTests(unittest.TestCase):
    def test_gc_dry_run_json_reports_empty_actions_for_new_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("gc", "--state-root", state_root, "--dry-run", "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            data = json.loads(proc.stdout)
            self.assertEqual(data, {"actions": []})

    def test_gc_requires_state_root_when_environment_is_empty(self) -> None:
        env = {"EB_STAGED_REBUILD_STATE_ROOT": ""}
        proc = run_cli("gc", env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--state-root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
