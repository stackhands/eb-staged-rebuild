from __future__ import annotations

import tempfile
import unittest

from tests.support import run_cli


class CliRunRequiresEasyconfigParityTests(unittest.TestCase):
    def test_run_refuses_when_easyconfig_argument_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            software = f"{root}/software"
            modules = f"{root}/modules"

            binary = run_cli("run", "--software", software, "--modules", modules)
            self.assertEqual(binary.returncode, 2)
            self.assertIn("pass the easyconfig after --", binary.stderr)


if __name__ == "__main__":
    unittest.main()
