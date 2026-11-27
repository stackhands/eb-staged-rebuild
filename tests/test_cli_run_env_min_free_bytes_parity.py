from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli


class CliRunEnvMinFreeBytesParityTests(unittest.TestCase):
    def test_run_refuses_when_env_min_free_bytes_is_not_integer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ)
            env["EASYBUILD_INSTALLPATH_SOFTWARE"] = f"{root}/software"
            env["EASYBUILD_INSTALLPATH_MODULES"] = f"{root}/modules"
            env["EB_STAGED_REBUILD_MIN_FREE_BYTES"] = "bogus"

            binary = run_cli("run", "--", "demo.eb", env=env)
            self.assertEqual(binary.returncode, 2)
            self.assertIn("EB_STAGED_REBUILD_MIN_FREE_BYTES is not an integer: 'bogus'", binary.stderr)

    def test_dry_run_refuses_when_the_reserve_exceeds_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = os.path.join(root, "software")
            modules = os.path.join(root, "modules")
            state = os.path.join(root, "state", "missing")
            os.makedirs(software)
            os.makedirs(modules)
            args = (
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--state-root",
                state,
                "--backend",
                "installpath",
                "--dry-run",
                "--min-free-bytes",
                "18446744073709551615",
                "--",
                "demo.eb",
            )
            binary = run_cli(*args)
            self.assertEqual(binary.returncode, 2)
            self.assertIn("dry-run would refuse:", binary.stdout)
            self.assertIn("--min-free-bytes requires 18446744073709551615", binary.stdout)
            self.assertIn("backend:", binary.stdout)
            self.assertFalse(os.path.lexists(state))
            self.assertFalse(os.path.lexists(os.path.join(root, "state")))


if __name__ == "__main__":
    unittest.main()
