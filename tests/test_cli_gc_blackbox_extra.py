"""Extra black-box CLI checks for the gc subcommand."""

from __future__ import annotations

import json
import os
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

    def test_non_finite_lock_timeout_is_refused(self) -> None:
        """`--lock-timeout nan` is exit 2 with empty stdout on every subcommand."""
        with tempfile.TemporaryDirectory() as tmp:
            state_root = os.path.join(tmp, "state")
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            os.makedirs(software)
            os.makedirs(modules)
            missing = os.path.join(tmp, "missing.lua")
            rows = (
                ("gc", ("gc", "--state-root", state_root, "--dry-run", "--lock-timeout", "nan")),
                ("resume", ("resume", "--state-root", state_root, "--lock-timeout", "nan", "run1")),
                ("rollback", ("rollback", "--state-root", state_root, "--lock-timeout", "nan", "run1")),
                (
                    "run",
                    (
                        "run",
                        "--software",
                        software,
                        "--modules",
                        modules,
                        "--state-root",
                        state_root,
                        "--dry-run",
                        "--lock-timeout",
                        "nan",
                        "--",
                        "Demo-1.0.eb",
                    ),
                ),
                (
                    "retire",
                    ("retire", "--state-root", state_root, "--module-file", missing, "--lock-timeout", "nan"),
                ),
            )
            for command, argv in rows:
                with self.subTest(command=command):
                    proc = run_cli(*argv)
                    self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                    self.assertEqual(proc.stdout, "")
                    self.assertIn("--lock-timeout is not a finite number", proc.stderr)


if __name__ == "__main__":
    unittest.main()
