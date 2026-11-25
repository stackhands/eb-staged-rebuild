"""Extra black-box CLI checks for the gc subcommand."""

from __future__ import annotations

import json
import tempfile
import unittest

from tests.support import run_cli


class GcBlackBoxExtraTests(unittest.TestCase):
    def test_gc_dry_run_json_reports_empty_actions_for_new_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("gc", "--state-root", state_root, "--dry-run", "--json")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            data = json.loads(proc.stdout)
            self.assertEqual(data, {"actions": []})

    def test_gc_rejects_non_finite_lock_timeout(self) -> None:
        """Reproducer: eb-staged-rebuild gc --state-root TMP --dry-run --lock-timeout nan"""
        with tempfile.TemporaryDirectory() as state_root:
            proc = run_cli("gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "nan")
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("--lock-timeout is not a finite number", proc.stderr)

    def test_gc_requires_state_root_when_environment_is_empty(self) -> None:
        env = {"EB_STAGED_REBUILD_STATE_ROOT": ""}
        proc = run_cli("gc", env=env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertIn("--state-root", proc.stderr)


if __name__ == "__main__":
    unittest.main()
