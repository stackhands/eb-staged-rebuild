"""`run` refuses an empty `--build-dir` or `--prepare` before any build."""

from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli, run_cli_optional_binary


class CliRunEmptyOptionParityTests(unittest.TestCase):
    def test_run_refuses_an_empty_option_string(self) -> None:
        rows = (
            ("--build-dir", "--build-dir must name a directory; it is empty"),
            ("--prepare", "--prepare must name a program; it is empty"),
        )
        with tempfile.TemporaryDirectory() as root:
            software = f"{root}/software"
            modules = f"{root}/modules"
            for flag, message in rows:
                with self.subTest(flag=flag):
                    argv = ("run", "--software", software, "--modules", modules, flag, "", "--", "demo.eb")
                    proc = run_cli(*argv)
                    self.assertEqual(proc.returncode, 2)
                    self.assertIn(message, proc.stderr)

                    binary = run_cli_optional_binary(*argv)
                    if binary is not None:
                        self.assertEqual(binary.returncode, 2)
                        self.assertIn(message, binary.stderr)


if __name__ == "__main__":
    unittest.main()
