from __future__ import annotations

import os
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRunRequiresSoftwareModulesParityTests(unittest.TestCase):
    def test_run_refuses_when_software_and_modules_are_unset(self) -> None:
        env = dict(os.environ)
        env.pop("EASYBUILD_INSTALLPATH_SOFTWARE", None)
        env.pop("EASYBUILD_INSTALLPATH_MODULES", None)

        proc = run_cli("run", "--", "demo.eb", env=env)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(
            "--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required",
            proc.stderr,
        )

        binary = run_cli_optional_binary("run", "--", "demo.eb", env=env)
        if binary is not None:
            self.assertEqual(binary.returncode, 2)
            self.assertIn(
                "--software and --modules (or EASYBUILD_INSTALLPATH_SOFTWARE and EASYBUILD_INSTALLPATH_MODULES) are required",
                binary.stderr,
            )


if __name__ == "__main__":
    unittest.main()
