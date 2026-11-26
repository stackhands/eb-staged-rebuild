from __future__ import annotations

import os
import unittest

from tests.support import run_cli


class CliRunRequiresSoftwareModulesParityTests(unittest.TestCase):
    def test_run_refuses_when_software_and_modules_are_unset(self) -> None:
        env = dict(os.environ)
        env.pop("EASYBUILD_INSTALLPATH_SOFTWARE", None)
        env.pop("EASYBUILD_INSTALLPATH_MODULES", None)

        binary = run_cli("run", "--", "demo.eb", env=env)
        self.assertEqual(binary.returncode, 2)
        self.assertIn(
            "--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required",
            binary.stderr,
        )


if __name__ == "__main__":
    unittest.main()
