"""Extra black-box CLI checks for the run subcommand."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli


class RunBlackBoxExtraTests(unittest.TestCase):
    def test_run_dry_run_succeeds_with_explicit_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            state_root = os.path.join(tmp, "state")
            os.makedirs(software)
            os.makedirs(modules)
            proc = run_cli(
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                state_root,
                "--dry-run",
                "--",
                "Demo-1.0.eb",
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            self.assertIn("dry-run: no build, no module file written", proc.stdout)

if __name__ == "__main__":
    unittest.main()
