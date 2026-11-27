"""CLI coverage for rejecting a relative --prepare executable path."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import golden, run_cli, scrub


class PrepareAbsolutePathCliTests(unittest.TestCase):
    def test_run_refuses_a_relative_prepare_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-path-") as tmp:
            software = os.path.join(tmp, "software")
            modules = os.path.join(tmp, "modules")
            state_root = os.path.join(tmp, "state")
            os.makedirs(software)
            os.makedirs(modules)
            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                state_root,
                "--dry-run",
                "--prepare",
                "relative-helper",
                "--",
                "demo.eb",
            )
            done = run_cli(*args)
            self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
            self.assertIn(
                "publication preparation executable must be an absolute path",
                done.stderr,
            )
            golden(self, "stdout", scrub(done.stdout, STATE=state_root, TMP=tmp))
            golden(self, "stderr", scrub(done.stderr, STATE=state_root, TMP=tmp))


if __name__ == "__main__":
    unittest.main()
