from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRunEnvMinFreeBytesParityTests(unittest.TestCase):
    def test_run_refuses_when_env_min_free_bytes_is_not_integer(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            env = dict(os.environ)
            env["EASYBUILD_INSTALLPATH_SOFTWARE"] = f"{root}/software"
            env["EASYBUILD_INSTALLPATH_MODULES"] = f"{root}/modules"
            env["EB_STAGED_REBUILD_MIN_FREE_BYTES"] = "bogus"

            proc = run_cli("run", "--", "demo.eb", env=env)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("EB_STAGED_REBUILD_MIN_FREE_BYTES is not an integer: 'bogus'", proc.stderr)

            binary = run_cli_optional_binary("run", "--", "demo.eb", env=env)
            if binary is not None:
                self.assertEqual(binary.returncode, 2)
                self.assertIn(
                    "EB_STAGED_REBUILD_MIN_FREE_BYTES is not an integer: 'bogus'",
                    binary.stderr,
                )


if __name__ == "__main__":
    unittest.main()
