from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRunEmptyPrepareParityTests(unittest.TestCase):
    def test_run_refuses_empty_prepare_program(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = f"{root}/software"
            modules = f"{root}/modules"

            proc = run_cli(
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--prepare",
                "",
                "--",
                "demo.eb",
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("--prepare must name a program; it is empty", proc.stderr)

            binary = run_cli_optional_binary(
                "run",
                "--software",
                software,
                "--modules",
                modules,
                "--prepare",
                "",
                "--",
                "demo.eb",
            )
            if binary is not None:
                self.assertEqual(binary.returncode, 2)
                self.assertIn("--prepare must name a program; it is empty", binary.stderr)


if __name__ == "__main__":
    unittest.main()
